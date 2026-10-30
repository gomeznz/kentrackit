"""
Channel activity: every transmission the monitor radio hears (busy S1 .. clear S0) is logged with
its duration, whether it was data or voice, and which unit it was if we can tell.

This is the base for the channel-load page, the "on air" display and, later, dispatcher logging.
"""
import json
import logging
import threading
import time

log = logging.getLogger("radio-avl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transmissions (
    id INTEGER PRIMARY KEY,
    start REAL NOT NULL,
    end REAL NOT NULL,
    duration REAL NOT NULL,
    kind TEXT NOT NULL,             -- data | voice | poll
    unit_id TEXT,
    display TEXT,                   -- what the monitor radio's display showed during it
    detail TEXT                     -- JSON, room for later (recording file, talkgroup, ...)
);
CREATE INDEX IF NOT EXISTS ix_tx_start ON transmissions(start);
CREATE INDEX IF NOT EXISTS ix_tx_unit ON transmissions(unit_id, start);
"""
DATA_MESSAGES = ("Receive Data", "Complete")
STUCK_S = 120          # carrier longer than this raises an alert (stuck transmitter / open mic)


class Channel:
    def __init__(self, app):
        self.app = app
        self.db, self.lock = app.store.db, app.store.lock
        with self.lock:
            self.db.executescript(SCHEMA)
        self.mx = threading.Lock()
        self.busy_since = None
        self.last_clear = 0.0
        self.cur = None
        self.stuck_alerted = False
        self.idle_fields = set()    # what the display shows when nobody's talking (channel, zone)

    @property
    def busy(self):
        return self.busy_since is not None

    # ---- events from the radio --------------------------------------------
    def on_busy(self, busy, t):
        with self.mx:
            if busy and self.busy_since is None:
                self.busy_since = t
                self.cur = {"data": False, "unit": None, "texts": []}
                self.stuck_alerted = False
                self.app.broker.publish("onair", {"busy": True, "since": t, "unit": None, "kind": None}, sticky=True)
            elif not busy and self.busy_since is not None:
                self._finish(t)

    def on_position(self, unit_id, t):
        with self.mx:
            if self.cur is not None:
                changed = self.cur["unit"] != unit_id or not self.cur["data"]
                self.cur["data"], self.cur["unit"] = True, unit_id
                if changed:
                    self._publish_live()

    def on_display(self, fields, message, t):
        with self.mx:
            if not message or message in DATA_MESSAGES:
                # the radio's normal display (channel, zone) is everything except a pop-up message
                self.idle_fields = {f for f in fields if f and f != message}
                if not message:
                    return
            if self.cur is None:
                return
            if message in DATA_MESSAGES:
                self.cur["data"] = True
                return
            text = " ".join(f for f in fields if f and f not in self.idle_fields)
            if text and text not in self.cur["texts"]:
                self.cur["texts"].append(text)
            if not self.cur["unit"]:
                u = self.app.alerts.find_unit_in_text(text)
                if u:
                    self.cur["unit"] = u["id"]
                    self._publish_live()

    def tick(self, now):
        """Called by housekeeping: alert on a channel that's been busy too long."""
        with self.mx:
            if self.busy_since is not None and not self.stuck_alerted and now - self.busy_since > STUCK_S:
                self.stuck_alerted = True
                unit = self.cur and self.cur["unit"]
                self.app.alerts.raise_alert("channel", f"Channel busy for over {STUCK_S // 60} min "
                                            "(stuck transmitter or open microphone?)", unit, now)

    def _publish_live(self):
        self.app.broker.publish("onair", {"busy": True, "since": self.busy_since, "unit": self.cur["unit"],
                                          "kind": "data" if self.cur["data"] else None}, sticky=True)

    def _finish(self, t):
        start, cur = self.busy_since, self.cur
        self.busy_since, self.cur, self.last_clear = None, None, t
        dur = max(0.0, t - start)
        self.app.broker.publish("onair", {"busy": False, "t": t}, sticky=True)
        if dur < 0.05:
            return
        kind = "data" if cur["data"] else "voice"
        self.record(start, t, kind, cur["unit"], " / ".join(cur["texts"])[:300])

    def record(self, start, end, kind, unit_id=None, display="", detail=None):
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO transmissions (start, end, duration, kind, unit_id, display, detail) VALUES (?,?,?,?,?,?,?)",
                (start, end, round(end - start, 3), kind, unit_id, display, json.dumps(detail) if detail else None))
        tx = {"id": cur.lastrowid, "start": start, "end": end, "duration": round(end - start, 3),
              "kind": kind, "unit_id": unit_id, "display": display}
        self.app.broker.publish("tx", tx)
        return tx

    # ---- queries -----------------------------------------------------------------
    def log(self, t0, t1, unit_id=None, kind=None, limit=500, allowed=None):
        q, args = "SELECT id, start, end, duration, kind, unit_id, display FROM transmissions WHERE start BETWEEN ? AND ?", [t0, t1]
        if unit_id:
            q += " AND unit_id=?"; args.append(unit_id)
        if kind:
            q += " AND kind=?"; args.append(kind)
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q + " ORDER BY start DESC LIMIT ?", args + [limit * 3])]
        if allowed is not None:
            rows = [r for r in rows if r["unit_id"] is None or r["unit_id"] in allowed]
        return rows[:limit]

    def stats(self, t0, t1, bucket_s, tz_offset_s=0):
        """Airtime by kind in time buckets, plus a weekday x hour heatmap and per-unit totals."""
        with self.lock:
            rows = self.db.execute("SELECT start, end, kind, unit_id FROM transmissions WHERE end >= ? AND start <= ?",
                                   (t0, t1)).fetchall()
        n = max(1, int((t1 - t0) // bucket_s) + (1 if (t1 - t0) % bucket_s else 0))
        buckets = [{"t": t0 + i * bucket_s, "data": 0.0, "voice": 0.0, "poll": 0.0} for i in range(n)]
        heat = [[0.0] * 24 for _ in range(7)]         # seconds busy per weekday (Mon=0) x hour, local time
        per_unit, totals, counts = {}, {"data": 0.0, "voice": 0.0, "poll": 0.0}, {"data": 0, "voice": 0, "poll": 0}
        longest = None
        for r in rows:
            s, e, kind = max(r["start"], t0), min(r["end"], t1), r["kind"]
            if e <= s:
                continue
            kind = kind if kind in totals else "data"
            totals[kind] += e - s
            counts[kind] += 1
            if not longest or (r["end"] - r["start"]) > longest["duration"]:
                longest = {"start": r["start"], "duration": round(r["end"] - r["start"], 1), "kind": kind, "unit_id": r["unit_id"]}
            if r["unit_id"]:
                u = per_unit.setdefault(r["unit_id"], {"data": 0.0, "voice": 0.0, "poll": 0.0, "count": 0})
                u[kind] += e - s
                u["count"] += 1
            i = int((s - t0) // bucket_s)
            while s < e and i < n:
                b_end = t0 + (i + 1) * bucket_s
                seg = min(e, b_end) - s
                buckets[i][kind] += seg
                s += seg
                i += 1
            # heatmap, split at hour boundaries in local time
            s = max(r["start"], t0)
            while s < e:
                lt = time.gmtime(s + tz_offset_s)
                hour_end = s + (3600 - (lt.tm_min * 60 + lt.tm_sec))
                seg = min(e, hour_end) - s
                heat[lt.tm_wday][lt.tm_hour] += seg
                s += max(seg, 0.001)
        span = max(1.0, t1 - t0)
        # how many times each weekday/hour slot occurs in the range, to turn seconds into %
        slots = [[0] * 24 for _ in range(7)]
        h = t0 - ((t0 + tz_offset_s) % 3600)
        while h < t1:
            lt = time.gmtime(h + tz_offset_s)
            slots[lt.tm_wday][lt.tm_hour] += 1
            h += 3600
        heat_pct = [[round(100 * heat[d][hh] / (slots[d][hh] * 3600), 2) if slots[d][hh] else None
                     for hh in range(24)] for d in range(7)]
        busy = sum(totals.values())
        return {
            "from": t0, "to": t1, "bucket_s": bucket_s,
            "buckets": [{k: (round(v, 2) if k != "t" else v) for k, v in b.items()} for b in buckets],
            "totals": {k: round(v, 1) for k, v in totals.items()},
            "counts": counts,
            "utilisation_pct": round(100 * busy / span, 2),
            "heatmap_pct": heat_pct,
            "per_unit": {k: {kk: (round(vv, 1) if kk != "count" else vv) for kk, vv in v.items()} for k, v in per_unit.items()},
            "longest": longest,
            "busy_now": self.busy, "busy_since": self.busy_since,
        }

    def prune(self, cutoff):
        with self.lock, self.db:
            self.db.execute("DELETE FROM transmissions WHERE start < ?", (cutoff,))
