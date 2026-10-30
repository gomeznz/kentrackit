"""HTTP server: UI, JSON API, settings, live updates (Server-Sent Events), tile cache."""
import base64
import csv
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config as config_mod

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
    """Serves map tiles from disk and fetches/stores them when upstream is reachable.
    Each map source has its own cache folder, so switching sources never mixes tiles."""

    def __init__(self, cfg, data_dir):
        self.upstream, self.attribution, key = config_mod.tile_settings(cfg)
        self.root = os.path.join(data_dir, "tiles")
        self.dir = os.path.join(self.root, key)
        self.max_zoom = int(cfg["tiles"].get("max_zoom", 18))
        self.ua = "KenTrackIt/0.2 (Raspberry Pi AVL; single-site tile cache)"

    def enabled(self):
        return bool(self.upstream) or os.path.isdir(self.dir)

    def get(self, z, x, y):
        if z > self.max_zoom:
            return None
        path = os.path.join(self.dir, str(z), str(x), str(y))
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        if not self.upstream:
            return None
        url = self.upstream.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
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

    def usage(self):
        files = size = 0
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                try:
                    size += os.path.getsize(os.path.join(dirpath, n))
                    files += 1
                except OSError:
                    pass
        return {"files": files, "bytes": size}

    def clear(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ---- system information ------------------------------------------------------
def listening_ports():
    """TCP ports something on this Pi is listening on (any user)."""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh)
                for line in fh:
                    parts = line.split()
                    if parts[3] == "0A":                     # LISTEN
                        ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        except (OSError, StopIteration, IndexError, ValueError):
            pass
    return sorted(ports)


def serial_ports(current):
    """Serial devices the Pi can see, preferring stable /dev/serial/by-id names."""
    by_id = {}
    d = "/dev/serial/by-id"
    if os.path.isdir(d):
        for n in os.listdir(d):
            by_id[os.path.realpath(os.path.join(d, n))] = os.path.join(d, n)
    out = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device.startswith("/dev/ttyAMA") or p.device.startswith("/dev/ttyS"):
                if not by_id.get(p.device):
                    continue                                 # Pi's internal UARTs: usually not the radio
            out.append({"path": by_id.get(p.device, p.device), "device": p.device,
                        "description": p.description if p.description != "n/a" else "",
                        "manufacturer": p.manufacturer or "", "serial": p.serial_number or ""})
    except ImportError:
        pass
    if current and not any(current in (x["path"], x["device"]) for x in out):
        out.append({"path": current, "device": current, "description": "not currently connected",
                    "manufacturer": "", "serial": ""})
    return out


