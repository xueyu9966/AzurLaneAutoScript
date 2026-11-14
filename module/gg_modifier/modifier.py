"""GG modifier task hook.

Reads ./config/gg_modifier/settings.json (shared with the control panel).

Modes:
    always_on   - ensure applied before every task
    always_off  - ensure restored before every task
    auto        - per-task switches; Tasks[name] == true -> apply,
                  everything else -> restore (default off)

Safety layers:
    * ProtectSensitive: a hard list of tasks (exercise, guild, operation siren,
      ...) is always restored even if somebody accidentally enables it.
    * Task end: auto-mode tasks restore when the task finishes, so the modified
      stats do not linger while ALAS is idle or while the user plays manually.
    * Watchdog: a per-task thread re-checks the game PID and the 8 target
      addresses every WATCHDOG_INTERVAL seconds.  It re-applies after a game
      restart and restores if something else flipped the values.
    * Orphan cleanup: state entries record the scheduler process PID; the panel
      restores values left behind when the scheduler was hard-killed.
    * DryRun: log decisions without touching memory, for auditing a merged
      instance before enabling it.

Mid-task: when the dock fills up, ALAS enters retirement / enhancement.
GG-modified stats break enhancement, so those two handlers are wrapped:
restore right before them, re-apply right after they finish.
"""
import functools
import glob
import json
import os
import re
import struct
import subprocess
import threading
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

# Tasks that read ship attributes / fleet power, or that modify ships.
# They are restored even if they are accidentally added to "Tasks".
SENSITIVE_TASKS = [
    "Exercise", "Guild", "Daily", "Hard", "Meowfficer", "Awaken",
    "Shipyard", "Tactical", "Hospital", "Restart", "Start", "GotoMain",
    "EventShop", "EventStory",
]
SENSITIVE_PREFIXES = ["Opsi", "Raid", "Coalition"]

WATCHDOG_INTERVAL = 15.0       # seconds between checks
WATCHDOG_MAX_BACKOFF = 120.0   # seconds, after repeated failures

DEFAULT_CONFIG = {
    "version": 4,
    "Enable": True,
    "Mode": "auto",
    "Multiplier": 555,
    "Tasks": {name: True for name in DEFAULT_TASKS_ON},
    # Bind to one ALAS instance (config name). Empty = any instance.
    "Instance": "",
    # Explicit adb serial for this instance. Empty = read from the ALAS config.
    "Serial": "",
    # Audit only: log what would happen, never write memory.
    "DryRun": False,
    # Refuse to apply on SENSITIVE_TASKS even if they are enabled in Tasks.
    "ProtectSensitive": True,
}


class GGModifierUnsafeError(RuntimeError):
    """Raised when a sensitive task must not run because restore failed."""


def _norm(name):
    return str(name).lower().replace("_", "").replace(" ", "")


_SENSITIVE_NORM = {_norm(x) for x in SENSITIVE_TASKS}
_SENSITIVE_PREFIX_NORM = tuple(_norm(x) for x in SENSITIVE_PREFIXES)

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


def _is_sensitive(name):
    resolved = _resolve(name)
    norm = _norm(resolved)
    return norm in _SENSITIVE_NORM or norm.startswith(_SENSITIVE_PREFIX_NORM)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        for k in (
            "Enable", "Mode", "Multiplier", "version", "Instance", "Serial",
            "DryRun", "ProtectSensitive",
        ):
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
    cfg["Enable"] = bool(cfg.get("Enable", True))
    cfg["DryRun"] = bool(cfg.get("DryRun", False))
    cfg["ProtectSensitive"] = bool(cfg.get("ProtectSensitive", True))

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
    name = _resolve(command)
    # Hard safety: sensitive tasks never get the modifier, even if the user
    # accidentally enables them (or selects always_on).
    if cfg.get("ProtectSensitive", True) and _is_sensitive(name):
        return "restore"
    mode = cfg.get("Mode", "auto")
    if mode == "always_on":
        return "apply"
    if mode == "always_off":
        return "restore"
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
        emu = data.get("Alas", {}).get("Emulator", {})
        if not emu:
            emu = data.get("Emulator", {}) if isinstance(data, dict) else {}
        serial = emu.get("Serial", "auto") or "auto"
        package = emu.get("PackageName", "auto") or "auto"
    except (OSError, ValueError, AttributeError):
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


