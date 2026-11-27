#!/usr/bin/env python3
"""Stop or restore the configured ALAS/MuMu VM for one identified maintenance cycle.

ZCode invokes "auto". Its original scheduled timestamp identifies the event.
Other schedulers can pass --scheduled-at or invoke an explicit scheduled role.
Manual stop/start and status remain available.
"""
import argparse
import datetime as dt
import json
import os
import socket
import sqlite3
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

sys.path.insert(0, ALAS_ROOT)
from module.gg_modifier.runtime_lock import file_lock
from module.gg_modifier.maintenance.zcode_trigger import planned_time

DEFAULT_CONFIG = {
    "EmulatorVmIndex": 0,
    "MuMuManager": "D:/Game_D/MuMuPlayer/nx_main/MuMuManager.exe",
    "Adb": os.path.join(
        ALAS_ROOT, "toolkit", "Lib", "site-packages", "adbutils", "binaries", "adb.exe"
    ),
    "Serial": "127.0.0.1:16384",
    "ControlPort": 22334,
    "PanelPort": 22333,
    "WebUIPort": 22267,
    "WebUIExe": os.path.join(ALAS_ROOT, "Alas.exe"),
    "Instances": ["alas小开"],
    "Scheduler": "zcode",
    "BootTimeout": 180,
    "ShutdownTimeout": 90,
    "HideEmulator": True,
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
        if not isinstance(data, dict):
            raise ValueError("invalid maintenance configuration")
        cfg.update(data)
    except FileNotFoundError:
        pass
    if not os.path.exists(cfg.get("Adb", "")):
        fallback = "D:/Game_D/MuMuPlayer/nx_main/adb.exe"
        cfg["Adb"] = fallback if os.path.exists(fallback) else "adb"
    return cfg


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict) or not state:
        raise ValueError("invalid maintenance journal")
    if "cycle" not in state:
        # A legacy pending restore from before the upgrade still belongs to its original date.
        when = dt.datetime.strptime(state["stopped_at"], "%Y-%m-%d %H:%M:%S")
        role = state["role"]
        planned = when
        if role == "stop-night" and when.hour == 0:
            planned = when - dt.timedelta(days=1)
        state.update(cycle=cycle_key(role, planned), phase="stopped",
                     vm_requested=bool(state.get("emulator_was_running")))
    if state.get("phase") not in ("stopping", "stopped", "restoring", "complete"):
        raise ValueError("unknown maintenance journal phase")
    if not isinstance(state.get("instances"), list) or not isinstance(state.get("vm_requested"), bool):
        raise ValueError("invalid pending restore data")
    return state


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = STATE_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, STATE_PATH)


SCHEDULE = {"stop-night": (23, 50), "start-night": (0, 10),
            "stop-month-end": (5, 50), "start-month-end": (6, 10)}


def last_day(date):
    return (date + dt.timedelta(days=1)).day == 1


def scheduled_role(planned):
    for role, clock in SCHEDULE.items():
        if (planned.hour, planned.minute) != clock:
            continue
        if role.endswith("month-end"):
            return role if last_day(planned.date()) else None
        if role == "stop-night":
            return role if not last_day(planned.date()) else None
        return role if planned.day != 1 else None
    return None


def nominal_time(role, now):
    hour, minute = SCHEDULE[role]
    day = now.date()
    if role == "stop-night" and now.hour == 0:
        day -= dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(hour, minute))


def cycle_key(role, planned):
    if role in ("stop", "start"):
        return "manual/" + planned.isoformat()
    day = planned.date()
    if role == "start-night":
        day -= dt.timedelta(days=1)
    return role.split("-", 1)[1] + "/" + day.isoformat()


def due(role, planned, now):
    if scheduled_role(planned) != role or now < planned:
        return False
    if role == "stop-night":
        return now < planned + dt.timedelta(minutes=15)
    if role == "stop-month-end":
        return now < planned + dt.timedelta(minutes=10)
    # A late restore can finish the same cycle until the next maintenance period.
    # Do not take an earlier month's/night's pending state into a new cycle.
    cutoff = now.replace(hour=23, minute=45, second=0, microsecond=0)
    if role == "start-night" and last_day(planned.date()):
        cutoff = cutoff.replace(hour=5, minute=40)
    return now.date() == planned.date() and now < cutoff


def run(cmd, timeout=60):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, check=False)
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise RuntimeError("{} exited {}: {}".format(os.path.basename(cmd[0]), result.returncode, output[:350]))
    return output


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


