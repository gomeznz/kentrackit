"""
Radio coverage built from the fleet's own traffic.

Every position report is a "heard here" sample. A missed report is a "not heard here" sample:
  - when KenTrackIt polls (polling on): a poll with no reply, at the unit's last known position
  - passively (polling off): a moving unit that suddenly misses several of its usual reports;
    the missing reports are spread along the line between where it was last heard and where it
    was heard again ("inferred").
Samples are aggregated into a square grid for the map overlay.
"""
import math
import threading

from . import geo

SCHEMA = """
CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY,
    t REAL NOT NULL,
    unit_id TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL,
    heard INTEGER NOT NULL,          -- 1 heard, 0 missed
    source TEXT NOT NULL             -- report | inferred | poll
);
CREATE INDEX IF NOT EXISTS ix_cov_t ON coverage(t);
"""
MAX_GAP_S = 600        # longer silences are probably a radio switched off, not coverage
MIN_MOVE_M = 80        # a unit that hasn't moved and goes quiet is more likely off than out of range


class Coverage:
    def __init__(self, app):
        self.app = app
        self.db, self.lock = app.store.db, app.store.lock
        with self.lock:
            self.db.executescript(SCHEMA)
        self.mx = threading.Lock()
        self.last = {}          # unit -> (t, lat, lon)
        self.open_misses = {}   # unit -> [(row id, t)] polls missed since it was last heard
        self.interval = {}      # unit -> typical seconds between reports (EMA)

    def _add(self, rows):
        if rows:
            with self.lock, self.db:
                self.db.executemany("INSERT INTO coverage (t, unit_id, lat, lon, heard, source) VALUES (?,?,?,?,?,?)", rows)

    def on_report(self, unit_id, lat, lon, t, polling_active=False):
        """A valid position report arrived."""
        rows = [(t, unit_id, lat, lon, 1, "report")]
        with self.mx:
            prev = self.last.get(unit_id)
            self.last[unit_id] = (t, lat, lon)
            # Polls it missed were first pinned to where it was last heard. Now we know where it came
            # back, spread them along the way by time, so the red follows the actual dead area.
            misses = self.open_misses.pop(unit_id, [])
            if misses and prev and t > prev[0]:
                upd = []
                for rid, mt in misses:
                    f = min(1.0, max(0.0, (mt - prev[0]) / (t - prev[0])))
                    upd.append((prev[1] + (lat - prev[1]) * f, prev[2] + (lon - prev[2]) * f, rid))
                with self.lock, self.db:
                    self.db.executemany("UPDATE coverage SET lat=?, lon=? WHERE id=?", upd)
            if prev:
                gap = t - prev[0]
                est = self.interval.get(unit_id)
                if 3 <= gap <= 120:
                    self.interval[unit_id] = gap if est is None else est * 0.8 + gap * 0.2
                # Passive inference only when we aren't polling ourselves (polls give exact misses).
                if not polling_active and est and est * 2.5 < gap <= MAX_GAP_S:
                    moved = geo.haversine(prev[1], prev[2], lat, lon)
                    if moved >= MIN_MOVE_M:
                        n = min(int(round(gap / est)) - 1, 40)
                        for k in range(1, n + 1):
                            f = k / (n + 1)
                            rows.append((prev[0] + gap * f, unit_id, prev[1] + (lat - prev[1]) * f,
                                         prev[2] + (lon - prev[2]) * f, 0, "inferred"))
        self._add(rows)

    def on_poll_missed(self, unit_id, lat, lon, t):
        if lat is None:
            return
        with self.lock, self.db:
            rid = self.db.execute("INSERT INTO coverage (t, unit_id, lat, lon, heard, source) VALUES (?,?,?,?,?,?)",
                                  (t, unit_id, lat, lon, 0, "poll")).lastrowid
        with self.mx:
            self.open_misses.setdefault(unit_id, []).append((rid, t))

    def grid(self, t0, t1, cell_m=200, allowed=None, sources=None):
        q, args = "SELECT unit_id, lat, lon, heard, source FROM coverage WHERE t BETWEEN ? AND ?", [t0, t1]
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        if not rows:
            return {"cells": [], "cell_m": cell_m, "samples": 0}
        lat0 = sum(r["lat"] for r in rows[:200]) / min(len(rows), 200)
        dlat = cell_m / 111_320.0
        dlon = cell_m / (111_320.0 * max(0.2, math.cos(math.radians(lat0))))
        cells, n = {}, 0
        for r in rows:
            if allowed is not None and r["unit_id"] not in allowed:
                continue
            if sources and r["source"] not in sources:
                continue
            key = (math.floor(r["lat"] / dlat), math.floor(r["lon"] / dlon))
            c = cells.setdefault(key, [0, 0, 0])      # heard, missed(poll/inferred), inferred-only
            if r["heard"]:
                c[0] += 1
            else:
                c[1] += 1
                if r["source"] == "inferred":
                    c[2] += 1
            n += 1
        out = []
        for (i, j), (h, m, inf) in cells.items():
            out.append({"s": round(i * dlat, 6), "w": round(j * dlon, 6), "n": round((i + 1) * dlat, 6),
                        "e": round((j + 1) * dlon, 6), "heard": h, "missed": m, "inferred": inf,
                        "pct": round(100 * h / (h + m), 1)})
        return {"cells": out, "cell_m": cell_m, "samples": n}

    def unit_summary(self, t0, t1):
        with self.lock:
            rows = self.db.execute("SELECT unit_id, SUM(heard) h, SUM(1-heard) m FROM coverage "
                                   "WHERE t BETWEEN ? AND ? GROUP BY unit_id", (t0, t1)).fetchall()
        return {r["unit_id"]: {"heard": r["h"], "missed": r["m"]} for r in rows}

    def prune(self, cutoff):
        with self.lock, self.db:
            self.db.execute("DELETE FROM coverage WHERE t < ?", (cutoff,))
