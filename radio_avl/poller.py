"""
Smart polling: KenTrackIt asks units for their position itself, instead of KAS-20.

Modes
  off      nothing is sent (default)
  dry_run  decides who it would poll and when, logs it, sends nothing; for checking the schedule
           while KAS-20 is still running
  live     sends the poll command to the radio's serial port

The poll command isn't known yet (it has to be captured from KAS-20 on a bench setup), so it's a
template in Settings. Placeholders:
  {id}    unit ID as reported, e.g. U00112
  {num}   unit number without leading zeros, e.g. 112
  {num5}  unit number as 5 digits, e.g. 00112
  {seq}   2-digit rolling counter, like the radio's own frames
Escapes: \\x02 \\x03 \\r \\n (or write the command as hex with format "hex").

Scheduling
  - emergency units: every `emergency_s`
  - moving units:    every `moving_s`
  - parked units:    every `parked_s`
  - a unit that reported recently anyway (e.g. KAS-20 polled it) isn't polled again yet
  - "Poll now" from the map jumps the queue
  - waits for the channel to be clear for `guard_s` before sending; one poll in flight at a time
  - no reply within `timeout_s` = missed poll, recorded on the coverage map
"""
import codecs
import logging
import re
import threading
import time

log = logging.getLogger("radio-avl")


def build_command(cfg, unit_id, seq):
    num = re.sub(r"^[A-Za-z]*0*", "", unit_id) or "0"
    text = cfg["command"].replace("{id}", unit_id).replace("{num5}", num.zfill(5)) \
        .replace("{num}", num).replace("{seq}", f"{seq % 100:02d}")
    if cfg.get("format") == "hex":
        return bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", text))
    return codecs.decode(text.encode("latin-1"), "unicode_escape").encode("latin-1")


class Poller:
    def __init__(self, app):
        self.app = app
        self.mx = threading.Lock()
        self.last_poll = {}          # unit -> t
        self.queue = []              # on-demand unit ids
        self.pending = None          # {"unit", "t", "on_demand"}
        self.sent = []               # timestamps of recent polls (rate limit)
        self.seq = 0
        self.stats = {"sent": 0, "answered": 0, "missed": 0, "dry_run": 0, "last": None}
        self.missed_by_unit = {}

    @property
    def cfg(self):
        return self.app.cfg["polling"]

    def request(self, unit_id):
        with self.mx:
            if unit_id not in self.queue:
                self.queue.append(unit_id)

    def on_position(self, unit_id, t):
        with self.mx:
            if self.pending and self.pending["unit"] == unit_id:
                self.stats["answered"] += 1
                self.app.broker.publish("poll", {"unit": unit_id, "result": "answered",
                                                 "rtt": round(t - self.pending["t"], 2)})
                self.pending = None

    def _interval(self, u, emerg):
        c = self.cfg
        if u["id"] in emerg:
            return c["emergency_s"]
        if (u["last_speed_kn"] or 0) * 1.852 > 3:
            return c["moving_s"]
        return c["parked_s"]

    def _choose(self, now):
        units = self.app.store.units()
        wanted = {x.strip() for x in self.cfg.get("units", "").split(",") if x.strip()}
        if wanted:
            known = {u["id"] for u in units}
            units = [u for u in units if u["id"] in wanted]
            # units listed in settings get polled even if they've never answered yet
            units += [{"id": w, "last_seen": None, "last_speed_kn": None} for w in sorted(wanted - known)]
        if self.queue:
            uid = self.queue.pop(0)
            return uid, True
        emerg = {a["unit_id"] for a in self.app.store.alerts(True, 500) if a["severity"] == "critical" and a["unit_id"]}
        best, best_over = None, 0
        for u in units:
            iv = self._interval(u, emerg)
            since_poll = now - self.last_poll.get(u["id"], 0)
            since_heard = now - (u["last_seen"] or 0)
            if since_poll < iv or since_heard < iv * 0.8:
                continue
            over = min(since_poll, since_heard) - iv
            if best is None or over > best_over:
                best, best_over = u["id"], over
        return best, False

    def tick(self):
        """Called a few times a second from the poller thread."""
        c, now = self.cfg, self.app.now()
        mode = c["mode"]
        with self.mx:
            if self.pending:
                if now - self.pending["t"] < c["timeout_s"]:
                    return
                self._missed(self.pending, now)
                self.pending = None
            if mode == "off":
                self.queue.clear()
                return
            ch = self.app.channel
            if ch.busy or now - ch.last_clear < c["guard_s"]:
                return
            self.sent = [x for x in self.sent if now - x < 60]
            if len(self.sent) >= c["max_per_min"]:
                return
            uid, on_demand = self._choose(now)
            if not uid:
                return
            self.last_poll[uid] = now
            self.sent.append(now)
            self.seq += 1
            if mode == "dry_run" or not c["command"]:
                self.stats["dry_run"] += 1
                self.stats["last"] = {"unit": uid, "t": now, "mode": "dry_run"}
                self.app.broker.publish("poll", {"unit": uid, "result": "dry_run", "on_demand": on_demand})
                return
            try:
                data = build_command(c, uid, self.seq)
            except Exception as e:
                self.app.store.add_event("poll", f"bad poll command template: {e}")
                return
            if not self.app.serial_write(data):
                return
            self.pending = {"unit": uid, "t": now, "on_demand": on_demand}
            self.stats["sent"] += 1
            self.stats["last"] = {"unit": uid, "t": now, "mode": "live"}
            self.app.channel.record(now, now + 0.5, "poll", uid, "", {"bytes": data.hex()})
            self.app.broker.publish("poll", {"unit": uid, "result": "sent", "on_demand": on_demand})

    def _missed(self, p, now):
        uid = p["unit"]
        self.stats["missed"] += 1
        self.missed_by_unit[uid] = self.missed_by_unit.get(uid, 0) + 1
        u = self.app.store.unit(uid)
        if u:
            self.app.coverage.on_poll_missed(uid, u["last_lat"], u["last_lon"], now)
        self.app.broker.publish("poll", {"unit": uid, "result": "missed"})
        if p["on_demand"]:
            self.app.store.add_event("poll", f"{uid} didn't answer a poll", uid, now)

    def run(self):
        while not self.app.stop.wait(0.25):
            try:
                self.tick()
            except Exception as e:
                log.warning("poller: %s", e)

    def status(self):
        return dict(self.stats, mode=self.cfg["mode"], pending=self.pending,
                    queue=list(self.queue), missed_by_unit=dict(self.missed_by_unit))
