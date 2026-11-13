#!/usr/bin/env python3
"""ALAS x GameGuardian multiplier control panel.

Local-only web UI (127.0.0.1) for:
  * editing the gg_modifier.json settings shared with the ALAS hook
  * switching mode: always_on / always_off / auto (per task)
  * immediate apply / restore
  * live status: game PID, current memory state, ALAS current task

Run:  python server.py [--port 22333]
"""
import argparse
import glob
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(PANEL_DIR)
ALAS_ROOT = os.path.abspath(os.path.join(MODULE_DIR, "..", ".."))
sys.path.insert(0, MODULE_DIR)
import alas_gg  # noqa: E402

DEFAULT_CONFIG_PATH = os.path.join(ALAS_ROOT, "config", "gg_modifier", "settings.json")
DEFAULT_LOG_DIR = os.path.join(ALAS_ROOT, "log")
DEFAULT_ALAS_PORT = 22267
DEFAULT_PORT = 22333

DEFAULT_TASKS_ON = [
    "Main", "Main2", "Main3",
    "Event", "Event2", "EventA", "EventB", "EventC", "EventD", "EventSp",
]

DEFAULT_CONFIG = {
    "version": 2,
    "Enable": True,
    "Mode": "auto",  # always_on | always_off | auto（按任务开关）
    "Multiplier": 555,
    "Tasks": {name: True for name in DEFAULT_TASKS_ON},
}

TASK_GROUPS = {
    "出战/活动": [
        "Main", "Main2", "Main3", "Event", "Event2",
        "EventA", "EventB", "EventC", "EventD", "EventSp",
        "Raid", "RaidDaily", "GemsFarming", "Coalition", "CoalitionSp",
        "WarArchives", "Hospital", "EventShop",
    ],
    "日常/其他": [
        "Exercise", "Daily", "Hard", "Commission", "Research", "Dorm",
        "Meowfficer", "Guild", "Reward", "ShopFrequent", "ShopOnce",
        "Tactical", "Freebies", "Minigame", "PrivateQuarters",
        "Awaken", "Shipyard", "Gacha", "EventStory",
        "IslandProduction", "IslandOrder", "IslandTransport", "IslandFreebie",
        "IslandCollect", "IslandSeasonTask", "IslandBusiness", "IslandProductionPlanner",
    ],
    "大世界": [
        "OpsiAshAssist", "OpsiAshBeacon", "OpsiExplore", "OpsiShop",
        "OpsiVoucher", "OpsiDaily", "OpsiObscure", "OpsiMonthBoss",
        "OpsiAbyssal", "OpsiArchive", "OpsiStronghold",
        "OpsiMeowfficerFarming", "OpsiHazard1Leveling", "OpsiCrossMonth",
    ],
}

# original task name -> localized name (loaded from ALAS i18n)
TASK_DISPLAY = {}
# normalized original/display name -> original task name
TASK_ALIASES = {}


def _norm_task(name):
    return str(name).strip().lower().replace("_", "").replace(" ", "")


def resolve_task(name):
    name = str(name).strip()
    return TASK_ALIASES.get(_norm_task(name), name)


