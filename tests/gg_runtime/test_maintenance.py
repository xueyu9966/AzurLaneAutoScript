import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from module.gg_modifier.maintenance import maintenance as m
from module.gg_modifier.maintenance import zcode_trigger


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "config").mkdir()
        self.alive = {"active": True, "paused": False, "other-vm": True}
        for name in self.alive:
            serial = "localhost:123" if name != "other-vm" else "localhost:456"
            (self.root / "config" / (name + ".json")).write_text(
                json.dumps({"Alas": {"Emulator": {"Serial": serial}}}), encoding="utf-8")
        self.vm_on = True
        self.calls = []
        self.cfg = dict(m.DEFAULT_CONFIG, Serial="127.0.0.1:123", Instances=["paused"])
        mocks = patch.multiple(
            m, ALAS_ROOT=str(self.root), DATA_DIR=str(self.root),
            STATE_PATH=str(self.root / "state.json"), LOCK_PATH=str(self.root / "maintenance.lock"),
            load_config=lambda: self.cfg, log=lambda line: None, gpu_memory=lambda: "test",
            api=self.api, manager_result=self.manager, ensure_webui=lambda cfg: True,
            adb_booted=lambda cfg: True,
        )
        mocks.start()
        self.addCleanup(mocks.stop)

    def api(self, path, params=None, **kwargs):
        if path == "/status":
            return {"ok": True, "instances": {n: {"alive": v} for n, v in self.alive.items()}}
        name = params["config"]
        before = self.alive[name]
        self.calls.append((path, name))
        self.alive[name] = path == "/start"
        return {"ok": True, "config": name, "stopped": before, "started": not before}

    def manager(self, cfg, args, timeout):
        if args[0] == "info":
            return {"index": "0", "is_process_started": self.vm_on, "adb_port": 123}
        self.calls.append(("VM", args[-1]))
        if args[-1] in ("launch", "shutdown"):
            self.vm_on = args[-1] == "launch"
        return {"errcode": 0}

    def auto(self, planned, actual=None):
        return m.main(["auto", "--scheduled-at", planned, "--now", actual or planned])

    def stop(self):
        return self.auto("2026-11-17 23:50")

    def test_delayed_check_never_stops(self):
        self.assertEqual(self.auto("2026-11-17 23:10", "2026-11-18 00:01"), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(m.load_state(), {})

    def test_delayed_stop_and_recovery_are_scoped_and_idempotent(self):
        self.assertEqual(self.auto("2026-11-17 23:50", "2026-11-18 00:01"), 0)
        self.assertEqual(m.load_state()["instances"], ["active"])
        self.assertEqual(self.calls, [("/stop", "active"), ("VM", "shutdown")])
        self.assertTrue(self.alive["other-vm"])
        self.assertEqual(self.auto("2026-11-17 23:50", "2026-11-18 00:03"), 0)
        self.assertEqual(self.auto("2026-11-18 00:10", "2026-11-18 00:45"), 0)
        self.alive["active"] = False  # user pauses it after the successful restore
        self.assertEqual(self.auto("2026-11-18 00:50"), 0)
        self.assertEqual(self.auto("2026-11-18 00:10", "2026-11-18 00:55"), 0)
        self.assertFalse(self.alive["active"])
        self.assertFalse(self.alive["paused"])
        self.assertEqual(m.load_state()["phase"], "complete")
        self.assertEqual(len(self.calls), 5)
        self.assertIn(("VM", "hide_window"), self.calls)

    def test_early_or_expired_stop_is_skipped(self):
        for actual in ("2026-11-17 23:49", "2026-11-18 00:05", "2026-11-18 00:20"):
            self.assertEqual(self.auto("2026-11-17 23:50", actual), 0)
        self.assertEqual(self.calls, [])

    def test_late_stop_trigger_continues_with_start(self):
        self.assertEqual(self.auto("2026-11-17 23:50", "2026-11-18 00:01"), 0)
        self.assertEqual(m.load_state()["phase"], "stopped")
        self.assertEqual(self.auto("2026-11-17 23:50", "2026-11-18 00:10"), 0)
        self.assertTrue(self.alive["active"])
        self.assertTrue(self.vm_on)
        self.assertEqual(m.load_state()["phase"], "complete")
        self.assertIn(("VM", "launch"), self.calls)
        self.assertIn(("VM", "hide_window"), self.calls)

    def test_hide_emulator_can_be_disabled(self):
        self.cfg["HideEmulator"] = False
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.auto("2026-11-18 00:10"), 0)
        self.assertTrue(self.alive["active"])
        self.assertNotIn(("VM", "hide_window"), self.calls)

    def test_checkpoint_can_restore_only_an_existing_cycle(self):
        self.assertEqual(self.auto("2026-11-18 00:10"), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.auto("2026-11-19 00:10"), 0)
        self.assertFalse(self.vm_on)
        self.assertEqual(self.auto("2026-11-18 00:50"), 0)
        self.assertTrue(self.alive["active"])

    def test_month_end_and_year_boundary(self):
        for when in ("2026-11-30 23:50", "2026-12-01 00:10", "2026-12-31 23:50",
                     "2027-01-01 00:10", "2028-02-28 05:50"):
            self.assertIsNone(m.scheduled_role(dt.datetime.fromisoformat(when)))
        self.assertEqual(self.auto("2026-11-30 05:50"), 0)
        self.assertEqual(self.auto("2026-11-30 06:10", "2026-11-30 06:45"), 0)
        self.assertTrue(self.alive["active"])
        self.assertEqual(m.scheduled_role(dt.datetime(2028, 2, 29, 5, 50)), "stop-month-end")

    def test_journal_failure_prevents_stop(self):
        with patch.object(m, "save_state", side_effect=OSError("disk full")):
            self.assertEqual(self.stop(), 1)
        self.assertEqual(self.calls, [])

    def test_shutdown_failure_retains_confirmed_instances(self):
        with patch.object(m, "shutdown_emulator", return_value=False):
            self.assertEqual(self.stop(), 1)
        self.assertEqual(m.load_state()["instances"], ["active"])
        self.assertEqual(self.auto("2026-11-18 00:10"), 0)
        self.assertTrue(self.alive["active"])
        self.assertTrue(self.alive["other-vm"])
        self.assertNotIn(("VM", "launch"), self.calls)

    def test_partial_restore_keeps_only_unfinished_instances(self):
        self.alive["paused"] = True
        self.assertEqual(self.stop(), 0)
        def partial(path, params=None, **kwargs):
            if path == "/start" and params["config"] == "paused":
                raise OSError("interrupted")
            return self.api(path, params, **kwargs)
        with patch.object(m, "api", side_effect=partial):
            self.assertEqual(self.auto("2026-11-18 00:10"), 1)
        self.assertEqual(m.load_state()["instances"], ["paused"])
        self.alive["active"] = False
        self.assertEqual(self.auto("2026-11-18 00:50"), 0)
        self.assertFalse(self.alive["active"])

    def test_unknown_instance_or_wrong_vm_fails_before_stop(self):
        self.alive["unconfigured"] = True
        self.assertEqual(self.stop(), 1)
        del self.alive["unconfigured"]
        with patch.object(m, "manager_result", return_value={"index": "1", "is_process_started": True}):
            self.assertEqual(self.stop(), 1)
        self.assertEqual(self.calls, [])

    def test_duplicate_stop_cannot_erase_a_pending_list(self):
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.stop(), 0)
        self.assertEqual(m.load_state()["instances"], ["active"])
        self.assertEqual(self.calls.count(("/stop", "active")), 1)

    def test_unacknowledged_stop_preserves_journal_and_never_guesses(self):
        def lost_reply(path, params=None, **kwargs):
            result = self.api(path, params, **kwargs)
            if path == "/stop":
                raise OSError("acknowledgement lost")
            return result
        with patch.object(m, "api", side_effect=lost_reply):
            self.assertEqual(self.stop(), 1)
        self.assertEqual(m.load_state()["stopping"], "active")
        self.assertEqual(self.auto("2026-11-18 00:10"), 1)
        self.assertNotIn(("/start", "active"), self.calls)

    def test_legacy_pending_state_survives_midnight_upgrade(self):
        Path(m.STATE_PATH).write_text(json.dumps({
            "role": "stop-night", "stopped_at": "2026-11-18 00:02:00",
            "instances": ["active"], "emulator_was_running": True,
        }), encoding="utf-8")
        self.alive["active"] = self.vm_on = False
        self.assertEqual(self.auto("2026-11-18 00:10"), 0)
        self.assertTrue(self.alive["active"])

    def test_no_device_writes_in_dry_run(self):
        with patch.object(m, "log") as report:
            self.assertEqual(m.main(["stop", "--dry-run"]), 0)
            self.assertTrue(any("dry_run=True" in call[0][0] for call in report.call_args_list))
        self.assertEqual(self.calls, [])
        self.assertEqual(m.load_state(), {})


