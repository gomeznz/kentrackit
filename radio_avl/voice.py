"""
Voice recording: the monitor radio's receive audio, recorded per transmission.

The radio already decodes the NXDN voice; its audio output goes into a USB sound card on the Pi.
The serial port's busy (S1) / clear (S0) frames start and stop each recording, with a short
pre-roll so the first syllable isn't lost. Data bursts (position reports) are discarded. Each
recording is attached to its entry in the transmission log.

Audio source (Settings -> Voice recording -> Sound card)
  alsa device, e.g. "plughw:CARD=Device,DEV=0"   captured with `arecord`
  "test"                                         a synthetic signal, for testing without hardware

Files are Opus (about 2 kB/s at 16 kbit/s) when opusenc or ffmpeg is installed, else WAV.
"""
import array
import collections
import datetime
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import wave

log = logging.getLogger("radio-avl")
CHUNK_MS = 20
MAX_SECONDS = 600


def list_devices():
    """Capture devices from `arecord -l`."""
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devs = []
    for m in re.finditer(r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): [^\[]*\[([^\]]*)\]", out, re.M):
        num, cid, cname, dev, dname = m.groups()
        devs.append({"device": f"plughw:CARD={cid},DEV={dev}", "name": f"{cname} – {dname}", "card": cid})
    return devs


def encoder():
    if shutil.which("opusenc"):
        return "opusenc"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    return None


