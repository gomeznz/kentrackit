"""Dispatch console talk engine: floor, busy lockout, private dial, K5TRA PTT bytes, time limit."""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radio_avl import talk as T  # noqa: E402


class FakeChannel:
    busy_since = None
    radio_names = {"U00110": "MORRIS"}

    def __init__(self):
        self.recorded = []

    def record(self, start, end, kind, unit, display, detail=None):
        tx = {"id": len(self.recorded) + 1, "start": start, "end": end, "kind": kind, "unit_id": unit, "display": display}
        self.recorded.append(tx)
        return tx

    def attach_audio(self, *a):
        pass


class FakeApp:
    def __init__(self, cfg):
        self.cfg = {"console": cfg, "recording": {"device": "test"}, "data_dir": tempfile.mkdtemp()}
        self.channel = FakeChannel()
        self.sent = []
        self.stop = threading.Event()
        self.store = type("S", (), {"unit": lambda s, u: None, "add_event": lambda *a: None})()
        self.auth = type("A", (), {"audit": lambda *a: None})()
        self.broker = type("B", (), {"publish": lambda *a, **k: None})()
        self.voice = type("V", (), {"root": tempfile.mkdtemp()})()

    def serial_write(self, data):
        self.sent.append(data)
        return True


def cfg(**kw):
    c = {"enabled": True, "path": "k5tra", "group_name": "ICC", "max_talk_s": 60, "talk_over_busy": False,
         "ptt_lead_ms": 0, "ptt_tail_ms": 0, "hid_device": "", "ptt_gpio": 3, "ptt_invert": False,
         "playback_device": "test", "private_command": "", "private_format": "ascii", "private_delay_ms": 0}
    c.update(kw)
    return c


ALEX = {"id": 1, "username": "alex", "display_name": "Alex"}
SAM = {"id": 2, "username": "sam", "display_name": "Sam"}


def k5(app):
    """A K5TRA path whose HID 'device' is a plain file, so the PTT reports can be read back."""
    fd, path = tempfile.mkstemp()
    os.close(fd)
    app.cfg["console"]["hid_device"] = path
    talk = T.Talk(app)
    talk.path = T.K5traPath(talk)
    return talk, path


def test_k5tra_ptt_reports_and_floor():
    app = FakeApp(cfg())
    talk, path = k5(app)
    assert talk.path.ready, talk.path.status()
    assert talk.request(ALEX, {"kind": "group"}) is None
    assert talk.request(SAM, {"kind": "group"}) == "Alex is talking"          # one dispatcher at a time
    talk.audio(ALEX, b"\x01\x00" * 160)
    talk.release(ALEX)
    with open(path, "rb") as f:
        reports = f.read()
    # [0, 0, data, direction-mask, 0]: GPIO3 = bit 2 (0x04): released, keyed, released
    assert reports == bytes([0, 0, 0, 4, 0, 0, 0, 4, 4, 0, 0, 0, 0, 4, 0])
    assert app.channel.recorded[0]["kind"] == "dispatch" and app.channel.recorded[0]["display"] == "Alex → ICC"


def test_busy_lockout_and_override():
    app = FakeApp(cfg())
    talk, _ = k5(app)
    app.channel.busy_since = time.time()
    assert "busy" in talk.request(ALEX, {"kind": "group"}).lower()
    app.cfg["console"]["talk_over_busy"] = True
    assert talk.request(ALEX, {"kind": "group"}) is None


def test_private_call_dials_first():
    app = FakeApp(cfg(private_command="\\x02IC{num5}\\x03"))
    talk, _ = k5(app)
    assert talk.request(ALEX, {"kind": "unit", "unit": "U00110"}) is None
    assert app.sent == [b"\x02IC00110\x03"]
    talk.release(ALEX)
    assert app.channel.recorded[0]["unit_id"] == "U00110" and "MORRIS" in app.channel.recorded[0]["display"]


def test_private_refused_without_command():
    app = FakeApp(cfg())
    talk, _ = k5(app)
    assert "Private calls" in talk.request(ALEX, {"kind": "unit", "unit": "U00110"})


def test_time_limit():
    app = FakeApp(cfg(max_talk_s=5))
    talk, _ = k5(app)
    talk.request(ALEX, {"kind": "group"})
    talk.tick()
    assert talk.floor is not None
    talk.floor["since"] -= 10
    talk.tick()
    assert talk.floor is None
    assert app.channel.recorded[0]["kind"] == "dispatch"
