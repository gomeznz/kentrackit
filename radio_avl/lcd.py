"""
Front-panel status on a 20x4 (or 16x2) character LCD: HD44780 with the usual PCF8574 I2C "backpack".

No extra Python packages: it talks to /dev/i2c-N directly. Enable I2C with `sudo raspi-config`
(Interface Options -> I2C); install.sh adds the service user to the i2c group.

Wiring (Pi header):  GND -> pin 6 · VCC -> pin 2 (5 V) · SDA -> pin 3 · SCL -> pin 5
Most backpacks are at address 0x27; some (PCF8574A) at 0x3F. Settings -> Front-panel display can scan.

What it shows (20x4):
    KenTrackIt     13:42        title + clock
    ON AIR MORRIS   12s        who's talking right now, or "Channel clear" / "NO RADIO LINK"
    Last MORRIS     3m         the last voice caller
    Units 8/10 Alerts 0        rotating: units heard, calls today, web address
An emergency takes over the screen (and flashes the backlight) until it's acknowledged.
"""
import fcntl
import logging
import os
import socket
import threading
import time

log = logging.getLogger("radio-avl")

I2C_SLAVE = 0x0703
LCD_ADDRESSES = list(range(0x20, 0x28)) + list(range(0x38, 0x40))   # PCF8574 / PCF8574A

# PCF8574 pin -> LCD: P0 RS, P1 RW, P2 EN, P3 backlight, P4-P7 D4-D7
RS, EN, BL = 0x01, 0x04, 0x08
ROW_ADDR = (0x00, 0x40, 0x14, 0x54)


class LCD:
    def __init__(self, bus, addr, cols=20, rows=4):
        self.cols, self.rows, self.bl = cols, rows, BL
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, addr)
            self._init()
        except OSError:
            os.close(self.fd)
            raise
        self.shown = [None] * rows

    def close(self):
        try:
            self.clear()
            self.backlight(False)
        except OSError:
            pass
        os.close(self.fd)

    # ---- low level -------------------------------------------------------
    def _w(self, b):
        os.write(self.fd, bytes([b | self.bl]))

    def _nibble(self, n, mode):
        b = (n & 0xF0) | mode
        self._w(b | EN)
        self._w(b)

    def _byte(self, v, mode=0):
        self._nibble(v & 0xF0, mode)
        self._nibble((v << 4) & 0xF0, mode)

    def _init(self):
        self._w(0)
        time.sleep(0.05)
        for _ in range(3):                  # force 8-bit mode, whatever state it was left in
            self._nibble(0x30, 0)
            time.sleep(0.005)
        self._nibble(0x20, 0)               # then 4-bit
        for c in (0x28, 0x0C, 0x06):        # 2-line mode/5x8, display on no cursor, left-to-right
            self._byte(c)
        self.clear()
        # custom characters: 0 = antenna, 1 = bell
        for i, rows in enumerate(((0x15, 0x15, 0x0E, 0x04, 0x04, 0x04, 0x04, 0x00),
                                  (0x04, 0x0E, 0x0E, 0x0E, 0x1F, 0x00, 0x04, 0x00))):
            self._byte(0x40 | (i << 3))
            for r in rows:
                self._byte(r, RS)

    def clear(self):
        self._byte(0x01)
        time.sleep(0.002)
        self.shown = [None] * self.rows

    def backlight(self, on):
        nb = BL if on else 0
        if nb != self.bl:
            self.bl = nb
            self._w(0)

    def show(self, lines):
        """Write only the lines that changed (cheap on the shared I2C bus)."""
        for r in range(self.rows):
            text = (lines[r] if r < len(lines) else "").ljust(self.cols)[:self.cols]
            if text == self.shown[r]:
                continue
            self._byte(0x80 | ROW_ADDR[r])
            for ch in text:
                self._byte(ch if isinstance(ch, int) else _rom(ch), RS)
            self.shown[r] = text


ANTENNA, BELL = "\x00", "\x01"


def _rom(ch):
    o = ord(ch)
    if o < 8:
        return o                            # custom characters
    if 32 <= o < 126 and ch != "\\":
        return o
    return {"°": 0xDF, "·": 0xA5, "→": 0x7E, "←": 0x7F}.get(ch, ord("?"))


def scan(bus):
    """Which LCD-backpack addresses answer on this bus? Returns (addresses, error)."""
    path = f"/dev/i2c-{bus}"
    if not os.path.exists(path):
        return [], f"{path} doesn't exist: turn I2C on with sudo raspi-config (Interface Options → I2C)"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as e:
        return [], f"Can't open {path}: {e.strerror} (is the service user in the i2c group?)"
    found = []
    try:
        for a in LCD_ADDRESSES:
            try:
                fcntl.ioctl(fd, I2C_SLAVE, a)
                os.read(fd, 1)
                found.append(a)
            except OSError:
                pass
    finally:
        os.close(fd)
    return found, None


def buses():
    try:
        return sorted(int(n[4:]) for n in os.listdir("/dev") if n.startswith("i2c-") and n[4:].isdigit())
    except OSError:
        return []


def _ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))        # no packets sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return ""


def _ago(s):
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 86400}d"


def _lr(left, right, w):
    """left text, right text flush right, within w columns."""
    right = right[:w]
    return left[:max(0, w - len(right) - 1)].ljust(w - len(right)) + right


