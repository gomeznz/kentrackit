"""
Alerts to iPhones through Apple's push notification service (APNs), so emergencies arrive with
the KenTrackIt app closed.

Apple gives ASI one key (an AuthKey_XXXXXXXXXX.p8 file, made under Certificates, Identifiers &
Profiles -> Keys). It's uploaded on the Settings page of a box, or once on /hub for the whole
hosted service, together with its Key ID and ASI's Team ID.

Each iPhone registers its push token while signed in. The token is tied to that sign-in, so a
phone that signs out, or whose user is disabled or removed, stops getting alerts. Users who can
only see some units only get alerts for those units.

Needs python3-httpx, python3-h2 and python3-cryptography (APNs only speaks HTTP/2). Without them
everything else works and the Settings page says push isn't available.
"""
import base64
import json
import logging
import re
import threading
import time

from .auth import _h

log = logging.getLogger("radio-avl")

HOSTS = {"production": "https://api.push.apple.com", "development": "https://api.sandbox.push.apple.com"}
TOKEN_RE = re.compile(r"^[0-9a-f]{64,200}$")
KEY_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9.-]{3,155}$")
DEFAULTS = {"key_id": "", "team_id": "", "bundle_id": "nz.co.asiltd.kentrackit", "key": "", "critical": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_hash TEXT,
    env TEXT NOT NULL,
    platform TEXT DEFAULT 'ios',
    other_alerts INTEGER DEFAULT 1,
    created REAL,
    updated REAL,
    last_ok REAL,
    last_error TEXT
);
"""


class PushError(ValueError):
    pass


def available():
    try:
        import httpx  # noqa: F401
        import h2  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def check_config(p):
    """Validate the push settings from the Settings page. Returns the cleaned dict."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in p.items() if k in DEFAULTS})
    out["key_id"] = str(out["key_id"]).strip().upper()
    out["team_id"] = str(out["team_id"]).strip().upper()
    out["bundle_id"] = str(out["bundle_id"]).strip() or DEFAULTS["bundle_id"]
    out["critical"] = bool(out["critical"])
    if out["key_id"] and not KEY_ID_RE.match(out["key_id"]):
        raise PushError("Key ID: the 10 letters and digits shown with the key in your Apple developer account")
    if out["team_id"] and not KEY_ID_RE.match(out["team_id"]):
        raise PushError("Team ID: 10 letters and digits, shown under Membership in your Apple developer account")
    if not BUNDLE_RE.match(out["bundle_id"]):
        raise PushError("Bundle ID should look like nz.co.asiltd.kentrackit")
    key = str(out["key"] or "").strip()
    if key:
        if "BEGIN PRIVATE KEY" not in key:
            raise PushError("That isn't an Apple push key (.p8 file)")
        if available():
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric import ec
            try:
                k = load_pem_private_key(key.encode(), password=None)
            except (ValueError, TypeError):
                raise PushError("That .p8 key file couldn't be read")
            if not isinstance(k, ec.EllipticCurvePrivateKey):
                raise PushError("That isn't an Apple push key (.p8 file)")
        out["key"] = key + "\n"
    return out


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Apns:
    """One connection to Apple for one key. Shared by every company on the hosted service."""

    def __init__(self, get_cfg, transport=None):
        self.get_cfg = get_cfg              # -> the push settings dict
        self.transport = transport          # tests pass a fake
        self.lock = threading.Lock()
        self._jwt = (None, 0, None)         # (token, made at, for which key)
        self._clients = {}
        self.last_error = ""
        self.last_ok = None

    def configured(self):
        c = self.get_cfg()
        return bool(c.get("key") and c.get("key_id") and c.get("team_id") and c.get("bundle_id"))

    def _token(self):
        c = self.get_cfg()
        ident = (c["key_id"], c["team_id"], c["key"])
        tok, made, for_key = self._jwt
        if tok and for_key == ident and time.time() - made < 45 * 60:   # Apple wants a fresh one within the hour
            return tok
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(c["key"].encode(), password=None)
        now = int(time.time())
        head = _b64(json.dumps({"alg": "ES256", "kid": c["key_id"]}, separators=(",", ":")).encode())
        body = _b64(json.dumps({"iss": c["team_id"], "iat": now}, separators=(",", ":")).encode())
        der = key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        tok = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        self._jwt = (tok, time.time(), ident)
        return tok

    def _client(self, env):
        import httpx
        if env not in self._clients:
            kw = {"http2": True, "timeout": 15}
            if self.transport:
                kw = {"transport": self.transport, "timeout": 15}
            self._clients[env] = httpx.Client(base_url=HOSTS[env], **kw)
        return self._clients[env]

    def send(self, token, env, payload, push_type="alert", priority=10, collapse_id=None):
        """-> (HTTP status, Apple's reason or '')."""
        c = self.get_cfg()
        with self.lock:
            headers = {"authorization": f"bearer {self._token()}", "apns-topic": c["bundle_id"],
                       "apns-push-type": push_type, "apns-priority": str(priority)}
            if collapse_id:
                headers["apns-collapse-id"] = collapse_id[:64]
            try:
                r = self._client(env if env in HOSTS else "production").post(
                    f"/3/device/{token}", content=json.dumps(payload).encode(), headers=headers)
            except Exception as e:
                self.last_error = f"can't reach Apple: {e}"
                return 0, self.last_error
        reason = ""
        if r.status_code != 200:
            try:
                reason = r.json().get("reason", "")
            except ValueError:
                reason = r.text[:100]
            if r.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                self._jwt = (None, 0, None)
            if r.status_code not in (400, 410) or reason not in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                self.last_error = f"{r.status_code} {reason}"
        else:
            self.last_ok = time.time()
        return r.status_code, reason


