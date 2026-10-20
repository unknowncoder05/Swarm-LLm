"""SQLite persistence behind an injected database path; no scheduler state."""

import asyncio
import json
import os
import sqlite3
import time
from typing import Callable, Optional

try:
    from api_models import RegisterPayload
    from models import MachineState, ImageJob, VideoJob
except ModuleNotFoundError:
    from coordinator.api_models import RegisterPayload
    from coordinator.models import MachineState, ImageJob, VideoJob


def _container_job_dict(row: sqlite3.Row, include_private: bool = False) -> dict:
    result = {
        "id": row["id"],
        "image": row["image"],
        "command": json.loads(row["command_json"]),
        "artifacts": json.loads(row["artifacts_json"]),
        "timeout_sec": row["timeout_sec"],
        "cpus": row["cpus"],
        "memory_mb": row["memory_mb"],
        "network": bool(row["network"]),
        "platform": row["platform"],
        "status": row["status"],
        "agent_id": row["agent_id"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "elapsed_ms": row["elapsed_ms"],
        "exit_code": row["exit_code"],
        "stdout": row["stdout"],
        "stderr": row["stderr"],
        "error": row["error"],
        "has_artifacts": bool(row["artifact_path"]),
    }
    if include_private:
        result["env"] = json.loads(row["env_json"])
    return result


class CoordinatorRepository:
    def __init__(self, path_provider: Callable[[], str]):
        self.path_provider = path_provider

    def latest_agent_reports(self):
        with self.db() as con:
            return {
                row["agent_id"]: json.loads(row["payload_json"])
                for row in con.execute(
                    "SELECT agent_id, payload_json FROM agent_reports WHERE id IN "
                    "(SELECT MAX(id) FROM agent_reports GROUP BY agent_id)"
                )
            }

    def db(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path_provider()), exist_ok=True)
        con = sqlite3.connect(self.path_provider(), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute(
            "PRAGMA journal_mode=WAL"
        )  # concurrent readers + writer don't block each other
        con.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; faster than FULL
        return con

    def init_db(self) -> None:
        with self.db() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS agent_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL, hostname TEXT, model TEXT,
                    received_at REAL NOT NULL, payload_json TEXT NOT NULL
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_reports_agent ON agent_reports(agent_id, id)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS machines (
                    agent_id       TEXT PRIMARY KEY,
                    ip             TEXT,
                    port           INTEGER,
                    model          TEXT,
                    hostname       TEXT,
                    cpu_name       TEXT,
                    cpu_cores      INTEGER,
                    ram_total_gb   REAL,
                    ram_free_gb    REAL,
                    gpu_name       TEXT,
                    gpu_vram_gb    REAL,
                    network_mbps   REAL,
                    ollama_version TEXT,
                    os_version     TEXT,
                    registered_at  REAL,
                    last_heartbeat REAL,
                    requests_ok    INTEGER DEFAULT 0,
                    requests_err   INTEGER DEFAULT 0,
                    lan_ip         TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                REAL    NOT NULL,
                    model             TEXT    NOT NULL,
                    user_id           TEXT    NOT NULL,
                    prompt_tokens     INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens      INTEGER DEFAULT 0,
                    elapsed_ms        REAL,
                    success           INTEGER DEFAULT 1
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts    ON usage_logs(ts)")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_logs(model)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS container_jobs (
                    id TEXT PRIMARY KEY, image TEXT NOT NULL, command_json TEXT NOT NULL,
                    env_json TEXT NOT NULL, artifacts_json TEXT NOT NULL,
                    timeout_sec INTEGER NOT NULL, cpus REAL NOT NULL, memory_mb INTEGER NOT NULL,
                    network INTEGER NOT NULL, platform TEXT, status TEXT NOT NULL,
                    agent_id TEXT, lease_until REAL, created_at REAL NOT NULL,
                    started_at REAL, finished_at REAL, elapsed_ms REAL, exit_code INTEGER,
                    stdout TEXT, stderr TEXT, error TEXT, artifact_path TEXT
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_container_jobs_status ON container_jobs(status, created_at)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS image_jobs (
                    id          TEXT PRIMARY KEY,
                    model       TEXT,
                    prompt      TEXT,
                    status      TEXT,
                    file_paths  TEXT,
                    error       TEXT,
                    created_at  REAL,
                    elapsed_ms  REAL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS video_jobs (
                    id          TEXT PRIMARY KEY,
                    model       TEXT,
                    prompt      TEXT,
                    status      TEXT,
                    file_path   TEXT,
                    error       TEXT,
                    created_at  REAL,
                    elapsed_ms  REAL
                )
            """)
            # Column migrations — silently skipped when the column already exists
            for _col in [
                "ALTER TABLE machines ADD COLUMN lan_ip TEXT",
                "ALTER TABLE usage_logs ADD COLUMN finish_reason   TEXT",
                "ALTER TABLE usage_logs ADD COLUMN hostname        TEXT",
                "ALTER TABLE usage_logs ADD COLUMN queue_wait_ms   REAL",
                "ALTER TABLE image_jobs ADD COLUMN negative_prompt TEXT",
                "ALTER TABLE image_jobs ADD COLUMN n               INTEGER DEFAULT 1",
                "ALTER TABLE image_jobs ADD COLUMN size            TEXT",
                "ALTER TABLE image_jobs ADD COLUMN quality         TEXT",
                "ALTER TABLE video_jobs ADD COLUMN negative_prompt TEXT",
                "ALTER TABLE video_jobs ADD COLUMN duration        INTEGER",
                "ALTER TABLE video_jobs ADD COLUMN width           INTEGER",
                "ALTER TABLE video_jobs ADD COLUMN height          INTEGER",
            ]:
                try:
                    con.execute(_col)
                except Exception:
                    pass

    def upsert_machine(self, m: "MachineState") -> None:
        with self.db() as con:
            con.execute(
                """
                INSERT INTO machines (
                    agent_id, ip, port, model, hostname,
                    cpu_name, cpu_cores, ram_total_gb, ram_free_gb,
                    gpu_name, gpu_vram_gb, network_mbps, ollama_version, os_version,
                    registered_at, last_heartbeat, requests_ok, requests_err, lan_ip
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    ip=excluded.ip, port=excluded.port, model=excluded.model,
                    hostname=excluded.hostname, cpu_name=excluded.cpu_name,
                    cpu_cores=excluded.cpu_cores, ram_total_gb=excluded.ram_total_gb,
                    ram_free_gb=excluded.ram_free_gb, gpu_name=excluded.gpu_name,
                    gpu_vram_gb=excluded.gpu_vram_gb, network_mbps=excluded.network_mbps,
                    ollama_version=excluded.ollama_version, os_version=excluded.os_version,
                    last_heartbeat=excluded.last_heartbeat,
                    requests_ok=excluded.requests_ok, requests_err=excluded.requests_err,
                    lan_ip=excluded.lan_ip
            """,
                (
                    m.agent_id,
                    m.ip,
                    m.port,
                    m.model,
                    m.hostname,
                    m.cpu_name,
                    m.cpu_cores,
                    m.ram_total_gb,
                    m.ram_free_gb,
                    m.gpu_name,
                    m.gpu_vram_gb,
                    m.network_mbps,
                    m.ollama_version,
                    m.os_version,
                    m.registered_at,
                    m.last_heartbeat,
                    m.requests_ok,
                    m.requests_err,
                    m.lan_ip,
                ),
            )

    def log_usage(
        self,
        model: str,
        user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        elapsed_ms: Optional[float],
        success: bool,
        finish_reason: Optional[str] = None,
        hostname: Optional[str] = None,
        queue_wait_ms: Optional[float] = None,
    ) -> None:
        try:
            with self.db() as con:
                con.execute(
                    "INSERT INTO usage_logs "
                    "(ts, model, user_id, prompt_tokens, completion_tokens, total_tokens, "
                    " elapsed_ms, success, finish_reason, hostname, queue_wait_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        model,
                        user_id,
                        prompt_tokens,
                        completion_tokens,
                        prompt_tokens + completion_tokens,
                        elapsed_ms,
                        1 if success else 0,
                        finish_reason,
                        hostname,
                        queue_wait_ms,
                    ),
                )
        except Exception as e:
            print(f"[coordinator] usage log failed: {e}")

    def save_image_job(self, job: "ImageJob") -> None:
        try:
            with self.db() as con:
                con.execute(
                    """
                    INSERT INTO image_jobs
                        (id, model, prompt, negative_prompt, n, size, quality,
                         status, file_paths, error, created_at, elapsed_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status, file_paths=excluded.file_paths,
                        error=excluded.error, elapsed_ms=excluded.elapsed_ms
                """,
                    (
                        job.id,
                        job.model,
                        job.prompt,
                        job.negative_prompt,
                        job.n,
                        job.size,
                        job.quality,
                        job.status,
                        json.dumps(job.file_paths),
                        job.error,
                        job.created_at,
                        job.elapsed_ms,
                    ),
                )
        except Exception as e:
            print(f"[coordinator] save image job failed: {e}")

    def save_video_job(self, job: "VideoJob") -> None:
        try:
            with self.db() as con:
                con.execute(
                    """
                    INSERT INTO video_jobs
                        (id, model, prompt, negative_prompt, duration, width, height,
                         status, file_path, error, created_at, elapsed_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status, file_path=excluded.file_path,
                        error=excluded.error, elapsed_ms=excluded.elapsed_ms
                """,
                    (
                        job.id,
                        job.model,
                        job.prompt,
                        job.negative_prompt,
                        job.duration,
                        job.width,
                        job.height,
                        job.status,
                        job.file_path,
                        job.error,
                        job.created_at,
                        job.elapsed_ms,
                    ),
                )
        except Exception as e:
            print(f"[coordinator] save video job failed: {e}")

    def load_media_jobs(self) -> "tuple[dict, dict]":
        """Load pending image/video jobs from DB to survive coordinator restarts."""
        imgs: dict = {}
        vids: dict = {}
        try:
            with self.db() as con:
                for row in con.execute(
                    "SELECT * FROM image_jobs WHERE status IN ('queued','processing') ORDER BY created_at"
                ):
                    job = object.__new__(ImageJob)
                    job.id = row["id"]
                    job.model = row["model"]
                    job.prompt = row["prompt"] or ""
                    job.negative_prompt = row["negative_prompt"] or ""
                    job.n = row["n"] or 1
                    job.size = row["size"] or "1024x1024"
                    job.quality = row["quality"] or "standard"
                    job.created_at = row["created_at"]
                    job.status = "queued"
                    job.dequeued_at = None
                    job.done_event = asyncio.Event()
                    job.file_paths = json.loads(row["file_paths"] or "[]")
                    job.error = None
                    job.elapsed_ms = None
                    job.cancelled = False
                    job.progress = None
                    imgs[job.id] = job
                for row in con.execute(
                    "SELECT * FROM video_jobs WHERE status IN ('queued','processing') ORDER BY created_at"
                ):
                    job = object.__new__(VideoJob)
                    job.id = row["id"]
                    job.model = row["model"]
                    job.prompt = row["prompt"] or ""
                    job.negative_prompt = row["negative_prompt"] or ""
                    job.duration = row["duration"] or 5
                    job.width = row["width"] or 512
                    job.height = row["height"] or 512
                    job.num_inference_steps = 0  # default — not persisted currently
                    job.created_at = row["created_at"]
                    job.status = "queued"
                    job.dequeued_at = None
                    job.file_path = None
                    job.error = None
                    job.elapsed_ms = None
                    job.progress = None
                    vids[job.id] = job
        except Exception as e:
            print(f"[coordinator] load media jobs failed: {e}")
        return imgs, vids

    def load_machines(self) -> dict[str, "MachineState"]:
        """Load persisted machines at startup. Mark all as dead until they heartbeat."""
        result: dict[str, MachineState] = {}
        try:
            with self.db() as con:
                for row in con.execute("SELECT * FROM machines"):
                    reg = RegisterPayload(
                        agent_id=row["agent_id"],
                        port=row["port"],
                        model=row["model"],
                        hostname=row["hostname"],
                        cpu_name=row["cpu_name"],
                        cpu_cores=row["cpu_cores"],
                        ram_total_gb=row["ram_total_gb"],
                        ram_free_gb=row["ram_free_gb"],
                        gpu_name=row["gpu_name"],
                        gpu_vram_gb=row["gpu_vram_gb"],
                        network_mbps=row["network_mbps"],
                        ollama_version=row["ollama_version"],
                        os_version=row["os_version"],
                        lan_ip=row["lan_ip"],
                    )
                    m = MachineState(row["ip"], reg)
                    m.registered_at = row["registered_at"]
                    m.last_heartbeat = 0  # force dead until agent heartbeats
                    m.alive = False
                    m.requests_ok = row["requests_ok"]
                    m.requests_err = row["requests_err"]
                    result[m.agent_id] = m
        except Exception as e:
            print(f"[coordinator] DB load warning: {e}")
        return result

    def usage_stats(
        self,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        bucket: str = "day",
    ):
        now = time.time()
        if from_ts is None:
            from_ts = now - 7 * 86400
        if to_ts is None:
            to_ts = now
        if bucket not in ("hour", "day", "week"):
            bucket = "day"

        if bucket == "hour":
            fmt = "%Y-%m-%dT%H:00:00"
        elif bucket == "week":
            fmt = "%Y-W%W"
        else:
            fmt = "%Y-%m-%d"

        with self.db() as con:
            series_rows = con.execute(
                """
                SELECT
                    strftime(?, datetime(ts, 'unixepoch')) AS bkt,
                    SUM(prompt_tokens)                     AS prompt_tokens,
                    SUM(completion_tokens)                 AS completion_tokens,
                    SUM(total_tokens)                      AS total_tokens,
                    COUNT(*)                               AS requests,
                    SUM(CASE WHEN success=0 THEN 1 ELSE 0 END)  AS errors,
                    AVG(CASE WHEN success=1 THEN elapsed_ms END) AS avg_ms,
                    AVG(CASE WHEN queue_wait_ms IS NOT NULL THEN queue_wait_ms END) AS avg_queue_ms
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY bkt
                ORDER BY bkt
            """,
                (fmt, from_ts, to_ts),
            ).fetchall()

            model_rows = con.execute(
                """
                SELECT
                    model,
                    SUM(prompt_tokens)     AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(total_tokens)      AS total_tokens,
                    COUNT(*)               AS requests,
                    SUM(CASE WHEN success=0 THEN 1 ELSE 0 END)  AS errors,
                    AVG(CASE WHEN success=1 THEN elapsed_ms END) AS avg_ms,
                    AVG(CASE WHEN queue_wait_ms IS NOT NULL THEN queue_wait_ms END) AS avg_queue_ms
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY model
                ORDER BY requests DESC
            """,
                (from_ts, to_ts),
            ).fetchall()

            user_rows = con.execute(
                """
                SELECT
                    user_id,
                    SUM(total_tokens) AS total_tokens,
                    COUNT(*)          AS requests
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY user_id
                ORDER BY requests DESC
                LIMIT 20
            """,
                (from_ts, to_ts),
            ).fetchall()

            hour_rows = con.execute(
                """
                SELECT
                    CAST(strftime('%H', datetime(ts, 'unixepoch')) AS INTEGER) AS hour,
                    COUNT(*)                                                    AS requests,
                    AVG(CASE WHEN success=1 THEN elapsed_ms END)               AS avg_ms
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY hour
                ORDER BY hour
            """,
                (from_ts, to_ts),
            ).fetchall()

            reason_rows = con.execute(
                """
                SELECT
                    COALESCE(finish_reason, 'unknown') AS reason,
                    COUNT(*) AS count
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY reason
                ORDER BY count DESC
            """,
                (from_ts, to_ts),
            ).fetchall()

            host_rows = con.execute(
                """
                SELECT
                    COALESCE(hostname, 'unknown') AS hostname,
                    COUNT(*)                      AS requests,
                    SUM(total_tokens)             AS total_tokens,
                    AVG(CASE WHEN success=1 THEN elapsed_ms END)               AS avg_ms,
                    AVG(CASE WHEN queue_wait_ms IS NOT NULL THEN queue_wait_ms END) AS avg_queue_ms
                FROM usage_logs
                WHERE ts >= ? AND ts <= ?
                GROUP BY hostname
                ORDER BY requests DESC
            """,
                (from_ts, to_ts),
            ).fetchall()

        # Fill in all 24 hours even if no data (makes heatmap uniform)
        hour_map = {r["hour"]: r for r in hour_rows}
        by_hour = [
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "requests": (hour_map[h]["requests"] if h in hour_map else 0),
                "avg_ms": (
                    round(hour_map[h]["avg_ms"], 1)
                    if h in hour_map and hour_map[h]["avg_ms"]
                    else None
                ),
            }
            for h in range(24)
        ]

        return {
            "from_ts": from_ts,
            "to_ts": to_ts,
            "bucket": bucket,
            "series": [
                {
                    "bucket": r["bkt"],
                    "prompt_tokens": r["prompt_tokens"] or 0,
                    "completion_tokens": r["completion_tokens"] or 0,
                    "total_tokens": r["total_tokens"] or 0,
                    "requests": r["requests"] or 0,
                    "errors": r["errors"] or 0,
                    "avg_ms": round(r["avg_ms"], 1) if r["avg_ms"] else None,
                    "avg_queue_ms": round(r["avg_queue_ms"], 1)
                    if r["avg_queue_ms"]
                    else None,
                }
                for r in series_rows
            ],
            "by_model": [
                {
                    "model": r["model"],
                    "prompt_tokens": r["prompt_tokens"] or 0,
                    "completion_tokens": r["completion_tokens"] or 0,
                    "total_tokens": r["total_tokens"] or 0,
                    "requests": r["requests"] or 0,
                    "errors": r["errors"] or 0,
                    "avg_ms": round(r["avg_ms"], 1) if r["avg_ms"] else None,
                    "avg_queue_ms": round(r["avg_queue_ms"], 1)
                    if r["avg_queue_ms"]
                    else None,
                }
                for r in model_rows
            ],
            "by_user": [
                {
                    "user_id": r["user_id"],
                    "total_tokens": r["total_tokens"] or 0,
                    "requests": r["requests"] or 0,
                }
                for r in user_rows
            ],
            "by_hour": by_hour,
            "by_finish_reason": [
                {"reason": r["reason"], "count": r["count"]} for r in reason_rows
            ],
            "by_hostname": [
                {
                    "hostname": r["hostname"],
                    "requests": r["requests"] or 0,
                    "total_tokens": r["total_tokens"] or 0,
                    "avg_ms": round(r["avg_ms"], 1) if r["avg_ms"] else None,
                    "avg_queue_ms": round(r["avg_queue_ms"], 1)
                    if r["avg_queue_ms"]
                    else None,
                }
                for r in host_rows
            ],
        }
