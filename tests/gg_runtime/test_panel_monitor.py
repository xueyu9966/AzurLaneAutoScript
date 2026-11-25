import datetime as dt
import json
import logging
from pathlib import Path
import sys
import tempfile
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


class TaskGroupTests(unittest.TestCase):
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
        for change in [
            patch.object(panel.alas_gg, "DATA_DIR", str(self.data_dir)),
            patch.object(panel, "_plog"),
        ]:
            change.start()
            self.addCleanup(change.stop)
        self.sample = self.config_dir / "sample.json"
        self.sample.write_text(json.dumps({
            "Main": {"Scheduler": {"Enable": True}},
            "Main2": {"Scheduler": {"Enable": False}},
            "Main3": {"Scheduler": {"Enable": True}},
            "Event": {"Scheduler": {"Enable": True}},
            "Event2": {"Scheduler": {"Enable": False}},
            "EventA": {"Scheduler": {"Enable": False}},
            "EventB": {"Scheduler": {"Enable": False}},
            "EventC": {"Scheduler": {"Enable": False}},
            "EventD": {"Scheduler": {"Enable": False}},
        }), encoding="utf-8")
        self.config_path = str(self.settings)

    def states(self):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        return {key: data[key]["Scheduler"]["Enable"] for key in (
            "Main", "Main2", "Main3", "Event", "Event2",
            "EventA", "EventB", "EventC", "EventD",
        )}

    def test_gate_off_keeps_selection_and_on_restores_it(self):
        ok, message, groups = panel.set_schedule_group(
            self.config_path, "sample", "main", False
        )
        self.assertTrue(ok, message)
        states = self.states()
        self.assertFalse(states["Main"] or states["Main2"] or states["Main3"])
        self.assertTrue(states["Event"])
        group = groups["groups"][0]
        self.assertFalse(group["master"])
        self.assertEqual(group["selected"], ["Main", "Main3"])
        saved = json.loads((self.data_dir / "task_groups.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["sample"]["main"], {"master": False, "selected": ["Main", "Main3"]})

        ok, message, groups = panel.set_schedule_group(
            self.config_path, "sample", "main", True
        )
        self.assertTrue(ok, message)
        states = self.states()
        self.assertTrue(states["Main"])
        self.assertFalse(states["Main2"])
        self.assertTrue(states["Main3"])
        self.assertTrue(states["Event"])
        self.assertTrue(groups["groups"][0]["master"])
        self.assertEqual(groups["groups"][0]["selected"], ["Main", "Main3"])

    def test_individual_requires_gate_open(self):
        ok, message, _groups = panel.set_schedule_group(
            self.config_path, "sample", "main", False
        )
        self.assertTrue(ok, message)
        ok, message, groups = panel.set_schedule_task(
            self.config_path, "sample", "Main2", True
        )
        self.assertFalse(ok)
        self.assertFalse(self.states()["Main2"])
        self.assertEqual(groups["groups"][0]["selected"], ["Main", "Main3"])

        ok, message, _groups = panel.set_schedule_group(
            self.config_path, "sample", "main", True
        )
        self.assertTrue(ok, message)
        ok, message, groups = panel.set_schedule_task(
            self.config_path, "sample", "Main2", True
        )
        self.assertTrue(ok, message)
        states = self.states()
        self.assertTrue(all(states[key] for key in ("Main", "Main2", "Main3")))
        self.assertEqual(groups["groups"][0]["selected"], ["Main", "Main2", "Main3"])

    def test_gate_open_with_empty_selection_runs_nothing(self):
        ok, message, _groups = panel.set_schedule_group(
            self.config_path, "sample", "main", True
        )
        self.assertTrue(ok, message)
        for task in ("Main", "Main3"):
            ok, message, groups = panel.set_schedule_task(
                self.config_path, "sample", task, False
            )
            self.assertTrue(ok, message)
        self.assertFalse(any(self.states()[key] for key in ("Main", "Main2", "Main3")))
        self.assertTrue(groups["groups"][0]["master"])
        self.assertEqual(groups["groups"][0]["selected"], [])

        ok, message, _groups = panel.set_schedule_group(
            self.config_path, "sample", "main", False
        )
        self.assertTrue(ok, message)
        ok, message, groups = panel.set_schedule_group(
            self.config_path, "sample", "main", True
        )
        self.assertTrue(ok, message)
        self.assertFalse(any(self.states()[key] for key in ("Main", "Main2", "Main3")))
        self.assertTrue(groups["groups"][0]["master"])
        self.assertEqual(groups["groups"][0]["selected"], [])

    def test_individual_rejects_unknown_task_and_flag(self):
        self.assertFalse(panel.set_schedule_task(self.config_path, "sample", "Nope", True)[0])
        self.assertFalse(panel.set_schedule_task(self.config_path, "sample", "Main", "yes")[0])

    def test_old_list_state_migrates_to_gate_and_selection(self):
        data = json.loads(self.sample.read_text(encoding="utf-8"))
        for task in ("Main", "Main2", "Main3"):
            data[task]["Scheduler"]["Enable"] = False
        self.sample.write_text(json.dumps(data), encoding="utf-8")
        (self.data_dir / "task_groups.json").write_text(
            json.dumps({"sample": {"main": ["Main"]}}), encoding="utf-8"
        )
        group = panel.schedule_group_state(self.config_path, "sample")["groups"][0]
        self.assertFalse(group["master"])
        self.assertEqual(group["selected"], ["Main"])

        ok, message, _groups = panel.set_schedule_group(
            self.config_path, "sample", "main", True
        )
        self.assertTrue(ok, message)
        states = self.states()
        self.assertTrue(states["Main"])
        self.assertFalse(states["Main2"] or states["Main3"])

    def test_rejects_unknown_group_instance_and_flag(self):
        self.assertFalse(panel.set_schedule_group(self.config_path, "sample", "nope", True)[0])
        self.assertFalse(panel.set_schedule_group(self.config_path, "missing", "main", True)[0])
        self.assertFalse(panel.set_schedule_group(self.config_path, "sample", "main", "yes")[0])

    def test_partial_state_is_reported(self):
        group = panel.schedule_group_state(self.config_path, "sample")["groups"][0]
        self.assertTrue(group["master"])
        self.assertEqual(group["selected"], ["Main", "Main3"])
        self.assertEqual(group["enabled"], ["Main", "Main3"])
        self.assertTrue(group["partial"])
        self.assertFalse(group["all"])
