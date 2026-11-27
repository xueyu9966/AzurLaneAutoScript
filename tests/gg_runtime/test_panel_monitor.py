import datetime as dt
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from module.gg_modifier import monitor
from module.gg_modifier.panel import server as panel

logger_module = types.ModuleType("module.logger")
logger_module.logger = logging.getLogger("gg-runtime-test")
with patch.dict(sys.modules, {"module.logger": logger_module}):
    from module.gg_modifier import modifier


class SafetyTests(unittest.TestCase):
    def test_failed_cleanup_blocks_protected_callback(self):
        callback = Mock()
        with patch.object(modifier, "_CURRENT_CONFIG", "sample"), \
                patch.object(modifier, "load_config", return_value={"Enable": True}), \
                patch.object(modifier, "_read_state", return_value={"sample": {"mode": "applied"}}), \
                patch.object(modifier, "_patcher", side_effect=OSError("device unavailable")), \
                patch.object(modifier, "_apply_after_midtask") as followup:
            with self.assertRaises(modifier.GGModifierUnsafeError):
                modifier._run_midtask(object(), callback)
            callback.assert_not_called()
            followup.assert_not_called()
            self.assertFalse(modifier._MIDTASK_ACTIVE.is_set())

    def test_confirmed_safe_state_calls_handler(self):
        callback = Mock(return_value=17)
        with patch.object(modifier, "_CURRENT_CONFIG", "sample"), \
                patch.object(modifier, "load_config", return_value={"Enable": True}), \
                patch.object(modifier, "_read_state", return_value={"sample": {"mode": "restored"}}):
            self.assertEqual(modifier._run_midtask(object(), callback), 17)
        callback.assert_called_once()

    def test_unknown_state_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("{", encoding="utf-8")
            with patch.object(modifier, "STATE_PATH", str(path)), patch.object(modifier, "load_config", return_value={}):
                with self.assertRaises(modifier.GGModifierUnsafeError):
                    modifier._restore_for_midtask("sample")

    def test_trial_blocks_manual_actions_and_active_watchdog(self):
        with patch.object(panel, "load_config", return_value={"DryRun": True}), \
                patch.object(panel, "_plog"), patch.object(panel.alas_gg, "MemPatcher") as device:
            for action in ("apply", "restore"):
                ok, message = panel.do_action(action, "unused", "example")
                self.assertTrue(ok)
                self.assertIn("试运行", message)
            device.assert_not_called()
        with patch.object(modifier, "load_config", return_value={"DryRun": True}), \
                patch.object(modifier, "_patcher") as device:
            self.assertTrue(modifier._TaskWatchdog._tick(object())[1])
            device.assert_not_called()


class ResourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "resources.jsonl"
        self.logs = self.root / "log"
        self.logs.mkdir()
        self.res = panel.gg_resources
        for change in [
            patch.multiple(self.res, DATA_DIR=str(self.root), DATA_PATH=str(self.path)),
            patch.multiple(panel, _RES_CACHE={}, _RES_TIMES={}, _RES_OFFSETS={}),
        ]:
            change.start()
            self.addCleanup(change.stop)

    def test_split_line_and_split_pair_survive_poll_boundary(self):
        stamp = dt.datetime.now() - dt.timedelta(minutes=1)
        path = self.logs / (stamp.strftime("%Y-%m-%d") + "_sample.txt")
        a = stamp.strftime("%Y-%m-%d %H:%M:%S") + ".100 | INFO | [OCR_OIL 0.1s] 100\n"
        b = (stamp + dt.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S") + ".000 | INFO | [OCR_COIN 0.1s] 200\n"
        path.write_bytes((a + b[:24]).encode())
        self.assertEqual(panel._res_tail(str(self.logs)), 0)
        with path.open("ab") as out:
            out.write(b[24:].encode())
        with patch.object(self.res, "append_record", return_value=False):
            with self.assertRaises(OSError):
                panel._res_tail(str(self.logs))
            self.assertIsNone(panel.resource_state("sample")["latest"])
        self.assertEqual(panel._res_tail(str(self.logs)), 1)
        self.assertEqual(panel.resource_state("sample")["count"], 1)
        panel._RES_OFFSETS.clear()
        panel._res_reload()
        self.assertEqual(panel._res_tail(str(self.logs)), 0)

    def test_all_48_hours_are_counted_including_rotated_file(self):
        now = dt.datetime.now().replace(microsecond=0)
        def row(i):
            return dict(config="sample", source="goto_main", oil=14140 if i == 0 else 9666,
                        coin=217432 if i == 0 else 269388,
                        time=(now - dt.timedelta(seconds=211-i)).strftime("%Y-%m-%d %H:%M:%S"))
        rows = [row(i) for i in range(210)]
        older = dict(rows[0], time=(now - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), oil=99999)
        Path(str(self.path) + ".1").write_text("\n".join(map(json.dumps, [older] + rows[:10])), encoding="utf-8")
        self.path.write_text("\n".join(map(json.dumps, rows[10:])) + '\n{"broken":true}\n', encoding="utf-8")
        panel._res_reload()
        state = panel.resource_state("sample")
        self.assertEqual(state["count"], 210)
        self.assertEqual(len(state["samples"]), 200)
        self.assertEqual(state["range"], {"oil": [9666, 14140], "coin": [217432, 269388]})
        self.assertEqual(state["latest"]["time"], rows[-1]["time"])
        self.assertEqual(panel.resource_state("other")["count"], 0)


class MonitorTests(unittest.TestCase):
    def test_restart_crosses_midnight_once_and_preserves_a_partial_line(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = str(root / "monitor.log")
            one = root / "2026-11-17_example.txt"
            two = root / "2026-11-18_example.txt"
            one.write_bytes(b"old event\nunfinished")
            seen = []
            write = lambda c: monitor.save_cursor(out, c)
            cursor = {"config": "example"}
            monitor.read_logs(folder, cursor, seen.append, write)
            self.assertEqual(seen, ["old event"])
            with one.open("ab") as stream:
                stream.write(b" event\n")
            two.write_bytes(b"next day\n")
            cursor = monitor.load_cursor(out, "example")
            monitor.read_logs(folder, cursor, seen.append, write)
            monitor.read_logs(folder, cursor, seen.append, write)
            self.assertEqual(seen, ["old event", "unfinished event", "next day"])
            two.write_bytes(b"new\n")
            monitor.read_logs(folder, cursor, seen.append, write)
            self.assertEqual(seen[-1], "new")
            two.write_bytes(b"replacement grew\n")
            monitor.read_logs(folder, cursor, seen.append, write)
            self.assertEqual(seen[-1], "replacement grew")

    def test_legacy_heartbeat_replays_the_unobserved_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / "monitor.log"
            out.write_text("2026-11-18 21:28:59 HEARTBEAT\n", encoding="utf-8")
            (root / "2026-11-18_example.txt").write_text(
                "2026-11-18 21:00:00 old\n2026-11-18 21:30:00 new\n", encoding="utf-8")
            seen = []
            monitor.read_logs(folder, monitor.load_cursor(str(out), "example"), seen.append, lambda c: None)
            self.assertEqual(seen, ["2026-11-18 21:30:00 new"])

    def test_supervisor_restarts_a_failed_worker(self):
        with patch.object(monitor, "args", types.SimpleNamespace(duration=1), create=True), \
                patch.object(sys, "argv", ["monitor.py", "--supervise", "--duration", "1"]), \
                patch.object(monitor.subprocess, "call", side_effect=[9, 0]) as worker, \
                patch.object(monitor.time, "sleep") as wait, patch.object(monitor, "log"):
            monitor.supervise()
            self.assertEqual(worker.call_count, 2)
            self.assertNotIn("--supervise", worker.call_args[0][0])
            wait.assert_called_once_with(5)


class PauseGroupTests(unittest.TestCase):
    TASKS = (
        "Main", "Main2", "Main3", "GemsFarming",
        "EventGeneral", "Coalition", "Event", "Event2", "Raid", "Hospital",
        "EventShop", "WarArchives",
        "CoalitionSp", "EventA", "EventB", "EventC", "EventD", "EventSp", "RaidDaily",
    )

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_dir = self.root / "config"
        (self.config_dir / "gg_modifier").mkdir(parents=True)
        self.settings = self.config_dir / "gg_modifier" / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        change = patch.object(panel, "_plog")
        change.start()
        self.addCleanup(change.stop)
        self.sample = self.config_dir / "sample.json"
        data = {task: {"Scheduler": {"Enable": True}} for task in self.TASKS}
        self.sample.write_text(json.dumps(data), encoding="utf-8")
        self.config_path = str(self.settings)

    def states(self):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        return {key: data[key]["Scheduler"]["Enable"] for key in self.TASKS}

    def test_pause_farm_disables_all_farm_tasks(self):
        ok, message = panel.pause_schedule_group(self.config_path, "sample", "farm")
        self.assertTrue(ok, message)
        states = self.states()
        self.assertFalse(any(states[key] for key in ("Main", "Main2", "Main3", "GemsFarming")))
        self.assertTrue(states["EventGeneral"])
        self.assertTrue(states["Event"])

    def test_pause_event_disables_all_event_tasks_but_not_general(self):
        ok, message = panel.pause_schedule_group(self.config_path, "sample", "event")
        self.assertTrue(ok, message)
        states = self.states()
        self.assertTrue(states["EventGeneral"])
        self.assertFalse(any(states[key] for key in (
            "Coalition", "Event", "Event2", "Raid", "Hospital", "EventShop", "WarArchives",
        )))

    def test_pause_event_daily_disables_all_daily_tasks(self):
        ok, message = panel.pause_schedule_group(self.config_path, "sample", "event_daily")
        self.assertTrue(ok, message)
        states = self.states()
        self.assertFalse(any(states[key] for key in (
            "CoalitionSp", "EventA", "EventB", "EventC", "EventD", "EventSp", "RaidDaily",
        )))
        self.assertTrue(states["Main"])

    def test_pause_rejects_unknown_group_and_instance(self):
        self.assertFalse(panel.pause_schedule_group(self.config_path, "sample", "nope")[0])
        self.assertFalse(panel.pause_schedule_group(self.config_path, "missing", "farm")[0])


class PanelPidCacheTests(unittest.TestCase):
    def test_game_pid_is_cached(self):
        panel._PID_CACHE.clear()
        self.addCleanup(panel._PID_CACHE.clear)
        with patch.object(panel.alas_gg, "MemPatcher") as patcher:
            patcher.return_value.get_pid.return_value = 123
            self.assertEqual(panel.game_pid("serial", "package"), 123)
            self.assertEqual(panel.game_pid("serial", "package"), 123)
            self.assertEqual(patcher.return_value.get_pid.call_count, 1)


class AutoFarmTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config_dir = self.root / "config"
        (self.config_dir / "gg_modifier").mkdir(parents=True)
        self.settings = self.config_dir / "gg_modifier" / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config_path = str(self.settings)
        self.log_dir = str(self.root / "log")
        self.sample = self.config_dir / "sample.json"
        data = {task: {"Scheduler": {"Enable": False, "NextRun": "2020-01-01 00:00:00"}}
                for task in (
                    "Main", "Main2", "Main3", "GemsFarming", "Coalition", "Event",
                    "Event2", "Raid", "Hospital", "EventShop", "WarArchives",
                    "CoalitionSp", "EventA", "EventB", "EventC", "EventD", "EventSp",
                    "RaidDaily",
                )}
        self.sample.write_text(json.dumps(data), encoding="utf-8")
        self.oil = 0
        self.scheduler = True
        for change in [
            patch.object(panel, "AUTO_FARM_PATH", str(self.data_dir / "auto_farm.json")),
            patch.object(panel.alas_gg, "DATA_DIR", str(self.data_dir)),
            patch.object(panel, "_plog"),
            patch.object(panel, "scheduler_running", side_effect=lambda *_: self.scheduler),
            patch.object(panel, "resource_state", side_effect=lambda *_: {
                "latest": {"oil": self.oil, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            }),
        ]:
            change.start()
            self.addCleanup(change.stop)

    def states(self):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        return {key: data[key]["Scheduler"]["Enable"] for key in ("Main", "Main2", "Main3")}

    def start_state(self, stop_at="2099-01-01 08:00:00"):
        Path(panel.AUTO_FARM_PATH).write_text(json.dumps({
            "active": True, "armed": False, "tasks": ["Main", "Main2"],
            "stop_at": stop_at, "last_oil": 20000,
        }), encoding="utf-8")

    def enable_main(self, enabled=True):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        data["Main"]["Scheduler"]["Enable"] = enabled
        data["Main2"]["Scheduler"]["Enable"] = enabled
        self.sample.write_text(json.dumps(data), encoding="utf-8")

    def test_starts_near_24000_when_idle(self):
        self.oil = 23200
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertTrue(state["active"])
        self.assertFalse(state["armed"])
        self.assertTrue(state["stop_at"].endswith("08:00:00"))
        states = self.states()
        self.assertTrue(states["Main"] and states["Main2"])
        self.assertFalse(states["Main3"])

    def test_starts_at_23000(self):
        self.oil = 23000
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertTrue(state["active"])

    def test_does_not_start_below_23000(self):
        self.oil = 22500
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertFalse(state["active"])
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_does_not_start_when_any_task_enabled(self):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        data["Main3"]["Scheduler"]["Enable"] = True
        self.sample.write_text(json.dumps(data), encoding="utf-8")
        self.oil = 23200
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertFalse(state["active"])
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_does_not_start_when_scheduler_stopped(self):
        self.scheduler = False
        self.oil = 23200
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertIsNone(state)
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_monitor_stops_at_18000(self):
        self.enable_main()
        self.start_state()
        self.oil = 18000
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertFalse(state["active"])
        self.assertEqual(state["last_action"], "stopped_low_oil")
        self.assertEqual(state["stop_threshold"], 18000)
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_monitor_stops_at_20000_on_event_day(self):
        (self.data_dir / "update_check.json").write_text(json.dumps({
            "wiki": {"timestamp": time.strftime("%Y-%m-%dT15:00:00Z")},
        }), encoding="utf-8")
        self.assertEqual(panel.auto_farm_stop_threshold(), 20000)
        self.enable_main()
        self.start_state()
        self.oil = 20000
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertEqual(state["last_action"], "stopped_low_oil")
        self.assertEqual(state["stop_threshold"], 20000)

    def test_monitor_stops_at_8am(self):
        self.enable_main()
        self.start_state(stop_at="2020-01-01 00:00:00")
        self.oil = 20000
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertFalse(state["active"])
        self.assertEqual(state["last_action"], "stopped_time")
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_monitor_external_stop(self):
        self.start_state()
        self.oil = 20000
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertFalse(state["active"])
        self.assertEqual(state["last_action"], "stopped_externally")
        self.assertFalse(self.states()["Main"] or self.states()["Main2"])

    def test_after_stop_does_not_stop_user_reenabled_tasks(self):
        self.enable_main()
        self.start_state()
        self.oil = 18000
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertFalse(state["active"])

        self.enable_main()
        self.oil = 20000
        state = panel.auto_farm_check(self.config_path, None, self.log_dir)
        self.assertFalse(state["active"])
        state = panel.auto_farm_monitor_once(self.config_path, self.log_dir)
        self.assertFalse(state["active"])
        self.assertTrue(self.states()["Main"] and self.states()["Main2"])