# ---------------- state ----------------
_STATE_LOCK = threading.RLock()
_MEM_LOCK = threading.RLock()
_MIDTASK_ACTIVE = threading.Event()

_TASK_PID = os.getpid()
_TASK_CTIME = None
try:
    import psutil

    _TASK_CTIME = psutil.Process(_TASK_PID).create_time()
except Exception:
    psutil = None


def _read_state():
    with _STATE_LOCK:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _write_state(config_name, pid, mode, value, serial=None, **extra):
    with _STATE_LOCK:
        data = _read_state()
        entry = {
            "pid": int(pid or 0),
            "serial": serial,
            "mode": mode,
            "value": value,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_pid": _TASK_PID,
        }
        if _TASK_CTIME is not None:
            entry["task_ctime"] = _TASK_CTIME
        entry.update({k: v for k, v in extra.items() if v is not None})
        data[config_name] = entry
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            logger.warning(f"GGModifier: cannot write state: {e}")


def _state_matches(state, pid, expected_mode, value, serial):
    if state.get("pid") != pid or state.get("mode") != expected_mode:
        return False
    if state.get("serial") and serial and state.get("serial") != serial:
        return False
    cache = alas_gg.load_json(alas_gg.CACHE_PATH, {})
    try:
        cache_value = float(cache.get("value"))
    except (TypeError, ValueError):
        cache_value = None
    expected_cache = value if expected_mode == "applied" else 1.0
    return (
        cache.get("pid") == pid
        and cache_value is not None
        and abs(cache_value - float(expected_cache)) < 1e-9
    )


def _applied_value(state, cfg):
    """The multiplier that is actually in memory (may differ from the config)."""
    try:
        value = float(state.get("value"))
    except (TypeError, ValueError):
        value = None
    if value is None or abs(value - 1.0) < 1e-9:
        try:
            return float(cfg["Multiplier"])
        except (KeyError, TypeError, ValueError):
            return 555.0
    return value


# ---------------- memory operations ----------------
def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _MEM_LOCK:
            return fn(*args, **kwargs)

    return wrapper


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


@_serialized
def _apply(p, pid, value):
    # If a different multiplier is currently applied, restore it first so the
    # ordinary "find 1.0" path can run again.
    cache = alas_gg.load_json(alas_gg.CACHE_PATH, {})
    if cache.get("pid") == pid:
        try:
            old = float(cache.get("value"))
        except (TypeError, ValueError):
            old = None
        if old is not None and abs(old - 1.0) > 1e-9 and abs(old - value) > 1e-9:
            logger.info(
                f"GGModifier: multiplier changed {old:g} -> {value:g}, restoring first"
            )
            try:
                _restore(p, pid, old)
            except Exception as e:
                logger.warning(f"GGModifier: cannot restore old value: {e}")

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


@_serialized
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


