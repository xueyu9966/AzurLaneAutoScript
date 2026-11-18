"""Resource snapshot storage shared by the ALAS hook and the GG panel.

Two sources:
  * ocr-log:  parsed from ALAS log lines `[OCR_OIL ...]` / `[OCR_COIN ...]`
  * goto_main: OCR taken by ALAS itself after it returns to the main page
               while the task queue is empty

Records are appended to config/gg_modifier/resources.jsonl as one JSON object
per line.  No ALAS modules are imported at module level, so the panel process
can import this file cheaply.
"""
import collections
import datetime as dt
import json
import os
import re
import threading
import time

if __package__:
    from .runtime_lock import file_lock
else:
    from runtime_lock import file_lock

HERE = os.path.dirname(os.path.abspath(__file__))
ALAS_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DATA_DIR = os.environ.get(
    "GG_MODIFIER_DATA_DIR", os.path.join(ALAS_ROOT, "config", "gg_modifier")
)
DATA_PATH = os.path.join(DATA_DIR, "resources.jsonl")
LOG_DIR = os.path.join(ALAS_ROOT, "log")

RE_OCR = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ \| [A-Z]+ \| "
    r"\[OCR_(OIL|COIN) [^\]]+\] (\d+)"
)
RE_LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_(.+)\.txt$")

# Current CN top bar layout (1280x720).  ALAS's built-in OCR_OIL/OCR_COIN
# coordinates are for an older layout; these are used only as a fallback when
# the standard OCR returns 0.
CUSTOM_OIL_AREA = (548, 23, 622, 51)
CUSTOM_COIN_AREA = (715, 23, 798, 51)

_WRITE_LOCK = threading.RLock()
_MAX_BYTES = 20 * 1024 * 1024


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value):
    return dt.datetime.fromisoformat(value)


def config_from_path(path):
    name = os.path.basename(path)
    match = RE_LOG_NAME.match(name)
    if not match:
        return None
    config = match.group(1)
    if config == "gui" or config.endswith("_gui"):
        return None
    return config


def parse_log_text(text, config_name, pending=None):
    """Pair OCR_OIL/OCR_COIN lines that are at most 5 seconds apart."""
    if pending is None:
        pending = {}
    records = []
    for line in text.splitlines():
        match = RE_OCR.match(line)
        if not match:
            continue
        moment = parse_time(match.group(1))
        kind = match.group(2)
        value = int(match.group(3))
        if value <= 0:
            pending.pop(kind, None)
            continue
        pending[kind] = (moment, value)
        if "OIL" not in pending or "COIN" not in pending:
            continue
        t_oil, oil = pending["OIL"]
        t_coin, coin = pending["COIN"]
        if abs((t_oil - t_coin).total_seconds()) > 5:
            # Keep the newer half; the other reading is stale.
            if t_oil > t_coin:
                pending.pop("COIN", None)
            else:
                pending.pop("OIL", None)
            continue
        records.append(
            {
                "time": max(t_oil, t_coin).strftime("%Y-%m-%d %H:%M:%S"),
                "config": config_name,
                "oil": oil,
                "coin": coin,
                "source": "ocr-log",
            }
        )
        pending.clear()
    return records


def parse_log_file(path, config_name=None):
    config_name = config_name or config_from_path(path)
    if not config_name:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return parse_log_text(f.read(), config_name)
    except OSError:
        return []