class Source:
    """Yields 20 ms chunks of 16-bit mono PCM."""

    def __init__(self, device, rate):
        self.device, self.rate, self.proc = device, rate, None
        self.n = int(rate * CHUNK_MS / 1000) * 2
        self.t = 0.0

    def open(self):
        if self.device == "test":
            return
        if not shutil.which("arecord"):
            raise RuntimeError("arecord isn't installed (sudo apt install alsa-utils)")
        self.proc = subprocess.Popen(["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(self.rate),
                                      "-c", "1", "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read(self):
        if self.device == "test":
            # a voice-ish test signal: a warbling tone with syllable-like bursts, paced in real time
            time.sleep(CHUNK_MS / 1000)
            k = self.n // 2
            a = array.array("h")
            for i in range(k):
                t = self.t + i / self.rate
                env = 0.5 + 0.5 * math.sin(2 * math.pi * 3.5 * t)
                f = 300 + 120 * math.sin(2 * math.pi * 0.7 * t)
                a.append(int(6000 * env * math.sin(2 * math.pi * f * t)))
            self.t += k / self.rate
            return a.tobytes()
        data = self.proc.stdout.read(self.n)
        if not data:
            err = self.proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(err or "the sound card stopped sending audio")
        return data

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc = None


class VoiceRecorder:
    def __init__(self, app):
        self.app = app
        self.mx = threading.Lock()
        self.pre = collections.deque()
        self.cur = None             # {"chunks": [...], "start": t, "stop_at": t|None, "tx": tx|None}
        self.level = {"peak": 0.0, "rms": 0.0, "t": 0}
        self.status = {"state": "off", "error": "", "since": time.time()}
        self.restart = threading.Event()
        self.saved = 0
        self.paused_for_disk = False

    @property
    def cfg(self):
        return self.app.cfg["recording"]

    @property
    def root(self):
        return os.path.join(self.app.cfg["data_dir"], "recordings")

    # ---- triggers from the channel tracker -------------------------------------
    def busy_start(self, t):
        with self.mx:
            if self.cur:                      # a new transmission during the hang time: close the last one
                self._close(self.cur)
            if not self.cfg["enabled"] or self.status["state"] != "listening" or self.paused_for_disk:
                self.cur = None
                return
            self.cur = {"chunks": list(self.pre), "start": t - len(self.pre) * CHUNK_MS / 1000,
                        "stop_at": None, "tx": None, "kind": None}

    def busy_end(self, tx):
        """The channel tracker has logged the transmission (tx may be None if it was too short)."""
        with self.mx:
            if not self.cur:
                return
            if not tx or (tx["kind"] == "data" and self.cfg["voice_only"]):
                self.cur = None
                return
            self.cur["tx"] = tx
            self.cur["stop_at"] = time.time() + self.cfg["hang_ms"] / 1000

    # ---- capture loop -----------------------------------------------------------------
    def run(self):
        while not self.app.stop.is_set():
            c = self.cfg
            listen = self.app.cfg["console"]["enabled"]
            if not (c["enabled"] or listen) or not c["device"]:
                self._state("off")
                self.restart.wait(2); self.restart.clear()
                continue
            src = Source(c["device"], c["rate"])
            try:
                src.open()
                self._state("listening")
                pre_n = max(1, c["preroll_ms"] // CHUNK_MS)
                while not self.app.stop.is_set() and not self.restart.is_set():
                    data = src.read()
                    self._meter(data)
                    self.app.talk.listen_feed(data, c["rate"])
                    with self.mx:
                        self.pre.append(data)
                        while len(self.pre) > pre_n:
                            self.pre.popleft()
                        cur = self.cur
                        if cur:
                            cur["chunks"].append(data)
                            if cur["stop_at"] and time.time() >= cur["stop_at"]:
                                self._close(cur)
                                self.cur = None
                            elif len(cur["chunks"]) * CHUNK_MS / 1000 > MAX_SECONDS:
                                self._close(cur)
                                self.cur = None
            except Exception as e:
                self._state("error", str(e)[:300])
                log.warning("voice recorder: %s", e)
                self.restart.wait(10)
            finally:
                src.close()
                self.restart.clear()

    def _state(self, s, err=""):
        if self.status["state"] != s or self.status["error"] != err:
            self.status = {"state": s, "error": err, "since": time.time()}
            self.app.broker.publish("voice_status", self.status, sticky=True)

    def _meter(self, data):
        a = array.array("h"); a.frombytes(data)
        if not a:
            return
        peak = max(abs(min(a)), abs(max(a))) / 32768
        rms = math.sqrt(sum(x * x for x in a[::4]) / max(1, len(a[::4]))) / 32768
        lv = self.level
        lv["peak"] = max(peak, lv["peak"] * 0.85)
        lv["rms"] = rms
        lv["t"] = time.time()

    def levels(self):
        db = lambda v: round(20 * math.log10(v), 1) if v > 1e-5 else -100
        return {"peak_db": db(self.level["peak"]), "rms_db": db(self.level["rms"]),
                "fresh": time.time() - self.level["t"] < 1}

    # ---- saving ------------------------------------------------------------------------
    def _close(self, cur):
        if not cur.get("tx"):
            return
        threading.Thread(target=self._save, args=(cur,), daemon=True).start()

    def _save(self, cur):
        c, tx = self.cfg, cur["tx"]
        try:
            d = datetime.datetime.fromtimestamp(tx["start"])
            rel_dir = f"{d:%Y/%m/%d}"
            os.makedirs(os.path.join(self.root, rel_dir), exist_ok=True)
            base = os.path.join(self.root, rel_dir, f"tx{tx['id']}_{d:%H%M%S}")
            wav = base + ".wav"
            with wave.open(wav, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(c["rate"])
                w.writeframes(b"".join(cur["chunks"]))
            path = wav
            enc = encoder() if c["format"] == "opus" else None
            if enc:
                out = base + ".opus"
                cmd = (["opusenc", "--quiet", "--bitrate", str(c["bitrate_kbps"]), wav, out] if enc == "opusenc" else
                       ["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", f"{c['bitrate_kbps']}k",
                        "-application", "voip", out])
                if subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0 and os.path.getsize(out) > 0:
                    os.unlink(wav)
                    path = out
            rel = os.path.relpath(path, self.root)
            secs = round(len(cur["chunks"]) * CHUNK_MS / 1000, 1)
            self.app.channel.attach_audio(tx["id"], rel, secs)
            self.saved += 1
            self.app.broker.publish("recording", {"tx_id": tx["id"], "unit_id": tx["unit_id"], "seconds": secs})
        except Exception as e:
            log.warning("saving recording: %s", e)
            self.app.store.add_event("voice", f"couldn't save a recording: {e}")

    def file_for(self, rel):
        """Absolute path of a recording, or None (refuses anything outside the recordings folder)."""
        if not rel:
            return None
        p = os.path.normpath(os.path.join(self.root, rel))
        return p if p.startswith(os.path.abspath(self.root) + os.sep) and os.path.isfile(p) else None

    # ---- housekeeping ------------------------------------------------------------------
    def housekeeping(self):
        c = self.cfg
        try:
            free_mb = shutil.disk_usage(self.app.cfg["data_dir"]).free / 1e6
        except OSError:
            free_mb = 1e9
        low = free_mb < c["min_free_mb"]
        if low and not self.paused_for_disk:
            self.app.alerts.raise_alert("voice", f"Voice recording paused: only {free_mb:.0f} MB of disk left")
        self.paused_for_disk = low
        if c["keep_days"]:
            cutoff = time.time() - c["keep_days"] * 86400
            for rel in self.app.channel.expire_audio(cutoff):
                p = self.file_for(rel)
                if p:
                    os.unlink(p)
            # tidy empty day folders
            for dirpath, dirs, files in os.walk(self.root, topdown=False):
                if dirpath != self.root and not dirs and not files:
                    os.rmdir(dirpath)

    def usage(self):
        n = size = 0
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                n += 1
                size += os.path.getsize(os.path.join(dirpath, f))
        return {"files": n, "bytes": size}

    def info(self):
        return dict(self.status, saved=self.saved, paused_for_disk=self.paused_for_disk,
                    encoder=encoder() if self.cfg["format"] == "opus" else None, levels=self.levels())
