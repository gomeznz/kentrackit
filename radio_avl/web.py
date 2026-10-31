"""HTTP server: UI, JSON API, settings, live updates (Server-Sent Events), tile cache."""
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
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs, quote

from . import config as config_mod
from . import geo, reports, voice as voice_mod, lcd as lcd_mod
from .auth import AuthError, ROLES, REMEMBER_S

STATIC = os.path.join(os.path.dirname(__file__), "static")
TILE_RE = re.compile(r"^/tiles/(sat/)?(\d{1,2})/(\d{1,7})/(\d{1,7})$")
_WN = {"mtime": None, "releases": []}


def _releases():
    """Release notes from whatsnew.json (re-read if the file changes)."""
    path = os.path.join(os.path.dirname(__file__), "whatsnew.json")
    try:
        mt = os.path.getmtime(path)
        if mt != _WN["mtime"]:
            with open(path, encoding="utf-8") as f:
                _WN["releases"], _WN["mtime"] = json.load(f)["releases"], mt
    except (OSError, ValueError, KeyError):
        pass
    return _WN["releases"]


def whats_new(user, show_all=False):
    """The releases this user hasn't seen yet (or all of them), with items they can't use left out.
    Someone who's never seen any gets just the latest, not the whole history."""
    rel = _releases()
    if not rel:
        return {"latest": None, "releases": []}
    seen = user.get("whats_new_seen") or ""
    versions = [r["version"] for r in rel]
    if show_all:
        pick = rel
    elif seen == versions[0]:
        pick = []
    elif seen in versions:
        pick = rel[:versions.index(seen)]
    else:
        pick = rel[:1]
    perms = set(user["perms"])
    out = []
    for r in pick:
        items = [i for i in r.get("items", []) if not i.get("perm") or i["perm"] in perms]
        if items:
            out.append({**{k: r.get(k) for k in ("version", "date", "title")}, "items": items})
    return {"latest": versions[0], "releases": out}


class _SafeCSV:
    """csv.writer that stops text like =HYPERLINK(...) (a radio name, a zone name) running as a
    formula when the file is opened in Excel. Numbers are left alone."""
    def __init__(self, f):
        self.w = csv.writer(f)

    def writerow(self, row):
        self.w.writerow(["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
                         for v in row])


