"""Continuous read-only monitor for the ALAS 'alas小开' scheduler.

Watches the live log and the GG panel/control APIs, records task transitions,
GGModifier decisions, memory state changes and VRAM, and flags if 555 is
applied while a non-whitelisted task is running.

Runs until stopped (Ctrl+C / task kill).  All ALAS/adb writes are untouched.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import time
import sys
from datetime import datetime
import urllib.parse
import urllib.request

ALAS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ALAS)
from module.gg_modifier.runtime_lock import file_lock

LOG_DIR = os.path.join(ALAS, "log")
CONFIG = "alas小开"
OUT_DEFAULT = os.path.join(ALAS, "config", "gg_modifier", "monitor.log")
WHITELIST = {
    "Main", "Main2", "Main3",
    "Event", "Event2", "EventA", "EventB", "EventC", "EventD", "EventSp",
}

started = time.time()
events = 0
errors = []
violations = []
starts = 0
ends = 0
applies = 0
restores = 0
last_mem = None
last_mem_line = None
last_task = None
last_alive = None


def log(line, source=None):
    if source:
        line += " | ALAS " + source
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    if sys.stdout is not None:
        print(line, flush=True)
    if os.path.exists(args.out) and os.path.getsize(args.out) > 2 * 1024 * 1024:
        os.replace(args.out, args.out + ".1")
    with open(args.out, "a", encoding="utf-8") as out:
        out.write(line + "\n")


def api(port, path, timeout=10):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=timeout
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def panel():
    try:
        return api(22333, "/api/state?config=" + urllib.parse.quote(CONFIG), timeout=20)
    except Exception:
        return None


def control():
    try:
        return api(22334, "/status", timeout=10)
    except Exception:
        return None


def gpu_memory():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        ).stdout.decode("utf-8", errors="replace").strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def check_state():
    """Poll panel + control APIs; log notable changes."""
    global last_mem, last_mem_line, last_task, last_alive, violations
    state = panel()
    if state is None:
        if last_mem_line != "panel-down":
            log("PANEL unavailable")
            last_mem_line = "panel-down"
        state = {}
    mem = state.get("memory") or {}
    alas = state.get("alas") or {}
    mode = mem.get("mode")
    value = mem.get("value")
    task = alas.get("task")
    last_task = task
    last_mem = mode
    line = f"MEM {mode} value={value} task={task or '-'}"
    if state and line != last_mem_line:
        log(line)
        last_mem_line = line
    if mode == "applied" and task and task not in WHITELIST:
        key = (task, value)
        if key not in violations:
            violations.append(key)
            log(
                f"VIOLATION: memory applied {value} while "
                f"non-whitelisted task `{task}` is running"
            )
    if mode in ("mixed", "error"):
        log(f"STATE {mode}: {json.dumps(mem, ensure_ascii=False)}")
    ctrl = control()
    alive = None
    if ctrl:
        alive = (ctrl.get("instances") or {}).get(CONFIG, {}).get("alive")
    if alive != last_alive:
        log(f"SCHEDULER alive={alive}")
        last_alive = alive


def record_event(line):
    global starts, ends, events, applies, restores
    stamp = line[:23]
    if "Scheduler: Start task" in line:
        starts += 1
        log("TASK START " + line.split("Scheduler: Start task", 1)[1].strip(), stamp)
    elif "Scheduler: End task" in line:
        ends += 1
        log("TASK END   " + line.split("Scheduler: End task", 1)[1].strip(), stamp)
    elif "Wait until" in line and "for task" in line:
        log("WAIT " + line.split("Wait until", 1)[1].strip(), stamp)
    elif "GGModifier:" in line:
        events += 1
        if "applied" in line and "failed" not in line:
            applies += 1
        if "restored" in line and "failed" not in line:
            restores += 1
        log("GG " + line.split("GGModifier:", 1)[1].strip(), stamp)
    elif any(
        marker in line
        for marker in ("ERROR", "CRITICAL", "Traceback", "Task failed")
    ):
        if len(errors) < 500:
            errors.append(line.strip())
        log("ERR " + line.strip(), stamp)


def legacy_watermark(out):
    # The old monitor carried no byte offset. Replay the last heartbeat's second.
    try:
        with open(out, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 16384))
            for line in reversed(stream.read().splitlines()):
                stamp = line[:19].decode("ascii", errors="ignore")
                try:
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                    return stamp
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return ""


def load_cursor(out, config):
    try:
        with open(out + ".cursor.json", encoding="utf-8") as stream:
            cursor = json.load(stream)
        if cursor.get("config") == config:
            return cursor
    except FileNotFoundError:
        pass
    return {"config": config, "watermark": legacy_watermark(out)}


def save_cursor(out, cursor):
    path = out + ".cursor.json"
    with open(path + ".tmp", "w", encoding="utf-8") as stream:
        json.dump(cursor, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(path + ".tmp", path)


def cursor_signature(stream, offset):
    start = max(0, offset - 64)
    stream.seek(start)
    return hashlib.sha256(stream.read(offset - start)).hexdigest()


def read_logs(directory, cursor, consume, checkpoint):
    pattern = os.path.join(directory, "????-??-??_" + glob.escape(cursor["config"]) + ".txt")
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path)
        if name < cursor.get("file", "") or name[:10] < cursor.get("watermark", "")[:10]:
            continue
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            identity = [stat.st_dev, stat.st_ino]
            offset = cursor.get("offset", 0) if name == cursor.get("file") else 0
            if identity != cursor.get("identity") or offset > stat.st_size:
                offset = 0
            if offset and cursor.get("signature") and cursor_signature(stream, offset) != cursor["signature"]:
                offset = 0
            stream.seek(offset)
            while True:
                raw = stream.readline()
                if not raw.endswith(b"\n"):
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                stamp = line[:19]
                if not cursor.get("watermark") or stamp >= cursor["watermark"]:
                    consume(line)
                offset = stream.tell()
            signature = cursor_signature(stream, offset)
        if (name, identity, offset, signature) != (
                cursor.get("file"), cursor.get("identity"), cursor.get("offset"), cursor.get("signature")):
            updated = dict(cursor, file=name, identity=identity, offset=offset, signature=signature)
            checkpoint(updated)
            cursor.update(updated)


def main():
    cursor = load_cursor(args.out, CONFIG)
    save_cursor(args.out, cursor)
    log("monitor start: config={} cursor={} offset={}".format(CONFIG, cursor.get("file"), cursor.get("offset")))
    next_state = next_heartbeat = next_vram = 0
    while args.duration <= 0 or time.time() - started < args.duration:
        read_logs(LOG_DIR, cursor, record_event, lambda current: save_cursor(args.out, current))
        now = time.monotonic()
        if now >= next_state:
            check_state()
            next_state = now + args.state_interval
        if now >= next_heartbeat:
            log("HEARTBEAT task={} memory={}".format(last_task or "-", last_mem or "-"))
            next_heartbeat = now + args.heartbeat
        if args.vram_interval > 0 and now >= next_vram:
            vram = gpu_memory()
            if vram:
                log("VRAM {} MiB".format(vram))
            next_vram = now + args.vram_interval
        time.sleep(args.poll)
    log("SUMMARY starts={} ends={} gg_events={} applies={} restores={} errors={}".format(
        starts, ends, events, applies, restores, len(errors)))


def supervise():
    command = [sys.executable, "-I", os.path.abspath(__file__)] + [arg for arg in sys.argv[1:] if arg != "--supervise"]
    while True:
        code = subprocess.call(command, creationflags=0x08000000 if os.name == "nt" else 0)
        if code == 0 and args.duration > 0:
            return
        log("worker exited {}. Restarting in five seconds".format(code))
        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--state-interval", type=int, default=30)
    parser.add_argument("--heartbeat", type=int, default=300)
    parser.add_argument("--vram-interval", type=int, default=600)
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--out", default=OUT_DEFAULT)
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args()
    CONFIG = args.config
    args.out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    suffix = ".supervisor.lock" if args.supervise else ".worker.lock"
    try:
        with file_lock(args.out + suffix):
            if args.supervise:
                supervise()
            else:
                main()
    except TimeoutError:
        sys.exit(3)
