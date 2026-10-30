"""Run with:  python3 -m pytest tests   (or python3 tests/test_kenwood.py)"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radio_avl.kenwood import FrameReader, decode, unit_label, nmea_checksum_ok


def nmea(body):
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"${body}*{cs:02X}"


SENT = nmea("PKNDS,010203,A,4500.0000,S,17000.0000,E,010.0,090.0,301026,22.70,W00,U00042,207,00,")

# One cycle in the same shape as the real capture (synthetic position).
CYCLE = (
    b"\x02S174\x03"
    b"\x02JA\xc0\x80\x88\x8275\x03"
    b"\x02JA\xd0\x80\x88\x8276\x03"
    b"\x02JE0Repeater      \x00\x00      \x00\x00ICC\x00\x00Receive Data\x00\x00\x00\x0077\x03"
    b"\x02" + SENT.encode() + b"\r\n78\x03"
    b"\x02JE0Repeater      \x00\x00      \x00\x00ICC\x00\x00Complete\x00\x00\x00\x0079\x03"
    b"\x02S080\x03"
)


def frames_of(data, chunk=7):
    r, out = FrameReader(), []
    for i in range(0, len(data), chunk):          # split to mimic serial reads
        out += r.feed(data[i:i + chunk])
    return [decode(f) for f in out]


def test_cycle():
    d = frames_of(CYCLE)
    assert [x["kind"] for x in d] == ["busy", "status", "status", "display",
                                      "position", "display", "busy"]
    assert [x["seq"] for x in d] == [74, 75, 76, 77, 78, 79, 80]
    assert d[0]["busy"] is True and d[-1]["busy"] is False
    assert d[3]["message"] == "Receive Data" and d[3]["fields"][0] == "Repeater"
    p = d[4]
    assert p["unit"] == "U00042" and p["valid"] and p["checksum_ok"]
    assert abs(p["lat"] + 45.0) < 1e-9 and abs(p["lon"] - 170.0) < 1e-9
    assert p["speed_kn"] == 10.0 and p["course"] == 90.0
    assert p["fix_time"] == 1793322123  # 2026-10-30 01:02:03 UTC


def test_garbage_and_resync():
    d = frames_of(b"\xff\xfe junk \x02half" + b"\x02S080\x03")
    assert len(d) == 1 and d[0]["kind"] == "busy"


def test_bad_checksum_flagged():
    bad = SENT[:-2] + "00"
    assert nmea_checksum_ok(bad) is False
    d = frames_of(b"\x02" + bad.encode() + b"\r\n12\x03")
    assert d[0]["kind"] == "position" and d[0]["checksum_ok"] is False


def test_no_fix():
    s = nmea("PKNDS,010203,V,,,,,,,301026,22.70,W00,U00042,207,00,")
    d = frames_of(b"\x02" + s.encode() + b"\r\n12\x03")
    assert d[0]["kind"] == "position" and not d[0]["valid"]


def test_unit_label():
    assert unit_label("U00112") == "112"
    assert unit_label("weird") == "weird"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
