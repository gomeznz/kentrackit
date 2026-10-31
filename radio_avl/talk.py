"""
Dispatcher talk-back: a dispatcher's browser microphone, out over the radio system.

Two ways out (Settings -> Dispatch console -> Talk path):

  k5tra     The monitor radio itself transmits. A K5TRA (CM119A) keys its PTT over USB HID and plays the
            dispatcher's audio into the radio's mic input. The radio's selected channel/group is used,
            unless a "private call" serial command is set: that's sent first, so only one unit hears.
  nxr_sip   A SIP call straight to the NXR-1800's SIP interface (no PBX), G.711 RTP. The NXR puts it out
            on its configured group. Calls the NXR places to us (group traffic) are played to the console.
  test      No hardware: everything behaves normally but nothing is transmitted.

One dispatcher talks at a time (the floor). Talking over a busy channel is refused unless allowed.
Every transmission is logged in the channel log (type "dispatch", with who sent it) and recorded.
"""
import array
import asyncio
import datetime
import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from collections import deque

from .poller import build_command
from .voice import encoder

log = logging.getLogger("radio-avl")
RATE = 8000
FRAME = 160                     # samples per 20 ms frame at 8 kHz
CMEDIA = "00000D8C"


def find_hidraw():
    out = []
    for p in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(p) as f:
                if CMEDIA in f.read().upper():
                    out.append("/dev/" + p.split("/")[4])
        except OSError:
            pass
    return out


