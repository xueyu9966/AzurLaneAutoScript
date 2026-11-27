"""Daily game-event update check for the ALAS maintenance flow.

The ALAS instance config is authoritative for the event *type*:
``coalition_*`` campaigns are coalition events, ``event_*`` campaigns are
regular event maps.  The Bilibili wiki recent-changes feed is used only to
notice that a newer event page exists, because the wiki page itself does not
always use the word "共斗".
"""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ALAS_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DATA_DIR = os.path.join(ALAS_ROOT, "config", "gg_modifier")
REPORT_PATH = os.path.join(DATA_DIR, "update_check.json")
WIKI_API = "https://wiki.biligame.com/blhx/api.php"
CAMPAIGN_RE = re.compile(r"^(coalition|event)_(\d{8})")
EVENT_TYPES = {"coalition": "共斗", "event": "活动图"}
TASKS = ("Coalition", "Event", "EventA", "EventB", "EventC", "EventD", "EventSp")


def campaign_info(name):
    match = CAMPAIGN_RE.match(str(name or ""))
    if not match:
        return None
    kind, stamp = match.group(1), match.group(2)
    try:
        date = dt.datetime.strptime(stamp, "%Y%m%d").date().isoformat()
    except ValueError:
        return None
    return {
        "campaign": str(name),
        "kind": kind,
        "label": EVENT_TYPES[kind],
        "date": date,
    }


def load_instance(instance):
    path = os.path.join(ALAS_ROOT, "config", f"{instance}.json")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def current_event(instance):
    """Return the newest coalition/event campaign configured for an instance."""
    data = load_instance(instance)
    if data is None:
        return None
    campaigns = []
    for task in TASKS:
        entry = data.get(task)
        if not isinstance(entry, dict):
            continue
        campaign = (entry.get("Campaign") or {}).get("Event")
        info = campaign_info(campaign)
        if info:
            campaigns.append(info)
    if not campaigns:
        return None
    return max(campaigns, key=lambda item: item["date"])


def coalition_campaign(instance):
    """Return the newest coalition campaign configured for an instance."""
    data = load_instance(instance)
    if data is None:
        return None
    campaigns = []
    for task in ("Coalition", "CoalitionSp"):
        entry = data.get(task)
        if not isinstance(entry, dict):
            continue
        info = campaign_info((entry.get("Campaign") or {}).get("Event"))
        if info and info["kind"] == "coalition":
            campaigns.append(info)
    if not campaigns:
        return None
    return max(campaigns, key=lambda item: item["date"])["campaign"]


def settings_path():
    return os.path.join(ALAS_ROOT, "config", "gg_modifier", "settings.json")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def reset_coalition_multiplier(log=None):
    """Turn off the Coalition/CoalitionSp multiplier for a new coalition event."""
    path = settings_path()
    data = _read_json(path)
    tasks = data.get("Tasks")
    if not isinstance(tasks, dict):
        return False
    changed = False
    for task in ("Coalition", "CoalitionSp"):
        if task in tasks:
            tasks.pop(task, None)
            changed = True
    if not changed:
        return False
    try:
        _write_json(path, data)
    except OSError as error:
        if log:
            log(f"coalition multiplier reset write failed: {error}")
        return False
    if log:
        log("new coalition event; reset Coalition/CoalitionSp multiplier to off")
    return True


def _get_json(url, timeout=15):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wiki_events(limit=50):
    """New wiki pages that look like event hubs or update announcements."""
    query = urllib.parse.urlencode({
        "action": "query",
        "list": "recentchanges",
        "rctype": "new",
        "rcnamespace": "0",
        "rclimit": str(limit),
        "format": "json",
    })
    data = _get_json(f"{WIKI_API}?{query}")
    changes = data.get("query", {}).get("recentchanges", []) if isinstance(data, dict) else []
    out = []
    for change in changes:
        title = str(change.get("title") or "")
        if not any(word in title for word in ("活动专题", "活动预告", "更新公告")):
            continue
        out.append({
            "title": title,
            "timestamp": change.get("timestamp") or "",
            "url": "https://wiki.biligame.com/blhx/" + urllib.parse.quote(title.replace(" ", "_")),
        })
    out.sort(key=lambda item: item["timestamp"], reverse=True)
    return out


def _write_report(report):
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = REPORT_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, REPORT_PATH)


def check(instance, log=None):
    """Check the configured event type and whether the wiki has a newer page."""
    event = current_event(instance)
    coalition = coalition_campaign(instance)
    previous = _read_json(REPORT_PATH)
    last_coalition = previous.get("last_coalition") if isinstance(previous, dict) else None
    actions = []
    if coalition and coalition != last_coalition:
        if last_coalition:
            try:
                if reset_coalition_multiplier(log):
                    actions.append("reset_coalition_multiplier")
            except Exception as error:
                actions.append("reset_coalition_multiplier_failed")
                if log:
                    log(
                        "coalition multiplier reset failed: "
                        f"{type(error).__name__}: {error}"
                    )
        last_coalition = coalition
    elif coalition:
        last_coalition = coalition

    wiki = []
    status = "ok"
    message = ""
    try:
        wiki = wiki_events()
    except Exception as error:
        status = "wiki_error"
        message = f"wiki 请求失败: {type(error).__name__}: {error}"
    latest = wiki[0] if wiki else None
    if event is None:
        status = "no_config"
        message = "实例配置里没有 coalition_/event_ 活动"
    elif latest:
        try:
            wiki_date = dt.datetime.strptime(
                latest["timestamp"], "%Y-%m-%dT%H:%M:%SZ"
            ).date()
            config_date = dt.date.fromisoformat(event["date"])
            if wiki_date > config_date + dt.timedelta(days=1):
                status = "wiki_newer"
                message = "wiki 出现比配置更新的活动页，请核对更新公告"
            elif not message:
                message = "配置与 wiki 一致"
        except ValueError:
            if not message:
                message = "wiki 时间无法解析"
    elif not message:
        status = "no_wiki"
        message = "wiki 未取到活动页"
    report = {
        "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "instance": instance,
        "config": event,
        "coalition": coalition,
        "last_coalition": last_coalition,
        "actions": actions,
        "wiki": latest,
        "status": status,
        "message": message,
    }
    try:
        _write_report(report)
    except OSError as error:
        if log:
            log(f"update check report write failed: {error}")
    if log:
        event_text = (
            f"{event['campaign']} ({event['label']})"
            if event else "未配置"
        )
        wiki_text = latest["title"] if latest else "无"
        log(
            "update check: event={} coalition={} wiki={} status={} actions={} message={}".format(
                event_text, coalition or "无", wiki_text, status,
                ",".join(actions) or "无", message,
            )
        )
    return report


def check_updates(cfg, log=None):
    """Maintenance entry point: use the first configured instance."""
    instances = cfg.get("Instances") or []
    instance = instances[0] if instances else "alas小开"
    return check(instance, log=log)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("instance", nargs="?", default="alas小开")
    args = parser.parse_args()
    print(json.dumps(check(args.instance), ensure_ascii=False, indent=2))
