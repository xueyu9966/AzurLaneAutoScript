"""GG modifier task hook.

Reads ./config/gg_modifier.json (shared with the local control panel).

Modes:
    always_on   - ensure applied before every task
    always_off  - ensure restored before every task
    auto        - per-task switches; Tasks[name] == true -> apply,
                  everything else -> restore (default off)

Mid-task: when the dock fills up, ALAS enters retirement / enhancement.
GG-modified stats break enhancement, so those two handlers are wrapped:
restore right before them, re-apply right after they finish.
"""
import glob
import json
import os
import re
import struct
import subprocess
import time

from module.logger import logger
from . import alas_gg

HERE = os.path.dirname(os.path.abspath(__file__))
ALAS_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CONFIG_DIR = os.environ.get(
    "GG_MODIFIER_DATA_DIR", os.path.join(ALAS_ROOT, "config", "gg_modifier")
)
ALAS_CONFIG_DIR = os.path.join(ALAS_ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

DEFAULT_TASKS_ON = [
    "Main", "Main2", "Main3",
    "Event", "Event2", "EventA", "EventB", "EventC", "EventD", "EventSp",
]

DEFAULT_CONFIG = {
    "version": 3,
    "Enable": True,
    "Mode": "auto",
    "Multiplier": 555,
    "Tasks": {name: True for name in DEFAULT_TASKS_ON},
    # Bind to one ALAS instance (config name). Empty = any instance.
    "Instance": "",
    # Explicit adb serial for this instance. Empty = read from the ALAS config.
    "Serial": "",
}


def _norm(name):
    return str(name).lower().replace("_", "").replace(" ", "")


_ALIASES = None


def _aliases():
    """normalized original/display task name -> original task name."""
    global _ALIASES
    if _ALIASES is not None:
        return _ALIASES
    aliases = {}
    path = os.path.join(ALAS_ROOT, "module", "config", "i18n", "zh-CN.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        task = data.get("Task", {}) if isinstance(data, dict) else {}
        for key, val in task.items():
            aliases[_norm(key)] = key
            if isinstance(val, dict) and val.get("name"):
                aliases[_norm(val["name"])] = key
    except (OSError, ValueError):
        pass
    _ALIASES = aliases
    return aliases


def _resolve(name):
    return _aliases().get(_norm(name), str(name))


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        for k in ("Enable", "Mode", "Multiplier", "version", "Instance", "Serial"):
            if k in data:
                cfg[k] = data[k]
    if cfg.get("Mode") not in ("always_on", "always_off", "auto"):
        cfg["Mode"] = "auto"
    try:
        cfg["Multiplier"] = float(cfg.get("Multiplier", 555))
        if cfg["Multiplier"] <= 0:
            cfg["Multiplier"] = 555.0
    except (TypeError, ValueError):
        cfg["Multiplier"] = 555.0

    raw_tasks = data.get("Tasks") if isinstance(data, dict) else None
    tasks = {}
    if isinstance(raw_tasks, dict):
        for key, value in raw_tasks.items():
            if value:
                tasks[_resolve(key)] = True
    else:
        # Migrate the old RestoreTasks/ApplyTasks schema (default off).
        for item in (data.get("ApplyTasks") or []) if isinstance(data, dict) else []:
            if isinstance(item, str):
                tasks[_resolve(item)] = True
    cfg["Tasks"] = tasks
    return cfg


def decide(command, cfg):
    """Return 'apply', 'restore' or None."""
    if not cfg.get("Enable", True):
        return None
    mode = cfg.get("Mode", "auto")
    if mode == "always_on":
        return "apply"
    if mode == "always_off":
        return "restore"
    name = _resolve(command)
    return "apply" if cfg.get("Tasks", {}).get(name, False) else "restore"


_SERIAL_CACHE = {}


def _serial_from_log(config_name):
    """Read the serial ALAS actually used for this instance from its log."""
    if config_name in _SERIAL_CACHE:
        return _SERIAL_CACHE[config_name]
    log_dir = os.path.join(ALAS_ROOT, "log")
    try:
        files = [
            f for f in glob.glob(os.path.join(log_dir, f"*_{config_name}.txt"))
            if not os.path.basename(f).endswith("_gui.txt")
        ]
    except Exception:
        return None
    if not files:
        return None
    path = max(files, key=os.path.getmtime)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
            if size > 256 * 1024:
                f.seek(max(0, size - 2 * 1024 * 1024))
                tail = f.read()
            else:
                tail = b""
        text = (head + b"\n" + tail).decode("utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"\[AdbDevice\]\s*AdbDevice\(serial=([^)\s]+)\)", text)
    serial = matches[-1] if matches else None
    _SERIAL_CACHE[config_name] = serial
    return serial


def _device_info(config_name):
    serial, package = "auto", "auto"
    path = os.path.join(ALAS_CONFIG_DIR, f"{config_name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        emu = data.get("Emulator", {}) if isinstance(data, dict) else {}
        serial = emu.get("Serial", "auto") or "auto"
        package = emu.get("PackageName", "auto") or "auto"
    except (OSError, ValueError):
        pass
    if package == "auto":
        package = "com.bilibili.azurlane"
    if serial in ("", "auto", None):
        serial = _serial_from_log(config_name) or serial
    return serial, package


def _adb_devices(adb):
    try:
        out = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=10
        ).stdout
        return [
            line.split("\t")[0].strip()
            for line in out.splitlines()
            if "\tdevice" in line
        ]
    except Exception:
        return []


def _patcher(config_name, cfg, serial=None, package=None):
    cfg_serial, cfg_package = _device_info(config_name)
    if cfg.get("Serial"):
        serial = cfg["Serial"]
    if not serial:
        serial = cfg_serial
    if not package:
        package = cfg_package
    adb = alas_gg.DEFAULT_ADB
    if serial in ("", "auto", None):
        devices = _adb_devices(adb)
        if len(devices) == 1:
            serial = devices[0]
        elif not devices:
            raise RuntimeError("no adb device")
        else:
            raise RuntimeError(
                "multiple adb devices and no explicit Serial; "
                "set a per-instance Serial in gg_modifier settings"
            )
    return alas_gg.MemPatcher(adb=adb, serial=serial, package=package)


def _read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(config_name, pid, mode, value, serial=None):
    data = _read_state()
    data[config_name] = {
        "pid": pid,
        "serial": serial,
        "mode": mode,
        "value": value,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        logger.warning(f"GGModifier: cannot write state: {e}")


def _verify(p, pid, addresses, expected):
    bufs = p.read_many(pid, addresses, length=8, target_offset=0)
    return all(
        len(bufs.get(a, b"")) == 8
        and abs(struct.unpack("<d", bufs[a])[0] - expected) < 1e-9
        for a in addresses
    )


def _backup_and_patch(p, pid, addresses, value):
    old = p.read_many(pid, addresses, length=8, target_offset=0)
    backup = {"pid": pid, "values": {}}
    for a in addresses:
        if len(old.get(a, b"")) == 8:
            backup["values"][str(a)] = struct.unpack("<d", old[a])[0]
    alas_gg.save_json(alas_gg.BACKUP_PATH, backup)

    p.patch_many(pid, addresses, value)
    if not _verify(p, pid, addresses, value):
        raise RuntimeError("write verification failed")
    alas_gg.save_json(
        alas_gg.CACHE_PATH,
        {"pid": pid, "value": value, "addresses": addresses, "time": time.time()},
    )
    logger.info(f"GGModifier: applied {value} to {len(addresses)} addresses")


def _apply(p, pid, value):
    # Already applied? Cache says target value and the addresses verify.
    addresses = alas_gg.cache_valid(p, pid, value)
    if addresses:
        logger.info(f"GGModifier: already applied {value} at {len(addresses)} addresses")
        return

    # Cache says 1.0: patch those addresses directly.
    addresses = alas_gg.cache_valid(p, pid, 1.0)
    if addresses:
        _backup_and_patch(p, pid, addresses, value)
        return

    # Fresh scan for 1.0 targets.
    addresses = p.scan(pid, anchor=0.0001, target=1.0)
    if addresses:
        _backup_and_patch(p, pid, addresses, value)
        return

    # Fresh scan for the target value: already modified, repair cache/state.
    addresses = p.scan(pid, anchor=0.0001, target=value)
    if addresses and _verify(p, pid, addresses, value):
        alas_gg.save_json(
            alas_gg.CACHE_PATH,
            {"pid": pid, "value": value, "addresses": addresses, "time": time.time()},
        )
        logger.info(
            f"GGModifier: already applied {value} at {len(addresses)} addresses (cache repaired)"
        )
        return

    raise RuntimeError("modifier targets not found")


def _restore(p, pid, value):
    # Cache says target value: patch back to 1.0.
    addresses = alas_gg.cache_valid(p, pid, value)
    if addresses:
        p.patch_many(pid, addresses, 1.0)
        if not _verify(p, pid, addresses, 1.0):
            raise RuntimeError("restore verification failed")
        alas_gg.save_json(
            alas_gg.CACHE_PATH,
            {"pid": pid, "value": 1.0, "addresses": addresses, "time": time.time()},
        )
        logger.info(f"GGModifier: restored {len(addresses)} addresses to 1.0")
        return

    # Already restored?
    addresses = alas_gg.cache_valid(p, pid, 1.0)
    if addresses:
        logger.info("GGModifier: already restored")
        return

    # Fresh scan for the modified value.
    addresses = p.scan(pid, anchor=0.0001, target=value)
    if addresses:
        p.patch_many(pid, addresses, 1.0)
        if not _verify(p, pid, addresses, 1.0):
            raise RuntimeError("restore verification failed")
        alas_gg.save_json(
            alas_gg.CACHE_PATH,
            {"pid": pid, "value": 1.0, "addresses": addresses, "time": time.time()},
        )
        logger.info(f"GGModifier: restored {len(addresses)} addresses to 1.0")
        return

    # Fresh scan for 1.0: already restored, repair cache.
    addresses = p.scan(pid, anchor=0.0001, target=1.0)
    if addresses:
        alas_gg.save_json(
            alas_gg.CACHE_PATH,
            {"pid": pid, "value": 1.0, "addresses": addresses, "time": time.time()},
        )
        logger.info("GGModifier: already restored (cache repaired)")
        return

    raise RuntimeError(f"restore targets not found (value {value})")


# ---------------- mid-task retire / enhance hooks ----------------
_CURRENT_CONFIG = None
_MIDTASK_HOOKED = False


def _install_midtask_hooks():
    """Wrap Retirement._retire_handler and Enhancement._enhance_handler once."""
    global _MIDTASK_HOOKED
    if _MIDTASK_HOOKED:
        return
    try:
        from module.retire.retirement import Retirement
        from module.retire.enhancement import Enhancement
    except Exception as e:
        logger.warning(f"GGModifier: cannot import retire modules: {e}")
        return

    def wrap(cls, name):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_gg_wrapped", False):
            return

        def wrapper(self, *args, **kwargs):
            return _run_midtask(self, original, *args, **kwargs)

        wrapper._gg_wrapped = True
        setattr(cls, name, wrapper)

    wrap(Retirement, "_retire_handler")
    wrap(Enhancement, "_enhance_handler")
    _MIDTASK_HOOKED = True
    logger.info("GGModifier: mid-task retire/enhance hooks installed")


def _run_midtask(obj, original, *args, **kwargs):
    config_name = _CURRENT_CONFIG
    try:
        cfg = getattr(obj, "config", None)
        if cfg is not None and getattr(cfg, "config_name", None):
            config_name = cfg.config_name
    except Exception:
        pass
    if not config_name:
        return original(obj, *args, **kwargs)

    restored = _restore_for_midtask(config_name)
    try:
        return original(obj, *args, **kwargs)
    finally:
        if restored:
            _apply_after_midtask(config_name)


def _restore_for_midtask(config_name):
    try:
        cfg = load_config()
        if not cfg.get("Enable", True):
            return False
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            return False
        state = _read_state().get(config_name) or {}
        if state.get("mode") != "applied":
            return False
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        pid = p.get_pid()
        value = float(cfg["Multiplier"])
        logger.info(f"GGModifier: mid-task restore before retire/enhance")
        _restore(p, pid, value)
        _write_state(config_name, pid, "restored", value, p.serial)
        return True
    except Exception as e:
        logger.warning(f"GGModifier: mid-task restore failed: {e}")
        return False


def _apply_after_midtask(config_name):
    try:
        cfg = load_config()
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            return
        state = _read_state().get(config_name) or {}
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        pid = p.get_pid()
        value = float(cfg["Multiplier"])
        logger.info(f"GGModifier: mid-task re-apply after retire/enhance")
        _apply(p, pid, value)
        _write_state(config_name, pid, "applied", value, p.serial)
    except Exception as e:
        logger.warning(f"GGModifier: mid-task re-apply failed: {e}")


def on_task_start(config_name, command, serial=None, package=None):
    """Called at the beginning of AzurLaneAutoScript.run()."""
    global _CURRENT_CONFIG
    try:
        cfg = load_config()
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            logger.info(f"GGModifier: bound to `{cfg['Instance']}`, skip `{config_name}`")
            return
        if cfg.get("Enable", True):
            _CURRENT_CONFIG = config_name
            _install_midtask_hooks()

        mode = decide(command, cfg)
        if mode is None:
            return

        p = _patcher(config_name, cfg, serial=serial, package=package)
        try:
            pid = p.get_pid()
        except Exception:
            logger.info(f"GGModifier: game is not running, skip `{command}`")
            return

        state = _read_state().get(config_name) or {}
        expected_state = "applied" if mode == "apply" else "restored"
        value = float(cfg["Multiplier"])
        if (
            state.get("pid") == pid
            and state.get("mode") == expected_state
            and (not state.get("serial") or state.get("serial") == p.serial)
        ):
            # Cross-check the cache: a stale state must not skip a real switch.
            cache = alas_gg.load_json(alas_gg.CACHE_PATH, {})
            try:
                cache_value = float(cache.get("value"))
            except (TypeError, ValueError):
                cache_value = None
            expected_cache = value if mode == "apply" else 1.0
            cache_ok = (
                cache.get("pid") == pid
                and cache_value is not None
                and abs(cache_value - float(expected_cache)) < 1e-9
            )
            if cache_ok:
                return
            logger.info("GGModifier: state/cache mismatch, re-checking")

        logger.info(f"GGModifier: task `{command}` -> {mode}")
        if mode == "apply":
            _apply(p, pid, value)
            _write_state(config_name, pid, "applied", value, p.serial)
        else:
            _restore(p, pid, value)
            _write_state(config_name, pid, "restored", value, p.serial)
    except Exception as e:
        logger.warning(f"GGModifier: {type(e).__name__}: {e}")
