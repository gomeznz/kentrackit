"""
KenTrackIt - Raspberry Pi replacement for Kenwood KAS-20.

  python3 -m radio_avl --config config.json
  python3 -m radio_avl --config config.json --replay raw_20261030_152012.log
"""
import argparse
import datetime
import logging
import os
import re
import signal
import sys
import threading
import time

from . import config as config_mod
from . import kenwood
from .alerts import AlertEngine
from .store import Store
from .web import Broker, TileCache, serve, rebind

log = logging.getLogger("radio-avl")

class App:
    VERSION = "0.3.1"

    def __init__(self, cfg, config_path=None, mode="serial"):
        self.cfg = cfg
        self.config_path = config_path
        self.mode = mode
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.store = Store(os.path.join(cfg["data_dir"], "avl.db"))
        self.broker = Broker()
        self.alerts = AlertEngine(self)
        self.tiles = TileCache(cfg, cfg["data_dir"])
        self.sat_tiles = TileCache(cfg, cfg["data_dir"], "sat")
        self.servers = []
        self.auth_cache = set()
        self.cfg_lock = threading.Lock()
        self.last_seq = None
        self.clock_offset = 0.0
        self.stop = threading.Event()
        self.reconnect = threading.Event()
        self.restart_requested = False
        self.stats = {"started": time.time(), "frames": 0, "positions": 0, "seq_gaps": 0,
                      "last_frame": None, "link_up": False, "link_detail": "",
                      "serial_port": None}

    # ---- settings -----------------------------------------------------
    def update_settings(self, patch):
        """Validate, save and apply a settings change. Returns (cfg, notes)."""
        with self.cfg_lock:
            old = self.cfg
            new = config_mod.apply_patch(old, patch)
            notes = []
            new_server = None
            if new["web"]["port"] != old["web"]["port"]:
                new_server = rebind(self, new)          # raises if the port is taken
            elif new["web"]["host"] != old["web"]["host"]:
                notes.append("Restart the service to apply the new listen address.")
            if self.config_path:
                try:
                    config_mod.save(self.config_path, new)
                except OSError as e:
                    if new_server:
                        new_server.server_close()
                    raise config_mod.Invalid(f"Couldn't save settings: {e}")
            self.cfg = new
            if new["serial"] != old["serial"]:
                self.reconnect.set()
                notes.append("Reconnecting to the radio with the new serial settings.")
            if new["tiles"] != old["tiles"]:
                self.tiles = TileCache(new, new["data_dir"])
                self.sat_tiles = TileCache(new, new["data_dir"], "sat")
            if (new["web"]["username"], new["web"]["password_hash"]) != \
               (old["web"]["username"], old["web"]["password_hash"]):
                self.auth_cache.clear()
            if new_server:
                self.servers.append(new_server)
                threading.Timer(2.0, self._retire_servers, [new_server]).start()
                notes.append(f"Web page moved to port {new['web']['port']}.")
            self.store.add_event("settings", "Settings changed: " + ", ".join(sorted(patch.keys())))
            return new, notes

    def _retire_servers(self, keep):
        for srv in [s for s in self.servers if s is not keep]:
            srv.shutdown()
            srv.server_close()
            self.servers.remove(srv)

    # ---- decoded frame handling ---------------------------------------
    def handle(self, frame: bytes, rx_time: float):
        d = kenwood.decode(frame)
        self.stats["frames"] += 1
        self.stats["last_frame"] = rx_time
        self._check_seq(d.get("seq"), rx_time)
        kind = d["kind"]

        if kind == "position":
            if d["checksum_ok"] is False and self.cfg["drop_bad_checksum"]:
                self.store.add_event("bad_checksum", d["raw"], d["unit"], rx_time)
                return
            rx_time = self._discipline_clock(d, rx_time)
            unit = self.store.add_position(d, rx_time)
            self.stats["positions"] += 1
            zones = self.alerts.on_position(d, unit, rx_time)
            self.broker.publish("position", {
                "unit": unit, "rx_time": rx_time, "valid": d["valid"], "zones": zones["fences"],
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
            self.alerts.on_display(d, rx_time)
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
            self.stats["seq_gaps"] += 1
            self.store.add_event("seq_gap", f"expected {(self.last_seq + 1) % 100:02d} got {seq:02d}",
                                 None, rx_time)
        self.last_seq = seq

    def link(self, up, detail=""):
        self.stats["link_up"], self.stats["link_detail"] = up, detail
        self.broker.publish("link", {"up": up, "detail": detail, "t": time.time()}, sticky=True)

    # ---- sources ------------------------------------------------------
    def run_serial(self):
        import serial  # imported here so --replay works without pyserial
        while not self.stop.is_set():
            self.reconnect.clear()
            port, baud = self.cfg["serial"]["port"], self.cfg["serial"]["baud"]
            self.stats["serial_port"] = port
            try:
                with serial.Serial(port, baud, timeout=0.1, exclusive=True) as ser:
                    log.info("serial open %s @ %d", port, baud)
                    self.link(True, f"{port} @ {baud}")
                    self.store.add_event("link", f"serial open {port} @ {baud}")
                    reader, self.last_seq = kenwood.FrameReader(), None
                    while not self.stop.is_set() and not self.reconnect.is_set():
                        data = ser.read(512)
                        if data:
                            now = self.now()
                            for fr in reader.feed(data):
                                self.handle(fr, now)
                self.link(False, "reconnecting")
            except Exception as e:  # unplugged, permission, port busy, etc.
                log.warning("serial %s: %s", port, e)
                self.link(False, str(e))
                self.store.add_event("link", f"serial error: {e}")
                for _ in range(50):                 # 5 s, but react to settings changes
                    if self.stop.is_set() or self.reconnect.is_set():
                        break
                    time.sleep(0.1)

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
        last_prune = 0
        while not self.stop.wait(30):
            try:
                self.alerts.check_overdue()
                if time.time() - last_prune > 3600:
                    self.store.prune(self.cfg["keep_days"])
                    last_prune = time.time()
            except Exception as e:
                log.warning("housekeeping: %s", e)


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

    cfg = config_mod.load(a.config)
    app = App(cfg, a.config, "replay" if a.replay else "serial")
    try:
        serve(app)
    except OSError as e:
        log.error("can't listen on port %d: %s. Another service may be using it; "
                  "change web.port in %s", cfg["web"]["port"], e, a.config)
        return 2
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
    if app.restart_requested:
        return 3                      # non-zero so systemd restarts us


if __name__ == "__main__":
    sys.exit(main())
