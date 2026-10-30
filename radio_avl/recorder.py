"""
Raw serial recorder: while KenTrackIt holds the radio's serial port, nxdn_listen.py can't run, so
captures (a voice call, a status message, an emergency, a poll) are made from Settings instead.
Files use the nxdn_listen.py raw log format, so they can be replayed with --replay.
Bytes KenTrackIt sends to the radio are logged as "TX" lines (ignored by replay).
"""
import datetime
import os
import re
import threading
import time

MAX_BYTES = 20 * 1024 * 1024
NAME_RE = re.compile(r"^capture_\d{8}_\d{6}\.log$")


def _fmt(data):
    return data.hex(" ") + "  |" + "".join(chr(c) if 32 <= c < 127 else "." for c in data) + "|"


class Recorder:
    def __init__(self, app):
        self.app = app
        self.dir = os.path.join(app.cfg["data_dir"], "captures")
        self.mx = threading.Lock()
        self.fh = None
        self.name = None
        self.until = 0
        self.size = 0
        self.by = None

    def start(self, minutes, user=None):
        with self.mx:
            self._close()
            os.makedirs(self.dir, exist_ok=True)
            self.name = f"capture_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
            self.fh = open(os.path.join(self.dir, self.name), "a")
            self.until = time.time() + max(1, min(int(minutes), 120)) * 60
            self.size, self.by = 0, user
            return self.name

    def stop(self):
        with self.mx:
            self._close()

    def _close(self):
        if self.fh:
            self.fh.close()
        self.fh, self.until = None, 0

    def _write(self, prefix, data, t):
        with self.mx:
            if not self.fh:
                return
            if time.time() > self.until or self.size > MAX_BYTES:
                self._close()
                return
            ts = datetime.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")
            line = f"{ts} {prefix}{_fmt(data)}\n"
            self.fh.write(line)
            self.fh.flush()
            self.size += len(line)

    def rx(self, data, t):
        if self.fh:
            self._write("", data, t)

    def tx(self, data, t):
        if self.fh:
            self._write("TX ", data, t)

    def status(self):
        with self.mx:
            if self.fh and time.time() > self.until:
                self._close()
            return {"recording": bool(self.fh), "name": self.name if self.fh else None,
                    "until": self.until if self.fh else None, "bytes": self.size if self.fh else 0}

    def files(self):
        if not os.path.isdir(self.dir):
            return []
        out = []
        for n in sorted(os.listdir(self.dir), reverse=True):
            if NAME_RE.match(n):
                p = os.path.join(self.dir, n)
                out.append({"name": n, "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return out

    def path(self, name):
        if not NAME_RE.match(name or ""):
            return None
        p = os.path.join(self.dir, name)
        return p if os.path.isfile(p) else None

    def delete(self, name):
        p = self.path(name)
        if p and not (self.fh and name == self.name):
            os.unlink(p)
            return True
        return False