# ---------------- task watchdog ----------------
class _TaskWatchdog(threading.Thread):
    """Re-assert the desired memory state while a task is running."""

    def __init__(self, config_name, command, desired, value, serial=None, package=None):
        super().__init__(name=f"GGWatchdog-{config_name}", daemon=True)
        self.config_name = config_name
        self.command = command
        self.desired = desired
        self.value = float(value)
        self.serial = serial
        self.package = package
        self._stop_event = threading.Event()
        self._fails = 0
        self._last_error = None
        self._last_state = None
        self._waiting_logged = False

    def stop(self):
        self._stop_event.set()

    def _wait(self, seconds):
        return self._stop_event.wait(seconds)

    def _desired_now(self, cfg):
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(self.config_name):
            return "restore", True
        if not cfg.get("Enable", True):
            return "restore", True
        mode = decide(self.command, cfg)
        if mode is None:
            return None, True
        return mode, False

    def run(self):
        # on_task_start has just performed the action; give ALAS time to settle.
        if self._wait(WATCHDOG_INTERVAL):
            return
        while not self._stop_event.is_set():
            if _MIDTASK_ACTIVE.is_set():
                delay, stop_after = 3.0, False
            else:
                try:
                    delay, stop_after = self._tick()
                except Exception as e:
                    delay, stop_after = self._fail(e), False
            if stop_after:
                return
            if self._wait(delay):
                return

    def _tick(self):
        cfg = load_config()
        self.value = float(cfg.get("Multiplier", 555))
        desired, stop_after = self._desired_now(cfg)
        self.desired = desired or self.desired
        if desired is None:
            return WATCHDOG_INTERVAL, True

        p = _patcher(self.config_name, cfg, serial=self.serial, package=self.package)
        self.serial = p.serial
        self.package = p.package
        try:
            pid = p.get_pid()
        except Exception:
            if desired == "restore":
                if self._last_state != ("restored", 0):
                    logger.info("GGModifier: game is not running, watchdog marks restored")
                    _write_state(
                        self.config_name, 0, "restored", self.value, p.serial,
                        task=self.command, desired="restore",
                    )
                    self._last_state = ("restored", 0)
                return WATCHDOG_INTERVAL, stop_after
            if not self._waiting_logged:
                logger.info("GGModifier: watchdog waiting for the game to start")
                self._waiting_logged = True
            return WATCHDOG_INTERVAL, stop_after
        self._waiting_logged = False

        state = _read_state().get(self.config_name) or {}
        restore_value = _applied_value(state, cfg)

        # Checks and memory writes must be atomic with task end / mid-task
        # restore; otherwise the watchdog can re-apply right after the task
        # restored (or during enhancement).
        with _MEM_LOCK:
            if self._stop_event.is_set():
                return WATCHDOG_INTERVAL, True
            if _MIDTASK_ACTIVE.is_set():
                return 3.0, False
            if desired == "apply":
                if alas_gg.cache_valid(p, pid, self.value):
                    self._success("applied", pid, self.value)
                    self._fails = 0
                    return WATCHDOG_INTERVAL, stop_after
                logger.info("GGModifier: watchdog applying")
                _apply(p, pid, self.value)
                logger.info("GGModifier: watchdog applied")
            else:
                if alas_gg.cache_valid(p, pid, 1.0):
                    self._success("restored", pid, self.value)
                    self._fails = 0
                    return WATCHDOG_INTERVAL, stop_after
                logger.info("GGModifier: watchdog restoring")
                _restore(p, pid, restore_value)
                logger.info("GGModifier: watchdog restored")
            self._success("applied" if desired == "apply" else "restored", pid, self.value)
        self._fails = 0
        return WATCHDOG_INTERVAL, stop_after

    def _success(self, mode, pid, value):
        if self._stop_event.is_set():
            return
        with _MEM_LOCK:
            # Task end may have restored while the watchdog was writing.
            if self._stop_event.is_set():
                return
            if self._last_state == (mode, pid):
                return
            _write_state(
                self.config_name, pid, mode, value, self.serial,
                task=self.command,
                desired="apply" if mode == "applied" else "restore",
            )
            self._last_state = (mode, pid)

    def _fail(self, error):
        self._fails += 1
        message = f"{type(error).__name__}: {error}"
        if self._fails == 1 or message != self._last_error:
            logger.warning(
                f"GGModifier: watchdog check failed ({self._fails}): {message}"
            )
        self._last_error = message
        if self._fails >= 3:
            with _MEM_LOCK:
                state = _read_state().get(self.config_name) or {}
                if state.get("mode") != "error":
                    _write_state(
                        self.config_name, state.get("pid") or 0, "error", self.value,
                        self.serial, task=self.command, desired=self.desired,
                        error=message,
                    )
                    logger.error("GGModifier: watchdog failed 3 times, state=error")
        delay = WATCHDOG_INTERVAL * (2 ** min(self._fails, 3))
        return min(WATCHDOG_MAX_BACKOFF, delay)


_WATCHDOG = None
_WATCHDOG_LOCK = threading.Lock()


def _stop_watchdog(config_name=None):
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        watchdog = _WATCHDOG
        if watchdog is not None and (
            config_name is None or watchdog.config_name == config_name
        ):
            _WATCHDOG = None
        else:
            watchdog = None
    if watchdog is not None:
        watchdog.stop()
        watchdog.join(timeout=5)


def _start_watchdog(config_name, command, desired, value, serial, package):
    global _WATCHDOG
    _stop_watchdog(config_name)
    watchdog = _TaskWatchdog(
        config_name, command, desired, value, serial=serial, package=package
    )
    with _WATCHDOG_LOCK:
        _WATCHDOG = watchdog
    watchdog.start()


