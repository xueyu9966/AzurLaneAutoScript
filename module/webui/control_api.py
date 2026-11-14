"""Local HTTP control API for the ALAS WebUI process.

The WebUI keeps ProcessManager objects in memory, so an external script cannot
start/stop a scheduler by simply launching/killing processes without confusing
the UI.  This module exposes a tiny localhost-only API from inside the WebUI
process:

    GET  /status              -> {"ok": true, "instances": {name: {"alive": bool}}}
    POST /start?config=NAME   -> start the scheduler for one config
    POST /stop?config=NAME    -> stop the scheduler for one config
    POST /stop_all            -> stop every running scheduler

It is used by module/gg_modifier/maintenance/maintenance.py (Windows scheduled
maintenance), but can also be called manually.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from module.logger import logger

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 22334

_started = False
_start_lock = threading.Lock()


def start_control_api(list_managers, get_manager, event_provider,
                      port=CONTROL_PORT):
    """Start the control server once.

    list_managers: callable -> dict[name, ProcessManager]
    get_manager:   callable(name) -> ProcessManager
    event_provider: callable -> multiprocessing.Event or None
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _json(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def _query(self):
            parsed = urlparse(self.path)
            return parsed.path, {
                k: v[0] for k, v in parse_qs(parsed.query).items()
            }

        def do_GET(self):
            path, _ = self._query()
            if path in ("/status", "/health"):
                try:
                    managers = list(list_managers().items())
                    instances = {
                        name: {"alive": bool(manager.alive)}
                        for name, manager in managers
                    }
                    self._json(200, {"ok": True, "instances": instances})
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path, query = self._query()
            if path == "/start":
                name = query.get("config")
                if not name:
                    self._json(400, {"ok": False, "error": "config required"})
                    return
                try:
                    manager = get_manager(name)
                    already = bool(manager.alive)
                    if not already:
                        manager.start(None, event_provider())
                    self._json(200, {
                        "ok": True, "config": name, "started": not already,
                    })
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            if path == "/stop":
                name = query.get("config")
                if not name:
                    self._json(400, {"ok": False, "error": "config required"})
                    return
                try:
                    manager = get_manager(name)
                    was_alive = bool(manager.alive)
                    if was_alive:
                        manager.stop()
                    self._json(200, {
                        "ok": True, "config": name, "stopped": was_alive,
                    })
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            if path == "/stop_all":
                try:
                    stopped = []
                    for name, manager in list(list_managers().items()):
                        if manager.alive:
                            manager.stop()
                            stopped.append(name)
                    self._json(200, {"ok": True, "stopped": stopped})
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            self._json(404, {"ok": False, "error": "not found"})

    try:
        server = ThreadingHTTPServer((CONTROL_HOST, port), Handler)
    except OSError as e:
        logger.warning(f"GGControl: cannot bind {CONTROL_HOST}:{port}: {e}")
        return

    thread = threading.Thread(
        target=server.serve_forever,
        name="GGControlAPI",
        daemon=True,
    )
    thread.start()
    logger.info(f"GGControl: listening on http://{CONTROL_HOST}:{port}/")