def active_instances(cfg):
    status = api("/status", timeout=5, port=cfg["ControlPort"])
    if status.get("ok") is not True or not isinstance(status.get("instances"), dict):
        raise ValueError("invalid control API status")
    names = []
    for name, entry in status["instances"].items():
        if not isinstance(entry.get("alive"), bool):
            raise ValueError("unknown scheduler status for {}".format(name))
        if entry["alive"]:
            names.append(name)
    return names


def normalized_serial(serial):
    return str(serial or "").strip().lower().replace("localhost:", "127.0.0.1:")


def instance_serial(name):
    if not name or "/" in name or chr(92) in name or ":" in name or name in (".", ".."):
        raise ValueError("invalid instance name")
    path = os.path.join(ALAS_ROOT, "config", name + ".json")
    with open(path, encoding="utf-8-sig") as stream:
        data = json.load(stream)
    emu = data.get("Alas", {}).get("Emulator") or data.get("Emulator", {})
    serial = normalized_serial(emu.get("Serial"))
    if not serial or serial == "auto":
        raise ValueError("cannot confirm the VM for {}".format(name))
    return serial


def target_instances(cfg):
    serial = normalized_serial(cfg["Serial"])
    return [name for name in active_instances(cfg) if instance_serial(name) == serial]


def manager_result(cfg, arguments, timeout):
    raw = run([cfg["MuMuManager"]] + arguments, timeout=timeout)
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("errcode", data.get("error_code", 0)) != 0:
        raise RuntimeError("MuMuManager: {}".format(raw[:350]))
    return data


def emulator_info(cfg):
    index = str(cfg["EmulatorVmIndex"])
    info = manager_result(cfg, ["info", "-v", index], 20)
    if str(info.get("index")) != index:
        raise ValueError("MuMuManager did not identify VM {}".format(index))
    port = info.get("adb_port")
    if port:
        actual = "{}:{}".format(info.get("adb_host_ip") or "127.0.0.1", port)
        if normalized_serial(actual) != normalized_serial(cfg["Serial"]):
            raise ValueError("VM {} reports a different ADB serial: {}".format(index, actual))
    return info


def emulator_running(cfg):
    info = emulator_info(cfg)
    process = info.get("is_process_started")
    android = info.get("is_android_started")
    if process is True or android is True:
        return True
    if process is False and android in (None, False):
        return False
    raise ValueError("MuMuManager did not report the run state for the target VM")


def gpu_memory():
    try:
        output = run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                      "--format=csv,noheader,nounits"], timeout=15)
        return output.splitlines()[0] if output else "unknown"
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return "unknown ({})".format(error)


def shutdown_emulator(cfg):
    if not emulator_running(cfg):
        return True
    index = str(cfg["EmulatorVmIndex"])
    log("shutdown emulator vm={}".format(index))
    manager_result(cfg, ["control", "-v", index, "shutdown"], 60)
    deadline = time.monotonic() + int(cfg["ShutdownTimeout"])
    while time.monotonic() < deadline:
        if not emulator_running(cfg):
            return True
        time.sleep(2)
    raise RuntimeError("VM {} did not stop within {}s".format(index, cfg["ShutdownTimeout"]))


def launch_emulator(cfg):
    if not emulator_running(cfg):
        index = str(cfg["EmulatorVmIndex"])
        log("launch emulator vm={}".format(index))
        manager_result(cfg, ["control", "-v", index, "launch"], 90)


def hide_emulator_window(cfg):
    """Hide the player window like the MuMu boss key after a start."""
    if not cfg.get("HideEmulator", True):
        return True
    index = str(cfg["EmulatorVmIndex"])
    try:
        manager_result(cfg, ["control", "-v", index, "hide_window"], 20)
        log("hide emulator window vm={}".format(index))
        return True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        log("hide emulator window failed: {}: {}".format(type(error).__name__, error))
        return False


def adb_booted(cfg):
    adb, serial = cfg["Adb"], cfg["Serial"]
    run([adb, "connect", serial], timeout=15)
    return run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=15) == "1"


def wait_boot(cfg):
    deadline = time.monotonic() + int(cfg["BootTimeout"])
    reason = "boot flag not ready"
    while time.monotonic() < deadline:
        try:
            if adb_booted(cfg):
                log("emulator boot completed")
                return True
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            reason = str(error)
        time.sleep(2)
    raise RuntimeError("emulator boot timeout: {}".format(reason))


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


def set_instance(cfg, name, action):
    result = api("/" + action, params={"config": name}, method="POST", port=cfg["ControlPort"])
    if result.get("ok") is not True or result.get("config") != name:
        raise RuntimeError("control API {} {}: {}".format(action, name, result))
    changed = result.get("started" if action == "start" else "stopped")
    if not isinstance(changed, bool):
        raise ValueError("missing acknowledgement for {} {}".format(action, name))
    return changed


