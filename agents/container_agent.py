#!/usr/bin/env python3
"""Pull-based, constrained Docker job worker for swarm-llm."""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform as host_platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

LOG_LIMIT = 65536


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def load_agent_id(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        value = f"container-{uuid.uuid4()}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return value


def docker_platform() -> str:
    try:
        result = docker("info", "--format", "{{.OSType}}/{{.Architecture}}",
                        capture_output=True, text=True)
        return result.stdout.strip().replace("x86_64", "amd64").replace("aarch64", "arm64")
    except Exception:
        machine = host_platform.machine().lower().replace("x86_64", "amd64").replace("aarch64", "arm64")
        return f"linux/{machine}"


def register(base_url: str, headers: dict, agent_id: str) -> None:
    payload = {
        "agent_id": agent_id, "port": 0, "model": "container-runtime",
        "hostname": socket.gethostname(), "cpu_cores": os.cpu_count(),
        "os_version": host_platform.platform(),
    }
    response = requests.post(f"{base_url}/register", json=payload, headers=headers, timeout=15)
    response.raise_for_status()


def heartbeat_loop(base_url: str, headers: dict, agent_id: str, stop: threading.Event) -> None:
    while not stop.wait(15):
        try:
            response = requests.post(f"{base_url}/heartbeat",
                                     json={"agent_id": agent_id, "port": 0},
                                     headers=headers, timeout=8)
            if response.status_code == 404:
                register(base_url, headers, agent_id)
        except Exception as exc:
            print(f"heartbeat failed: {exc}", flush=True)


def tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - LOG_LIMIT))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def archive_artifacts(workspace: Path, patterns: list[str], destination: Path) -> bool:
    files: set[Path] = set()
    for pattern in patterns:
        for raw in glob.glob(str(workspace / pattern), recursive=True):
            path = Path(raw)
            if path.is_file() and not path.is_symlink():
                try:
                    path.resolve().relative_to(workspace.resolve())
                except ValueError:
                    continue
                files.add(path)
    if not files:
        return False
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(files):
            archive.add(path, arcname=str(path.relative_to(workspace)), recursive=False)
    return True


def run_job(job: dict, base_url: str, headers: dict, agent_id: str) -> None:
    job_id = job["id"]
    container = f"swarm-job-{job_id}"
    volume = f"swarm-job-{job_id}"
    started = time.monotonic()
    exit_code = 125
    error = None
    is_windows = (job.get("platform") or "").startswith("windows/")
    container_workspace = "C:/workspace" if is_windows else "/workspace"

    with tempfile.TemporaryDirectory(prefix="swarm-container-") as temp:
        temp_path = Path(temp)
        stdout_path, stderr_path = temp_path / "stdout", temp_path / "stderr"
        workspace = temp_path / "workspace"
        workspace.mkdir()
        try:
            print(f"[{job_id[:8]}] pulling {job['image']}", flush=True)
            docker("pull", job["image"])
            docker("volume", "create", volume, stdout=subprocess.DEVNULL)
            create = [
                "create", "--name", container, "--network",
                "nat" if is_windows and job.get("network") else
                "bridge" if job.get("network") else "none",
                "--cpus", str(job["cpus"]), "--memory", f"{job['memory_mb']}m",
                "--mount", f"type=volume,src={volume},dst={container_workspace}",
                "--workdir", container_workspace,
            ]
            if not is_windows:
                create += [
                    "--read-only", "--pids-limit", "512", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                ]
            else:
                # Windows named volumes are not writable by Nano Server's default
                # ContainerUser. The host remains isolated; this is admin only inside
                # the container, not on the EC2 machine.
                create += ["--user", "ContainerAdministrator"]
            if job.get("platform"):
                create += ["--platform", job["platform"]]
            for key, value in job.get("env", {}).items():
                create += ["--env", f"{key}={value}"]
            create.append(job["image"])
            create.extend(job.get("command") or [])
            docker(*create, stdout=subprocess.DEVNULL)
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                try:
                    result = subprocess.run(["docker", "start", "-a", container], stdout=out,
                                            stderr=err, timeout=job["timeout_sec"])
                    exit_code = result.returncode
                except subprocess.TimeoutExpired:
                    exit_code = 124
                    error = f"job exceeded timeout of {job['timeout_sec']} seconds"
                    subprocess.run(["docker", "kill", container], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)

            # docker cp works from a stopped container and avoids mounting host paths into workloads.
            if is_windows:
                inspected = docker(
                    "volume", "inspect", "--format", "{{.Mountpoint}}", volume,
                    capture_output=True, text=True,
                )
                volume_path = Path(inspected.stdout.strip())
                if not volume_path.is_dir():
                    raise RuntimeError(f"Docker volume mountpoint is unavailable: {volume_path}")
                shutil.copytree(str(volume_path), str(workspace), dirs_exist_ok=True)
            else:
                copy_source = f"{container}:{container_workspace}/."
                copy_destination = str(workspace)
                copied = subprocess.run(
                    ["docker", "cp", copy_source, copy_destination],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if copied.returncode != 0:
                    raise RuntimeError(f"could not copy workspace: {copied.stderr.strip()}")
            artifact = temp_path / "artifacts.tar.gz"
            wanted_artifacts = job.get("artifacts", [])
            if archive_artifacts(workspace, wanted_artifacts, artifact):
                with artifact.open("rb") as stream:
                    upload = requests.post(
                        f"{base_url}/agent/container/jobs/{job_id}/artifacts?agent_id={quote(agent_id)}",
                        data=stream, headers={**headers, "Content-Type": "application/gzip"}, timeout=600)
                    upload.raise_for_status()
            elif wanted_artifacts:
                raise RuntimeError("job completed but none of the requested artifacts were found")
        except Exception as exc:
            error = str(exc)
            print(f"[{job_id[:8]}] failed: {error}", flush=True)
        finally:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "volume", "rm", "-f", volume], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

        payload = {
            "exit_code": exit_code, "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "stdout": tail(stdout_path), "stderr": tail(stderr_path), "error": error,
        }
        response = requests.post(
            f"{base_url}/agent/container/jobs/{job_id}/result?agent_id={quote(agent_id)}",
            json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        print(f"[{job_id[:8]}] {response.json()['status']} (exit {exit_code})", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="swarm-llm container worker")
    parser.add_argument("--coordinator", required=True, help="Coordinator API URL, e.g. https://host/swarm/api")
    parser.add_argument("--api-key", default=os.environ.get("SWARM_AGENT_KEY"), help="Agent key or SWARM_AGENT_KEY")
    parser.add_argument("--poll-seconds", type=float, default=3)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key or SWARM_AGENT_KEY is required")
    docker("version", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    base_url = args.coordinator.rstrip("/")
    headers = {"X-API-Key": args.api_key}
    state = Path.home() / ".swarm-llm" / "container-agent-id"
    agent_id = load_agent_id(state)
    worker_platform = docker_platform()
    register(base_url, headers, agent_id)
    print(f"registered {agent_id} ({worker_platform})", flush=True)

    stop = threading.Event()
    thread = threading.Thread(target=heartbeat_loop, args=(base_url, headers, agent_id, stop), daemon=True)
    thread.start()
    try:
        while True:
            try:
                response = requests.get(
                    f"{base_url}/agent/container/jobs/next",
                    params={"agent_id": agent_id, "platform": worker_platform},
                    headers=headers, timeout=15)
                if response.status_code == 200:
                    run_job(response.json(), base_url, headers, agent_id)
                elif response.status_code != 204:
                    response.raise_for_status()
            except requests.RequestException as exc:
                print(f"poll failed: {exc}", flush=True)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
