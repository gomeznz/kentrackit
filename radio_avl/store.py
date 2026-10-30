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
"""

UNIT_COLS = ("id, alias, colour, notes, first_seen, last_seen, last_lat, last_lon, "
             "last_speed_kn, last_course, last_fix_time, last_valid, reports")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

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

    def prune(self, keep_days):
        if not keep_days:
            return
        cutoff = time.time() - keep_days * 86400
        with self.lock, self.db:
            self.db.execute("DELETE FROM positions WHERE rx_time < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE rx_time < ?", (cutoff,))

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