# ---------------- mid-task retire / enhance hooks ----------------
_CURRENT_CONFIG = None
_CURRENT_TASK = None
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

    _MIDTASK_ACTIVE.set()
    try:
        restored = _restore_for_midtask(config_name)
        try:
            return original(obj, *args, **kwargs)
        finally:
            if restored:
                _apply_after_midtask(config_name)
    finally:
        _MIDTASK_ACTIVE.clear()


def _restore_for_midtask(config_name):
    try:
        cfg = load_config()
        if not cfg.get("Enable", True) or cfg.get("DryRun", False):
            return False
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            return False
        state = _read_state().get(config_name) or {}
        if state.get("mode") != "applied":
            return False
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        pid = p.get_pid()
        value = _applied_value(state, cfg)
        logger.info("GGModifier: mid-task restore before retire/enhance")
        _restore(p, pid, value)
        _write_state(
            config_name, pid, "restored", value, p.serial,
            task=_CURRENT_TASK, desired="restore", mid_task=True,
        )
        return True
    except Exception as e:
        logger.warning(f"GGModifier: mid-task restore failed: {e}")
        return False


def _apply_after_midtask(config_name):
    try:
        cfg = load_config()
        if not cfg.get("Enable", True) or cfg.get("DryRun", False):
            return
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            return
        if _CURRENT_TASK is not None and decide(_CURRENT_TASK, cfg) != "apply":
            logger.info("GGModifier: task no longer wants apply, leave restored")
            return
        state = _read_state().get(config_name) or {}
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        pid = p.get_pid()
        value = float(cfg["Multiplier"])
        logger.info("GGModifier: mid-task re-apply after retire/enhance")
        _apply(p, pid, value)
        _write_state(
            config_name, pid, "applied", value, p.serial,
            task=_CURRENT_TASK, desired="apply", mid_task=True,
        )
    except Exception as e:
        logger.warning(f"GGModifier: mid-task re-apply failed: {e}")


# ---------------- task entry points ----------------
def _safe_restore_if_applied(config_name, cfg):
    """When the hook is disabled, do not leave an already-applied state behind."""
    state = _read_state().get(config_name) or {}
    if state.get("mode") not in ("applied", "pending", "error"):
        return
    if state.get("task_pid") not in (None, _TASK_PID):
        return
    try:
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        value = _applied_value(state, cfg)
        try:
            pid = p.get_pid()
        except Exception:
            _write_state(
                config_name, 0, "restored", value,
                p.serial, task=state.get("task"), desired="restore", disabled=True,
            )
            return
        with _MEM_LOCK:
            _restore(p, pid, value)
            _write_state(
                config_name, pid, "restored", value, p.serial,
                task=state.get("task"), desired="restore", disabled=True,
            )
        logger.info("GGModifier: hook disabled, restored applied values")
    except Exception as e:
        logger.warning(f"GGModifier: cannot restore after disabling hook: {e}")