def port_free(host, port):
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---- HTTP handler ---------------------------------------------------------------
def make_handler(app):

    class Handler(BaseHTTPRequestHandler):
        server_version = "KenTrackIt"
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

        def _body(self):
            n = min(int(self.headers.get("Content-Length", 0) or 0), 100_000)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def _authorised(self):
            web = app.cfg["web"]
            if not web.get("password_hash"):
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr in app.auth_cache:
                return True
            if hdr.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                except ValueError:
                    user = pw = ""
                if user == web["username"] and config_mod.check_password(pw, web["password_hash"]):
                    if len(app.auth_cache) > 50:
                        app.auth_cache.clear()
                    app.auth_cache.add(hdr)
                    return True
            self._send(401, "Login required", "text/plain",
                       {"WWW-Authenticate": f'Basic realm="{web["title"]}"'})
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
            cfg = app.cfg

            if p == "/":
                return self._static("index.html")
            if p in ("/settings", "/settings/"):
                return self._static("settings.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/config":
                return self._json({
                    "title": cfg["web"]["title"],
                    "map": cfg["map"],
                    "stale_after_s": cfg["display"]["stale_after_s"],
                    "lost_after_s": cfg["display"]["lost_after_s"],
                    "speed_units": cfg["display"]["speed_units"],
                    "tiles": app.tiles.enabled(),
                    "attribution": app.tiles.attribution,
                    "max_zoom": app.tiles.max_zoom,
                    "now": app.now(),
                })
            if p == "/api/settings":
                return self._json(config_mod.public(cfg))
            if p == "/api/system":
                return self._json(self._system())
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
            if p == "/api/backup.db":
                return self._backup()
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
            p = urlparse(self.path).path
            body = self._body()
            if body is None:
                return self._json({"error": "Bad request"}, 400)

            if p == "/api/settings":
                try:
                    new, notes = app.update_settings(body)
                except config_mod.Invalid as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "notes": notes, "settings": config_mod.public(new)})
            if p == "/api/check-port":
                host = str(body.get("host", app.cfg["web"]["host"]))
                port = int(body.get("port", 0) or 0)
                mine = port == app.cfg["web"]["port"]
                return self._json({"port": port, "free": mine or port_free(host, port), "mine": mine})
            if p == "/api/tiles/clear":
                app.tiles.clear()
                return self._json({"ok": True})
            if p == "/api/restart":
                self._json({"ok": True})
                app.restart_requested = True
                threading.Timer(0.5, app.stop.set).start()
                return
            m = UNIT_RE.match(p)
            if m:
                u = app.store.set_unit(m.group(1), body.get("alias"), body.get("colour"),
                                       body.get("notes"))
                app.broker.publish("unit", u)
                return self._json(u)
            self._send(404, "Not found", "text/plain")

        def do_DELETE(self):
            if not self._authorised():
                return
            m = UNIT_RE.match(urlparse(self.path).path)
            if not m:
                return self._send(404, "Not found", "text/plain")
            app.store.delete_unit(m.group(1))
            app.store.add_event("settings", f"Unit {m.group(1)} and its history deleted")
            app.broker.publish("unit_removed", {"id": m.group(1)})
            self._json({"ok": True})

        # ---- pieces ---------------------------------------------------
        def _system(self):
            cfg = app.cfg
            db = os.path.join(cfg["data_dir"], "avl.db")
            try:
                db_bytes = sum(os.path.getsize(db + s) for s in ("", "-wal") if os.path.exists(db + s))
            except OSError:
                db_bytes = None
            st = dict(app.stats)
            st.update({
                "version": app.VERSION,
                "mode": app.mode,
                "now": app.now(),
                "clock_offset_s": round(app.clock_offset, 1),
                "uptime_s": round(time.time() - st["started"]),
                "hostname": socket.gethostname(),
                "config_path": app.config_path,
                "config_writable": bool(app.config_path) and os.access(
                    app.config_path if os.path.exists(app.config_path)
                    else os.path.dirname(os.path.abspath(app.config_path)), os.W_OK),
                "data_dir": os.path.abspath(cfg["data_dir"]),
                "db_bytes": db_bytes,
                "store": app.store.counts(),
                "tiles": app.tiles.usage(),
                "listening_ports": listening_ports(),
                "serial_ports": serial_ports(cfg["serial"]["port"]),
                "bauds": config_mod.BAUDS,
            })
            try:
                du = shutil.disk_usage(cfg["data_dir"])
                st["disk"] = {"free": du.free, "total": du.total}
            except OSError:
                pass
            return st

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

        def _backup(self):
            fd, tmp = tempfile.mkstemp(suffix=".db", dir=app.cfg["data_dir"])
            os.close(fd)
            try:
                app.store.backup_to(tmp)
                with open(tmp, "rb") as f:
                    data = f.read()
            finally:
                os.unlink(tmp)
            name = f"kentrackit_{time.strftime('%Y%m%d_%H%M')}.db"
            self._send(200, data, "application/octet-stream",
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
                while not app.stop.is_set():
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


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


def _start(app, host, port):
    srv = _Server((host, port), make_handler(app))     # raises OSError if the port is taken
    threading.Thread(target=srv.serve_forever, daemon=True, name=f"web:{port}").start()
    return srv


def serve(app):
    srv = _start(app, app.cfg["web"]["host"], app.cfg["web"]["port"])
    app.servers.append(srv)
    return srv


def rebind(app, new_cfg):
    """Start listening on the new address before the old one is closed.
    Raises config.Invalid if something else already has the port."""
    host, port = new_cfg["web"]["host"], new_cfg["web"]["port"]
    try:
        return _start(app, host, port)
    except OSError:
        raise config_mod.Invalid(f"Port {port} is already in use on this Pi "
                                 f"(probably the other service). Pick another.")