def load_task_names(config_path):
    """Load Task.<name>.name from ALAS zh-CN i18n next to the config dir."""
    global TASK_DISPLAY, TASK_ALIASES
    TASK_DISPLAY = {}
    TASK_ALIASES = {}
    root = os.path.dirname(os.path.abspath(config_path))
    for _ in range(6):
        if os.path.isdir(os.path.join(root, "module", "config", "i18n")):
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    candidates = [
        os.path.join(root, "module", "config", "i18n", "zh-CN.json"),
        os.path.join(root, "module", "config", "i18n", "zh_CN.json"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        task = data.get("Task", {}) if isinstance(data, dict) else {}
        if isinstance(task, dict):
            for key, val in task.items():
                display = val.get("name") if isinstance(val, dict) else None
                TASK_DISPLAY[key] = display or key
                TASK_ALIASES[_norm_task(key)] = key
                if display:
                    TASK_ALIASES[_norm_task(display)] = key
        break

RE_START = re.compile(r"Scheduler: Start task `([^`]+)`")
RE_END = re.compile(r"Scheduler: End task `([^`]+)`")
RE_WAIT = re.compile(r"Wait until (.+?) for task `([^`]+)`")

_ACTION_LOCK = threading.Lock()


# ---------------- config ----------------
def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return normalize_config(data)


def normalize_config(data):
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
    cfg["Enable"] = bool(cfg.get("Enable", True))
    if cfg.get("Mode") not in ("always_on", "always_off", "auto"):
        cfg["Mode"] = "auto"
    try:
        cfg["Multiplier"] = float(cfg.get("Multiplier", 555))
        if cfg["Multiplier"] <= 0:
            cfg["Multiplier"] = 555.0
    except (TypeError, ValueError):
        cfg["Multiplier"] = 555.0

    # New schema: {"Tasks": {"Main": true, ...}}.  Migrate the old
    # RestoreTasks/ApplyTasks schema when Tasks is absent.
    raw_tasks = data.get("Tasks") if isinstance(data, dict) else None
    tasks = {}
    if isinstance(raw_tasks, dict):
        for key, value in raw_tasks.items():
            if value:
                tasks[resolve_task(key)] = True
    else:
        for item in (data.get("ApplyTasks") or []) if isinstance(data, dict) else []:
            if isinstance(item, str):
                tasks[resolve_task(item)] = True
        # RestoreTasks entries are off by default and can be omitted.
    cfg["Tasks"] = tasks
    return cfg


def save_config(path, data):
    cfg = normalize_config(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return cfg


# ---------------- status ----------------
def port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def latest_log(log_dir):
    files = [
        f for f in glob.glob(os.path.join(log_dir, "*.txt"))
        if not os.path.basename(f).endswith("_gui.txt")
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def alas_state(log_dir, port):
    state = {"running": port_open(port), "task": None, "waiting": None, "log": None}
    path = latest_log(log_dir)
    if not path:
        return state
    state["log"] = os.path.basename(path)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return state
    lines = text.splitlines()
    seen_end = False
    for line in reversed(lines):
        m = RE_START.search(line)
        if m:
            if not seen_end:
                state["task"] = m.group(1)
            break
        if RE_END.search(line):
            seen_end = True
    for line in reversed(lines):
        m = RE_WAIT.search(line)
        if m:
            state["waiting"] = {"until": m.group(1), "task": m.group(2)}
            break
    return state


def memory_state(pid, multiplier):
    out = {"mode": "unknown", "value": None, "count": 0, "values": None, "cached": False}
    cache = alas_gg.load_json(alas_gg.CACHE_PATH, {})
    addrs = cache.get("addresses") or []
    if not addrs:
        return out
    out["cached"] = cache.get("pid") == pid
    if cache.get("pid") != pid:
        return out
    p = alas_gg.MemPatcher()
    try:
        bufs = p.read_many(pid, addrs, length=8, target_offset=0)
    except Exception:
        return out
    vals = []
    for a in addrs:
        b = bufs.get(a, b"")
        vals.append(struct.unpack("<d", b)[0] if len(b) == 8 else None)
    out["count"] = len(addrs)
    out["values"] = vals
    if all(v is not None and abs(v - multiplier) < 1e-9 for v in vals):
        out["mode"] = "applied"
        out["value"] = multiplier
    elif all(v is not None and abs(v - 1.0) < 1e-9 for v in vals):
        out["mode"] = "restored"
        out["value"] = 1.0
    elif any(v is not None for v in vals):
        out["mode"] = "mixed"
        out["value"] = vals[0]
    return out


def get_state(config_path, log_dir, alas_port):
    cfg = load_config(config_path)
    p = alas_gg.MemPatcher()
    pid = None
    try:
        pid = p.get_pid()
    except Exception:
        pass
    mem = memory_state(pid, float(cfg["Multiplier"])) if pid else {
        "mode": "unknown", "value": None, "count": 0, "values": None, "cached": False,
    }
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "config_path": config_path,
        "game": {"running": pid is not None, "pid": pid},
        "memory": mem,
        "alas": alas_state(log_dir, alas_port),
        "task_groups": TASK_GROUPS,
        "task_names": TASK_DISPLAY,
    }


# ---------------- actions ----------------
def do_action(action, config_path):
    cfg = load_config(config_path)
    value = float(cfg["Multiplier"])
    # Short-circuit when already in the requested state.
    try:
        probe = alas_gg.MemPatcher()
        pid = probe.get_pid()
        mem = memory_state(pid, value)
        if action == "apply" and mem["mode"] == "applied":
            return True, f"已经是修改状态（{value:g}）"
        if action == "restore" and mem["mode"] == "restored":
            return True, "已经是未修改状态（1.0）"
    except Exception:
        pass

    buf = io.StringIO()
    with _ACTION_LOCK:
        p = alas_gg.MemPatcher()
        try:
            if action == "apply":
                with redirect_stdout(buf):
                    code = alas_gg.cmd_apply(p, value, False)
            elif action == "restore":
                with redirect_stdout(buf):
                    code = alas_gg.cmd_restore(p, value, False)
            else:
                return False, f"unknown action: {action}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    text = buf.getvalue().strip()
    return code == 0, text or f"exit code {code}"


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    config_path = DEFAULT_CONFIG_PATH
    log_dir = DEFAULT_LOG_DIR
    alas_port = DEFAULT_ALAS_PORT

    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            index = os.path.join(PANEL_DIR, "index.html")
            try:
                with open(index, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._json(200, get_state(self.config_path, self.log_dir, self.alas_port))
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if path == "/api/config":
            self._json(200, load_config(self.config_path))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid json"})
            return

        if path == "/api/config":
            try:
                cfg = save_config(self.config_path, body)
                self._json(200, {"ok": True, "config": cfg})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/action":
            action = body.get("action")
            ok, message = do_action(action, self.config_path)
            self._json(200 if ok else 500, {"ok": ok, "message": message})
            return

        self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--alas-port", type=int, default=DEFAULT_ALAS_PORT)
    args = ap.parse_args()

    Handler.config_path = args.config
    Handler.log_dir = args.log_dir
    Handler.alas_port = args.alas_port
    load_task_names(args.config)

    if not os.path.exists(args.config):
        save_config(args.config, DEFAULT_CONFIG)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"GG panel: http://{args.host}:{args.port}/", flush=True)
    print(f"config:   {args.config}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
