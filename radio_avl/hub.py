"""
KenTrackIt hosted service: many companies on one server, each with its own data.

  python3 -m radio_avl.hub --data /var/lib/kentrackit-hub --port 8090

Each company is a complete KenTrackIt (the same App a Pi runs) in its own folder with its own
database: users, units, positions, alerts, zones, recordings and settings never share a table
with another company. A site's Pi sends its radio data here as a gateway (see uplink.py), using
a key that belongs to one company.

Requests find their company from the session (the session key starts with the company's short
name) or from the web address, when a company has its own (e.g. council.track.example.nz).
People sign in on the usual page without choosing a company: usernames are unique across the
service, so the username says which company it is.

ASI's own administrators sign in at /hub to add companies, hand out gateway keys and open
support sessions.
"""
import argparse
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse, quote

from . import appdist
from . import config as config_mod
from .appdist import AppStore
from .auth import Auth, AuthError, SUPPORT_USER, _h
from .store import Store
from .web import TileCache, make_handler, _Server

log = logging.getLogger("radio-avl")

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")
RESERVED_SLUGS = {"hub", "api", "static", "www", "admin", "app", "login", "setup", "tiles"}
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
HUB_COOKIE = "kt_hub"
GATEWAY_OFFLINE_S = 45          # no word from a gateway for this long: its company's radio link shows down
SUPPORT_SESSION_S = 4 * 3600
MAX_GATEWAY_BODY = 8 * 1024 * 1024
COMPANY_COMPONENTS_LOCKED = {"recording": {"enabled": False}, "console": {"enabled": False},
                             "lcd": {"enabled": False}, "polling": {"mode": "off"},
                             "uplink": {"enabled": False}}

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL,
    enabled INTEGER DEFAULT 1,
    token_hash TEXT,
    token_hint TEXT,
    domains TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    gw_stream TEXT,
    gw_seq INTEGER DEFAULT 0,
    gw_last REAL,
    gw_info TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS hub_settings (key TEXT PRIMARY KEY, value TEXT);
"""


class HubError(Exception):
    pass


# ---- the company a request is for --------------------------------------------------------------
class _Current:
    """Stands in for `app` in the web handler: each request thread points it at its company.
    The server handles one request at a time per thread, so this can't leak between requests."""

    def __init__(self):
        object.__setattr__(self, "_l", threading.local())

    def set(self, app):
        self._l.app = app

    def get(self):
        return getattr(self._l, "app", None)

    def __getattr__(self, name):
        a = getattr(self._l, "app", None)
        if a is None:
            raise RuntimeError("no company for this request")
        return getattr(a, name)

    def __setattr__(self, name, value):
        setattr(self._l.app, name, value)


CURRENT = _Current()


class _HubCore:
    """What Auth needs from an App, for ASI's own administrator accounts."""
    VERSION = None

    def __init__(self, hub):
        self.hub_owner = hub
        self.store = Store(os.path.join(hub.data_dir, "hub.db"))
        self.cfg = {"web": {"password_hash": ""}}

    def now(self):
        return time.time()