class PushService:
    """One company's iPhones: who's registered, and sending them alerts as they happen."""

    def __init__(self, app, apns):
        self.app = app
        self.apns = apns
        self.db, self.lock = app.store.db, app.store.lock
        with self.lock:
            self.db.executescript(SCHEMA)
        self.sent = 0

    def critical(self):
        return bool(self.apns.get_cfg().get("critical"))

    # ---- phones ------------------------------------------------------------------------------
    def register(self, user, session_token, token, env, platform="ios", other_alerts=True):
        token = str(token or "").strip().lower()
        if not TOKEN_RE.match(token):
            raise PushError("That isn't a push token")
        env = "development" if env == "development" else "production"
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO push_tokens (token, user_id, session_hash, env, platform, other_alerts, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET user_id=excluded.user_id, "
                "session_hash=excluded.session_hash, env=excluded.env, platform=excluded.platform, "
                "other_alerts=excluded.other_alerts, updated=excluded.updated",
                (token, user["id"], _h(session_token) if session_token else None, env, str(platform)[:10],
                 int(bool(other_alerts)), now, now))

    def unregister(self, token):
        with self.lock, self.db:
            self.db.execute("DELETE FROM push_tokens WHERE token=?", (str(token).lower(),))

    def phones(self, user_id=None):
        """Registered phones still signed in."""
        q = ("SELECT p.token, p.user_id, p.env, p.other_alerts, p.last_ok, p.last_error, u.units, u.username "
             "FROM push_tokens p JOIN users u ON u.id = p.user_id AND u.enabled = 1 "
             "JOIN sessions s ON s.token_hash = p.session_hash AND s.expires > ?")
        args = [time.time()]
        if user_id is not None:
            q += " WHERE p.user_id = ?"
            args.append(user_id)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["units"] = json.loads(d["units"]) if d["units"] else None
            out.append(d)
        return out

    def status(self):
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0]
        return {"available": available(), "configured": self.apns.configured(), "critical": self.critical(),
                "phones": len(self.phones()), "registered": n, "sent": self.sent,
                "last_ok": self.apns.last_ok, "last_error": self.apns.last_error}

    # ---- sending -----------------------------------------------------------------------------------
    def _deliver(self, phone, payload, collapse_id, priority=10):
        code, reason = self.apns.send(phone["token"], phone["env"], payload, priority=priority, collapse_id=collapse_id)
        with self.lock, self.db:
            if code in (400, 410) and reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                self.db.execute("DELETE FROM push_tokens WHERE token=?", (phone["token"],))
            elif code == 200:
                self.db.execute("UPDATE push_tokens SET last_ok=?, last_error=NULL WHERE token=?", (time.time(), phone["token"]))
            else:
                self.db.execute("UPDATE push_tokens SET last_error=? WHERE token=?", (f"{code} {reason}", phone["token"]))
        if code == 200:
            self.sent += 1
        return code, reason

    def alert_payload(self, a):
        title = "Emergency" if a["severity"] == "critical" else (a.get("kind") or "Alert").replace("_", " ").capitalize()
        aps = {"alert": {"title": title, "body": a.get("detail") or ""}, "thread-id": "emergency" if a["severity"] == "critical" else "alerts"}
        if a["severity"] == "critical":
            if self.critical():
                # sounds even on silent and through Do Not Disturb (needs Apple's Critical Alerts approval)
                aps["sound"] = {"critical": 1, "name": "alarm.wav", "volume": 1.0}
                aps["interruption-level"] = "critical"
            else:
                aps["sound"] = "alarm.wav"
                aps["interruption-level"] = "time-sensitive"
        else:
            aps["sound"] = "default"
        if self.app.hosted:
            aps["alert"]["subtitle"] = self.app.cfg["web"]["title"]
        return {"aps": aps, "alert_id": a["id"], "unit": a.get("unit_id")}

    def on_alert(self, a):
        if a.get("severity") not in ("critical", "warn") or not self.apns.configured() or not available():
            return
        payload = self.alert_payload(a)
        for ph in self.phones():
            if ph["units"] is not None and a.get("unit_id") and a["unit_id"] not in ph["units"]:
                continue
            if a["severity"] == "warn" and not ph["other_alerts"]:
                continue
            self._deliver(ph, payload, f"alert-{a['id']}", priority=10 if a["severity"] == "critical" else 5)

    def on_ack(self, ids, note=""):
        """Replaces the alert on the phones, so an emergency someone has dealt with stops shouting."""
        if not self.apns.configured() or not available():
            return
        for i in ids or []:
            payload = {"aps": {"alert": {"title": "Acknowledged", "body": note or "The alert was acknowledged on KenTrackIt."},
                               "interruption-level": "passive", "thread-id": "emergency"}, "alert_id": i, "ack": True}
            for ph in self.phones():
                self._deliver(ph, payload, f"alert-{i}", priority=5)

    def test(self, user):
        """A test alarm to this user's own iPhones. -> list of (username, result)."""
        if not available():
            raise PushError("Push needs python3-httpx, python3-h2 and python3-cryptography on the server (see the install guide)")
        if not self.apns.configured():
            raise PushError("Add the Apple push key first")
        phones = self.phones(user["id"])
        if not phones:
            raise PushError("No iPhone is signed in as you with alerts allowed. Open the KenTrackIt app, sign in, and allow notifications.")
        payload = self.alert_payload({"id": 0, "severity": "critical", "kind": "test",
                                      "detail": "Test alarm from KenTrackIt. This is how an emergency arrives."})
        payload["aps"]["alert"]["title"] = "Test alarm"
        out = []
        for ph in phones:
            code, reason = self._deliver(ph, payload, "alert-test")
            out.append("sent" if code == 200 else f"failed: {code} {reason}".strip())
        return out

    def run(self):
        """Listens for alerts like a browser would, and passes them on."""
        q = self.app.broker.subscribe(lambda ev, _d: ev in ("alert", "alert_ack"))
        try:
            while not self.app.stop.is_set():
                try:
                    ev, payload = q.get(timeout=1)
                except Exception:
                    continue
                try:
                    data = json.loads(payload)
                    if ev == "alert":
                        self.on_alert(data)
                    elif ev == "alert_ack":
                        self.on_ack(data.get("ids"), data.get("note", ""))
                except Exception as e:
                    log.warning("push: %s", e)
        finally:
            self.app.broker.unsubscribe(q)