def wait_instance(cfg, name, alive):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (name in active_instances(cfg)) == alive:
            return
        time.sleep(1)
    raise RuntimeError("{} failed to reach alive={}".format(name, alive))


def check_owner(state, cfg):
    if (str(state.get("vm", cfg["EmulatorVmIndex"])) != str(cfg["EmulatorVmIndex"])
            or normalized_serial(state.get("serial", cfg["Serial"])) != normalized_serial(cfg["Serial"])):
        raise ValueError("maintenance target changed since stop; check the journal")


def resolve_stop_intent(cfg, state):
    name = state.get("stopping")
    if name:
        if name not in active_instances(cfg):
            raise RuntimeError("stop acknowledgement missing for {}; review journal before recovery".format(name))
        state.pop("stopping")
        save_state(state)


def do_stop(cfg, role, cycle, now, dry_run=False):
    state = load_state()
    if state and state["cycle"] == cycle and state["phase"] in ("stopped", "restoring", "complete"):
        log("cycle already {}: {}".format(state["phase"], cycle))
        return
    if state and state["phase"] != "complete" and state["cycle"] != cycle:
        raise ValueError("unresolved previous cycle: {}".format(state["cycle"]))
    if not state or state["cycle"] != cycle:
        state = dict(role=role, stopped_at=now.strftime("%Y-%m-%d %H:%M:%S"), cycle=cycle,
                     phase="stopping", vm=cfg["EmulatorVmIndex"], serial=cfg["Serial"],
                     targets=target_instances(cfg), instances=[], emulator_was_running=emulator_running(cfg),
                     vm_requested=False)
    check_owner(state, cfg)
    log("stop plan: cycle={} instances={} VM={}".format(cycle, state["targets"], state["vm"]))
    if dry_run:
        return
    save_state(state)
    resolve_stop_intent(cfg, state)
    for name in state["targets"]:
        if name in state["instances"] or name not in target_instances(cfg):
            continue
        state["stopping"] = name
        save_state(state)
        changed = set_instance(cfg, name, "stop")
        state.pop("stopping")
        if changed:
            state["instances"].append(name)
        save_state(state)
        wait_instance(cfg, name, False)
    if target_instances(cfg):
        raise RuntimeError("a scheduler still uses the target VM; shutdown cancelled")
    if state["emulator_was_running"]:
        state["vm_requested"] = True
        save_state(state)
        if not shutdown_emulator(cfg):
            raise RuntimeError("target VM shutdown failed")
    state["phase"] = "stopped"
    save_state(state)
    log("stop confirmed: instances={} VM={}".format(state["instances"], state["vm"]))
    if not dry_run:
        try:
            from module.gg_modifier.maintenance.update_check import check_updates
            check_updates(cfg, log)
        except Exception as error:
            log("update check failed: {}: {}".format(type(error).__name__, error))


def do_start(cfg, role, cycle, dry_run=False):
    state = load_state()
    manual = role == "start"
    if state and not manual and state["cycle"] != cycle:
        log("no restore for this cycle: {}".format(cycle))
        return
    if state and state["phase"] == "complete" and not manual:
        log("cycle already complete: {}".format(cycle))
        return
    if not state or state["phase"] == "complete":
        if not manual:
            log("no pending restore for {}".format(cycle))
            return
        state = dict(cycle=cycle, phase="restoring", vm=cfg["EmulatorVmIndex"],
                     serial=cfg["Serial"], instances=list(cfg.get("Instances") or []),
                     vm_requested=bool(cfg.get("Instances")))
    check_owner(state, cfg)
    names = list(state["instances"])
    log("restore plan: cycle={} instances={} VM requested={}".format(state["cycle"], names, state["vm_requested"]))
    if dry_run:
        return
    resolve_stop_intent(cfg, state)
    for name in names:
        if instance_serial(name) != normalized_serial(cfg["Serial"]):
            raise ValueError("instance {} changed device; restore paused".format(name))
    state["phase"] = "restoring"
    save_state(state)
    if state["vm_requested"]:
        launch_emulator(cfg)
    if names or state["vm_requested"]:
        if not wait_boot(cfg):
            raise RuntimeError("target VM is not booted")
        hide_emulator_window(cfg)
    if names and not ensure_webui(cfg):
        raise RuntimeError("scheduler control API unavailable")
    for name in names:
        set_instance(cfg, name, "start")
        wait_instance(cfg, name, True)
        state["instances"].remove(name)
        save_state(state)
    state["phase"] = "complete"
    save_state(state)
    log("restore confirmed: {}".format(names))