def list_playback():
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [{"device": f"plughw:CARD={cid},DEV={dev}", "name": f"{cname} – {dname}"}
            for _, cid, cname, dev, dname in
            re.findall(r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): [^\[]*\[([^\]]*)\]", out, re.M)]


# ---- talk paths -----------------------------------------------------------------------------
class TestPath:
    name = "test"

    def __init__(self, talk):
        self.talk, self.ready, self.error = talk, True, ""

    def key(self, target):
        return True

    def audio(self, pcm):
        pass

    def unkey(self):
        pass

    def close(self):
        pass

    def status(self):
        return "Test mode: nothing is transmitted"


class K5traPath:
    """PTT on a CM119A GPIO via /dev/hidraw, audio out with aplay."""
    name = "k5tra"

    def __init__(self, talk):
        self.talk, self.cfg = talk, talk.cfg
        self.fd, self.player, self.error = None, None, ""
        path = self.cfg["hid_device"] or (find_hidraw() or [None])[0]
        if not path:
            self.error = "No K5TRA found (no C-Media USB device). Check it's plugged in."
        else:
            try:
                self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                self.hid = path
                self._ptt(False)
            except OSError as e:
                self.error = f"Can't open the K5TRA ({path}): {e.strerror}. Run sudo ./install.sh again, then restart."
        self.ready = self.fd is not None and bool(self._device())
        if self.fd is not None and not self._device():
            self.error = "Pick the sound card the radio's mic input is wired to"

    def _device(self):
        return self.cfg["playback_device"] or self.talk.app.cfg["recording"]["device"]

    def _ptt(self, on):
        if self.fd is None:
            return
        bit = 1 << (int(self.cfg["ptt_gpio"]) - 1)
        level = on != bool(self.cfg["ptt_invert"])
        os.write(self.fd, bytes([0, 0, bit if level else 0, bit, 0]))

    def key(self, target):
        dev = self._device()
        if dev == "test":
            self.player = None
        else:
            if not shutil.which("aplay"):
                raise RuntimeError("aplay isn't installed (sudo apt install alsa-utils)")
            self.player = subprocess.Popen(["aplay", "-q", "-D", dev, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                                            "-t", "raw", "--buffer-time=200000"],
                                           stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._ptt(True)
        return True

    def audio(self, pcm):
        if self.player and self.player.stdin:
            try:
                self.player.stdin.write(pcm)
                self.player.stdin.flush()
            except (BrokenPipeError, OSError):
                self.player = None

    def unkey(self):
        try:
            self._ptt(False)
        finally:
            if self.player:
                try:
                    self.player.stdin.close()
                    self.player.wait(timeout=2)
                except Exception:       # noqa: BLE001
                    self.player.kill()
                self.player = None

    def close(self):
        try:
            self._ptt(False)
        except OSError:
            pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def status(self):
        return self.error or f"K5TRA on {self.hid}, PTT on GPIO {self.cfg['ptt_gpio']}, audio to {self._device()}"


class SipPath:
    """Calls the NXR's SIP interface. The call is held open for a few seconds after each over."""
    name = "nxr_sip"

    def __init__(self, talk):
        self.talk, self.cfg = talk, talk.cfg
        self.ready, self.error = False, ""
        self.frames = deque()
        self.call = None
        self.ua = None
        try:
            import numpy  # noqa: F401  (needed for the RTP audio)
        except ImportError:
            self.error = "NXR SIP needs numpy: sudo apt install python3-numpy, then restart"
            return
        from . import sip
        self.sip = sip
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="sip")
        self.thread.start()
        try:
            self.ua = self._run(self._start(), 10)
            self.ready = True
        except Exception as e:      # noqa: BLE001
            self.error = f"SIP: {e}"
        self.hold_until = 0.0

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _sipcfg(self):
        c = self.cfg
        return {"target_host": c["nxr_host"], "target_port": c["nxr_port"], "target_user": c["nxr_user"],
                "local_port": c["sip_local_port"], "local_user": "kentrackit", "display_name": "Dispatch",
                "auth_user": c["sip_auth_user"], "auth_password": c["sip_auth_password"],
                "register": c["sip_register"], "register_expires": 300, "codec": c["sip_codec"], "ptime_ms": 20,
                "dtmf_mode": "rfc2833", "send_gain_db": 0.0, "inbound_enabled": c["sip_inbound"], "inbound_allow": ""}

    async def _start(self):
        ua = self.sip.SipUA(self._sipcfg())
        await ua.start()
        ua.on_incoming = self._incoming
        return ua

    def _incoming(self, call):
        """The NXR called us (group traffic): play it to the console."""
        self.talk.app.store.add_event("dispatch", "NXR call in")
        threading.Thread(target=self._pump, args=(call,), daemon=True).start()

    def _pump(self, call):
        while call.state not in ("ended", "failed"):
            while call.rx_frames:
                self.talk.listen_feed(call.rx_frames.popleft().tobytes(), RATE)
            time.sleep(0.02)

    def key(self, target):
        if self.call and self.call.state not in ("ended", "failed"):
            return True
        self.frames.clear()
        self.call = self._run(self.ua.place_call(self.frames), 12)
        if not self.call:
            raise RuntimeError(f"The NXR didn't answer: {self.ua.last_error or 'no reply'}")
        threading.Thread(target=self._pump, args=(self.call,), daemon=True).start()
        return True

    def audio(self, pcm):
        import numpy as np
        self.frames.append(np.frombuffer(pcm, dtype=np.int16).copy())

    def unkey(self):
        self.hold_until = time.time() + self.cfg["sip_hold_s"]

    def tick(self):
        if self.call and self.call.state not in ("ended", "failed") and not self.talk.keyed and time.time() > self.hold_until:
            call, self.call = self.call, None
            fut = asyncio.run_coroutine_threadsafe(call.hangup(), self.loop)
            fut.add_done_callback(lambda _: self.talk._publish())

    def close(self):
        if self.ua:
            try:
                self._run(self.ua.stop(), 5)
            except Exception:       # noqa: BLE001
                pass
        if getattr(self, "loop", None):
            self.loop.call_soon_threadsafe(self.loop.stop)

    def status(self):
        if self.error:
            return self.error
        st = f"NXR {self.cfg['nxr_host']}:{self.cfg['nxr_port']}"
        if self.cfg["sip_register"]:
            st += " · registered" if self.ua.registered else f" · not registered ({self.ua.reg_error or 'trying'})"
        if self.call and self.call.state not in ("ended", "failed"):
            st += f" · call {self.call.state}"
        return st


PATHS = {"test": TestPath, "k5tra": K5traPath, "nxr_sip": SipPath}


# ---- the engine ----------------------------------------------------------------------------------
class Talk:
    def __init__(self, app):
        self.app = app
        self.mx = threading.RLock()
        self.path = None
        self.floor = None           # {"user_id", "name", "target", "since", "frames": [...]}
        self.keyed = False
        self.listeners = set()      # callables(pcm_bytes_8k)
        self.restart = threading.Event()
        self.last_rx_mute = 0.0

    @property
    def cfg(self):
        return self.app.cfg["console"]

    # ---- status -------------------------------------------------------------------------------
    def state(self):
        p, f = self.path, self.floor
        return {"enabled": self.cfg["enabled"], "path": self.cfg["path"],
                "ready": bool(p and p.ready), "detail": p.status() if p else "Off",
                "private": bool(self.cfg["path"] == "k5tra" and self.cfg["private_command"]),
                "group": self.cfg["group_name"], "max_s": self.cfg["max_talk_s"],
                "floor": {k: f[k] for k in ("user_id", "name", "target", "since")} if f else None,
                "channel_busy": self.app.channel.busy_since is not None and not self.keyed}

    def _publish(self):
        self.app.broker.publish("talk", self.state(), sticky=True)

    # ---- floor --------------------------------------------------------------------------------------
    def request(self, user, target):
        """Take the floor and key up. target = {"kind": "group"} or {"kind": "unit", "unit": "U00110"}."""
        c = self.cfg
        with self.mx:
            if not c["enabled"] or not self.path:
                return "The dispatch console is turned off in Settings"
            if not self.path.ready:
                return self.path.status()
            if self.floor and self.floor["user_id"] != user["id"]:
                return f"{self.floor['name']} is talking"
            if self.floor:
                return None
            if self.app.channel.busy_since is not None and not c["talk_over_busy"]:
                return "Channel busy: wait for it to clear"
            if target.get("kind") == "unit":
                if not (c["path"] == "k5tra" and c["private_command"]):
                    return "Private calls aren't set up for this talk path; talk to the group instead"
                uid = str(target.get("unit", ""))
                if not re.fullmatch(r"[A-Za-z0-9]{1,16}", uid):
                    return "Unknown unit"
                cmd = build_command({"command": c["private_command"], "format": c["private_format"]}, uid, 0)
                if not self.app.serial_write(cmd):
                    return "Can't reach the radio to dial the unit (no serial link)"
                time.sleep(c["private_delay_ms"] / 1000)
            name = user.get("display_name") or user["username"]
            try:
                self.path.key(target)
            except Exception as e:      # noqa: BLE001
                log.warning("talk: key failed: %s", e)
                return str(e)
            self.floor = {"user_id": user["id"], "name": name, "username": user["username"], "target": target,
                          "since": time.time(), "frames": [], "lead_until": time.time() + c["ptt_lead_ms"] / 1000}
            self.keyed = True
        self.app.auth.audit(user, f"talked to {self._target_text(target)}")
        self._publish()
        return None

    def audio(self, user, pcm):
        with self.mx:
            f = self.floor
            if not f or f["user_id"] != user["id"]:
                return
            if len(pcm) % 2:
                pcm = pcm[:-1]
            if time.time() < f["lead_until"]:       # give the radio time to key up before audio
                return
            f["frames"].append(pcm)
            self.path.audio(pcm)

    def release(self, user=None, reason=""):
        with self.mx:
            f = self.floor
            if not f or (user and f["user_id"] != user["id"]):
                return
            time.sleep(self.cfg["ptt_tail_ms"] / 1000)
            try:
                self.path.unkey()
            except Exception as e:      # noqa: BLE001
                log.warning("talk: unkey: %s", e)
            self.floor, self.keyed = None, False
            self.last_rx_mute = time.time() + 1.0
        self._log(f, time.time(), reason)
        self._publish()

    def _target_text(self, t):
        if t.get("kind") == "unit":
            u = self.app.store.unit(t["unit"])
            return (u and u["alias"]) or self.app.channel.radio_names.get(t["unit"]) or t["unit"]
        return self.cfg["group_name"] or "the group"

    def _log(self, f, end, reason):
        if end - f["since"] < self.app.cfg["recording"].get("min_call_s", 0):
            return                      # an accidental tap of the talk button
        to = self._target_text(f["target"])
        unit = f["target"].get("unit") if f["target"].get("kind") == "unit" else None
        tx = self.app.channel.record(f["since"], end, "dispatch", unit, f"{f['name']} → {to}"[:300],
                                     {"user": f["username"], "private": bool(unit), "stopped": reason or None})
        if f["frames"]:
            threading.Thread(target=self._save, args=(tx, f["frames"]), daemon=True).start()

    def _save(self, tx, frames):
        rec = self.app.voice
        try:
            d = datetime.datetime.fromtimestamp(tx["start"])
            rel_dir = f"{d:%Y/%m/%d}"
            os.makedirs(os.path.join(rec.root, rel_dir), exist_ok=True)
            base = os.path.join(rec.root, rel_dir, f"tx{tx['id']}_{d:%H%M%S}_dispatch")
            wav = base + ".wav"
            with wave.open(wav, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                w.writeframes(b"".join(frames))
            path = wav
            if encoder() == "opusenc":
                if subprocess.run(["opusenc", "--quiet", "--bitrate", "12", wav, base + ".opus"],
                                  capture_output=True, timeout=60).returncode == 0:
                    os.unlink(wav)
                    path = base + ".opus"
            secs = round(sum(len(x) for x in frames) / 2 / RATE, 1)
            self.app.channel.attach_audio(tx["id"], os.path.relpath(path, rec.root), secs)
            self.app.broker.publish("recording", {"tx_id": tx["id"], "unit_id": tx["unit_id"], "seconds": secs})
        except Exception as e:      # noqa: BLE001
            log.warning("saving dispatch audio: %s", e)

    # ---- live listen ----------------------------------------------------------------------------------
    def listen_feed(self, pcm, rate):
        """Channel audio (from the recorder's sound card or an NXR call) for consoles that are listening."""
        if not self.listeners or self.keyed or time.time() < self.last_rx_mute:
            return
        if rate != RATE:
            a = array.array("h"); a.frombytes(pcm)
            step = rate // RATE
            if step > 1:          # 16/24/48 kHz -> 8 kHz: average each group (a crude low-pass)
                a = array.array("h", (sum(a[i:i + step]) // step for i in range(0, len(a) - step + 1, step)))
            pcm = a.tobytes()
        for fn in list(self.listeners):
            try:
                fn(pcm)
            except Exception:       # noqa: BLE001
                self.listeners.discard(fn)

    # ---- loop ---------------------------------------------------------------------------------------
    def tick(self):
        f = self.floor
        if f and time.time() - f["since"] > self.cfg["max_talk_s"]:
            self.release(reason="time limit")
        if self.path and hasattr(self.path, "tick"):
            self.path.tick()

    def run(self):
        while not self.app.stop.is_set():
            self.restart.clear()
            c = self.cfg
            with self.mx:
                if self.floor:
                    self.release(reason="settings changed")
                if self.path:
                    self.path.close()
                    self.path = None
                if c["enabled"]:
                    try:
                        self.path = PATHS.get(c["path"], TestPath)(self)
                    except Exception as e:      # noqa: BLE001
                        log.warning("talk path: %s", e)
                        self.path = TestPath(self)
                        self.path.ready, self.path.error = False, str(e)
                        self.path.status = lambda: self.path.error
            self._publish()
            last_busy = None
            while not self.restart.is_set() and not self.app.stop.is_set():
                self.tick()
                busy = self.app.channel.busy_since is not None
                if busy != last_busy:
                    last_busy = busy
                    self._publish()
                self.restart.wait(0.25)