def append_record(record):
    """Append one record, rotating the file if it grows too large."""
    if not isinstance(record, dict):
        return False
    try:
        oil = int(record.get("oil") or 0)
        coin = int(record.get("coin") or 0)
    except (TypeError, ValueError):
        return False
    if not record.get("config") or oil <= 0 or coin <= 0:
        return False
    entry = {
        "time": record.get("time") or now_str(),
        "config": str(record.get("config") or ""),
        "oil": oil,
        "coin": coin,
        "source": str(record.get("source") or "unknown"),
    }
    with _WRITE_LOCK:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with file_lock(DATA_PATH + ".lock", timeout=5):
                archive = DATA_PATH + ".1"
                if (os.path.exists(DATA_PATH) and os.path.getsize(DATA_PATH) > _MAX_BYTES
                        and (not os.path.exists(archive) or
                             os.path.getmtime(archive) < time.time() - 48 * 3600)):
                    os.replace(DATA_PATH, archive)
                with open(DATA_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False


def append_records(records):
    return sum(1 for record in records if append_record(record))


def load_recent(limit=20000):
    """Last `limit` valid records, oldest first."""
    rows = collections.deque(maxlen=max(1, int(limit)))
    try:
        with open(DATA_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("config"):
                    rows.append(record)
    except OSError:
        pass
    return list(rows)


def load_last_by_config():
    """Last record per config, for dedupe after a restart."""
    last = {}
    for record in load_recent(limit=50000):
        last[record.get("config")] = record
    return last


def load_window(cutoff):
    """Read the whole window and the last older sample for each configuration."""
    if not os.path.isdir(DATA_DIR):
        return []
    with _WRITE_LOCK, file_lock(DATA_PATH + ".lock", timeout=5):
        return _read_window(cutoff)


def _read_window(cutoff):
    recent, latest_old = {}, {}
    for path in (DATA_PATH + ".1", DATA_PATH):
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        name, stamp = row["config"], row["time"]
                        if not isinstance(name, str) or not name or not isinstance(stamp, str):
                            continue
                        parse_time(stamp)
                        row["oil"], row["coin"] = int(row["oil"]), int(row["coin"])
                        if row["oil"] <= 0 or row["coin"] <= 0:
                            continue
                    except (KeyError, TypeError, ValueError):
                        continue
                    if stamp >= cutoff:
                        recent[(name, stamp, row.get("source", ""))] = row
                    elif name not in latest_old or stamp > latest_old[name]["time"]:
                        latest_old[name] = row
        except FileNotFoundError:
            continue
    return sorted(list(latest_old.values()) + list(recent.values()), key=lambda r: r["time"])


def log_files(days=3, log_dir=None):
    log_dir = log_dir or LOG_DIR
    cutoff = time.time() - max(1, days) * 86400
    files = []
    try:
        for name in os.listdir(log_dir):
            if not name.endswith(".txt") or "_gui.txt" in name:
                continue
            path = os.path.join(log_dir, name)
            if not config_from_path(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff:
                    files.append(path)
            except OSError:
                continue
    except OSError:
        pass
    files.sort(key=os.path.getmtime)
    return files


def _custom_ocr(image):
    """Fallback top-bar OCR for the current game UI layout."""
    try:
        from module.ocr.ocr import Digit

        height, width = image.shape[:2]
        sx = width / 1280.0
        sy = height / 720.0

        def scale(area):
            return (
                int(round(area[0] * sx)),
                int(round(area[1] * sy)),
                int(round(area[2] * sx)),
                int(round(area[3] * sy)),
            )

        oil = Digit(
            scale(CUSTOM_OIL_AREA), name="OCR_OIL_CUSTOM",
            letter=(255, 255, 255), threshold=128,
        ).ocr(image)
        coin = Digit(
            scale(CUSTOM_COIN_AREA), name="OCR_COIN_CUSTOM",
            letter=(255, 255, 255), threshold=128,
        ).ocr(image)
        if 0 < oil < 1000000 and 0 < coin < 100000000:
            return int(oil), int(coin)
    except Exception:
        pass
    return 0, 0


def snapshot_from_device(config, device, config_name, source="goto_main"):
    """Use ALAS's own OCR after it has returned to the main page."""
    try:
        from module.campaign.campaign_status import CampaignStatus
        from module.logger import logger
    except Exception:
        return None

    try:
        status = CampaignStatus(config=config, device=device)
        oil = coin = 0
        for attempt in range(3):
            try:
                device.screenshot()
            except Exception:
                time.sleep(1)
                continue
            oil = status.get_oil(skip_first_screenshot=True)
            coin = status.get_coin(skip_first_screenshot=True)
            if oil <= 0 or coin <= 0:
                oil, coin = _custom_ocr(device.image)
            if oil > 0 and coin > 0:
                record = {
                    "time": now_str(),
                    "config": config_name,
                    "oil": int(oil),
                    "coin": int(coin),
                    "source": source,
                }
                if not append_record(record):
                    logger.warning("GGResources: snapshot could not be saved")
                    return None
                logger.info(
                    f"GGResources: {source} snapshot oil={oil} coin={coin}"
                )
                return record
            time.sleep(1)
        logger.warning(
            f"GGResources: invalid OCR after retries oil={oil} coin={coin}"
        )
        return None
    except Exception as e:
        try:
            logger.warning(f"GGResources: snapshot failed: {e}")
        except Exception:
            pass
        return None
