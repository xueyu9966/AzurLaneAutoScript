#!/usr/bin/env python3
"""Scheduled maintenance for ALAS + MuMu.

Roles:
    stop-night        23:50 daily, except the last day of the month:
                      stop all running ALAS schedulers, close the emulator
    start-night       00:10 daily, except the first day of the month:
                      open the emulator, wait for boot, start ALAS schedulers
    stop-month-end    05:50 on the last day of the month: same stop
    start-month-end   06:10 on the last day of the month: same start
    status            print current state (no changes)

The monthly exception keeps ALAS online through the month-end reset at 00:00;
maintenance is moved to 05:50/06:10 on the last day instead.

Run normally from Windows Task Scheduler (see register_tasks.ps1):

    pythonw.exe maintenance.py stop-night

Manual testing:
    python maintenance.py stop --force
    python maintenance.py start --force
    python maintenance.py stop-night --now "2026-11-30 23:50" --dry-run
    python maintenance.py status
"""
import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ALAS_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DATA_DIR = os.path.join(ALAS_ROOT, "config", "gg_modifier")
CONFIG_PATH = os.path.join(DATA_DIR, "maintenance.json")
STATE_PATH = os.path.join(DATA_DIR, "maintenance_state.json")
LOG_PATH = os.path.join(DATA_DIR, "maintenance.log")
LOCK_PATH = os.path.join(DATA_DIR, "maintenance.lock")

DEFAULT_CONFIG = {
    "EmulatorVmIndex": 0,
    "MuMuManager": "D:/Game_D/MuMuPlayer/nx_main/MuMuManager.exe",
    "Adb": os.path.join(
        ALAS_ROOT, "toolkit", "Lib", "site-packages", "adbutils", "binaries", "adb.exe"
    ),
    "Serial": "127.0.0.1:16384",
    "ControlPort": 22334,
    "WebUIPort": 22267,
    "WebUIExe": os.path.join(ALAS_ROOT, "Alas.exe"),
    "Instances": ["alas小开"],
    "BootTimeout": 180,
    "ShutdownTimeout": 90,
}

ROLE_TITLES = {
    "auto": "自动判断",
    "stop-night": "夜间维护 - 停止",
    "start-night": "夜间维护 - 启动",
    "stop-month-end": "月底维护 - 停止",
    "start-month-end": "月底维护 - 启动",
    "stop": "手动停止",
    "start": "手动启动",
    "status": "状态",
}


def log(message):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(message)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    if not os.path.exists(CONFIG_PATH):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    if not os.path.exists(cfg.get("Adb", "")):
        fallback = "D:/Game_D/MuMuPlayer/nx_main/adb.exe"
        cfg["Adb"] = fallback if os.path.exists(fallback) else "adb"
    return cfg


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"cannot write state: {e}")


def clear_state():
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


def last_day(date):
    return (date + dt.timedelta(days=1)).day == 1


def role_allowed(role, now):
    """Date/time gate for the four scheduled roles."""
    if role == "stop-night":
        if now.hour == 23 and now.minute >= 45:
            return not last_day(now.date())
        if now.hour == 0 and now.minute <= 5:
            return not last_day(now.date() - dt.timedelta(days=1))
        return False
    if role == "start-night":
        if now.hour == 0 and now.minute <= 40:
            return now.date().day != 1
        return False
    if role == "stop-month-end":
        if now.hour == 5 and 40 <= now.minute <= 59:
            return last_day(now.date())
        return False
    if role == "start-month-end":
        if now.hour == 6 and now.minute <= 40:
            return last_day(now.date())
        return False
    if role in ("stop", "start"):
        return True
    return False


def detect_role(now):
    """Pick the maintenance role for the current time, or None."""
    if now.hour == 23 and now.minute >= 45:
        return "stop-night"
    if now.hour == 0 and now.minute <= 5:
        return "stop-night"
    if now.hour == 0 and now.minute <= 45:
        return "start-night"
    if now.hour == 5 and 40 <= now.minute <= 59:
        return "stop-month-end"
    if now.hour == 6 and now.minute <= 45:
        return "start-month-end"
    return None


def lock():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
        except OSError:
            age = 0
        if age > 30 * 60:
            log("removing stale maintenance lock")
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
            return lock()
        log("another maintenance run is in progress, exit")
        return None
    except OSError as e:
        log(f"cannot create lock: {e}")
        return None


def unlock(fd):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def run(cmd, timeout=60, check=False):
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=check,
        ).stdout.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s: {cmd}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def api(path, params=None, method="GET", timeout=30, port=None):
    port = port or load_config()["ControlPort"]
    url = f"http://127.0.0.1:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_ready(port=None):
    try:
        return api("/status", timeout=2, port=port).get("ok") is True
    except Exception:
        return False


def api_instances(port=None):
    try:
        data = api("/status", timeout=5, port=port)
    except Exception:
        return None
    instances = data.get("instances") or {}
    return [name for name, item in instances.items() if item.get("alive")]


