"""SQLite storage for units, positions and events."""
import json
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    alias TEXT DEFAULT '',
    colour TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    first_seen REAL,
    last_seen REAL,
    last_lat REAL, last_lon REAL,
    last_speed_kn REAL, last_course REAL,
    last_fix_time REAL,
    last_valid INTEGER DEFAULT 0,
    reports INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    unit_id TEXT NOT NULL,
    rx_time REAL NOT NULL,
    fix_time REAL,
    lat REAL, lon REAL,
    speed_kn REAL, course REAL,
    valid INTEGER,
    extra TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_pos_unit_time ON positions(unit_id, rx_time);
CREATE INDEX IF NOT EXISTS ix_pos_time ON positions(rx_time);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    rx_time REAL NOT NULL,
    kind TEXT NOT NULL,
    unit_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_time ON events(rx_time);
CREATE TABLE IF NOT EXISTS geofences (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    shape TEXT NOT NULL,            -- circle | polygon
    geometry TEXT NOT NULL,         -- {"center":[lat,lon],"radius":m} | {"points":[[lat,lon],...]}
    colour TEXT DEFAULT '#2563eb',
    alert_enter INTEGER DEFAULT 1,
    alert_exit INTEGER DEFAULT 1,
    active INTEGER DEFAULT 1,
    created REAL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    t REAL NOT NULL,
    kind TEXT NOT NULL,             -- emergency | status | overdue | speed | enter | exit | test
    severity TEXT NOT NULL,         -- critical | warn | info
    unit_id TEXT,
    geofence_id INTEGER,
    detail TEXT,
    lat REAL, lon REAL,
    ack_time REAL,
    ack_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_alert_t ON alerts(t);
CREATE INDEX IF NOT EXISTS ix_alert_open ON alerts(ack_time);
"""

ALERT_COLS = "id, t, kind, severity, unit_id, geofence_id, detail, lat, lon, ack_time, ack_note"

UNIT_COLS = ("id, alias, colour, notes, first_seen, last_seen, last_lat, last_lon, "
             "last_speed_kn, last_course, last_fix_time, last_valid, reports")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(units)")}
            if "state" not in cols:           # added in v0.3: alert engine state per unit
                self.db.execute("ALTER TABLE units ADD COLUMN state TEXT DEFAULT '{}'")

    # ---- writes -------------------------------------------------------
    def add_position(self, p, rx_time):
        valid = 1 if p["valid"] else 0
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO positions (unit_id, rx_time, fix_time, lat, lon, speed_kn, "
                "course, valid, extra, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (p["unit"], rx_time, p["fix_time"], p["lat"], p["lon"], p["speed_kn"],
                 p["course"], valid, json.dumps(p.get("extra", {})), p.get("raw")))
            self.db.execute(
                "INSERT INTO units (id, first_seen, last_seen, reports) VALUES (?,?,?,0) "
                "ON CONFLICT(id) DO NOTHING", (p["unit"], rx_time, rx_time))
            if valid:
                self.db.execute(
                    "UPDATE units SET last_seen=?, last_lat=?, last_lon=?, last_speed_kn=?, "
                    "last_course=?, last_fix_time=?, last_valid=1, reports=reports+1 WHERE id=?",
                    (rx_time, p["lat"], p["lon"], p["speed_kn"], p["course"],
                     p["fix_time"], p["unit"]))
            else:   # heard from it, but no GPS fix: keep the last good position
                self.db.execute(
                    "UPDATE units SET last_seen=?, last_valid=0, reports=reports+1 WHERE id=?",
                    (rx_time, p["unit"]))
        return self.unit(p["unit"])

    def set_unit(self, unit_id, alias=None, colour=None, notes=None):
        with self.lock, self.db:
            self.db.execute("INSERT INTO units (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                            (unit_id,))
            for col, val in (("alias", alias), ("colour", colour), ("notes", notes)):
                if val is not None:
                    self.db.execute(f"UPDATE units SET {col}=? WHERE id=?",
                                    (str(val)[:200], unit_id))
        return self.unit(unit_id)

    def add_event(self, kind, detail, unit_id=None, rx_time=None):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events (rx_time, kind, unit_id, detail) VALUES (?,?,?,?)",
                            (rx_time or time.time(), kind, unit_id, detail))

    # ---- alert engine state ---------------------------------------------
    def unit_states(self):
        with self.lock:
            rows = self.db.execute("SELECT id, state FROM units").fetchall()
        out = {}
        for r in rows:
            try:
                out[r[0]] = json.loads(r[1] or "{}")
            except ValueError:
                out[r[0]] = {}
        return out

    def set_unit_state(self, unit_id, state):
        with self.lock, self.db:
            self.db.execute("UPDATE units SET state=? WHERE id=?", (json.dumps(state), unit_id))

    # ---- geofences -------------------------------------------------------
    def _fence(self, r):
        d = dict(r)
        d["geometry"] = json.loads(d["geometry"])
        for k in ("alert_enter", "alert_exit", "active"):
            d[k] = bool(d[k])
        return d

    def geofences(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM geofences ORDER BY name").fetchall()
        return [self._fence(r) for r in rows]

    def save_geofence(self, f, fid=None):
        vals = (f["name"], f["shape"], json.dumps(f["geometry"]), f["colour"],
                int(f["alert_enter"]), int(f["alert_exit"]), int(f["active"]))
        with self.lock, self.db:
            if fid is None:
                cur = self.db.execute(
                    "INSERT INTO geofences (name, shape, geometry, colour, alert_enter, alert_exit, "
                    "active, created) VALUES (?,?,?,?,?,?,?,?)", vals + (time.time(),))
                fid = cur.lastrowid
            else:
                self.db.execute(
                    "UPDATE geofences SET name=?, shape=?, geometry=?, colour=?, alert_enter=?, "
                    "alert_exit=?, active=? WHERE id=?", vals + (fid,))
            r = self.db.execute("SELECT * FROM geofences WHERE id=?", (fid,)).fetchone()
        return self._fence(r) if r else None

    def delete_geofence(self, fid):
        with self.lock, self.db:
            self.db.execute("DELETE FROM geofences WHERE id=?", (fid,))

    # ---- alerts ----------------------------------------------------------
    def add_alert(self, a):
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO alerts (t, kind, severity, unit_id, geofence_id, detail, lat, lon) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a["t"], a["kind"], a["severity"], a.get("unit_id"), a.get("geofence_id"),
                 a.get("detail"), a.get("lat"), a.get("lon")))
            r = self.db.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE id=?",
                                (cur.lastrowid,)).fetchone()
        return dict(r)

    def alerts(self, open_only=False, limit=200, t0=None, t1=None):
        q, args = f"SELECT {ALERT_COLS} FROM alerts WHERE 1=1", []
        if open_only:
            q += " AND ack_time IS NULL"
        if t0 is not None:
            q += " AND t >= ?"; args.append(t0)
        if t1 is not None:
            q += " AND t <= ?"; args.append(t1)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY t DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def ack_alerts(self, ids=None, note="", unit_id=None, kind=None, t=None):
        """Acknowledge by id list, or every open alert matching unit/kind. Returns acked ids."""
        q, args = "SELECT id FROM alerts WHERE ack_time IS NULL", []
        if ids is not None:
            q += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
            args += list(ids)
        if unit_id is not None:
            q += " AND unit_id=?"; args.append(unit_id)
        if kind is not None:
            q += " AND kind=?"; args.append(kind)
        with self.lock, self.db:
            found = [r[0] for r in self.db.execute(q, args).fetchall()]
            if found:
                self.db.execute(f"UPDATE alerts SET ack_time=?, ack_note=? WHERE id IN "
                                f"({','.join('?' * len(found))})", [t or time.time(), note] + found)
        return found

    def delete_unit(self, unit_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM positions WHERE unit_id=?", (unit_id,))
            self.db.execute("DELETE FROM units WHERE id=?", (unit_id,))

    def backup_to(self, path):
        dest = sqlite3.connect(path)
        with self.lock:
            self.db.backup(dest)
        dest.close()

    def counts(self):
        with self.lock:
            p = self.db.execute("SELECT COUNT(*), MIN(rx_time) FROM positions").fetchone()
            u = self.db.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        return {"positions": p[0], "oldest": p[1], "units": u}

    def prune(self, keep_days):
        if not keep_days:
            return
        cutoff = time.time() - keep_days * 86400
        with self.lock, self.db:
            self.db.execute("DELETE FROM positions WHERE rx_time < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE rx_time < ?", (cutoff,))
            self.db.execute("DELETE FROM alerts WHERE t < ? AND ack_time IS NOT NULL", (cutoff,))

    # ---- reads --------------------------------------------------------
    def unit(self, unit_id):
        with self.lock:
            r = self.db.execute(f"SELECT {UNIT_COLS} FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(r) if r else None

    def units(self):
        with self.lock:
            rows = self.db.execute(f"SELECT {UNIT_COLS} FROM units ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def track(self, unit_id, t0, t1, limit=50000):
        with self.lock:
            rows = self.db.execute(
                "SELECT rx_time, fix_time, lat, lon, speed_kn, course FROM positions "
                "WHERE unit_id=? AND valid=1 AND rx_time BETWEEN ? AND ? "
                "ORDER BY rx_time LIMIT ?", (unit_id, t0, t1, limit)).fetchall()
        return [dict(r) for r in rows]

    def positions_for_export(self, unit_id, t0, t1):
        q = ("SELECT unit_id, rx_time, fix_time, lat, lon, speed_kn, course, valid, raw "
             "FROM positions WHERE rx_time BETWEEN ? AND ?")
        args = [t0, t1]
        if unit_id:
            q += " AND unit_id=?"
            args.append(unit_id)
        with self.lock:
            return [dict(r) for r in self.db.execute(q + " ORDER BY rx_time", args).fetchall()]

    def events(self, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT rx_time, kind, unit_id, detail FROM events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