UNIT_RE = re.compile(r"^/api/units/([A-Za-z0-9_.-]{1,32})$")
FENCE_RE = re.compile(r"^/api/geofences/(\d{1,9})$")
ACK_RE = re.compile(r"^/api/alerts/(\d{1,12})/ack$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
USER_RE = re.compile(r"^/api/users/(\d{1,9})$")
REC_RE = re.compile(r"^/api/recordings/(\d{1,12})$")
POLL_RE = re.compile(r"^/api/units/([A-Za-z0-9_.-]{1,32})/poll$")
CAPTURE_RE = re.compile(r"^/api/captures/(capture_\d{8}_\d{6}\.log)$")
COOKIE = "kt_session"
PUBLIC = {"/login", "/login/", "/setup", "/setup/", "/api/login", "/api/setup", "/api/auth-status"}
PRIVATE_EVENTS = {"audit", "settings"}       # only administrators see these in the activity log


def _safe_next(qs):
    """Only redirect back to a path on this site after sign-in."""
    n = qs.get("next", ["/"])[0]
    return n if n.startswith("/") and not n.startswith("//") else "/"


def _visible(user):
    """Filter for the live event stream: hide units this user isn't allowed to see."""
    if user["units"] is None:
        return None
    allowed = set(user["units"])

    def ok(event, data):
        if event in ("position",):
            return data["unit"]["id"] in allowed
        if event in ("unit", "unit_removed"):
            return data["id"] in allowed
        if event == "alert":
            return data.get("unit_id") is None or data["unit_id"] in allowed
        if event == "tx":
            return data.get("unit_id") is None or data["unit_id"] in allowed
        if event == "poll":
            return data.get("unit") in allowed
        if event == "recording":
            return data.get("unit_id") is None or data["unit_id"] in allowed
        return True
    return ok


class Broker:
    """Fan-out of live events to connected browsers."""

    def __init__(self):
        self.subs = {}                 # queue -> filter function (or None)
        self.lock = threading.Lock()
        self.state = {}          # last value of each sticky event (channel, link, display)

    def subscribe(self, allow=None):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs[q] = allow
            for ev, data in self.state.items():
                q.put_nowait((ev, data))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.pop(q, None)

    def publish(self, event, data, sticky=False):
        payload = json.dumps(data)
        with self.lock:
            if sticky:
                self.state[event] = payload
            subs = list(self.subs.items())
        for q, allow in subs:
            if allow and not allow(event, data):
                continue
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

    def __init__(self, cfg, data_dir, layer="map"):
        if layer == "sat":
            self.upstream, self.attribution, key, self.max_zoom = config_mod.sat_settings(cfg)
        else:
            self.upstream, self.attribution, key = config_mod.tile_settings(cfg)
            self.max_zoom = int(cfg["tiles"].get("max_zoom", 18))
        self.root = os.path.join(data_dir, "tiles")
        self.dir = os.path.join(self.root, key)
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
        sys_version = ""          # don't advertise the Python version
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---- helpers --------------------------------------------------
        def end_headers(self):
            # on every response, including files, tiles and the live stream
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy",
                             "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
            super().end_headers()

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

        # ---- identity and access ---------------------------------------
        def _ip(self):
            return self.client_address[0]

        def _token(self):
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            m = c.get(COOKIE)
            return m.value if m else None

        def _identify(self, allow_basic=False):
            """Session cookie first. HTTP Basic is only for scripts calling the API: browsers keep
            sending remembered Basic credentials after sign-out, so pages never accept it."""
            self.user, self.token = None, None
            tok = self._token()
            if tok:
                u = app.auth.session_user(tok)
                if u:
                    self.user, self.token = u, tok
                    return
            hdr = self.headers.get("Authorization", "")
            if allow_basic and hdr.startswith("Basic ") and not self._token():
                self.user = app.auth.basic_user(hdr, self._ip())

        def _redirect(self, where, headers=None):
            h = {"Location": where}
            h.update(headers or {})
            self._send(302, b"", "text/plain", h)

        def _gate(self, p):
            """Sign-in check. Returns True if the request may continue."""
            self.user = None
            if p.startswith("/static/") or p in PUBLIC:
                return True
            if app.auth.needs_setup():
                if p.startswith(("/api/", "/tiles/")):
                    self._json({"error": "First-time setup needed", "setup": True}, 403)
                else:
                    self._redirect("/setup")
                return False
            self._identify(allow_basic=p.startswith(("/api/", "/tiles/")))
            if self.user:
                return True
            if p.startswith(("/api/", "/tiles/")):
                self._json({"error": "Sign in required"}, 401)
            else:
                self._redirect("/login?next=" + quote(self.path, safe=""))
            return False

        def _need(self, perm):
            if app.auth.can(self.user, perm):
                return True
            self._json({"error": "You don't have permission to do that"}, 403)
            return False

        def _same_origin(self):
            """Browsers send Origin on POST/DELETE; refuse requests from other sites."""
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return origin is None
            # Behind a reverse proxy or Cloudflare Tunnel the Host header may be the local address
            # (e.g. localhost:8090) while the browser is on the public hostname, so also accept
            # the host the proxy says it forwarded. Session cookies are SameSite=Strict as well.
            o = urlparse(origin).netloc.lower()
            hosts = {self.headers.get("Host", "").lower()}
            hosts.update(h.strip().lower() for h in self.headers.get("X-Forwarded-Host", "").split(",") if h.strip())
            return o in hosts or o.split(":")[0] in {h.split(":")[0] for h in hosts if h}

        def _cookie(self, token, max_age=None):
            parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if max_age is not None:
                parts.append(f"Max-Age={max_age}")
            if self.headers.get("X-Forwarded-Proto", "") == "https":
                parts.append("Secure")
            return "; ".join(parts)

        def _who(self):
            u = self.user
            return (u["display_name"] or u["username"]) if u else "-"

        def _sees(self, unit_id):
            return app.auth.sees(self.user, unit_id)

        def _allowed_units(self):
            return None if self.user["units"] is None else set(self.user["units"])

        def _range(self, qs):
            now = app.now()
            t1 = float(qs.get("to", [now])[0])
            t0 = float(qs.get("from", [t1 - 3600])[0])
            return t0, t1

        # ---- routes ---------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = u.path
            if not self._gate(p):
                return
            cfg = app.cfg

            # pages
            if p in ("/login", "/login/"):
                self._identify()
                if self.user:
                    return self._redirect(_safe_next(qs))
                return self._static("login.html")
            if p in ("/setup", "/setup/"):
                return self._static("setup.html") if app.auth.needs_setup() else self._redirect("/")
            if p == "/":
                return self._static("index.html")
            if p in ("/settings", "/settings/"):
                return self._static("settings.html") if app.auth.can(self.user, "settings") \
                    else self._redirect("/account")
            if p in ("/reports", "/reports/"):
                return self._static("reports.html") if self._need("reports") else None
            if p in ("/leaderboard", "/leaderboard/"):
                return self._static("leaderboard.html") if self._need("reports") else None
            if p in ("/channel", "/channel/"):
                return self._static("channel.html") if self._need("reports") else None
            if p in ("/account", "/account/"):
                return self._static("account.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])

            # identity
            if p == "/api/auth-status":
                return self._json({"title": cfg["web"]["title"], "setup": app.auth.needs_setup()})
            if p == "/api/me":
                return self._json(app.auth.public(self.user))
            if p == "/api/whatsnew":
                return self._json(whats_new(self.user, qs.get("all", ["0"])[0] == "1"))

            if not self._need("view"):
                return
            if p == "/api/config":
                return self._json({
                    "title": cfg["web"]["title"],
                    "map": cfg["map"],
                    "stale_after_s": cfg["display"]["stale_after_s"],
                    "lost_after_s": cfg["display"]["lost_after_s"],
                    "speed_units": cfg["display"]["speed_units"],
                    "trail_min": cfg["display"]["trail_min"],
                    "reports": cfg["reports"],
                    "polling_mode": cfg["polling"]["mode"],
                    "recording": cfg["recording"]["enabled"],
                    "tiles": app.tiles.enabled(),
                    "attribution": app.tiles.attribution,
                    "max_zoom": app.tiles.max_zoom,
                    "satellite": app.sat_tiles.enabled(),
                    "sat_attribution": app.sat_tiles.attribution,
                    "sat_max_zoom": app.sat_tiles.max_zoom,
                    "now": app.now(),
                    "user": app.auth.public(self.user),
                })
            if p == "/api/units":
                zones = app.alerts.unit_fences()
                return self._json([dict(x, state={"fences": zones.get(x["id"], [])})
                                   for x in app.store.units() if self._sees(x["id"])])
            if p == "/api/track":
                unit = qs.get("unit", [""])[0]
                if not self._sees(unit):
                    return self._json({"error": "You don't have access to that unit"}, 403)
                t0, t1 = self._range(qs)
                return self._json(app.store.track(unit, t0, t1))
            if p == "/api/geofences":
                return self._json(app.store.geofences())
            if p == "/api/alerts":
                open_only = qs.get("open", ["0"])[0] == "1"
                rows = app.store.alerts(open_only, int(qs.get("limit", [200])[0]) * 3)
                return self._json([a for a in rows if self._sees(a["unit_id"])][:int(qs.get("limit", [200])[0])])
            if p == "/api/trails":
                mins = min(int(qs.get("minutes", [cfg["display"]["trail_min"]])[0]), 1440)
                t1 = app.now()
                out = {}
                for x in app.store.units():
                    if not self._sees(x["id"]):
                        continue
                    pts = app.store.track(x["id"], t1 - mins * 60, t1)
                    if pts:
                        out[x["id"]] = [[q["lat"], q["lon"], q["rx_time"]] for q in pts]
                return self._json(out)
            if p == "/api/events":
                admin = app.auth.can(self.user, "settings")
                rows = app.store.events(int(qs.get("limit", [100])[0]) * 3)
                rows = [e for e in rows if (admin or e["kind"] not in PRIVATE_EVENTS) and self._sees(e["unit_id"])]
                return self._json(rows[:int(qs.get("limit", [100])[0])])
            if p == "/api/stream":
                return self._stream()
            if p == "/api/coverage":
                t0, t1 = self._range(qs)
                cell = max(50, min(int(qs.get("cell", [200])[0]), 2000))
                src = set(qs.get("sources", [""])[0].split(",")) - {""} or None
                return self._json(app.coverage.grid(t0, t1, cell, self._allowed_units(), src))
            if p == "/api/poller":
                st = app.poller.status()
                if self.user["units"] is not None:
                    st["missed_by_unit"] = {k: v for k, v in st["missed_by_unit"].items() if self._sees(k)}
                return self._json(st)
            m = TILE_RE.match(p)
            if m:
                cache = app.sat_tiles if m.group(1) else app.tiles
                data = cache.get(*map(int, m.groups()[1:]))
                if data is None:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, _sniff(data), {"Cache-Control": "max-age=604800"})

            if p == "/api/report":
                if not self._need("reports"):
                    return
                t0, t1 = self._range(qs)
                unit = qs.get("unit", [""])[0] or None
                if unit and not self._sees(unit):
                    return self._json({"error": "You don't have access to that unit"}, 403)
                return self._json(reports.fleet_report(app.store, t0, t1, cfg, unit, self._allowed_units()))
            if p == "/api/channel/stats":
                if not self._need("reports"):
                    return
                t0, t1 = self._range(qs)
                bucket = max(60, min(int(qs.get("bucket", [3600])[0]), 86400))
                tz = int(qs.get("tz", [0])[0])
                st = app.channel.stats(t0, t1, bucket, tz)
                allowed = self._allowed_units()
                if allowed is not None:
                    st["per_unit"] = {k: v for k, v in st["per_unit"].items() if k in allowed}
                st["coverage"] = {k: v for k, v in app.coverage.unit_summary(t0, t1).items() if self._sees(k)}
                st["poller"] = {k: v for k, v in app.poller.status().items() if k in ("mode", "sent", "answered", "missed", "dry_run")}
                return self._json(st)
            if p == "/api/channel/log":
                if not self._need("reports"):
                    return
                t0, t1 = self._range(qs)
                return self._json(app.channel.log(t0, t1, qs.get("unit", [""])[0] or None, qs.get("kind", [""])[0] or None,
                                                  min(int(qs.get("limit", [500])[0]), 5000), self._allowed_units(),
                                                  with_audio=qs.get("audio", ["0"])[0] == "1"))
            m = REC_RE.match(p)
            if m:
                if not self._need("listen"):
                    return
                tx = app.channel.get(int(m.group(1)))
                if not tx or not tx["audio"] or not self._sees(tx["unit_id"]):
                    return self._json({"error": "No recording"}, 404)
                path = app.voice.file_for(tx["audio"])
                if not path:
                    return self._json({"error": "The recording file is missing"}, 404)
                if "Range" not in self.headers or self.headers.get("Range", "").startswith("bytes=0-"):
                    app.auth.audit(self.user, f"played the recording of {tx['unit_id'] or 'unknown unit'} at "
                                              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tx['start']))}")
                name = f"{tx['unit_id'] or 'call'}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(tx['start']))}{os.path.splitext(path)[1]}"
                return self._file(path, "audio/ogg" if path.endswith(".opus") else "audio/wav",
                                  qs.get("download", ["0"])[0] == "1" and name)
            if p == "/api/lcd":
                if not self._need("settings"):
                    return
                out = {"status": app.panel.info(), "buses": lcd_mod.buses()}
                if qs.get("scan"):
                    bus = int(qs.get("bus", [cfg["lcd"]["bus"]])[0])
                    found, err = lcd_mod.scan(bus)
                    if app.panel.lcd is not None and bus == cfg["lcd"]["bus"] and cfg["lcd"]["address"] not in found:
                        found.append(cfg["lcd"]["address"])     # in use by us right now
                    out["scan"] = {"bus": bus, "found": [f"0x{a:02X}" for a in sorted(found)], "error": err}
                return self._json(out)
            if p == "/api/voice":
                if not self._need("settings"):
                    return
                return self._json({"status": app.voice.info(), "devices": voice_mod.list_devices(),
                                   "usage": app.voice.usage()})
            if p == "/api/channel/log.csv":
                if not self._need("export"):
                    return
                t0, t1 = self._range(qs)
                rows = app.channel.log(t0, t1, None, None, 100000, self._allowed_units())
                out = io.StringIO(); w = _SafeCSV(out)
                w.writerow(["start_local", "duration_s", "type", "unit", "display"])
                for r in reversed(rows):
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["start"])), r["duration"],
                                r["kind"], r["unit_id"] or "", r["display"] or ""])
                return self._send(200, out.getvalue(), "text/csv",
                                  {"Content-Disposition": f'attachment; filename="channel_log_{time.strftime("%Y%m%d", time.localtime(t0))}.csv"'})
            if p == "/api/leaderboard":
                if not self._need("reports"):
                    return
                t0, t1 = self._range(qs)
                return self._json(reports.leaderboard(app, t0, t1, self._allowed_units()))
            if p == "/api/report.csv":
                return self._report_csv(qs) if self._need("export") else None
            if p == "/api/export.csv":
                return self._export(qs) if self._need("export") else None

            if p == "/api/settings":
                return self._json(config_mod.public(cfg)) if self._need("settings") else None
            if p == "/api/system":
                return self._json(self._system()) if self._need("settings") else None
            if p == "/api/backup.db":
                if not self._need("backup"):
                    return
                app.auth.audit(self.user, "downloaded a database backup")
                return self._backup()
            if p == "/api/users":
                if not self._need("users"):
                    return
                return self._json({"users": [app.auth.public(x) for x in app.auth.users()],
                                   "roles": {k: v["label"] for k, v in ROLES.items()},
                                   "units": [{"id": x["id"], "alias": x["alias"]} for x in app.store.units()]})
            if p == "/api/captures":
                if not self._need("settings"):
                    return
                return self._json({"status": app.recorder.status(), "files": app.recorder.files()})
            m = CAPTURE_RE.match(p)
            if m:
                if not self._need("settings"):
                    return
                path = app.recorder.path(m.group(1))
                if not path:
                    return self._send(404, "Not found", "text/plain")
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "text/plain",
                                      {"Content-Disposition": f'attachment; filename="{m.group(1)}"'})
            if p == "/api/audit":
                if not self._need("users"):
                    return
                rows = app.store.events(2000)
                return self._json([e for e in rows if e["kind"] in ("audit", "settings")][:int(qs.get("limit", [300])[0])])
            self._send(404, "Not found", "text/plain")

        def do_POST(self):
            p = urlparse(self.path).path
            if not self._same_origin():
                return self._json({"error": "Cross-site request refused"}, 403)
            if not self._gate(p):
                return
            body = self._body()
            if body is None:
                return self._json({"error": "Bad request"}, 400)

            # ---- sign in / out / first-time setup
            if p == "/api/login":
                try:
                    token, u = app.auth.login(body.get("username", ""), body.get("password", ""),
                                              bool(body.get("remember")), self._ip(),
                                              self.headers.get("User-Agent", ""))
                except AuthError as e:
                    time.sleep(0.5)
                    return self._json({"error": str(e)}, 401)
                age = REMEMBER_S if body.get("remember") else None
                self.send_response(200)
                out = json.dumps({"ok": True, "user": app.auth.public(u)}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Set-Cookie", self._cookie(token, age))
                self.end_headers()
                self.wfile.write(out)
                return
            if p == "/api/setup":
                if not app.auth.needs_setup():
                    return self._json({"error": "Setup has already been done"}, 409)
                try:
                    u = app.auth.save_user({"username": body.get("username"), "display_name": body.get("display_name", ""),
                                            "password": body.get("password"), "role": "admin"})
                except AuthError as e:
                    return self._json({"error": str(e)}, 400)
                app.auth.audit(u, "created the first administrator account")
                return self._json({"ok": True})
            if p == "/api/logout":
                self._identify()
                if self.token:
                    app.auth.logout(self.token)
                    app.auth.audit(self.user, "signed out")
                return self._send(200, b'{"ok": true}', "application/json",
                                  {"Set-Cookie": self._cookie("", 0)})
            if p == "/api/whatsnew/seen":
                v = str(body.get("version", ""))
                if v not in [r["version"] for r in _releases()]:
                    return self._json({"error": "Unknown version"}, 400)
                app.auth.set_whats_new_seen(self.user["id"], v)
                return self._json({"ok": True})
            if p == "/api/me/password":
                try:
                    app.auth.change_own_password(self.user, body.get("current", ""), body.get("new", ""), self.token)
                except AuthError as e:
                    return self._json({"error": str(e)}, 400)
                app.auth.audit(self.user, "changed their password")
                return self._json({"ok": True})

            # ---- users (admin)
            if p == "/api/users" or USER_RE.match(p):
                if not self._need("users"):
                    return
                m = USER_RE.match(p)
                try:
                    u = app.auth.save_user(body, int(m.group(1)) if m else None, self.user)
                except AuthError as e:
                    return self._json({"error": str(e)}, 400)
                what = [k for k in ("role", "units", "enabled", "display_name", "username") if k in body]
                if body.get("password"):
                    what.append("password")
                app.auth.audit(self.user, (f"updated user {u['username']} ({', '.join(what)})" if m
                                           else f"created user {u['username']} ({u['role']})"))
                return self._json(app.auth.public(u))

            # ---- alerts
            m = ACK_RE.match(p)
            if m or p == "/api/alerts/ack-all":
                if not self._need("ack"):
                    return
                visible = [a["id"] for a in app.store.alerts(True, 5000) if self._sees(a["unit_id"])]
                ids = [i for i in ([int(m.group(1))] if m else visible) if i in visible]
                return self._ack(ids, body.get("note", ""))
            if p == "/api/alerts/test":
                if not self._need("settings"):
                    return
                app.auth.audit(self.user, "raised a test emergency")
                return self._json(app.alerts.raise_alert("test", f"TEST: emergency alarm test by {self._who()}"))

            # ---- poll a unit now
            m = POLL_RE.match(p)
            if m:
                if not self._need("poll"):
                    return
                if not self._sees(m.group(1)):
                    return self._json({"error": "You don't have access to that unit"}, 403)
                if app.cfg["polling"]["mode"] == "off":
                    return self._json({"error": "Polling is switched off (Settings → Polling)"}, 409)
                app.poller.request(m.group(1))
                app.auth.audit(self.user, f"polled {m.group(1)}")
                return self._json({"ok": True, "mode": app.cfg["polling"]["mode"]})

            # ---- zones
            if p == "/api/geofences" or FENCE_RE.match(p):
                if not self._need("zones"):
                    return
                m = FENCE_RE.match(p)
                return self._save_fence(body, int(m.group(1)) if m else None)

            # ---- units
            m = UNIT_RE.match(p)
            if m:
                if not self._need("units"):
                    return
                if not self._sees(m.group(1)):
                    return self._json({"error": "You don't have access to that unit"}, 403)
                colour = body.get("colour")
                if colour is not None and colour != "" and not HEX_RE.match(str(colour)):
                    return self._json({"error": "Colour must look like #2563eb"}, 400)
                u = app.store.set_unit(m.group(1), body.get("alias"), colour, body.get("notes"))
                app.auth.audit(self.user, f"updated unit {m.group(1)} (name '{u['alias']}')")
                app.broker.publish("unit", u)
                return self._json(u)

            # ---- system settings (admin)
            if not self._need("settings"):
                return
            if p == "/api/settings":
                try:
                    new, notes = app.update_settings(body)
                except config_mod.Invalid as e:
                    return self._json({"error": str(e)}, 400)
                app.auth.audit(self.user, "changed settings: " + ", ".join(sorted(body.keys())))
                return self._json({"ok": True, "notes": notes, "settings": config_mod.public(new)})
            if p == "/api/check-port":
                host = str(body.get("host", app.cfg["web"]["host"]))
                port = int(body.get("port", 0) or 0)
                mine = port == app.cfg["web"]["port"]
                return self._json({"port": port, "free": mine or port_free(host, port), "mine": mine})
            if p == "/api/captures/start":
                name = app.recorder.start(body.get("minutes", 10), self._who())
                app.auth.audit(self.user, f"started a raw serial capture ({name})")
                return self._json({"ok": True, "name": name, "status": app.recorder.status()})
            if p == "/api/captures/stop":
                app.recorder.stop()
                return self._json({"ok": True, "status": app.recorder.status()})
            if p == "/api/tiles/clear":
                app.tiles.clear()
                app.auth.audit(self.user, "cleared the map tile cache")
                return self._json({"ok": True})
            if p == "/api/restart":
                app.auth.audit(self.user, "restarted the service")
                self._json({"ok": True})
                app.restart_requested = True
                threading.Timer(0.5, app.stop.set).start()
                return
            self._send(404, "Not found", "text/plain")

        def do_DELETE(self):
            p = urlparse(self.path).path
            if not self._same_origin():
                return self._json({"error": "Cross-site request refused"}, 403)
            if not self._gate(p):
                return
            m = FENCE_RE.match(p)
            if m:
                if not self._need("zones"):
                    return
                f = next((z for z in app.store.geofences() if z["id"] == int(m.group(1))), None)
                app.store.delete_geofence(int(m.group(1)))
                app.alerts.reload_fences()
                app.auth.audit(self.user, f"deleted zone {f['name'] if f else m.group(1)}")
                app.broker.publish("geofences", app.store.geofences())
                return self._json({"ok": True})
            m = CAPTURE_RE.match(p)
            if m:
                if not self._need("settings"):
                    return
                return self._json({"ok": app.recorder.delete(m.group(1))})
            m = USER_RE.match(p)
            if m:
                if not self._need("users"):
                    return
                try:
                    u = app.auth.delete_user(int(m.group(1)), self.user)
                except AuthError as e:
                    return self._json({"error": str(e)}, 400)
                app.auth.audit(self.user, f"deleted user {u['username']}")
                return self._json({"ok": True})
            m = UNIT_RE.match(p)
            if m:
                if not self._need("settings"):
                    return
                app.store.delete_unit(m.group(1))
                app.auth.audit(self.user, f"deleted unit {m.group(1)} and its history")
                app.broker.publish("unit_removed", {"id": m.group(1)})
                return self._json({"ok": True})
            self._send(404, "Not found", "text/plain")

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
                "poller": app.poller.status(),
                "voice": app.voice.info(),
            })
            try:
                du = shutil.disk_usage(cfg["data_dir"])
                st["disk"] = {"free": du.free, "total": du.total}
            except OSError:
                pass
            return st

        def _save_fence(self, body, fid):
            name = str(body.get("name", "")).strip()[:60]
            shape = body.get("shape")
            g = geo.valid_geometry(shape, body.get("geometry") or {})
            if not name:
                return self._json({"error": "Give the zone a name"}, 400)
            if not g:
                return self._json({"error": "Zone shape isn't valid (circle 5 m-100 km, or 3+ points)"}, 400)
            colour = body.get("colour", "#2563eb")
            f = {"name": name, "shape": shape, "geometry": g,
                 "colour": colour if HEX_RE.match(str(colour)) else "#2563eb",
                 "alert_enter": bool(body.get("alert_enter", True)),
                 "alert_exit": bool(body.get("alert_exit", True)),
                 "active": bool(body.get("active", True))}
            saved = app.store.save_geofence(f, fid)
            if not saved:
                return self._json({"error": "No such zone"}, 404)
            app.alerts.reload_fences()
            app.auth.audit(self.user, f"{'updated' if fid else 'created'} zone {saved['name']}")
            app.broker.publish("geofences", app.store.geofences())
            self._json(saved)

        def _ack(self, ids, note):
            note = (str(note)[:150] or "Acknowledged") + f" by {self._who()}"
            done = app.store.ack_alerts(ids, note, t=app.now()) if ids else []
            if done:
                app.auth.audit(self.user, f"acknowledged {len(done)} alert{'s' if len(done) > 1 else ''}")
                app.broker.publish("alert_ack", {"ids": done, "note": note})
            self._json({"ok": True, "ids": done})

        def _report_csv(self, qs):
            t0, t1 = self._range(qs)
            rep = reports.fleet_report(app.store, t0, t1, app.cfg, None, self._allowed_units())
            out = io.StringIO()
            w = _SafeCSV(out)
            loc = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
            hm = lambda s: f"{int(s // 3600)}:{int(s % 3600 // 60):02d}"
            w.writerow(["unit", "name", "first_report", "last_report", "distance_km", "moving_h:mm",
                        "stopped_h:mm", "stops", "trips", "max_kmh"])
            for r in rep["units"]:
                w.writerow([r["unit"], r["name"], loc(r["first"]), loc(r["last"]), r["distance_km"],
                            hm(r["moving_s"]), hm(r["stopped_s"]), len(r["stops"]), len(r["trips"]),
                            r["max_kmh"]])
            w.writerow([])
            w.writerow(["unit", "name", "type", "start", "end", "duration_h:mm", "km", "max_kmh",
                        "location / from", "to", "lat", "lon"])
            for r in rep["units"]:
                full = reports.unit_report(app.store, {"id": r["unit"], "alias": r["name"]}, t0, t1,
                                           app.store.geofences(), app.cfg, with_track=False)
                items = [("stop", s) for s in full["stops"]] + [("trip", x) for x in full["trips"]]
                for kind, x in sorted(items, key=lambda i: i[1]["start"]):
                    if kind == "stop":
                        w.writerow([r["unit"], r["name"], "stop", loc(x["start"]), loc(x["end"]),
                                    hm(x["duration_s"]), "", "", x["zone"], "", x["lat"], x["lon"]])
                    else:
                        w.writerow([r["unit"], r["name"], "trip", loc(x["start"]), loc(x["end"]),
                                    hm(x["duration_s"]), x["km"], x["max_kmh"], x["from_zone"],
                                    x["to_zone"], "", ""])
            name = f"kentrackit_report_{time.strftime('%Y%m%d', time.localtime(t0))}.csv"
            self._send(200, out.getvalue(), "text/csv",
                       {"Content-Disposition": f'attachment; filename="{name}"'})

        def _file(self, path, ctype, download_name=None):
            size = os.path.getsize(path)
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            mr = re.match(r"bytes=(\d*)-(\d*)$", rng)
            partial = bool(mr) and (mr.group(1) or mr.group(2))
            if partial:
                if mr.group(1):
                    start = int(mr.group(1)); end = int(mr.group(2)) if mr.group(2) else size - 1
                else:
                    start = max(0, size - int(mr.group(2)))
                end = min(end, size - 1)
                if start > end:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            h = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
            if partial:
                h["Content-Range"] = f"bytes {start}-{end}/{size}"
            if download_name:
                h["Content-Disposition"] = f'attachment; filename="{download_name}"'
            self._send(206 if partial else 200, data, ctype, h)

        def _static(self, rel):
            path = os.path.normpath(os.path.join(STATIC, rel))
            if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
                return self._send(404, "Not found", "text/plain")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype, {"Cache-Control": "no-cache"})

        def _export(self, qs):
            unit = qs.get("unit", [""])[0] or None
            if unit and not self._sees(unit):
                return self._json({"error": "You don't have access to that unit"}, 403)
            t0, t1 = self._range(qs)
            rows = [r for r in app.store.positions_for_export(unit, t0, t1) if self._sees(r["unit_id"])]
            out = io.StringIO()
            w = _SafeCSV(out)
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
            q = app.broker.subscribe(_visible(self.user))
            try:
                while not app.stop.is_set():
                    try:
                        ev, data = q.get(timeout=15)
                        self.wfile.write(f"event: {ev}\ndata: {data}\n\n".encode())
                    except queue.Empty:
                        # signed out, disabled or session expired: end the stream
                        if self.token and not app.auth.session_user(self.token):
                            self.wfile.write(b"event: signed_out\ndata: {}\n\n")
                            self.wfile.flush()
                            break
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

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return                      # browser went away mid-response: not worth logging
        super().handle_error(request, client_address)

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
