"""
User accounts, sessions, roles and unit restrictions.

Roles
  admin       everything: settings, users, backups, plus all dispatcher rights
  dispatcher  map, acknowledge alerts, edit zones, name units, reports, exports
  viewer      map and reports, read-only (also good for a wall-display account)

Any user can be restricted to a list of units; they then only see those units' positions,
history, alerts, reports and exports. Alerts not tied to a unit (e.g. an emergency whose unit
couldn't be identified) are shown to everyone.
"""
import base64
import hashlib
import json
import re
import secrets
import threading
import time

from . import config as config_mod

ROLES = {
    "admin": {"label": "Administrator",
              "perms": ["view", "reports", "export", "ack", "zones", "units", "poll", "listen", "settings", "users", "backup"]},
    "dispatcher": {"label": "Dispatcher", "perms": ["view", "reports", "export", "ack", "zones", "units", "poll", "listen"]},
    "viewer": {"label": "Viewer", "perms": ["view", "reports"]},
}

SESSION_IDLE_S = 12 * 3600          # normal login: signed out after 12 h without use
REMEMBER_S = 30 * 86400             # "keep me signed in": 30 days
MAX_FAILS, MAX_FAILS_IP, FAIL_WINDOW_S, LOCK_S = 5, 20, 300, 300   # per username / per address
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    units TEXT,                     -- JSON list of unit IDs, NULL = all units
    enabled INTEGER DEFAULT 1,
    created REAL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL, last_seen REAL, expires REAL,
    remember INTEGER, ip TEXT, agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_sess_user ON sessions(user_id);