class Hub:
    def __init__(self, data_dir, host="0.0.0.0", port=8090):
        from .main import App
        self.App = App
        self.VERSION = App.VERSION
        self.data_dir = os.path.abspath(data_dir)
        self.host, self.port = host, port
        os.makedirs(os.path.join(self.data_dir, "companies"), exist_ok=True)
        self.core = _HubCore(self)
        self.db, self.lock = self.core.store.db, self.core.store.lock
        with self.lock:
            self.db.executescript(SCHEMA)
        self.auth = Auth(self.core)                  # ASI administrators (role admin)
        self.stop = threading.Event()
        self.companies = {}                          # slug -> running App
        self.company_locks = {}
        self.clock = threading.Lock()                # creating / starting / stopping companies
        self.cfg = self._load_cfg()
        self.tiles = (TileCache(self.cfg, self.data_dir), TileCache(self.cfg, self.data_dir, "sat"))
        self.app_store = AppStore(self.data_dir)
        for row in self.rows():
            if row["enabled"]:
                self._start(row["slug"])

    # ---- hub settings (title, map tiles) ------------------------------------------------------
    def _load_cfg(self):
        with self.lock:
            r = self.db.execute("SELECT value FROM hub_settings WHERE key='cfg'").fetchone()
        saved = json.loads(r[0]) if r else {}
        cfg = config_mod.merge(config_mod.DEFAULTS, saved)
        cfg["data_dir"] = self.data_dir
        return cfg

    def update_cfg(self, patch):
        allowed = {"tiles": patch.get("tiles")} if patch.get("tiles") is not None else {}
        if patch.get("web", {}).get("title") is not None:
            allowed["web"] = {"title": patch["web"]["title"]}
        new = config_mod.apply_patch(self.cfg, allowed)
        keep = {"web": {"title": new["web"]["title"]}, "tiles": new["tiles"]}
        with self.lock, self.db:
            self.db.execute("INSERT INTO hub_settings VALUES ('cfg', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(keep),))
        if new["tiles"] != self.cfg["tiles"]:
            self.tiles = (TileCache(new, self.data_dir), TileCache(new, self.data_dir, "sat"))
            for app in list(self.companies.values()):
                app.tiles, app.sat_tiles = self.tiles
        self.cfg = new
        return new

    def title(self):
        return self.cfg["web"]["title"]

    # ---- companies ---------------------------------------------------------------------------
    def rows(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row(r) for r in rows]

    def row(self, slug):
        with self.lock:
            r = self.db.execute("SELECT * FROM companies WHERE slug=?", (slug,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r):
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        d["domains"] = json.loads(d["domains"] or "[]")
        d["gw_info"] = json.loads(d["gw_info"] or "{}")
        return d

    def company_dir(self, slug):
        return os.path.join(self.data_dir, "companies", slug)

    def _start(self, slug):
        with self.clock:
            if slug in self.companies:
                return self.companies[slug]
            d = self.company_dir(slug)
            path = os.path.join(d, "config.json")
            cfg = config_mod.load(path)
            cfg["data_dir"] = d
            # the radio, sound cards, display and talk-back are on the site's gateway, not here
            for section, vals in COMPANY_COMPONENTS_LOCKED.items():
                cfg[section].update(vals)
            app = self.App(cfg, path, mode="hosted", hub=self, slug=slug, tiles=self.tiles,
                           app_store=self.app_store)
            app.link(False, "gateway not connected yet")
            row = self.row(slug)
            if row and row["gw_last"] and time.time() - row["gw_last"] < GATEWAY_OFFLINE_S:
                link = row["gw_info"].get("link") or {}
                app.link(bool(link.get("up")), link.get("detail") or "")
            threading.Thread(target=app.housekeeping, daemon=True, name=f"hk:{slug}").start()
            self.companies[slug] = app
            self.company_locks[slug] = threading.Lock()
            log.info("company %s started", slug)
            return app

    def _stop(self, slug):
        with self.clock:
            app = self.companies.pop(slug, None)
        if app:
            app.stop.set()
            log.info("company %s stopped", slug)

    def app(self, slug):
        return self.companies.get(slug)

    def create_company(self, name, slug, admin, actor):
        name = str(name or "").strip()[:80]
        slug = str(slug or "").strip().lower()
        if not name:
            raise HubError("Enter the company's name")
        if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            raise HubError("Short name: 2-30 lowercase letters, digits and -, starting with a letter")
        if self.row(slug) or os.path.exists(self.company_dir(slug)):
            raise HubError(f"There's already a company called {slug}")
        username = str(admin.get("username", "")).strip()
        if self.username_taken(username, None):
            raise HubError(f"The username {username} is already used on this service")
        d = self.company_dir(slug)
        os.makedirs(d)
        config_mod.save(os.path.join(d, "config.json"),
                        config_mod.merge(config_mod.DEFAULTS, {"web": {"title": name}, "data_dir": d}))
        token = self._new_token()
        with self.lock, self.db:
            self.db.execute("INSERT INTO companies (slug, name, created, enabled, token_hash, token_hint) "
                            "VALUES (?,?,?,1,?,?)", (slug, name, time.time(), _h(token), token[-4:]))
        app = self._start(slug)
        try:
            u = app.auth.save_user({"username": username, "display_name": admin.get("display_name", ""),
                                    "password": admin.get("password"), "role": "admin"})
        except AuthError as e:
            self._stop(slug)
            with self.lock, self.db:
                self.db.execute("DELETE FROM companies WHERE slug=?", (slug,))
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            raise HubError(str(e))
        app.auth.audit(None, f"company created on the hosted service by ASI ({actor}); first administrator {u['username']}")
        self.audit(actor, f"created company {name} ({slug}) with administrator {u['username']}")
        return token

    @staticmethod
    def _new_token():
        return "gw_" + secrets.token_urlsafe(32)

    def new_token(self, slug, actor):
        if not self.row(slug):
            raise HubError("No such company")
        token = self._new_token()
        with self.lock, self.db:
            self.db.execute("UPDATE companies SET token_hash=?, token_hint=?, gw_stream=NULL, gw_seq=0 WHERE slug=?",
                            (_h(token), token[-4:], slug))
        self.audit(actor, f"issued a new gateway key for {slug} (the old one stops working)")
        return token

    def update_company(self, slug, data, actor):
        row = self.row(slug)
        if not row:
            raise HubError("No such company")
        name = str(data.get("name", row["name"])).strip()[:80]
        if not name:
            raise HubError("Enter the company's name")
        domains = data.get("domains", row["domains"])
        if isinstance(domains, str):
            domains = [x for x in re.split(r"[\s,]+", domains) if x]
        domains = sorted({str(x).strip().lower().rstrip(".") for x in domains})
        for dmn in domains:
            if not DOMAIN_RE.match(dmn):
                raise HubError(f"{dmn} isn't a web address (e.g. council.track.example.nz)")
            other = self.slug_for_host(dmn)
            if other and other != slug:
                raise HubError(f"{dmn} is already used by another company")
        notes = str(data.get("notes", row["notes"]))[:1000]
        enabled = bool(data.get("enabled", row["enabled"]))
        with self.lock, self.db:
            self.db.execute("UPDATE companies SET name=?, domains=?, notes=?, enabled=? WHERE slug=?",
                            (name, json.dumps(domains), notes, int(enabled), slug))
        if enabled and not row["enabled"]:
            self._start(slug)
            self.audit(actor, f"enabled company {slug}")
        elif not enabled and row["enabled"]:
            self._stop(slug)
            self.audit(actor, f"disabled company {slug}: its users can't sign in and its gateway is refused")
        changes = [k for k in ("name", "domains", "notes") if data.get(k) is not None and data.get(k) != row[k]]
        if changes:
            self.audit(actor, f"updated company {slug}: " + ", ".join(changes))
        return self.row(slug)

    # ---- users across companies ---------------------------------------------------------------
    def _user_in(self, slug, username):
        """-> True if this company has the username (running or not)."""
        app = self.companies.get(slug)
        if app:
            return app.auth.by_name(username) is not None
        db = os.path.join(self.company_dir(slug), "avl.db")
        if not os.path.exists(db):
            return False
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone() is not None
            finally:
                c.close()
        except sqlite3.Error:
            return False

    def username_taken(self, username, app):
        if str(username).lower() == SUPPORT_USER:
            return False                 # every company has its own support account
        mine = app.slug if app else None
        return any(self._user_in(r["slug"], username) for r in self.rows() if r["slug"] != mine)

    def company_of_user(self, username):
        """-> (slug, enabled) of the company with this username, or (None, False)."""
        if not username or str(username).lower() == SUPPORT_USER:
            return None, False
        for r in self.rows():
            if self._user_in(r["slug"], username):
                return r["slug"], r["enabled"]
        return None, False

    def slug_for_host(self, host):
        host = (host or "").split(":")[0].strip().lower().rstrip(".")
        if not host:
            return None
        for r in self.rows():
            if host in r["domains"]:
                return r["slug"]
        return None

    # ---- support sessions ------------------------------------------------------------------------
    def support_session(self, slug, hub_user, ip, agent):
        app = self.companies.get(slug)
        if not app:
            raise HubError("That company is disabled")
        u = app.auth.by_name(SUPPORT_USER)
        if not u:
            u = app.auth.save_user({"username": SUPPORT_USER, "display_name": "ASI support", "role": "admin",
                                    "password": secrets.token_urlsafe(24), "_support": True})
            app.auth.audit(None, "ASI support account added (used for support sessions from ASI)")
        if not u["enabled"]:
            raise HubError("This company has turned off ASI support access (the asi-support user is disabled)")
        token = app.auth.create_session(u, False, ip, agent, lifetime=SUPPORT_SESSION_S)
        who = hub_user["display_name"] or hub_user["username"]
        app.auth.audit(u, f"support session opened by {who} at ASI, from {ip}")
        self.audit(hub_user["username"], f"opened a support session for {slug}")
        return token

    # ---- gateways ---------------------------------------------------------------------------------
    def gateway_slug(self, token):
        if not token or not token.startswith("gw_"):
            return None
        with self.lock:
            r = self.db.execute("SELECT slug FROM companies WHERE token_hash=?", (_h(token),)).fetchone()
        return r[0] if r else None

    def gateway_ingest(self, slug, body, ip):
        """Radio data from a company's gateway. Returns how many chunks were new."""
        app = self.companies.get(slug)
        if not app:
            raise HubError("This company is disabled")
        stream = str(body.get("stream", ""))[:40]
        chunks = body.get("chunks") or []
        if not isinstance(chunks, list):
            raise HubError("chunks must be a list")
        with self.company_locks[slug]:
            row = self.row(slug)
            last = row["gw_seq"] if row["gw_stream"] == stream else 0
            new = []
            for c in chunks:
                try:
                    seq, t, data = int(c[0]), float(c[1]), bytes.fromhex(c[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if seq > last:
                    new.append((t, data))
                    last = seq
            if new:
                app.ingest(new)
            link = body.get("link") or {}
            up = bool(link.get("up"))
            detail = "gateway: " + (str(link.get("detail") or "")[:80] or ("radio connected" if up else "radio not connected"))
            if (up, detail) != (app.stats["link_up"], app.stats["link_detail"]):
                app.link(up, detail)
            info = {"version": str(body.get("version", ""))[:20], "ip": ip, "link": {"up": up, "detail": link.get("detail")},
                    "port": str(link.get("port") or "")[:60], "backlog": int(body.get("backlog") or 0),
                    "received": row["gw_info"].get("received", 0) + len(new)}
            with self.lock, self.db:
                self.db.execute("UPDATE companies SET gw_stream=?, gw_seq=?, gw_last=?, gw_info=? WHERE slug=?",
                                (stream, last, time.time(), json.dumps(info), slug))
            if not row["gw_last"] or time.time() - row["gw_last"] > GATEWAY_OFFLINE_S:
                app.store.add_event("gateway", f"gateway connected from {ip}")
        return len(new)

    def gateway_info(self, slug):
        r = self.row(slug)
        if not r:
            return None
        online = bool(r["gw_last"]) and time.time() - r["gw_last"] < GATEWAY_OFFLINE_S
        return dict(r["gw_info"], online=online, last=r["gw_last"], key_hint=r["token_hint"])

    def gateway_current(self, slug):
        """The gateway is online and has sent everything it kept while it was offline."""
        g = self.gateway_info(slug)
        return bool(g and g["online"] and not g.get("backlog"))

    def housekeeping(self):
        while not self.stop.wait(10):
            try:
                for r in self.rows():
                    app = self.companies.get(r["slug"])
                    if not app or not r["gw_last"]:
                        continue
                    if time.time() - r["gw_last"] > GATEWAY_OFFLINE_S and app.stats["link_detail"] != "gateway offline":
                        app.link(False, "gateway offline")
                        app.store.add_event("gateway", "gateway offline (no contact for %d s)" % GATEWAY_OFFLINE_S)
            except Exception as e:
                log.warning("hub housekeeping: %s", e)

    # ---- overview for ASI ------------------------------------------------------------------------
    def overview(self):
        out = []
        for r in self.rows():
            app = self.companies.get(r["slug"])
            c = {k: r[k] for k in ("slug", "name", "created", "enabled", "domains", "notes", "token_hint")}
            c["gateway"] = self.gateway_info(r["slug"])
            if app:
                counts = app.store.counts()
                users = app.auth.users()
                c.update(units=counts["units"], positions=counts["positions"],
                         users=len([u for u in users if u["username"].lower() != SUPPORT_USER]),
                         open_alerts=len(app.store.alerts(open_only=True, limit=1000)),
                         last_frame=app.stats.get("last_frame"))
            out.append(c)
        return out

    def audit(self, who, text):
        self.core.store.add_event("audit", f"{who or '-'}: {text}")

    def shutdown(self):
        self.stop.set()
        for slug in list(self.companies):
            self._stop(slug)


# ---- web --------------------------------------------------------------------------------------------
def make_hub_handler(hub):
    Base = make_handler(CURRENT)
    STATIC_HUB = {"/hub", "/hub/"}

    class HubHandler(Base):
        server_version = "KenTrackIt"

        # ---- which company ----------------------------------------------------------------------
        def _host_slug(self):
            hosts = [h.strip() for h in self.headers.get("X-Forwarded-Host", "").split(",") if h.strip()]
            hosts.append(self.headers.get("Host", ""))
            for h in hosts:
                s = hub.slug_for_host(h)
                if s:
                    return s
            return None

        def _cookie_slug(self):
            tok = self._token() or ""
            return tok.split("~", 1)[0] if "~" in tok else None

        def _company(self, p, method):
            """-> the company's App for this request, or None (handled by the hub)."""
            slug = self._host_slug()
            if not slug:
                slug = self._cookie_slug()
                if not slug and method == "POST" and p == "/api/login":
                    slug = self._login_slug()
                if not slug and self.headers.get("Authorization", "").startswith("Basic "):
                    slug = self._basic_slug()
            return hub.app(slug) if slug else None

        def _login_slug(self):
            n = min(int(self.headers.get("Content-Length", 0) or 0), 100_000)
            raw = self.rfile.read(n)
            self._replay = raw                      # the company's handler reads the body again
            try:
                name = json.loads(raw or b"{}").get("username", "")
            except (ValueError, AttributeError):
                return None
            slug, enabled = hub.company_of_user(str(name).strip())
            if slug and not enabled:
                self._disabled = True
                return None
            return slug

        def _basic_slug(self):
            import base64
            try:
                name = base64.b64decode(self.headers["Authorization"][6:]).decode().partition(":")[0]
            except (ValueError, UnicodeDecodeError):
                return None
            slug, enabled = hub.company_of_user(name)
            return slug if enabled else None

        def _dispatch(self, method):
            p = urlparse(self.path).path
            self._replay, self._disabled = None, False
            CURRENT.set(None)
            if p in STATIC_HUB or p.startswith("/hub/"):
                return self._hub(method, p)
            if p == "/api/gateway/frames" and method == "POST":
                return self._gateway()
            app = self._company(p, method)
            if app is None:
                return self._lobby(method, p)
            CURRENT.set(app)
            rfile = self.rfile
            if self._replay is not None:
                self.rfile = io.BytesIO(self._replay)
            try:
                getattr(Base, "do_" + method)(self)
            finally:
                self.rfile = rfile
                CURRENT.set(None)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---- no company yet: the sign-in page --------------------------------------------------------
        def _lobby(self, method, p):
            if method == "GET":
                if p.startswith("/static/"):
                    return self._static(p[len("/static/"):])
                if p in ("/login", "/login/"):
                    return self._static("login.html")
                if p == "/api/auth-status":
                    return self._json({"title": hub.title(), "setup": False})
                if p in ("/setup", "/setup/"):
                    return self._redirect("/login")
                if p.startswith(("/api/", "/tiles/", "/app/")):
                    return self._json({"error": "Sign in required"}, 401)
                return self._redirect("/login?next=" + quote(self.path, safe=""))
            if method == "POST" and p == "/api/login":
                if not self._same_origin():
                    return self._json({"error": "Cross-site request refused"}, 403)
                time.sleep(0.5)
                ip = self._ip()
                try:
                    hub.auth._check_lock(f"ip:{ip}")
                except AuthError as e:
                    return self._json({"error": str(e)}, 401)
                if self._disabled:
                    return self._json({"error": "Your company's access to this service is suspended. Contact ASI."}, 403)
                hub.auth._fail(f"ip:{ip}")
                return self._json({"error": "Wrong username or password"}, 401)
            if method == "POST" and p == "/api/logout":
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": self._cookie("", 0)})
            return self._json({"error": "Sign in required"}, 401)

        # ---- gateways ------------------------------------------------------------------------------------
        def _client_ip(self):
            return (self.headers.get("CF-Connecting-IP") or
                    self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self._ip())

        def _gateway(self):
            auth = self.headers.get("Authorization", "")
            slug = hub.gateway_slug(auth[7:].strip() if auth.startswith("Bearer ") else "")
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not slug:
                time.sleep(1)
                return self._json({"error": "Unknown gateway key"}, 401)
            if not 0 <= n <= MAX_GATEWAY_BODY:
                return self._json({"error": "Too much data in one go"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "Bad request"}, 400)
            row = hub.row(slug)
            if not row["enabled"]:
                return self._json({"error": "This company is disabled on the hosted service"}, 403)
            try:
                got = hub.gateway_ingest(slug, body, self._client_ip())
            except HubError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "accepted": got, "company": row["name"]})

        # ---- ASI administrators: /hub ------------------------------------------------------------------
        def _hub_token(self):
            from http.cookies import SimpleCookie
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            m = c.get(HUB_COOKIE)
            return m.value if m else None

        def _hub_cookie(self, token, max_age=None):
            parts = [f"{HUB_COOKIE}={token}", "Path=/hub", "HttpOnly", "SameSite=Strict"]
            if max_age is not None:
                parts.append(f"Max-Age={max_age}")
            if self.headers.get("X-Forwarded-Proto", "") == "https":
                parts.append("Secure")
            return "; ".join(parts)

        def _local(self):
            """A request from the server's own network, not one relayed by Cloudflare or a proxy."""
            if self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"):
                return False
            try:
                ip = ipaddress.ip_address(self._ip())
            except ValueError:
                return False
            return ip.is_private or ip.is_loopback

        def _hub(self, method, p):
            if method == "POST" and not self._same_origin():
                return self._json({"error": "Cross-site request refused"}, 403)
            self.hub_user = hub.auth.session_user(self._hub_token())
            if method == "GET" and p in STATIC_HUB:
                return self._static("hub.html")
            if not p.startswith("/hub/api/"):
                return self._send(404, "Not found", "text/plain")
            api = p[len("/hub/api"):]
            if method == "GET" and api == "/state":
                return self._json({"title": hub.title(), "setup": hub.auth.needs_setup(), "version": hub.VERSION,
                                   "user": hub.auth.public(self.hub_user) if self.hub_user else None})
            body = {}
            if method == "POST" and api != "/app/upload":
                body = self._body()
                if body is None:
                    return self._json({"error": "Bad request"}, 400)
            if method == "POST" and api == "/setup":
                if not hub.auth.needs_setup():
                    return self._json({"error": "Setup has already been done"}, 409)
                if not self._local():
                    return self._json({"error": "Create the first administrator from the server's own network "
                                                "(http://<server address>:<port>/hub), not through the public address"}, 403)
                try:
                    u = hub.auth.save_user({"username": body.get("username"), "display_name": body.get("display_name", ""),
                                            "password": body.get("password"), "role": "admin"})
                except AuthError as e:
                    return self._json({"error": str(e)}, 400)
                hub.audit(u["username"], "created the first ASI administrator account")
                return self._json({"ok": True})
            if method == "POST" and api == "/login":
                try:
                    token, u = hub.auth.login(body.get("username", ""), body.get("password", ""), False,
                                              self._client_ip(), self.headers.get("User-Agent", ""))
                except AuthError as e:
                    time.sleep(0.5)
                    return self._json({"error": str(e)}, 401)
                return self._send(200, json.dumps({"ok": True}), "application/json",
                                  {"Set-Cookie": self._hub_cookie(token)})
            if method == "POST" and api == "/logout":
                tok = self._hub_token()
                if tok:
                    hub.auth.logout(tok)
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": self._hub_cookie("", 0)})
            if not self.hub_user:
                return self._json({"error": "Sign in required"}, 401)
            who = self.hub_user["username"]
            try:
                return self._hub_api(method, api, body, who)
            except (HubError, AuthError, config_mod.Invalid) as e:
                return self._json({"error": str(e)}, 400)

        def _hub_api(self, method, api, body, who):
            m = re.match(r"^/companies/([a-z0-9-]{2,30})(/token|/support)?$", api)
            if method == "GET" and api == "/companies":
                return self._json({"companies": hub.overview()})
            if method == "POST" and api == "/companies":
                token = hub.create_company(body.get("name"), body.get("slug"), body.get("admin") or {}, who)
                return self._json({"ok": True, "token": token})
            if m and method == "POST":
                slug, action = m.group(1), m.group(2)
                if action == "/token":
                    return self._json({"ok": True, "token": hub.new_token(slug, who)})
                if action == "/support":
                    token = hub.support_session(slug, self.hub_user, self._client_ip(), self.headers.get("User-Agent", ""))
                    return self._send(200, b'{"ok": true, "url": "/"}', "application/json",
                                      {"Set-Cookie": self._cookie(token, SUPPORT_SESSION_S)})
                return self._json(hub.update_company(slug, body, who))
            if method == "GET" and api == "/admins":
                return self._json({"admins": [hub.auth.public(u) for u in hub.auth.users()]})
            if method == "POST" and api == "/admins":
                u = hub.auth.save_user(dict(body, role="admin"), body.get("id"), self.hub_user)
                hub.audit(who, f"{'updated' if body.get('id') else 'added'} ASI administrator {u['username']}")
                return self._json(hub.auth.public(u))
            ma = re.match(r"^/admins/(\d{1,9})$", api)
            if ma and method == "DELETE":
                u = hub.auth.delete_user(int(ma.group(1)), self.hub_user)
                hub.audit(who, f"removed ASI administrator {u['username']}")
                return self._json({"ok": True})
            if method == "GET" and api == "/settings":
                pub = config_mod.public(hub.cfg)
                return self._json({"title": pub["web"]["title"], "tiles": pub["tiles"],
                                   "tile_sources": list(config_mod.TILE_SOURCES), "sat_sources": list(config_mod.SAT_SOURCES)})
            if method == "POST" and api == "/settings":
                hub.update_cfg(body)
                hub.audit(who, "changed the service settings")
                return self._json({"ok": True})
            if method == "GET" and api == "/app":
                return self._json(hub.app_store.info() or {})
            if method == "POST" and api == "/app/upload":
                return self._hub_app_upload(who)
            if method == "DELETE" and api == "/app":
                hub.app_store.remove()
                hub.audit(who, "removed the phone app")
                return self._json({"ok": True})
            if method == "GET" and api == "/activity":
                return self._json([e for e in hub.core.store.events(300) if e["kind"] == "audit"])
            return self._send(404, "Not found", "text/plain")

        def _hub_app_upload(self, who):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not 0 < n <= appdist.MAX_SIZE:
                return self._json({"error": "Choose an APK file (up to 150 MB)"}, 400)
            buf = bytearray()
            while len(buf) < n:
                chunk = self.rfile.read(min(1 << 20, n - len(buf)))
                if not chunk:
                    break
                buf += chunk
            try:
                info = hub.app_store.put(bytes(buf))
            except appdist.APKError as e:
                return self._json({"error": str(e)}, 400)
            hub.audit(who, f"uploaded phone app {info['version_name']} ({info['version_code']})")
            for app in list(hub.companies.values()):
                app.broker.publish("app_update", info)
            return self._json(dict(info, ok=True))

    return HubHandler


def serve(hub):
    srv = _Server((hub.host, hub.port), make_hub_handler(hub))
    threading.Thread(target=srv.serve_forever, daemon=True, name=f"hub:{hub.port}").start()
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="KenTrackIt hosted service (many companies on one server)")
    ap.add_argument("--data", default="hub-data", help="folder for the service's data (made if missing)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    hub = Hub(a.data, a.host, a.port)
    try:
        serve(hub)
    except OSError as e:
        log.error("can't listen on port %d: %s", a.port, e)
        return 2
    log.info("hosted service on http://%s:%d (ASI admin at /hub), %d companies", a.host, a.port, len(hub.companies))
    threading.Thread(target=hub.housekeeping, daemon=True, name="hub-hk").start()
    signal.signal(signal.SIGTERM, lambda *_: hub.stop.set())
    try:
        hub.stop.wait()
    except KeyboardInterrupt:
        pass
    hub.shutdown()


if __name__ == "__main__":
    sys.exit(main())
