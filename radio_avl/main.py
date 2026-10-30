"""
KenTrackIt - Raspberry Pi replacement for Kenwood KAS-20.

  python3 -m radio_avl --config config.json
  python3 -m radio_avl --config config.json --replay raw_20261030_152012.log
"""
import argparse
import copy
import datetime
import json
import logging
import os
import re
import signal
import sys
import threading
import time

from . import kenwood
from .store import Store
from .web import Broker, TileCache, serve

log = logging.getLogger("radio-avl")

DEFAULTS = {
    "serial": {"port": "/dev/ttyUSB0", "baud": 9600},
    "data_dir": "data",
    "keep_days": 365,
    "web": {"host": "0.0.0.0", "port": 8090, "title": "KenTrackIt",
            "username": "admin", "password": ""},
    "map": {"center": [-46.413, 168.353], "zoom": 12},
    "display": {"stale_after_s": 60, "lost_after_s": 900, "speed_units": "kmh"},
    "tiles": {"upstream": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
              "attribution": "&copy; OpenStreetMap contributors", "max_zoom": 18},
    "drop_bad_checksum": True,
}


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path):
    cfg = {}
    if path and os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    elif path:
        log.warning("config %s not found, using defaults", path)
    return merge(DEFAULTS, cfg)


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.store = Store(os.path.join(cfg["data_dir"], "avl.db"))
        self.broker = Broker()
        self.tiles = TileCache(cfg["tiles"], cfg["data_dir"])
        self.last_seq = None
        self.clock_offset = 0.0
        self.stop = threading.Event()

    # ---- decoded frame handling ---------------------------------------
    def handle(self, frame: bytes, rx_time: float):
        d = kenwood.decode(frame)
        self._check_seq(d.get("seq"), rx_time)
        kind = d["kind"]

        if kind == "position":
            if d["checksum_ok"] is False and self.cfg["drop_bad_checksum"]:
                self.store.add_event("bad_checksum", d["raw"], d["unit"], rx_time)
                return
            rx_time = self._discipline_clock(d, rx_time)
            unit = self.store.add_position(d, rx_time)
            self.broker.publish("position", {
                "unit": unit, "rx_time": rx_time, "valid": d["valid"],
                "lat": d["lat"], "lon": d["lon"],
                "speed_kn": d["speed_kn"], "course": d["course"]})
            log.info("unit %s %s %.5f,%.5f", d["unit"], "fix" if d["valid"] else "NO FIX",
                     d["lat"] or 0, d["lon"] or 0)
        elif kind == "busy":
            self.broker.publish("channel", {"busy": d["busy"], "t": rx_time}, sticky=True)
        elif kind == "display":
            self.broker.publish("display", {"fields": d["fields"], "message": d["message"],
                                            "t": rx_time}, sticky=True)
            if d["message"] and d["message"] not in ("Receive Data", "Complete"):
                self.store.add_event("display", d["message"], None, rx_time)
        elif kind == "status":
            self.broker.publish("status", {"flags": d["flags"]}, sticky=True)
        elif kind in ("sentence", "unknown"):
            detail = d.get("raw") or d.get("hex")
            self.store.add_event(kind, detail, None, rx_time)
            self.broker.publish("event", {"kind": kind, "detail": detail, "t": rx_time})

    def _discipline_clock(self, d, rx_time):
        """The Pi may have no RTC or NTP on site. If its clock disagrees with GPS time by
        more than 30 s, correct our timestamps from the GPS reports from then on."""
        ft = d.get("fix_time")
        if d["valid"] and d["checksum_ok"] is not False and ft and abs(ft - rx_time) > 30:
            delta = ft - rx_time
            self.clock_offset += delta
            self.store.add_event("clock", f"Pi clock off by {delta:+.0f} s; corrected from GPS",
                                 d["unit"], ft)
            log.warning("clock corrected by %+.0f s from GPS", delta)
            return ft
        return rx_time

    def now(self):
        return time.time() + self.clock_offset

    def _check_seq(self, seq, rx_time):
        if seq is None:
            return
        if self.last_seq is not None and seq != (self.last_seq + 1) % 100:
            self.store.add_event("seq_gap", f"expected {(self.last_seq + 1) % 100:02d} got {seq:02d}",
                                 None, rx_time)
        self.last_seq = seq

    def link(self, up, detail=""):
        self.broker.publish("link", {"up": up, "detail": detail, "t": time.time()}, sticky=True)

    # ---- sources ------------------------------------------------------
    def run_serial(self):
        import serial  # imported here so --replay works without pyserial
        port, baud = self.cfg["serial"]["port"], self.cfg["serial"]["baud"]
        while not self.stop.is_set():
            try:
                with serial.Serial(port, baud, timeout=0.1) as ser:
                    log.info("serial open %s @ %d", port, baud)
                    self.link(True, port)
                    self.store.add_event("link", f"serial open {port}")
                    reader, self.last_seq = kenwood.FrameReader(), None
                    while not self.stop.is_set():
                        data = ser.read(512)
                        if data:
                            now = self.now()
                            for fr in reader.feed(data):
                                self.handle(fr, now)
            except Exception as e:  # unplugged, permission, etc.
                log.warning("serial %s: %s", port, e)
                self.link(False, str(e))
                self.store.add_event("link", f"serial error: {e}")
                self.stop.wait(5)

    def run_replay(self, path, speed):
        """Feed a raw_*.log written by nxdn_listen.py."""
        line_re = re.compile(r"^(\S+) ((?:[0-9a-f]{2} ?)+) +\|")
        reader, prev = kenwood.FrameReader(), None
        self.link(True, f"replay {os.path.basename(path)}")
        with open(path) as f:
            for line in f:
                m = line_re.match(line)
                if not m:
                    continue
                t = datetime.datetime.fromisoformat(m.group(1)).timestamp()
                if speed and prev is not None:
                    self.stop.wait(max(0, (t - prev) / speed))
                prev = t
                for fr in reader.feed(bytes.fromhex(m.group(2).replace(" ", ""))):
                    self.handle(fr, t)
                if self.stop.is_set():
                    return
        log.info("replay finished")

    def housekeeping(self):
        while not self.stop.wait(3600):
            try:
                self.store.prune(self.cfg["keep_days"])
            except Exception as e:
                log.warning("prune: %s", e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="KenTrackIt")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--replay", help="replay a raw_*.log capture instead of the serial port")
    ap.add_argument("--speed", type=float, default=0,
                    help="replay speed multiplier (0 = as fast as possible)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(a.config)
    app = App(cfg)
    serve(app)
    log.info("web on http://%s:%d", cfg["web"]["host"], cfg["web"]["port"])
    threading.Thread(target=app.housekeeping, daemon=True).start()

    signal.signal(signal.SIGTERM, lambda *_: app.stop.set())
    try:
        if a.replay:
            app.run_replay(a.replay, a.speed)
            app.stop.wait()           # keep the web UI up after replay
        else:
            app.run_serial()
    except KeyboardInterrupt:
        pass
    app.stop.set()


if __name__ == "__main__":
    sys.exit(main())
