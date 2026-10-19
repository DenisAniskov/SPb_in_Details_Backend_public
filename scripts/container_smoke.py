"""Exercise a built Linux image, root-owned volumes, real HTTP and persistence.

Run: python scripts/container_smoke.py --image smartnavigator-backend:a5
Fresh named test volumes are retained, even on failure. This script never
deletes a volume or mutates an existing staging database. Docker is required.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

try:
    from .http_smoke import check_http, request, require
except ImportError:
    from http_smoke import check_http, request, require

PORT = 8123
DATABASE_URL = "sqlite+aiosqlite:////data/smart_navigator_spb.db"


def docker(*args: str, timeout: int = 90, check: bool = True,
           include_stderr: bool = False) -> str:
    completed = subprocess.run(["docker", *args], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
    if check and completed.returncode:
        raise RuntimeError(f"Docker {args[0]} failed with exit code {completed.returncode}")
    return (completed.stdout + (completed.stderr if include_stderr else "")).strip()


def execute(name: str, source: str, *, user: str = "10001:10001") -> str:
    return docker("exec", "--user", user, name, "python", "-c", source)


def endpoint(name: str) -> str:
    info = json.loads(docker("inspect", name))[0]
    binding = info["NetworkSettings"]["Ports"][f"{PORT}/tcp"][0]
    require(binding["HostIp"] == "127.0.0.1", "Smoke port must bind to loopback")
    return f'http://127.0.0.1:{binding["HostPort"]}'


def wait_ready(base_url: str, seconds: int = 60) -> float:
    start = time.monotonic()
    host, port = "127.0.0.1", int(base_url.rsplit(":", 1)[1])
    while time.monotonic() - start < seconds:
        try:
            with socket.create_connection((host, port), timeout=1):
                pass
            status, body, _ = request(base_url, "/ready")
            if status == 200 and body.get("status") == "ready":
                return round(time.monotonic() - start, 3)
        except (OSError, ValueError, TimeoutError):
            pass
        time.sleep(0.25)
    raise RuntimeError("Container readiness did not pass within 60 seconds")


def wait_healthy(name: str, seconds: int = 75) -> dict:
    """Require the image's actual Docker HEALTHCHECK to finish successfully."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        state = json.loads(docker("inspect", name))[0]["State"]
        require(state["Running"] and not state["OOMKilled"],
                "Container stopped before its Docker healthcheck passed")
        health = state.get("Health")
        require(health is not None, "Built image has no Docker healthcheck state")
        if health["Status"] == "healthy":
            return {"status": "healthy", "wait_seconds": round(time.monotonic() - start, 3)}
        require(health["Status"] != "unhealthy", "Docker healthcheck reports unhealthy")
        time.sleep(1)
    raise RuntimeError(f"Docker healthcheck did not become healthy within {seconds} seconds")