def fallback_schedulers():
    """Find scheduler processes by the log file they hold open."""
    try:
        import psutil
    except Exception:
        return {}
    log_dir = os.path.abspath(os.path.join(ALAS_ROOT, "log"))
    found = {}
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name not in ("python.exe", "python", "python3"):
            continue
        try:
            for open_file in proc.open_files():
                path = os.path.abspath(open_file.path)
                if os.path.dirname(path) != log_dir:
                    continue
                basename = os.path.basename(path)
                # The GUI itself also holds log/YYYY-MM-DD_gui.txt.
                if basename.endswith("_gui.txt"):
                    continue
                match = re.match(r"^\d{4}-\d{2}-\d{2}_(.+)\.txt$", basename)
                if match and match.group(1) not in ("gui",):
                    found[match.group(1)] = proc.info["pid"]
        except Exception:
            continue
    return found


def running_instances():
    names = api_instances()
    if names is not None:
        return names
    return sorted(fallback_schedulers())


def emulator_info(cfg):
    out = run([cfg["MuMuManager"], "info", "-v", str(cfg["EmulatorVmIndex"])], timeout=20)
    try:
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {"raw": out}


def emulator_running(cfg):
    info = emulator_info(cfg)
    if info.get("is_process_started") or info.get("is_android_started"):
        return True
    if info.get("player_state") in ("starting", "start_finished", "running"):
        return True
    # Fallback: the VM process exists.
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "mumuvmmheadless.exe":
                return True
    except Exception:
        pass
    return False


