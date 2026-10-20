#!/usr/bin/env python3
"""Deploy a prebuilt coordinator while preserving idle direct QUIC clusters."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
GATE = """
    # BEGIN QUIC HANDOFF ADMISSION GATE
    location = /swarm/api/v1/chat/completions { add_header Retry-After 5 always; return 503; }
    location = /api/v1/chat/completions { add_header Retry-After 5 always; return 503; }
    # END QUIC HANDOFF ADMISSION GATE
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    values=dict(os.environ)
    for line in (ROOT/".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k,v=line.split("=",1);values.setdefault(k.strip(),v.strip().strip("\"'"))
    def api(path,payload=None):
        request=urllib.request.Request("http://127.0.0.1:8081/swarm/api"+path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-API-Key":values["ADMIN_KEY"],"Content-Type":"application/json"})
        with urllib.request.urlopen(request,timeout=10)as response:return json.load(response)
    def docker(*arguments,input=None):
        result=subprocess.run(["docker","compose",*arguments],cwd=ROOT,input=input,text=True,capture_output=True,timeout=60)
        if result.returncode:raise RuntimeError("Docker operation failed: "+result.stderr[-1000:])
        return result.stdout
    def idle(state):
        return (not state.get("queued") and not state.get("active_chat_streams")
                and not any(m.get("busy") for m in state["machines"]))
    before=api("/machines")
    if not args.apply:
        print(json.dumps({"idle":idle(before),"clusters":[{"cluster_id":c["cluster_id"],"transport":c["transport"],
              "status":c["status"]}for c in before.get("rpc_clusters",[])]}));return
    until=time.monotonic()+300
    while not idle(before):
        if time.monotonic()>until:raise RuntimeError("Active jobs did not drain; deployment was not started")
        time.sleep(2);before=api("/machines")
    config=ROOT/"nginx/gateway.conf";source=config.read_text()
    if "QUIC HANDOFF ADMISSION GATE" in source or source.count("server {")!=1:
        raise RuntimeError("Gateway configuration does not permit a unique temporary admission gate")
    handoff_id=str(uuid.uuid4());gated=False
    try:
        config.write_text(source.replace("server {","server {"+GATE,1));gated=True
        docker("exec","-T","gateway","nginx","-t")
        docker("exec","-T","gateway","nginx","-s","reload")
        until=time.monotonic()+5
        while True:
            try:
                api("/v1/chat/completions",{})
            except urllib.error.HTTPError as exc:
                if exc.code==503:break
            if time.monotonic()>until:
                raise RuntimeError("Temporary admission gate did not reject new chat")
            time.sleep(.1)
        until=time.monotonic()+60;before=api("/machines")
        while not idle(before):
            if time.monotonic()>until:raise RuntimeError("In-flight work did not drain; deployment cancelled")
            time.sleep(1);before=api("/machines")
        now=time.time();records={m["agent_id"]:m for m in before["machines"]}
        snapshot={"version":1,"created_at":now,"handoff_id":handoff_id,"clusters":[]}
        expected_pids={}
        for cluster in before.get("rpc_clusters",[]):
            if cluster["transport"]!="quic" or cluster["status"]!="active":
                raise RuntimeError("Only active idle direct QUIC clusters can be handed off")
            item={"cluster":cluster,"machines":{},"assignments":{}}
            for identity in [cluster["primary_id"],*cluster["worker_ids"]]:
                machine=records[identity];runtime=machine.get("runtime")or{}
                if not machine.get("alive") or machine.get("last_seen_sec",999)>20 or not runtime.get("rpc_pid"):
                    raise RuntimeError("Cluster members must report fresh native processes")
                item["machines"][identity]={"lan_ip":machine["lan_ip"],"last_heartbeat":now-machine["last_seen_sec"],
                    "runtime":{k:runtime.get(k) for k in ("agent_pid","agent_revision","rpc_pid","rpc_quic",
                        "rpc_relay_external","rpc_preflight","rpc_tuning","rpc_stream","rpc_gpu_layers",
                        "rpc_total_layers","supervised","gpu_used_mb")}}
                item["assignments"][identity]=api("/agent/rpc/assignment?agent_id="+identity)
                expected_pids[identity]=runtime["rpc_pid"]
            snapshot["clusters"].append(item)
        writer="import sys,os,pathlib; p=pathlib.Path('/data/rpc-handoff.json'); t=p.with_suffix('.tmp'); os.umask(0o077); t.write_text(sys.stdin.read()); t.replace(p)"
        docker("exec","-T","coordinator","python","-c",writer,input=json.dumps(snapshot))
        docker("up","-d","--no-build","--no-deps","coordinator")
        # Resolve the recreated coordinator's address while admission stays shut.
        docker("exec","-T","gateway","nginx","-s","reload")
        until=time.monotonic()+25
        while True:
            try:
                after=api("/machines");break
            except Exception:
                if time.monotonic()>until:raise RuntimeError("Coordinator did not recover after deployment") from None
                time.sleep(.5)
        restored={c["cluster_id"] for c in after.get("rpc_clusters",[])}
        if restored!={c["cluster_id"]for c in before.get("rpc_clusters",[])}:
            raise RuntimeError("QUIC cluster registry was not restored")
        actual={m["agent_id"]:(m.get("runtime")or{}).get("rpc_pid") for m in after["machines"]}
        if any(actual.get(identity)!=pid for identity,pid in expected_pids.items()):
            raise RuntimeError("Native process identity changed during deployment")
        print(json.dumps({"status":"passed","cluster_ids":sorted(restored),"native_pids":expected_pids}),flush=True)
    finally:
        if gated:
            current=config.read_text()
            if GATE in current:config.write_text(current.replace(GATE,"",1))
            docker("exec","-T","gateway","nginx","-t")
            docker("exec","-T","gateway","nginx","-s","reload")
        cleanup=("import pathlib,json; p=pathlib.Path('/data/rpc-handoff.json'); "
                 "p.unlink() if p.exists() and json.loads(p.read_text()).get('handoff_id')=="+repr(handoff_id)+" else None")
        docker("exec","-T","coordinator","python","-c",cleanup)


if __name__=="__main__":main()