class DeviceAndTriggerTests(unittest.TestCase):
    def test_exact_boot_flag_and_exit_status(self):
        for output, expected in [("1", True), ("0", False), ("error: 127.0.0.1 not found", False),
                                 ("timeout after 15s", False)]:
            with patch.object(m, "run", return_value=output):
                self.assertEqual(m.adb_booted(m.DEFAULT_CONFIG), expected)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"1")):
            with self.assertRaises(RuntimeError):
                m.adb_booted(m.DEFAULT_CONFIG)

    def test_queue_origin_follows_parent_message(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "db.sqlite"
            db = sqlite3.connect(str(db_path))
            db.executescript("CREATE TABLE part(session_id,message_id,time_created,data);"
                             "CREATE TABLE message(id,data);"
                             "CREATE TABLE session_input(session_id,promoted_message_id,payload);")
            actual = dt.datetime(2026, 11, 18, 0, 1, 53)
            original = dt.datetime(2026, 11, 17, 23, 10)
            cfg = {"ZCodeDb": str(db_path), "ZCodeSession": "s", "ZCodeAutomation": "a"}
            for parent, planned in [("original", original), ("newer", dt.datetime(2026, 11, 17, 23, 50))]:
                source = "a:" + str(int(planned.timestamp() * 1000))
                db.execute("insert into session_input values(?,?,?)", ("s", parent, json.dumps({
                    "conversationInputIntent": {"sourceCommandId": source}})))
            db.execute("insert into message values(?,?)", ("m", json.dumps({"parentID": "original"})))
            part = json.dumps({"tool": "Bash", "state": {"status": "running", "input": {
                "command": "python -I /dir/maintenance.py auto"}}})
            db.execute("insert into part values(?,?,?,?)", ("s", "m", int(actual.timestamp()*1000), part))
            db.commit()
            self.assertEqual(zcode_trigger.planned_time(cfg, actual), original)
            db.execute("delete from part")
            db.commit()
            db.close()
            with patch.object(zcode_trigger.time, "sleep"):
                with self.assertRaises(ValueError):
                    zcode_trigger.planned_time(cfg, actual)
