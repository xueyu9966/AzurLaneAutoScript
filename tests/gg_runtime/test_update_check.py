import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from module.gg_modifier.maintenance import update_check as uc


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()
        for change in [
            patch.object(uc, "ALAS_ROOT", str(self.root)),
            patch.object(uc, "DATA_DIR", str(self.root / "data")),
            patch.object(uc, "REPORT_PATH", str(self.root / "data" / "update_check.json")),
        ]:
            change.start()
            self.addCleanup(change.stop)

    def write_instance(self, campaigns):
        data = {}
        for task, campaign in campaigns.items():
            data[task] = {"Campaign": {"Event": campaign}}
        (self.root / "config" / "sample.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_campaign_type(self):
        coalition = uc.campaign_info("coalition_20261124")
        self.assertEqual(coalition["kind"], "coalition")
        self.assertEqual(coalition["label"], "共斗")
        self.assertEqual(coalition["date"], "2026-11-24")
        event = uc.campaign_info("event_20261022_cn")
        self.assertEqual(event["kind"], "event")
        self.assertEqual(event["label"], "活动图")
        self.assertIsNone(uc.campaign_info("raid_20261124"))

    def test_current_event_uses_newest_campaign(self):
        self.write_instance({
            "Coalition": "coalition_20261124",
            "Event": "event_20261022_cn",
        })
        self.assertEqual(uc.current_event("sample")["campaign"], "coalition_20261124")
        self.write_instance({
            "Coalition": "coalition_20261124",
            "Event": "event_20261201_cn",
        })
        self.assertEqual(uc.current_event("sample")["campaign"], "event_20261201_cn")

    def test_wiki_events_filters_and_sorts(self):
        payload = {
            "query": {
                "recentchanges": [
                    {"title": "碧蓝海事局甲活动专题", "timestamp": "2026-11-25T15:12:21Z"},
                    {"title": "无关页面", "timestamp": "2026-11-25T15:13:21Z"},
                    {"title": "碧蓝海事局乙活动专题", "timestamp": "2026-11-26T15:12:21Z"},
                ]
            }
        }
        with patch.object(uc, "_get_json", return_value=payload):
            rows = uc.wiki_events()
        self.assertEqual([row["title"] for row in rows],
                         ["碧蓝海事局乙活动专题", "碧蓝海事局甲活动专题"])

    def test_check_ok_and_wiki_newer(self):
        event = uc.campaign_info("coalition_20261124")
        latest = {"title": "碧蓝海事局真理学院的转校生活动专题",
                  "timestamp": "2026-11-25T15:12:21Z", "url": "x"}
        with patch.object(uc, "current_event", return_value=event), \
                patch.object(uc, "wiki_events", return_value=[latest]):
            report = uc.check("sample")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["config"]["label"], "共斗")
        self.assertTrue(Path(uc.REPORT_PATH).exists())

        newer = dict(latest, timestamp="2026-11-27T15:12:21Z")
        with patch.object(uc, "current_event", return_value=event), \
                patch.object(uc, "wiki_events", return_value=[newer]):
            report = uc.check("sample")
        self.assertEqual(report["status"], "wiki_newer")

    def test_check_reports_missing_config_and_wiki_errors(self):
        with patch.object(uc, "current_event", return_value=None), \
                patch.object(uc, "wiki_events", return_value=[]):
            self.assertEqual(uc.check("sample")["status"], "no_config")
        with patch.object(uc, "current_event", return_value=uc.campaign_info("event_20261022_cn")), \
                patch.object(uc, "wiki_events", side_effect=OSError("network")):
            self.assertEqual(uc.check("sample")["status"], "wiki_error")

    def write_settings(self, tasks):
        path = self.root / "config" / "gg_modifier" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"Tasks": tasks}), encoding="utf-8")
        return path

    def read_tasks(self, path):
        return json.loads(path.read_text(encoding="utf-8"))["Tasks"]

    def test_new_coalition_resets_multiplier(self):
        settings = self.write_settings({"Coalition": True, "CoalitionSp": True, "Main": True})
        Path(uc.REPORT_PATH).write_text(
            json.dumps({"last_coalition": "coalition_20260723"}), encoding="utf-8"
        )
        event = uc.campaign_info("coalition_20261124")
        with patch.object(uc, "current_event", return_value=event), \
                patch.object(uc, "coalition_campaign", return_value="coalition_20261124"), \
                patch.object(uc, "wiki_events", return_value=[]):
            report = uc.check("sample")
        self.assertIn("reset_coalition_multiplier", report["actions"])
        self.assertEqual(report["last_coalition"], "coalition_20261124")
        tasks = self.read_tasks(settings)
        self.assertNotIn("Coalition", tasks)
        self.assertNotIn("CoalitionSp", tasks)
        self.assertTrue(tasks["Main"])

    def test_same_coalition_does_not_reset(self):
        settings = self.write_settings({"Coalition": True, "CoalitionSp": True})
        Path(uc.REPORT_PATH).write_text(
            json.dumps({"last_coalition": "coalition_20261124"}), encoding="utf-8"
        )
        event = uc.campaign_info("coalition_20261124")
        with patch.object(uc, "current_event", return_value=event), \
                patch.object(uc, "coalition_campaign", return_value="coalition_20261124"), \
                patch.object(uc, "wiki_events", return_value=[]):
            report = uc.check("sample")
        self.assertEqual(report["actions"], [])
        self.assertTrue(self.read_tasks(settings)["Coalition"])

    def test_first_run_records_baseline_without_reset(self):
        settings = self.write_settings({"Coalition": True})
        Path(uc.REPORT_PATH).unlink(missing_ok=True)
        event = uc.campaign_info("coalition_20261124")
        with patch.object(uc, "current_event", return_value=event), \
                patch.object(uc, "coalition_campaign", return_value="coalition_20261124"), \
                patch.object(uc, "wiki_events", return_value=[]):
            report = uc.check("sample")
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["last_coalition"], "coalition_20261124")
        self.assertTrue(self.read_tasks(settings)["Coalition"])
