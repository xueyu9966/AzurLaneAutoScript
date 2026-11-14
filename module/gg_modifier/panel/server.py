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
import urllib.parse
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
    "version": 4,
    "Enable": True,
    "Mode": "auto",  # always_on | always_off | auto（按任务开关）
    "Multiplier": 555,
    "Tasks": {name: True for name in DEFAULT_TASKS_ON},
    "Instance": "",
    "Serial": "",
    # Audit only: log decisions, never write memory.
    "DryRun": False,
    # Always restore sensitive tasks even if they are enabled in Tasks.
    "ProtectSensitive": True,
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


def _save_state_file(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _plog(message):
    """Small panel-side log (stdout is discarded when started by ALAS)."""
    try:
        path = os.path.join(alas_gg.DATA_DIR, "panel.log")
        if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


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
    cfg["DryRun"] = bool(cfg.get("DryRun", False))
    cfg["ProtectSensitive"] = bool(cfg.get("ProtectSensitive", True))
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
    cfg["Instance"] = str(cfg.get("Instance") or "").strip()
    cfg["Serial"] = str(cfg.get("Serial") or "").strip()
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


def list_instances(config_path):
    """ALAS config names (files under ./config, excluding templates/mods)."""
    config_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    out = []
    try:
        for f in os.listdir(config_dir):
            name, ext = os.path.splitext(f)
            if ext != ".json" or "." in name or name.startswith("template"):
                continue
            out.append(name)
    except OSError:
        pass
    return sorted(out)


_SERIAL_CACHE = {}


def instance_device(config_path, config_name):
    """Return (serial or None, package) from the ALAS instance config."""
    config_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    serial = None
    package = "com.bilibili.azurlane"
    try:
        with open(os.path.join(config_dir, f"{config_name}.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        emu = data.get("Alas", {}).get("Emulator", {}) if isinstance(data.get("Alas"), dict) else {}
        if not emu:
            emu = data.get("Emulator", {}) if isinstance(data.get("Emulator"), dict) else {}
        s = emu.get("Serial")
        p = emu.get("PackageName")
        if s and s != "auto":
            serial = s
        if p and p != "auto":
            package = p
    except (OSError, ValueError):
        pass
    if not serial:
        # Fall back to the serial ALAS actually used, parsed from its log.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(config_path))))
        log_dir = os.path.join(root, "log")
        try:
            files = [
                f for f in glob.glob(os.path.join(log_dir, f"*_{config_name}.txt"))
                if not os.path.basename(f).endswith("_gui.txt")
            ]
            if files:
                path = max(files, key=os.path.getmtime)
                key = (path, os.path.getmtime(path))
                if key in _SERIAL_CACHE:
                    serial = _SERIAL_CACHE[key]
                else:
                    size = os.path.getsize(path)
                    with open(path, "rb") as f:
                        head = f.read(256 * 1024)
                        tail = b""
                        if size > 256 * 1024:
                            f.seek(max(0, size - 2 * 1024 * 1024))
                            tail = f.read()
                    text = (head + b"\n" + tail).decode("utf-8", errors="replace")
                    matches = re.findall(r"\[AdbDevice\]\s*AdbDevice\(serial=([^)\s]+)\)", text)
                    serial = matches[-1] if matches else None
                    _SERIAL_CACHE[key] = serial
                    if len(_SERIAL_CACHE) > 20:
                        _SERIAL_CACHE.clear()
                        _SERIAL_CACHE[key] = serial
        except OSError:
            pass
    return serial, package


def latest_log(log_dir, config_name=None):
    pattern = f"*_{config_name}.txt" if config_name else "*.txt"
    files = [
        f for f in glob.glob(os.path.join(log_dir, pattern))
        if not os.path.basename(f).endswith("_gui.txt")
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


_SCHED_CACHE = {"path": None, "time": 0.0, "value": None}


def scheduler_running(log_dir, config_name=None):
    """True if some process holds that instance's scheduler log file open."""
    try:
        import psutil
    except Exception:
        return None
    path = latest_log(log_dir, config_name)
    if not path:
        return None
    target = os.path.abspath(path)
    now = time.time()
    if _SCHED_CACHE["path"] == target and now - _SCHED_CACHE["time"] < 20:
        return _SCHED_CACHE["value"]
    found = False
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info.get("name") or "").lower() not in ("python.exe", "python", "python3"):
            continue
        try:
            for f in proc.open_files():
                if os.path.abspath(f.path) == target:
                    found = True
                    break
        except Exception:
            continue
        if found:
            break
    _SCHED_CACHE.update(path=target, time=now, value=found)
    return found


def running_instances(log_dir, config_names):
    """One psutil pass: which instances' logs are held open."""
    try:
        import psutil
    except Exception:
        return None
    held = set()
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info.get("name") or "").lower() not in ("python.exe", "python", "python3"):
            continue
        try:
            for f in proc.open_files():
                held.add(os.path.abspath(f.path))
        except Exception:
            continue
    out = []
    for name in config_names:
        path = latest_log(log_dir, name)
        if path and os.path.abspath(path) in held:
            out.append(name)
    return out


def latest_instance(log_dir, config_names):
    best = None
    for name in config_names:
        path = latest_log(log_dir, name)
        if not path:
            continue
        mtime = os.path.getmtime(path)
        if best is None or mtime > best[1]:
            best = (name, mtime)
    return best[0] if best else (config_names[0] if config_names else None)


def alas_state(log_dir, port, config_name=None):
    webui = port_open(port)
    sched = scheduler_running(log_dir, config_name)
    state = {
        "running": webui if sched is None else sched,
        "webui": webui,
        "scheduler_known": sched is not None,
        "task": None,
        "waiting": None,
        "last_task": None,
        "log": None,
    }
    path = latest_log(log_dir, config_name)
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
    if sched is False:
        # Scheduler stopped: do not present the stale task as currently running.
        state["last_task"] = state["task"] or (
            state["waiting"]["task"] if state["waiting"] else None
        )
        state["task"] = None
        state["waiting"] = None
    return state


def memory_state(pid, multiplier, config_name=None):
    out = {
        "mode": "unknown", "value": None, "count": 0, "values": None,
        "cached": False, "task": None, "error": None,
    }
    cache = alas_gg.load_json(alas_gg.CACHE_PATH, {})
    addrs = cache.get("addresses") or []
    out["count"] = len(addrs)

    # Prefer the state file written by the task hook: no adb reads while polling.
    state = alas_gg.load_json(os.path.join(alas_gg.DATA_DIR, "state.json"), {})
    entry = None
    if config_name and isinstance(state, dict) and isinstance(state.get(config_name), dict):
        cand = state[config_name]
        if cand.get("pid") != pid:
            # Selected instance has a stale entry (game restarted): unknown.
            return out
        entry = cand
    else:
        best = None
        for value in (state.values() if isinstance(state, dict) else []):
            if not isinstance(value, dict) or value.get("pid") != pid:
                continue
            if best is None or str(value.get("time", "")) > str(best.get("time", "")):
                best = value
        entry = best
    if entry:
        out["cached"] = True
        out["task"] = entry.get("task")
        out["error"] = entry.get("error") or entry.get("guard_error")
        state_mode = entry.get("mode")
        if state_mode == "pending":
            out["mode"] = "pending"
            out["value"] = entry.get("value", multiplier)
            return out
        if state_mode == "error":
            out["mode"] = "error"
            out["value"] = entry.get("value", multiplier)
            return out
        cache_value = None
        if cache.get("pid") == pid:
            try:
                cache_value = float(cache.get("value"))
            except (TypeError, ValueError):
                cache_value = None
        if cache_value is not None:
            cache_mode = (
                "applied" if abs(cache_value - float(multiplier)) < 1e-9
                else "restored" if abs(cache_value - 1.0) < 1e-9
                else None
            )
            if cache_mode and state_mode in ("applied", "restored") and cache_mode != state_mode:
                # State file and address cache disagree; do not claim a clean state.
                out["mode"] = "mixed"
                out["value"] = cache_value
                return out
        if state_mode == "applied":
            out["mode"] = "applied"
            out["value"] = entry.get("value", multiplier)
        elif state_mode == "restored":
            out["mode"] = "restored"
            out["value"] = 1.0
        return out

    if cache.get("pid") == pid and addrs:
        out["cached"] = True
        try:
            value = float(cache.get("value"))
        except (TypeError, ValueError):
            value = None
        if value is not None and abs(value - float(multiplier)) < 1e-9:
            out["mode"] = "applied"
            out["value"] = float(multiplier)
        elif value is not None and abs(value - 1.0) < 1e-9:
            out["mode"] = "restored"
            out["value"] = 1.0
    return out


def get_state(config_path, log_dir, alas_port, config_name=None):
    cfg = load_config(config_path)
    instances = list_instances(config_path)

    if not config_name:
        bound = cfg.get("Instance") or ""
        if bound and bound in instances:
            config_name = bound
        else:
            config_name = latest_instance(log_dir, instances)

    serial, package = (
        instance_device(config_path, config_name)
        if config_name
        else (None, "com.bilibili.azurlane")
    )
    p = alas_gg.MemPatcher(serial=serial or alas_gg.DEFAULT_SERIAL, package=package)
    pid = None
    try:
        pid = p.get_pid()
    except Exception:
        pass
    mem = memory_state(pid, float(cfg["Multiplier"]), config_name) if pid else {
        "mode": "unknown", "value": None, "count": 0, "values": None, "cached": False,
    }
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "config_path": config_path,
        "instances": instances,
        "selected": config_name,
        "instance_serial": serial,
        "game": {
            "running": pid is not None,
            "pid": pid,
            "serial": serial or alas_gg.DEFAULT_SERIAL,
        },
        "memory": mem,
        "alas": alas_state(log_dir, alas_port, config_name),
        "task_groups": TASK_GROUPS,
        "task_names": TASK_DISPLAY,
    }


# ---------------- actions ----------------
def do_action(action, config_path, config_name=None):
    cfg = load_config(config_path)
    value = float(cfg["Multiplier"])
    serial, package = (
        instance_device(config_path, config_name)
        if config_name
        else (None, "com.bilibili.azurlane")
    )
    serial = serial or alas_gg.DEFAULT_SERIAL

    # Short-circuit when already in the requested state.
    try:
        probe = alas_gg.MemPatcher(serial=serial, package=package)
        pid = probe.get_pid()
        mem = memory_state(pid, value, config_name)
        if action == "apply" and mem["mode"] == "applied":
            return True, f"已经是修改状态（{value:g}）"
        if action == "restore" and mem["mode"] == "restored":
            return True, "已经是未修改状态（1.0）"
    except Exception:
        pass

    buf = io.StringIO()
    with _ACTION_LOCK:
        p = alas_gg.MemPatcher(serial=serial, package=package)
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
    if code == 0:
        try:
            pid = p.get_pid()
            state_path = os.path.join(alas_gg.DATA_DIR, "state.json")
            data = alas_gg.load_json(state_path, {})
            data[config_name or "panel"] = {
                "pid": pid,
                "serial": serial,
                "mode": "applied" if action == "apply" else "restored",
                "value": value,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "task": "manual",
                "desired": action,
                "manual": True,
            }
            _save_state_file(state_path, data)
        except Exception:
            pass
    return code == 0, text or f"exit code {code}"


# ---------------- orphan cleanup ----------------
def _process_alive(pid, create_time=None):
    """True/False, or None when psutil is unavailable."""
    try:
        import psutil
    except Exception:
        return None
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running():
            return False
        if create_time:
            return abs(proc.create_time() - float(create_time)) < 2.0
        return True
    except Exception:
        return False


def orphan_guard_once(config_path, log_dir):
    """Restore values left behind by a scheduler that was hard-killed.

    State entries written by the task hook carry task_pid/task_ctime.  A manual
    apply from the panel has no task_pid and is intentionally left alone.
    """
    try:
        import psutil  # noqa: F401
    except Exception:
        return
    state_path = os.path.join(alas_gg.DATA_DIR, "state.json")
    state = alas_gg.load_json(state_path, {})
    if not isinstance(state, dict):
        return
    cfg = load_config(config_path)
    for name, entry in list(state.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("manual"):
            continue
        if entry.get("mode") not in ("applied", "pending", "error"):
            continue
        task_pid = entry.get("task_pid")
        if not task_pid:
            continue
        alive = _process_alive(task_pid, entry.get("task_ctime"))
        if alive is None or alive:
            continue
        # A new scheduler for the same instance may already own the state.
        if scheduler_running(log_dir, name) is not False:
            continue

        serial, package = instance_device(config_path, name)
        serial = serial or alas_gg.DEFAULT_SERIAL
        try:
            value = float(entry.get("value") or cfg.get("Multiplier") or 555)
        except (TypeError, ValueError):
            value = float(cfg.get("Multiplier") or 555)

        with _ACTION_LOCK:
            p = alas_gg.MemPatcher(serial=serial, package=package)
            try:
                pid = p.get_pid()
            except Exception:
                entry.update({
                    "mode": "restored", "value": value, "pid": 0,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "guard": "game_not_running",
                })
                entry.pop("guard_error", None)
                _save_state_file(state_path, state)
                _plog(f"orphan cleanup {name}: game not running")
                continue

            if entry.get("pid") in (None, 0) or entry.get("pid") != pid:
                # A new game process starts with 1.0; the old modification is gone.
                entry.update({
                    "mode": "restored", "value": value, "pid": pid,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "guard": "game_restarted",
                })
                entry.pop("guard_error", None)
                _save_state_file(state_path, state)
                _plog(f"orphan cleanup {name}: game restarted")
                continue

            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = alas_gg.cmd_restore(p, value, False)
                ok = code == 0
                if not ok and code == 1:
                    # cmd_restore found no modified values.  Confirm that the
                    # targets are actually at 1.0 before claiming success.
                    addrs = alas_gg.cache_valid(p, pid, 1.0)
                    if not addrs:
                        addrs = p.scan(pid, anchor=0.0001, target=1.0)
                    if addrs:
                        alas_gg.save_json(
                            alas_gg.CACHE_PATH,
                            {
                                "pid": pid, "value": 1.0,
                                "addresses": addrs, "time": time.time(),
                            },
                        )
                        ok = True
                if ok:
                    entry.update({
                        "mode": "restored", "value": value, "pid": pid,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "guard": "orphan_cleanup",
                    })
                    entry.pop("guard_error", None)
                    _save_state_file(state_path, state)
                    last = (buf.getvalue().strip().splitlines() or ["ok"])[-1]
                    _plog(f"orphan cleanup {name}: {last}")
                else:
                    entry["guard_error"] = (
                        buf.getvalue().strip() or f"restore exit code {code}"
                    )
                    _save_state_file(state_path, state)
                    _plog(f"orphan cleanup {name} failed: {entry['guard_error']}")
            except Exception as e:
                entry["guard_error"] = f"{type(e).__name__}: {e}"
                _save_state_file(state_path, state)
                _plog(f"orphan cleanup {name} error: {entry['guard_error']}")


def _guard_loop(config_path, log_dir):
    while True:
        try:
            orphan_guard_once(config_path, log_dir)
        except Exception as e:
            _plog(f"orphan guard error: {type(e).__name__}: {e}")
        time.sleep(20)


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
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            config_name = (qs.get("config") or [None])[0]
            try:
                self._json(200, get_state(self.config_path, self.log_dir, self.alas_port, config_name))
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
            ok, message = do_action(action, self.config_path, body.get("config"))
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

    # Warm the (slow) scheduler check in the background so the first page load
    # does not wait for psutil.
    def _warm():
        try:
            names = list_instances(args.config)
            name = latest_instance(args.log_dir, names)
            if name:
                scheduler_running(args.log_dir, name)
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()

    if not os.path.exists(args.config):
        save_config(args.config, DEFAULT_CONFIG)

    threading.Thread(
        target=_guard_loop, args=(args.config, args.log_dir), daemon=True
    ).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"GG panel: http://{args.host}:{args.port}/", flush=True)
    print(f"config:   {args.config}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