def launch(image: str, name: str, volume: str) -> None:
    environment = {"BACKEND_ENV": "staging", "HOST": "0.0.0.0", "PORT": str(PORT),
                   "WORKERS": "1", "RELOAD": "false", "LOG_LEVEL": "info",
                   "DATABASE_URL": DATABASE_URL, "SHADE_BUILDING_DATASET": "data/shade_buildings_spb_pilot.sqlite",
                   "SHADE_MAX_CONCURRENT_ANALYSES": "2", "SHADE_ANALYSIS_TIMEOUT_SECONDS": "10",
                   "ALLOWED_ORIGINS": "[]", "TRUSTED_PROXY_IPS": "", "GEOCODER_API_KEY": "",
                   "SUGGEST_API_KEY": "", "YANDEX_SUGGEST_DAILY_BUDGET": "0",
                   "YANDEX_GEOCODER_DAILY_BUDGET": "0", "REQUEST_MAX_BYTES": "16384",
                   "RAILWAY_RUN_UID": "0", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    args = ["run", "--detach", "--name", name, "--platform", "linux/amd64",
            "--user", "0:0", "--memory", "512m", "--cpus", "2",
            "--publish", f"127.0.0.1::{PORT}", "--mount", f"type=volume,source={volume},target=/data"]
    for key, value in environment.items():
        args.extend(("--env", f"{key}={value}"))
    docker(*args, image)


def snapshot(name: str) -> dict:
    source = r'''
import hashlib,json,os,sqlite3,stat
from pathlib import Path
connection=sqlite3.connect('file:/data/smart_navigator_spb.db?mode=ro',uri=True)
catalog=connection.execute('SELECT id,catalog_key FROM places ORDER BY id').fetchall()
ledger=connection.execute('SELECT id,product,reserved_at FROM upstream_quota_ledger ORDER BY id').fetchall()
uid=next(line.split()[1:] for line in Path('/proc/1/status').read_text().splitlines() if line.startswith('Uid:'))
gid=next(line.split()[1:] for line in Path('/proc/1/status').read_text().splitlines() if line.startswith('Gid:'))
osm=sqlite3.connect('file:/app/data/shade_buildings_spb_pilot.sqlite?mode=ro',uri=True)
result={'app_uid':uid,'app_gid':gid,'directory_uid':os.stat('/data').st_uid,
        'directory_mode':oct(stat.S_IMODE(os.stat('/data').st_mode)),
        'database_uid':os.stat('/data/smart_navigator_spb.db').st_uid,
        'poi':len(catalog),'catalog_sha256':hashlib.sha256(repr(catalog).encode()).hexdigest(),
        'quota_rows':len(ledger),'quota_sha256':hashlib.sha256(repr(ledger).encode()).hexdigest(),
        'integrity_check':connection.execute('PRAGMA integrity_check').fetchall(),
        'osm_buildings':osm.execute('SELECT COUNT(*) FROM buildings').fetchone()[0],
        'osm_zones':osm.execute('SELECT COUNT(*) FROM zones').fetchone()[0],
        'volume_bytes':sum(path.stat().st_size for path in Path('/data').rglob('*') if path.is_file())}
print(json.dumps(result))
'''
    result = json.loads(execute(name, source))
    require(result["app_uid"] == ["10001"] * 4 and result["app_gid"] == ["10001"] * 4,
            "Backend process has not dropped root privileges")
    require(result["directory_uid"] == 10001 and result["directory_mode"] == "0o700"
            and result["database_uid"] == 10001, "SQLite ownership/volume permissions incorrect")
    require(result["poi"] == 180 and result["integrity_check"] == [["ok"]], "SQLite catalog/integrity incorrect")
    require(result["osm_buildings"] == 1409 and result["osm_zones"] == 6, "Real OSM dataset counts incorrect")
    return result


def resources(name: str) -> dict:
    source = r'''
import json
from pathlib import Path
root=Path('/sys/fs/cgroup')
result={}
for name in ('memory.current','memory.peak','memory.max'):
    path=root/name
    if path.exists():
        value=path.read_text().strip()
        result[name]=int(value) if value.isdigit() else value
cpu=root/'cpu.stat'
if cpu.exists():
    result['cpu.stat']={key:int(value) for key,value in (line.split() for line in cpu.read_text().splitlines())}
print(json.dumps(result))
'''
    result = json.loads(execute(name, source))
    result["docker_stats"] = json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", name))
    info = json.loads(docker("inspect", name))[0]
    result["oom_killed"] = info["State"]["OOMKilled"]
    require(not result["oom_killed"], "512 MiB container was OOM-killed")
    if "memory.peak" in result:
        require(result["memory.peak"] < 512 * 1024 * 1024, "Observed cgroup peak exceeds 512 MiB")
    return result


def same_persistence(before: dict, after: dict) -> None:
    for key in ("poi", "catalog_sha256", "quota_rows", "quota_sha256"):
        require(before[key] == after[key], f"Persistent {key} changed across lifecycle operation")


def cpu_usage_usec(name: str) -> int:
    # cgroup total CPU counter, measured around the single shade request.
    # Includes the two tiny cat probes; no other workload is launched.
    content = docker("exec", "--user", "10001:10001", name, "cat", "/sys/fs/cgroup/cpu.stat")
    counters = dict(line.split() for line in content.splitlines())
    return int(counters["usage_usec"])


def stop_gracefully(name: str) -> dict:
    start = time.monotonic()
    docker("stop", "--time", "20", name, timeout=30)
    state = json.loads(docker("inspect", name))[0]["State"]
    require(state["ExitCode"] == 0 and not state["OOMKilled"], "Container did not stop gracefully")
    return {"exit_code": state["ExitCode"], "duration_seconds": round(time.monotonic() - start, 3)}


def run_smoke(image: str, output: Path) -> dict:
    identifier = uuid.uuid4().hex[:12]
    volume, restore_volume = f"smartnavigator-a5-{identifier}", f"smartnavigator-a5-restore-{identifier}"
    names = [f"smartnavigator-a5-{identifier}-{stage}" for stage in ("initial", "replacement", "restore")]
    negative_name = f"smartnavigator-a5-{identifier}-no-volume"
    result: dict = {"status": "RUNNING", "image": image, "port": PORT,
                    "source_commit": os.environ.get("GITHUB_SHA"),
                    "container_memory_limit_bytes": 512 * 1024 * 1024, "cpu_limit": 2,
                    "test_volumes_retained": [volume, restore_volume],
                    "cloud_deployment": "NOT_TESTED", "new_commit_redeploy": "NOT_TESTED"}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        image_info = json.loads(docker("image", "inspect", image))[0]
        config = image_info["Config"]
        result["image_id"] = image_info["Id"]
        require(config["User"] == "10001:10001", "Image default USER must remain 10001:10001")
        result["image_default_user"] = config["User"]
        docker("run", "--name", negative_name, image, timeout=30, check=False)
        negative_state = json.loads(docker("inspect", negative_name))[0]["State"]
        negative_logs = docker("logs", negative_name, check=False, include_stderr=True)
        require(negative_state["ExitCode"] == 1 and "Container startup refused:" in negative_logs,
                "Staging/production image must reject an absent persistent volume")
        result["absent_volume_refused"] = "PASS: startup exit 1"
        for target in (volume, restore_volume):
            docker("volume", "create", target)
            # Explicit root override models the documented Railway volume issue.
            owned = json.loads(docker("run", "--rm", "--user", "0:0", "--entrypoint", "python",
                "--mount", f"type=volume,source={target},target=/data", image, "-c",
                "import os,json;os.chown('/data',0,0);os.chmod('/data',0o700);"
                "print(json.dumps({'uid':os.stat('/data').st_uid,'files':os.listdir('/data')}))"))
            require(owned == {"uid": 0, "files": []}, "Test volume must start empty and root-owned")

        start = time.monotonic()
        launch(image, names[0], volume)
        url = endpoint(names[0])
        wait_ready(url)
        result["startup_seconds"] = round(time.monotonic() - start, 3)
        result["initial_docker_healthcheck"] = wait_healthy(names[0])
        initial = snapshot(names[0])
        require(initial["quota_rows"] == 0, "Fresh volume quota ledger is not empty")
        result["idle_resources"] = resources(names[0])
        result["http"] = check_http(url, cpu_usage_usec=lambda: cpu_usage_usec(names[0]))
        result["after_http_resources"] = resources(names[0])

        # Reserve through production durable controls in a separate process.
        # Only that process gets a test budget; the HTTP server stays at zero.
        execute(names[0], "import asyncio;from app.core.config import settings;"
                "from app.core.upstream_controls import upstream_controls;from app.db.session import engine;"
                "settings.YANDEX_SUGGEST_DAILY_BUDGET=2;"
                "exec('async def check():\\n await upstream_controls.reserve(\"suggest\")\\n await engine.dispose()');"
                "asyncio.run(check())")
        before = snapshot(names[0])
        require(before["quota_rows"] == 1, "Durable quota reservation did not persist")
        result["quota_test"] = "One local reserve through production controls; no external HTTP call"
        result["initial_shutdown"] = stop_gracefully(names[0])
        docker("start", names[0])
        wait_ready(endpoint(names[0]))
        result["restart_docker_healthcheck"] = wait_healthy(names[0])
        same_persistence(before, snapshot(names[0]))
        result["restart_persistence"] = "PASS"
        result["restart_shutdown"] = stop_gracefully(names[0])

        launch(image, names[1], volume)
        wait_ready(endpoint(names[1]))
        result["replacement_docker_healthcheck"] = wait_healthy(names[1])
        after = snapshot(names[1])
        same_persistence(before, after)
        result["replacement_container_persistence"] = "PASS: same built image, new container, same volume"
        result["persistent_snapshot"] = after

        backup = json.loads(docker("exec", "--user", "10001:10001", names[1], "python",
                                  "/app/scripts/sqlite_backup.py", "--output", "/tmp/a5-online-backup.db"))
        require(backup["status"] == "ok" and backup["integrity_check"] == "ok"
                and backup["restore_validation"] == "ok", "Online backup/isolated restore validation failed")
        backup_path = output.parent / f"a5-online-backup-{identifier}.db"
        docker("cp", f"{names[1]}:/tmp/a5-online-backup.db", str(backup_path))
        require(backup_path.is_file(), "Consistent backup was not exported outside volume")
        result["backup"] = {**backup, "exported_outside_volume": True}
        verified = subprocess.run([sys.executable, str(Path(__file__).parent / "sqlite_backup.py"),
                                   "--verify-only", str(backup_path)], capture_output=True,
                                  text=True, timeout=90)
        require(verified.returncode == 0 and json.loads(verified.stdout)["restore_validation"] == "ok",
                "Exported host backup failed independent restore verification")
        result["backup"]["host_export_restore_validation"] = "ok"

        # Restore into the independent empty volume. Existing source volume is untouched.
        source = ("import sqlite3,os;source=sqlite3.connect('file:/backup/" + backup_path.name +
                  "?mode=ro',uri=True);target=sqlite3.connect('/data/smart_navigator_spb.db');"
                  "source.backup(target);target.close();source.close();"
                  "os.chown('/data/smart_navigator_spb.db',10001,10001);"
                  "os.chmod('/data/smart_navigator_spb.db',0o600)")
        docker("run", "--rm", "--user", "0:0", "--entrypoint", "python",
               "--mount", f"type=volume,source={restore_volume},target=/data",
               "--mount", f"type=bind,source={output.parent.resolve()},target=/backup,readonly",
               image, "-c", source)
        launch(image, names[2], restore_volume)
        wait_ready(endpoint(names[2]))
        result["restore_docker_healthcheck"] = wait_healthy(names[2])
        same_persistence(before, snapshot(names[2]))
        status, catalog, _ = request(endpoint(names[2]), "/api/v1/places?limit=200")
        require(status == 200 and catalog["total"] == 180, "Restored application cannot serve catalog")
        result["restore_separate_volume"] = "PASS"
        result["final_resources"] = resources(names[1])
        result["replacement_shutdown"] = stop_gracefully(names[1])
        result["restore_shutdown"] = stop_gracefully(names[2])
        for name in names:
            logs = docker("logs", name, check=False, include_stderr=True)
            require(not any(marker in logs for marker in
                            ("A5_NO_NETWORK_CHECK", "59.9343", "30.3351", "GEOCODER_API_KEY=")),
                    "Container logs contain request data or a credential assignment")
        result["logs_privacy_check"] = "PASS: bounded markers absent; test keys unset"
        result["status"] = "PASS"
        return result
    except Exception as error:
        result["status"] = "FAIL"
        result["error"] = str(error)
        raise
    finally:
        for name in [*names, negative_name]:
            docker("rm", "--force", name, check=False)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, default=Path(".validation/container-smoke.json"))
    args = parser.parse_args()
    if not shutil.which("docker"):
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"status": "NOT_TESTED", "reason": "Docker CLI unavailable"}) + "\n")
        print("Docker CLI unavailable: container smoke NOT_TESTED")
        return 2
    result = run_smoke(args.image, args.report.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