def gpu_memory():
    out = run([
        "nvidia-smi", "--query-gpu=memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ], timeout=15)
    return out.splitlines()[0] if out else "unknown"


def stop_instances(cfg, dry_run=False, names=None):
    if names is None:
        names = running_instances()
    log(f"running ALAS instances: {names or 'none'}")
    if dry_run:
        return names
    if api_ready(cfg["ControlPort"]):
        try:
            result = api("/stop_all", method="POST", port=cfg["ControlPort"])
            log(f"control API stop_all: {result}")
        except Exception as e:
            log(f"control API stop_all failed: {e}")
    else:
        log("control API unavailable, using process fallback")
    # Kill leftovers (API unavailable, or an instance not tracked by the UI).
    leftovers = fallback_schedulers()
    if leftovers:
        try:
            import psutil
        except Exception:
            psutil = None
        for name, pid in leftovers.items():
            if psutil is None:
                continue
            try:
                psutil.Process(pid).kill()
                log(f"killed leftover scheduler {name} (pid {pid})")
            except Exception as e:
                log(f"cannot kill scheduler {name} (pid {pid}): {e}")
    return names


def shutdown_emulator(cfg, dry_run=False):
    if not emulator_running(cfg):
        log("emulator is not running, no shutdown needed")
        return True
    if dry_run:
        return True
    log(f"shutdown emulator vm={cfg['EmulatorVmIndex']}")
    out = run(
        [cfg["MuMuManager"], "control", "-v", str(cfg["EmulatorVmIndex"]), "shutdown"],
        timeout=60,
    )
    log(f"MuMuManager shutdown: {out}")
    deadline = time.time() + int(cfg["ShutdownTimeout"])
    while time.time() < deadline:
        if not emulator_running(cfg):
            log("emulator stopped")
            return True
        time.sleep(3)
    log("emulator did not stop in time, trying taskkill")
    run(["taskkill", "/F", "/IM", "MuMuVMMHeadless.exe"], timeout=30)
    time.sleep(3)
    stopped = not emulator_running(cfg)
    log(f"emulator stopped after taskkill: {stopped}")
    return stopped


def launch_emulator(cfg, dry_run=False):
    if emulator_running(cfg):
        log("emulator already running")
        return True
    if dry_run:
        return True
    log(f"launch emulator vm={cfg['EmulatorVmIndex']}")
    out = run(
        [cfg["MuMuManager"], "control", "-v", str(cfg["EmulatorVmIndex"]), "launch"],
        timeout=90,
    )
    log(f"MuMuManager launch: {out}")
    return True


def adb_booted(cfg):
    adb = cfg["Adb"]
    serial = cfg["Serial"]
    run([adb, "connect", serial], timeout=15)
    out = run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=15)
    return "1" in out


def wait_boot(cfg, dry_run=False):
    if dry_run:
        return True
    deadline = time.time() + int(cfg["BootTimeout"])
    last = ""
    while time.time() < deadline:
        if adb_booted(cfg):
            log("emulator boot completed")
            return True
        if last != "waiting":
            log(f"waiting for emulator boot ({cfg['Serial']})")
            last = "waiting"
        time.sleep(3)
    log(f"emulator boot timeout after {cfg['BootTimeout']}s")
    return False


def ensure_webui(cfg, dry_run=False):
    port = int(cfg["WebUIPort"])
    if port_open(port):
        deadline = time.time() + 45
        while time.time() < deadline:
            if api_ready(cfg["ControlPort"]):
                return True
            time.sleep(2)
        log(
            f"ALAS WebUI is running on {port}, but control API "
            f"{cfg['ControlPort']} is unavailable; restart ALAS once"
        )
        return False
    if dry_run:
        return True
    log("ALAS WebUI is not running, launching it")
    exe = cfg.get("WebUIExe") or ""
    if exe and os.path.exists(exe):
        try:
            subprocess.Popen(
                [exe], cwd=ALAS_ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log(f"cannot launch WebUI exe: {e}")
    else:
        python = os.path.join(ALAS_ROOT, "toolkit", "python.exe")
        try:
            subprocess.Popen(
                [python, os.path.join(ALAS_ROOT, "gui.py"), "--port", str(port)],
                cwd=ALAS_ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log(f"cannot launch gui.py: {e}")
    deadline = time.time() + 120
    while time.time() < deadline:
        if port_open(port) and api_ready(cfg["ControlPort"]):
            log("ALAS WebUI / control API ready")
            return True
        time.sleep(3)
    log("ALAS WebUI did not become ready in time")
    return False


def start_instances(cfg, names, dry_run=False):
    log(f"instances to start: {names or 'none'}")
    if dry_run:
        return True
    if not names:
        return True
    if not ensure_webui(cfg, dry_run):
        return False
    ok = True
    for name in names:
        try:
            result = api(
                "/start", params={"config": name},
                method="POST", port=cfg["ControlPort"],
            )
            log(f"control API start {name}: {result}")
        except Exception as e:
            log(f"cannot start {name}: {e}")
            ok = False
    deadline = time.time() + 60
    while time.time() < deadline:
        alive = api_instances(cfg["ControlPort"]) or []
        missing = [name for name in names if name not in alive]
        if not missing:
            log(f"all instances alive: {names}")
            return ok
        time.sleep(3)
    log(f"instances not alive after start: {names}")
    return False


def do_stop(cfg, role, dry_run=False):
    names = running_instances()
    for name in sorted(fallback_schedulers()):
        if name not in names:
            names.append(name)
    state = {
        "role": role,
        "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instances": names,
        "emulator_was_running": emulator_running(cfg),
    }
    log(f"stop state: {json.dumps(state, ensure_ascii=False)}")
    log(f"VRAM before stop: {gpu_memory()} MiB")
    if not dry_run:
        save_state(state)
        stop_instances(cfg, dry_run=False, names=names)
        time.sleep(2)
        shutdown_emulator(cfg, dry_run=False)
        log(f"VRAM after stop: {gpu_memory()} MiB")
    return True


def do_start(cfg, role, dry_run=False):
    state = load_state()
    if state:
        names = list(state.get("instances") or [])
        want_emulator = bool(names) or bool(state.get("emulator_was_running"))
    else:
        names = list(cfg.get("Instances") or [])
        want_emulator = bool(names)
        log("no maintenance state found, using configured Instances")
    log(f"start state: instances={names or 'none'}, emulator={want_emulator}")
    log(f"VRAM before start: {gpu_memory()} MiB")
    if not want_emulator and not names:
        log("nothing to restore")
        return True
    if dry_run:
        return True
    launch_emulator(cfg, dry_run=False)
    if not wait_boot(cfg, dry_run=False):
        return False
    if not start_instances(cfg, names, dry_run=False):
        return False
    clear_state()
    log(f"VRAM after start: {gpu_memory()} MiB")
    return True


def do_status(cfg):
    log(f"control API ready: {api_ready(cfg['ControlPort'])}")
    log(f"instances: {running_instances()}")
    info = emulator_info(cfg)
    log(f"emulator: running={emulator_running(cfg)}, "
        f"state={info.get('player_state')}, pid={info.get('pid')}")
    log(f"VRAM: {gpu_memory()} MiB")
    log(f"state file: {json.dumps(load_state(), ensure_ascii=False)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=[
            "auto",
            "stop-night", "start-night", "stop-month-end", "start-month-end",
            "stop", "start", "status",
        ],
    )
    parser.add_argument("--force", action="store_true", help="ignore date/time gate")
    parser.add_argument("--dry-run", action="store_true", help="print actions only")
    parser.add_argument("--now", help='override time, e.g. "2026-11-30 23:50"')
    args = parser.parse_args()

    cfg = load_config()
    if args.now:
        now = dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M")
    else:
        now = dt.datetime.now()
    role = args.role
    if role == "auto":
        detected = detect_role(now)
        if detected is None:
            log(
                f"--- 自动判断 | now={now:%Y-%m-%d %H:%M} | "
                "no maintenance window, exit ---"
            )
            return 0
        log(f"auto -> {detected}")
        role = detected
    log(f"--- {ROLE_TITLES.get(role, role)} | now={now:%Y-%m-%d %H:%M} ---")

    if role == "status":
        do_status(cfg)
        return 0

    if not args.force and not role_allowed(role, now):
        log(f"role {role} is not due at {now:%Y-%m-%d %H:%M} (last_day={last_day(now.date())}), exit")
        return 0

    fd = lock()
    if fd is None:
        return 0
    try:
        if role in ("stop-night", "stop-month-end", "stop"):
            return 0 if do_stop(cfg, role, args.dry_run) else 1
        if role in ("start-night", "start-month-end", "start"):
            return 0 if do_start(cfg, role, args.dry_run) else 1
        return 0
    finally:
        unlock(fd)


if __name__ == "__main__":
    sys.exit(main())