"""


class AuthError(Exception):
    pass


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, app):
        self.app = app
        self.db, self.lock = app.store.db, app.store.lock
        with self.lock:
            self.db.executescript(SCHEMA)
        self.fails = {}                 # key -> [timestamps]
        self.locked = {}                # key -> until
        self.basic_cache = {}           # Authorization header -> user id
        self.flock = threading.Lock()
        with self.lock, self.db:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
            if "whats_new_seen" not in cols:     # v0.7
                self.db.execute("ALTER TABLE users ADD COLUMN whats_new_seen TEXT DEFAULT ''")
        self._migrate_legacy()

    # ---- users ------------------------------------------------------------
    def _row(self, r):
        if not r:
            return None
        u = dict(r)
        u["units"] = json.loads(u["units"]) if u["units"] else None
        u["enabled"] = bool(u["enabled"])
        u["perms"] = ROLES.get(u["role"], ROLES["viewer"])["perms"]
        u["role_label"] = ROLES.get(u["role"], ROLES["viewer"])["label"]
        return u

    def public(self, u):
        return {k: u[k] for k in ("id", "username", "display_name", "role", "role_label", "units",
                                  "enabled", "created", "last_login", "perms")}

    def set_whats_new_seen(self, uid, version):
        with self.lock, self.db:
            self.db.execute("UPDATE users SET whats_new_seen=? WHERE id=?", (str(version)[:20], uid))

    def user(self, uid):
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def by_name(self, username):
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())

    def users(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
        return [self._row(r) for r in rows]

    def needs_setup(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def _admins_left(self, excluding=None):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1 AND id!=?",
                                   (excluding or -1,)).fetchone()[0]

    def save_user(self, data, uid=None, actor=None):
        """Create (uid None) or update a user from a dict of fields. Returns the user."""
        cur = self.user(uid) if uid else None
        if uid and not cur:
            raise AuthError("No such user")
        username = str(data.get("username", cur["username"] if cur else "")).strip()
        if not USERNAME_RE.match(username):
            raise AuthError("Username: 2-32 letters, numbers, . _ -")
        other = self.by_name(username)
        if other and (not cur or other["id"] != cur["id"]):
            raise AuthError(f"There's already a user called {username}")
        role = data.get("role", cur["role"] if cur else "viewer")
        if role not in ROLES:
            raise AuthError("Unknown role")
        display = str(data.get("display_name", cur["display_name"] if cur else "")).strip()[:60]
        enabled = bool(data.get("enabled", cur["enabled"] if cur else True))
        units = data.get("units", cur["units"] if cur else None)
        if units is not None:
            if not isinstance(units, list) or not all(isinstance(x, str) and len(x) <= 32 for x in units):
                raise AuthError("Units must be a list of unit IDs")
            units = sorted(set(units))
        pw = data.get("password")
        if not cur and not pw:
            raise AuthError("Set a password for the new user")
        if pw is not None and pw != "":
            if len(str(pw)) < 8:
                raise AuthError("Password must be at least 8 characters")
            pw_hash = config_mod.hash_password(str(pw))
        else:
            pw_hash = cur["password_hash"] if cur else None

        if cur and cur["role"] == "admin" and cur["enabled"] and (role != "admin" or not enabled) \
                and self._admins_left(cur["id"]) == 0:
            raise AuthError("This is the last administrator; make someone else an administrator first")
        if cur and actor and actor["id"] == cur["id"] and (not enabled or role != cur["role"]):
            raise AuthError("You can't disable yourself or change your own role")

        units_json = json.dumps(units) if units is not None else None
        with self.lock, self.db:
            if cur:
                self.db.execute("UPDATE users SET username=?, display_name=?, role=?, password_hash=?, units=?, "
                                "enabled=? WHERE id=?", (username, display, role, pw_hash, units_json,
                                                          int(enabled), uid))
            else:
                uid = self.db.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, units, enabled, created) "
                    "VALUES (?,?,?,?,?,?,?)", (username, display, role, pw_hash, units_json, int(enabled),
                                              time.time())).lastrowid
            if cur and (not enabled or (pw is not None and pw != "")):
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        self.basic_cache.clear()
        return self.user(uid)

    def delete_user(self, uid, actor):
        u = self.user(uid)
        if not u:
            raise AuthError("No such user")
        if actor and actor["id"] == uid:
            raise AuthError("You can't delete your own account")
        if u["role"] == "admin" and u["enabled"] and self._admins_left(uid) == 0:
            raise AuthError("This is the last administrator")
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            self.db.execute("DELETE FROM users WHERE id=?", (uid,))
        self.basic_cache.clear()
        return u

    def change_own_password(self, user, current, new, keep_token=None):
        if not config_mod.check_password(str(current), user["password_hash"]):
            raise AuthError("Current password is wrong")
        if len(str(new)) < 8:
            raise AuthError("New password must be at least 8 characters")
        with self.lock, self.db:
            self.db.execute("UPDATE users SET password_hash=? WHERE id=?",
                            (config_mod.hash_password(str(new)), user["id"]))
            # sign out everywhere else
            self.db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?",
                            (user["id"], _h(keep_token) if keep_token else ""))
        self.basic_cache.clear()

    def _migrate_legacy(self):
        """v0.2/v0.3 had one admin login in config.json; turn it into the first admin account."""
        web = self.app.cfg["web"]
        if self.needs_setup() and web.get("password_hash"):
            name = web.get("username") or "admin"
            if not USERNAME_RE.match(name):
                name = "admin"
            with self.lock, self.db:
                self.db.execute("INSERT INTO users (username, display_name, role, password_hash, enabled, created) "
                                "VALUES (?,?,?,?,1,?)", (name, "Administrator", "admin", web["password_hash"],
                                                         time.time()))
            self.app.store.add_event("audit", f"{name}: account created from the old single login")

    # ---- login / sessions -------------------------------------------------
    def _check_lock(self, *keys):
        now = time.time()
        with self.flock:
            for k in keys:
                if self.locked.get(k, 0) > now:
                    mins = int((self.locked[k] - now) // 60) + 1
                    raise AuthError(f"Too many failed attempts. Try again in {mins} min.")

    def _fail(self, *keys):
        now = time.time()
        with self.flock:
            for k in keys:
                lst = [t for t in self.fails.get(k, []) if now - t < FAIL_WINDOW_S] + [now]
                self.fails[k] = lst
                if len(lst) >= (MAX_FAILS_IP if k.startswith("ip:") else MAX_FAILS):
                    self.locked[k] = now + LOCK_S
                    self.fails[k] = []

    def login(self, username, password, remember, ip, agent):
        keys = (f"ip:{ip}", f"user:{str(username).lower()}")
        self._check_lock(*keys)
        u = self.by_name(str(username).strip())
        if not u or not config_mod.check_password(str(password), u["password_hash"]):
            self._fail(*keys)
            self.audit(None, f"failed sign-in as '{str(username)[:32]}' from {ip}")
            raise AuthError("Wrong username or password")
        if not u["enabled"]:
            self.audit(u, f"sign-in refused (account disabled) from {ip}")
            raise AuthError("This account is disabled")
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                            (_h(token), u["id"], now, now, now + (REMEMBER_S if remember else SESSION_IDLE_S),
                             int(bool(remember)), ip, str(agent)[:200]))
            self.db.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        self.audit(u, f"signed in from {ip}")
        return token, u

    def session_user(self, token):
        if not token:
            return None
        now = time.time()
        with self.lock:
            s = self.db.execute("SELECT * FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
        if not s or s["expires"] < now:
            return None
        u = self.user(s["user_id"])
        if not u or not u["enabled"]:
            return None
        if now - s["last_seen"] > 60:          # sliding expiry for normal sessions
            exp = s["expires"] if s["remember"] else now + SESSION_IDLE_S
            with self.lock, self.db:
                self.db.execute("UPDATE sessions SET last_seen=?, expires=? WHERE token_hash=?",
                                (now, exp, _h(token)))
        return u

    def logout(self, token):
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))

    def basic_user(self, header, ip):
        """HTTP Basic auth, for scripts and API access."""
        uid = self.basic_cache.get(header)
        if uid:
            u = self.user(uid)
            return u if u and u["enabled"] else None
        try:
            name, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return None
        try:
            self._check_lock(f"ip:{ip}", f"user:{name.lower()}")
        except AuthError:
            return None
        u = self.by_name(name)
        if not u or not u["enabled"] or not config_mod.check_password(pw, u["password_hash"]):
            self._fail(f"ip:{ip}", f"user:{name.lower()}")
            return None
        if len(self.basic_cache) > 100:
            self.basic_cache.clear()
        self.basic_cache[header] = u["id"]
        return u

    # ---- helpers ----------------------------------------------------------
    def audit(self, user, text):
        who = user["username"] if user else "-"
        self.app.store.add_event("audit", f"{who}: {text}", None, self.app.now())

    @staticmethod
    def can(user, perm):
        return bool(user) and perm in user["perms"]

    @staticmethod
    def sees(user, unit_id):
        """Can this user see this unit? Alerts with no unit (unit_id None) are visible to all."""
        return unit_id is None or user["units"] is None or unit_id in user["units"]
