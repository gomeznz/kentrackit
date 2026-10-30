"""HTTP server: static UI, JSON API, live updates (Server-Sent Events), tile cache."""
import base64
import csv
import io
import json
import mimetypes
import os
import queue
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATIC = os.path.join(os.path.dirname(__file__), "static")
TILE_RE = re.compile(r"^/tiles/(\d{1,2})/(\d{1,7})/(\d{1,7})$")
UNIT_RE = re.compile(r"^/api/units/([A-Za-z0-9_.-]{1,32})$")


class Broker:
    """Fan-out of live events to connected browsers."""

    def __init__(self):
        self.subs = set()
        self.lock = threading.Lock()
        self.state = {}          # last value of each sticky event (channel, link, display)

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.add(q)
            for ev, data in self.state.items():
                q.put_nowait((ev, data))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event, data, sticky=False):
        payload = json.dumps(data)
        with self.lock:
            if sticky:
                self.state[event] = payload
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((event, payload))
            except queue.Full:
                pass


def _sniff(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class TileCache:
    """Serves map tiles from disk; fetches and stores them when upstream is reachable.
    Pan around the operating area once while online and it works offline after."""

    def __init__(self, cfg, data_dir):
        self.upstream = cfg.get("upstream") or ""
        self.dir = os.path.join(data_dir, "tiles")
        self.max_zoom = int(cfg.get("max_zoom", 18))
        self.ua = cfg.get("user_agent", "radio-avl/0.1 (Raspberry Pi AVL; tile cache)")

    def get(self, z, x, y):
        if z > self.max_zoom:
            return None
        path = os.path.join(self.dir, str(z), str(x), str(y))
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        if not self.upstream:
            return None
        url = self.upstream.format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read()
        except Exception:
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data


def make_handler(app):
    cfg = app.cfg
    auth = cfg["web"].get("password")
    auth_ok = None
    if auth:
        user = cfg["web"].get("username", "admin")
        auth_ok = "Basic " + base64.b64encode(f"{user}:{auth}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "radio-avl"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---- helpers --------------------------------------------------
        def _send(self, code, body=b"", ctype="application/json", headers=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _authorised(self):
            if not auth_ok or self.headers.get("Authorization") == auth_ok:
                return True
            self._send(401, "Login required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="KenTrackIt"'})
            return False

        def _range(self, qs):
            now = app.now()
            t1 = float(qs.get("to", [now])[0])
            t0 = float(qs.get("from", [t1 - 3600])[0])
            return t0, t1

        # ---- routes ---------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._authorised():
                return
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = u.path

            if p == "/":
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/config":
                return self._json({
                    "title": cfg["web"].get("title", "KenTrackIt"),
                    "map": cfg["map"],
                    "stale_after_s": cfg["display"]["stale_after_s"],
                    "lost_after_s": cfg["display"]["lost_after_s"],
                    "speed_units": cfg["display"]["speed_units"],
                    "tiles": bool(app.tiles.upstream) or os.path.isdir(app.tiles.dir),
                    "attribution": cfg["tiles"].get("attribution", ""),
                    "max_zoom": app.tiles.max_zoom,
                    "now": app.now(),
                })
            if p == "/api/units":
                return self._json(app.store.units())
            if p == "/api/track":
                unit = qs.get("unit", [""])[0]
                t0, t1 = self._range(qs)
                return self._json(app.store.track(unit, t0, t1))
            if p == "/api/events":
                return self._json(app.store.events(int(qs.get("limit", [100])[0])))
            if p == "/api/export.csv":
                return self._export(qs)
            if p == "/api/stream":
                return self._stream()
            m = TILE_RE.match(p)
            if m:
                data = app.tiles.get(*map(int, m.groups()))
                if data is None:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, _sniff(data), {"Cache-Control": "max-age=604800"})
            self._send(404, "Not found", "text/plain")

        def do_POST(self):
            if not self._authorised():
                return
            m = UNIT_RE.match(urlparse(self.path).path)
            if not m:
                return self._send(404, "Not found", "text/plain")
            n = min(int(self.headers.get("Content-Length", 0)), 10000)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            u = app.store.set_unit(m.group(1), body.get("alias"), body.get("colour"),
                                   body.get("notes"))
            app.broker.publish("unit", u)
            self._json(u)

        def _static(self, rel):
            path = os.path.normpath(os.path.join(STATIC, rel))
            if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
                return self._send(404, "Not found", "text/plain")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype, {"Cache-Control": "no-cache"})

        def _export(self, qs):
            unit = qs.get("unit", [""])[0] or None
            t0, t1 = self._range(qs)
            rows = app.store.positions_for_export(unit, t0, t1)
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["unit", "received_utc", "gps_time_utc", "lat", "lon",
                        "speed_kmh", "course", "valid", "raw"])
            iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else ""
            for r in rows:
                kmh = round(r["speed_kn"] * 1.852, 1) if r["speed_kn"] is not None else ""
                w.writerow([r["unit_id"], iso(r["rx_time"]), iso(r["fix_time"]), r["lat"],
                            r["lon"], kmh, r["course"], r["valid"], r["raw"]])
            name = f"avl_{unit or 'all'}_{time.strftime('%Y%m%d_%H%M', time.localtime(t0))}.csv"
            self._send(200, out.getvalue(), "text/csv",
                       {"Content-Disposition": f'attachment; filename="{name}"'})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            q = app.broker.subscribe()
            try:
                while True:
                    try:
                        ev, data = q.get(timeout=15)
                        self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.broker.unsubscribe(q)

    return Handler


def serve(app):
    srv = ThreadingHTTPServer((app.cfg["web"]["host"], app.cfg["web"]["port"]), make_handler(app))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="web")
    t.start()
    return srv