def request_auto_farm_check(cfg):
    """Ask the panel to run the scheduled auto-farm trigger check."""
    port = int(cfg.get("PanelPort", 22333))
    instances = cfg.get("Instances") or []
    instance = instances[0] if instances else "alas小开"
    data = json.dumps({"config": instance}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auto_farm/check",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    state = result.get("state") or {}
    if state.get("active"):
        log("auto farm check: active tasks={} stop_at={} oil={}".format(
            state.get("tasks"), state.get("stop_at"), state.get("last_oil")))
    else:
        log("auto farm check: idle armed={} oil={}".format(
            state.get("armed"), state.get("last_oil")))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=list(ROLE_TITLES))
    parser.add_argument("--force", action="store_true", help="override time gate for an explicit role")
    parser.add_argument("--dry-run", action="store_true", help="inspect without changing VM or state")
    parser.add_argument("--now", help="diagnostic local time YYYY-MM-DD HH:MM")
    parser.add_argument("--scheduled-at", help="original scheduled local time YYYY-MM-DD HH:MM")
    args = parser.parse_args(argv)
    cfg = None
    try:
        cfg = load_config()
        now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now()
        role = args.role
        if role == "status":
            log("emulator={} instances={} VRAM={} MiB".format(emulator_running(cfg), active_instances(cfg), gpu_memory()))
            log("journal: {}".format(load_state()))
            return 0
        planned = dt.datetime.fromisoformat(args.scheduled_at) if args.scheduled_at else None
        if role == "auto":
            if cfg.get("Scheduler") == "windows" and not args.dry_run:
                log("Windows owns scheduled actions; journal={}".format(load_state()))
                return 0
            if planned is None:
                planned = now if args.dry_run else planned_time(cfg, now)
            role = scheduled_role(planned)
            if role is None:
                retry_role = {(0, 50): "start-night", (6, 50): "start-month-end"}.get(
                    (planned.hour, planned.minute))
                if retry_role and now >= planned:
                    recovery = nominal_time(retry_role, planned)
                    if due(retry_role, recovery, now):
                        pending = load_state()
                        if (pending.get("cycle") == cycle_key(retry_role, recovery)
                                and pending.get("phase") != "complete"):
                            log("recovery checkpoint scheduled={}".format(planned))
                            role, planned = retry_role, recovery
                if role is None:
                    log("check only: scheduled={} now={}".format(planned, now))
                    return 0
        if role in SCHEDULE:
            planned = planned or nominal_time(role, now)
            if not due(role, planned, now) and not args.force:
                # A delayed stop trigger may arrive at the paired start time.
                # Continue the stopped cycle instead of skipping both events.
                pair = {"stop-night": "start-night",
                        "stop-month-end": "start-month-end"}.get(role)
                if pair:
                    recovery = nominal_time(pair, now)
                    pending = load_state()
                    if (due(pair, recovery, now)
                            and pending.get("cycle") == cycle_key(pair, recovery)
                            and pending.get("phase") == "stopped"):
                        log("late {} trigger; continuing as {}".format(role, pair))
                        role, planned = pair, recovery
                    else:
                        log("skip {} scheduled={} now={}".format(role, planned, now))
                        return 0
                else:
                    log("skip {} scheduled={} now={}".format(role, planned, now))
                    return 0
        else:
            planned = now
        cycle = cycle_key(role, planned)
        log("--- {} | planned={} now={} cycle={} dry_run={} ---".format(role, planned, now, cycle, args.dry_run))
        os.makedirs(DATA_DIR, exist_ok=True)
        with file_lock(LOCK_PATH, timeout=5):
            if role == "stop":
                previous = load_state()
                if previous.get("role") == "stop" and previous.get("phase") != "complete":
                    cycle = previous["cycle"]
            log("VRAM before {}: {} MiB".format(role, gpu_memory()))
            if role.startswith("stop"):
                do_stop(cfg, role, cycle, now, args.dry_run)
            else:
                do_start(cfg, role, cycle, args.dry_run)
            if not args.dry_run:
                log("VRAM after {}: {} MiB".format(role, gpu_memory()))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        log("maintenance failed: {}: {}".format(type(error).__name__, error))
        return 1
    finally:
        if cfg is not None and not args.dry_run and args.role != "status":
            try:
                request_auto_farm_check(cfg)
            except Exception as error:
                log("auto farm check failed: {}: {}".format(type(error).__name__, error))


if __name__ == "__main__":
    sys.exit(main())