def on_task_start(config_name, command, serial=None, package=None):
    """Called at the beginning of AzurLaneAutoScript.run().

    Returns a small status dict.  Raises GGModifierUnsafeError when a task that
    must run with restored values could not be restored.
    """
    global _CURRENT_CONFIG, _CURRENT_TASK
    result = {"action": None, "ok": True, "dry_run": False, "error": None}
    mode = None
    restore_done = False
    try:
        cfg = load_config()
        result["dry_run"] = bool(cfg.get("DryRun", False))
        if cfg.get("Instance") and _norm(cfg["Instance"]) != _norm(config_name):
            logger.info(f"GGModifier: bound to `{cfg['Instance']}`, skip `{config_name}`")
            return result
        if not cfg.get("Enable", True):
            _safe_restore_if_applied(config_name, cfg)
            return result

        # A previous task's watchdog may still be finishing a slow adb call.
        _stop_watchdog(config_name)
        _CURRENT_CONFIG = config_name
        _CURRENT_TASK = command
        _install_midtask_hooks()

        mode = decide(command, cfg)
        result["action"] = mode
        if mode is None:
            return result
        value = float(cfg["Multiplier"])

        if result["dry_run"]:
            logger.info(
                f"GGModifier: [dry-run] task `{command}` -> {mode} "
                f"(value {value:g}), memory untouched"
            )
            return result

        p = _patcher(config_name, cfg, serial=serial, package=package)
        try:
            pid = p.get_pid()
        except Exception:
            if mode == "restore":
                logger.info("GGModifier: game is not running, nothing to restore")
                _write_state(
                    config_name, 0, "restored", value, p.serial,
                    task=command, desired="restore",
                )
                restore_done = True
                return result
            logger.info("GGModifier: game is not running yet, waiting in watchdog")
            _write_state(
                config_name, 0, "pending", value, p.serial,
                task=command, desired="apply",
            )
            _start_watchdog(config_name, command, mode, value, p.serial, p.package)
            return result

        state = _read_state().get(config_name) or {}
        expected_state = "applied" if mode == "apply" else "restored"
        already = _state_matches(state, pid, expected_state, value, p.serial)
        if state.get("pid") == pid and state.get("mode") == expected_state and not already:
            logger.info("GGModifier: state/cache mismatch, re-checking")
        if already and expected_state == "restored":
            restore_done = True

        if not already:
            logger.info(f"GGModifier: task `{command}` -> {mode}")
            if mode == "apply":
                try:
                    _apply(p, pid, value)
                except Exception as e:
                    _write_state(
                        config_name, pid, "error", value, p.serial,
                        task=command, desired="apply", error=str(e),
                    )
                    logger.error(
                        f"GGModifier: apply failed ({e}); task continues at 1.0, "
                        "watchdog will retry"
                    )
                    _start_watchdog(
                        config_name, command, mode, value, p.serial, p.package
                    )
                    result["error"] = str(e)
                    return result
                _write_state(
                    config_name, pid, "applied", value, p.serial,
                    task=command, desired="apply",
                )
            else:
                restore_value = _applied_value(state, cfg)
                try:
                    _restore(p, pid, restore_value)
                except Exception as e:
                    _write_state(
                        config_name, pid, "error", restore_value, p.serial,
                        task=command, desired="restore", error=str(e),
                    )
                    logger.error(
                        f"GGModifier: restore failed ({e}); refusing to run `{command}`"
                    )
                    result["ok"] = False
                    result["error"] = str(e)
                    raise GGModifierUnsafeError(
                        f"cannot restore before task `{command}`: {e}"
                    )
                _write_state(
                    config_name, pid, "restored", restore_value, p.serial,
                    task=command, desired="restore",
                )
                restore_done = True

        _start_watchdog(config_name, command, mode, value, p.serial, p.package)
        return result
    except GGModifierUnsafeError:
        raise
    except Exception as e:
        logger.warning(f"GGModifier: {type(e).__name__}: {e}")
        result["ok"] = False
        result["error"] = str(e)
        if mode == "restore" and not result["dry_run"] and not restore_done:
            raise GGModifierUnsafeError(
                f"cannot verify restore before task `{command}`: {e}"
            ) from e
        return result


def on_task_end(config_name, command):
    """Called at the end of AzurLaneAutoScript.run(), including error paths."""
    global _CURRENT_TASK
    try:
        _stop_watchdog(config_name)
        cfg = load_config()
        if cfg.get("DryRun", False):
            return
        state = _read_state().get(config_name) or {}
        if state.get("task_pid") not in (None, _TASK_PID):
            return
        if state.get("mode") not in ("applied", "pending", "error"):
            return

        bound_ok = not cfg.get("Instance") or _norm(cfg["Instance"]) == _norm(config_name)
        if (
            cfg.get("Mode") == "always_on"
            and cfg.get("Enable", True)
            and bound_ok
        ):
            # always_on means keep it applied between tasks as well.
            return

        value = _applied_value(state, cfg)
        p = _patcher(config_name, cfg, serial=state.get("serial"))
        try:
            pid = p.get_pid()
        except Exception:
            _write_state(
                config_name, 0, "restored", value, p.serial,
                task=command, desired="restore",
            )
            return
        logger.info("GGModifier: task end -> restore")
        with _MEM_LOCK:
            try:
                _restore(p, pid, value)
            except Exception as e:
                _write_state(
                    config_name, pid, "error", value, p.serial,
                    task=command, desired="restore", error=str(e),
                )
                logger.error(f"GGModifier: task end restore failed: {e}")
                return
            _write_state(
                config_name, pid, "restored", value, p.serial,
                task=command, desired="restore",
            )
    except Exception as e:
        logger.warning(f"GGModifier: task end hook failed: {e}")
    finally:
        _CURRENT_TASK = None
