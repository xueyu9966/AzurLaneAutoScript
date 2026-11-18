"""Resolve the scheduled timestamp of the Bash call currently running in ZCode."""
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import time


def planned_time(cfg, now):
    session = cfg.get("ZCodeSession")
    automation = cfg.get("ZCodeAutomation")
    if not session or not automation:
        raise ValueError("configure ZCodeSession and ZCodeAutomation, or pass --scheduled-at")
    path = Path(cfg.get("ZCodeDb") or Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        # The bundled Python 3.7 SQLite has no JSON1 extension.
        for attempt in range(3):
            matches = []
            rows = db.execute(
                "SELECT p.data,m.data FROM part p JOIN message m ON m.id=p.message_id "
                "WHERE p.session_id=? AND p.time_created BETWEEN ? AND ? "
                "AND p.data LIKE '%maintenance.py%auto%'",
                (session, int((now.timestamp() - 540) * 1000), int((now.timestamp() + 3) * 1000)),
            )
            for part_json, message_json in rows:
                part = json.loads(part_json)
                state = part.get("state", {})
                if part.get("tool") != "Bash" or state.get("status") != "running":
                    continue
                if not re.search(r"""maintenance\.py["']?\s+auto(?:\s|$)""",
                                 state.get("input", {}).get("command", "")):
                    continue
                parent = json.loads(message_json).get("parentID")
                row = db.execute(
                    "SELECT payload FROM session_input WHERE session_id=? AND promoted_message_id=?",
                    (session, parent),
                ).fetchone()
                if row:
                    intent = json.loads(row[0]).get("conversationInputIntent", {})
                    origin, _, stamp = intent.get("sourceCommandId", "").rpartition(":")
                    if origin == automation and stamp.isdigit():
                        matches.append(dt.datetime.fromtimestamp(int(stamp) / 1000))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError("multiple active maintenance calls; cannot identify this trigger")
            if attempt < 2:
                time.sleep(0.2)
        raise ValueError("no active maintenance call with a matching scheduled input")
    finally:
        db.close()