class FrontPanel:
    def __init__(self, app):
        self.app = app
        self.restart = threading.Event()
        self.state, self.error, self.lines = "off", "", []
        self.lcd = None
        self.active_until = 0.0

    def info(self):
        return {"state": self.state, "error": self.error, "lines": self.lines}

    # ---- what to show ---------------------------------------------------------
    def _name(self, unit_id, fallback=""):
        if not unit_id:
            return fallback
        u = self.app.store.unit(unit_id)
        return (u and u["alias"]) or self.app.channel.radio_names.get(unit_id) or unit_id

    def compose(self, now, cols=20, rows=4, rotate_s=5):
        app = self.app
        title = app.cfg["web"]["title"]
        clock = time.strftime("%H:%M", time.localtime(now))
        L = [_lr(title, clock, cols)]

        crit = [a for a in app.store.alerts(open_only=True, limit=20) if a["severity"] == "critical"]
        if crit:
            a = crit[0]
            flash = int(now * 2) % 2 == 0
            L.append((BELL + " EMERGENCY " + BELL).center(cols) if flash else "")
            L.append(self._name(a["unit_id"], "Unknown unit")[:cols])
            L.append(_lr(a["kind"].upper() if a["kind"] != "emergency" else "Ack on the map", _ago(now - a["t"]), cols))
            return L[:rows], "emergency", flash

        ch = app.channel
        if not app.stats.get("link_up"):
            L.append("NO RADIO LINK")
        elif ch.busy_since is not None:
            cur = ch.cur or {}
            who = self._name(cur.get("unit"), cur.get("name") or "")
            kind = "DATA" if cur.get("data") and not cur.get("voice") else "ON AIR"
            L.append(_lr(f"{ANTENNA}{kind} {who}".rstrip(), _ago(now - ch.busy_since), cols))
        else:
            L.append("Channel clear")

        lv = ch.last_voice
        L.append(_lr("Last " + self._name(lv["unit"], lv.get("name") or "unknown"), _ago(now - lv["end"]), cols)
                 if lv else "No calls yet")

        pages = []
        units = app.store.units()
        heard = sum(1 for u in units if u["last_seen"] and now - u["last_seen"] < app.cfg["display"]["lost_after_s"])
        n_alerts = len(app.store.alerts(open_only=True, limit=500))
        pages.append(_lr(f"Units {heard}/{len(units)}", f"Alerts {n_alerts}", cols))
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        calls = ch.count(midnight, now, "voice")
        pages.append(_lr(f"Calls today {calls}", "", cols))
        ip = _ip()
        if ip:
            pages.append(f"{ip}:{app.cfg['web']['port']}"[:cols])
        L.append(pages[int(now // max(1, rotate_s)) % len(pages)])
        return L[:rows], "active" if ch.busy_since is not None else "idle", True

    # ---- loop ---------------------------------------------------------------
    def run(self):
        while not self.app.stop.is_set():
            self.restart.clear()
            C = self.app.cfg["lcd"]
            if not C["enabled"]:
                self.state, self.error = "off", ""
                self._preview_loop()
                continue
            try:
                self.lcd = LCD(C["bus"], C["address"], C["cols"], C["rows"])
                self.state, self.error = "running", ""
                log.info("LCD on i2c-%s at 0x%02X", C["bus"], C["address"])
            except OSError as e:
                self.state = "error"
                if e.errno in (5, 6, 121):
                    self.error = f"No display answering at 0x{C['address']:02X} on i2c-{C['bus']}: check the wiring and address"
                elif e.errno == 2:
                    self.error = f"/dev/i2c-{C['bus']} doesn't exist: turn I2C on with sudo raspi-config (Interface Options → I2C)"
                elif e.errno == 13:
                    self.error = f"Not allowed to use /dev/i2c-{C['bus']}: run sudo ./install.sh again, then restart"
                else:
                    self.error = f"/dev/i2c-{C['bus']}: {e.strerror}"
                log.warning("LCD: %s", self.error)
                self._preview_loop(10)
                continue
            try:
                while not self.restart.is_set() and not self.app.stop.is_set():
                    now = self.app.now()
                    lines, mood, phase = self.compose(now, C["cols"], C["rows"], C["rotate_s"])
                    self.lines = lines
                    if mood != "idle":
                        self.active_until = time.time() + 30
                    mode = C["backlight"]
                    on = mode == "on" or (mode == "auto" and time.time() < self.active_until)
                    if mood == "emergency":         # always lit, flashing if wanted
                        on = phase or not C["flash_emergency"]
                    self.lcd.show(lines)
                    self.lcd.backlight(on)
                    self.restart.wait(0.5)
            except OSError as e:
                self.state, self.error = "error", f"Lost the display: {e.strerror}"
                log.warning("LCD: %s", self.error)
                self.restart.wait(5)
            finally:
                try:
                    self.lcd.close()
                except OSError:
                    pass
                self.lcd = None

    def _preview_loop(self, timeout=None):
        """No hardware: keep the on-screen preview in Settings up to date."""
        t_end = time.time() + timeout if timeout else None
        C = self.app.cfg["lcd"]
        while not self.restart.is_set() and not self.app.stop.is_set():
            try:
                self.lines = self.compose(self.app.now(), C["cols"], C["rows"], C["rotate_s"])[0]
            except Exception:      # noqa: BLE001 - the preview must never kill the thread
                log.exception("LCD preview")
            if t_end and time.time() > t_end:
                return
            self.restart.wait(1)
