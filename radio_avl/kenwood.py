"""
Kenwood NX-series data port framing and decoding.

Observed on an NX-series radio's COM port (capture 2026-10-30):

    STX <payload> <2 ASCII digit sequence 00-99> ETX

Payloads seen:
    S1 / S0                 channel busy / clear
    JA <4 bytes>            status flags (meaning not yet mapped)
    JE <line> <text fields separated by NULs>
                            display text: channel, ?, zone/system, pop-up message
    $PKNDS,...*hh\r\n       NXDN GPS report from another unit

$PKNDS fields (index after splitting on commas, '$PKNDS' = 0):
    1 hhmmss UTC   2 A/V fix   3 lat ddmm.mmmm  4 N/S
    5 lon dddmm.mmmm  6 E/W    7 speed knots   8 course deg
    9 ddmmyy       10 ? (22.70 constant)  11 ? (W00)
    12 source unit (e.g. U00112)  13 ? (207)  14 ? (00)
Unknown fields are kept in 'extra' so they can be mapped later.
"""
import calendar
import re

STX, ETX = 0x02, 0x03
MAX_BUF = 8192


class FrameReader:
    """Accumulates serial bytes and yields complete STX..ETX frames."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        frames = []
        while True:
            s = self.buf.find(STX)
            if s < 0:
                self.buf.clear()
                break
            e = self.buf.find(ETX, s + 1)
            if e < 0:
                if s:
                    del self.buf[:s]
                if len(self.buf) > MAX_BUF:      # runaway frame, resync
                    self.buf.clear()
                break
            s = self.buf.rfind(STX, s, e)        # a stray STX restarts the frame
            frames.append(bytes(self.buf[s + 1:e]))
            del self.buf[:e + 1]
        return frames


def nmea_checksum_ok(sentence: str):
    """True/False if a checksum is present, None if there isn't one."""
    if "*" not in sentence:
        return None
    body, cs = sentence.lstrip("$").split("*", 1)
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cs[:2], 16)
    except ValueError:
        return False


def _dm(value, hemi, deg_digits):
    if not value or not hemi:
        return None
    try:
        d = float(value[:deg_digits]) + float(value[deg_digits:]) / 60.0
    except ValueError:
        return None
    return -d if hemi in ("S", "W") else d


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _utc(hhmmss, ddmmyy):
    try:
        h, m, s = int(hhmmss[0:2]), int(hhmmss[2:4]), float(hhmmss[4:])
        d, mo, y = int(ddmmyy[0:2]), int(ddmmyy[2:4]), 2000 + int(ddmmyy[4:6])
        return calendar.timegm((y, mo, d, h, m, 0)) + s
    except (ValueError, IndexError):
        return None


def unit_label(unit_id: str) -> str:
    """'U00112' -> '112'."""
    m = re.match(r"^[A-Za-z]*0*(\d+)$", unit_id or "")
    return m.group(1) if m else (unit_id or "?")


def parse_sentence(s: str):
    ok = nmea_checksum_ok(s)
    f = s.lstrip("$").split("*")[0].split(",")
    if f[0] == "PKNDS" and len(f) >= 13:
        valid = f[2] == "A"
        lat, lon = _dm(f[3], f[4], 2), _dm(f[5], f[6], 3)
        return {
            "kind": "position",
            "proto": "PKNDS",
            "unit": f[12],
            "valid": bool(valid and lat is not None and lon is not None),
            "lat": lat,
            "lon": lon,
            "speed_kn": _float(f[7]),
            "course": _float(f[8]),
            "fix_time": _utc(f[1], f[9]),
            "status": f[13] if len(f) > 13 else "",       # unit status code (207 normal, 224 emergency)
            "extra": {"f10": f[10], "f11": f[11],
                      "f13": f[13] if len(f) > 13 else "",
                      "f14": f[14] if len(f) > 14 else ""},
            "checksum_ok": ok,
            "raw": s,
        }
    return {"kind": "sentence", "raw": s, "checksum_ok": ok}


def decode(frame: bytes) -> dict:
    seq = None
    body = frame
    if len(frame) >= 3 and frame[-2:].isdigit():
        seq, body = int(frame[-2:]), frame[:-2]

    if body.startswith(b"$"):
        out = parse_sentence(body.decode("ascii", "replace").strip())
    elif len(body) == 2 and body[:1] == b"S" and body[1:2] in (b"0", b"1"):
        out = {"kind": "busy", "busy": body[1:2] == b"1"}
    elif len(body) == 2 and body[:1] == b"T" and body[1:2] in (b"0", b"1"):
        # voice call on the channel the monitor radio is listening to (it unmutes): T1 start, T0 end
        out = {"kind": "voice", "on": body[1:2] == b"1"}
    elif body.startswith(b"gI") and len(body) >= 9:
        # caller ID: gI1<caller><other> at the start of a call, gI0... at the end, IDs like U00110
        ids = re.findall(r"[A-Z]\d{5}", body[3:].decode("latin-1"))
        out = {"kind": "caller", "active": body[2:3] == b"1", "unit": ids[0] if ids else "",
               "other": ids[1] if len(ids) > 1 else "", "raw": body.decode("latin-1")}
    elif body.startswith(b"gE") and len(body) >= 14:
        # status message: gE<from><to><status>, e.g. gEU00113U00100224 = U00113 sent status 224
        # (emergency) to U00100 (the dispatch / KAS ID); 207 is the normal status
        txt = body[2:].decode("latin-1")
        ids = re.findall(r"[A-Z]\d{5}", txt[:12])
        out = {"kind": "status_msg", "unit": ids[0] if ids else "", "to": ids[1] if len(ids) > 1 else "",
               "code": txt[12:].strip(), "raw": body.decode("latin-1")}
    elif body.startswith(b"gV") and len(body) >= 8:
        # the caller's name as programmed in the radio: gVU00110MORRIS + space padding
        txt = body[2:].decode("latin-1")
        out = {"kind": "caller_name", "unit": txt[:6], "name": txt[6:].strip()}
    elif body.startswith(b"JA"):
        out = {"kind": "status", "flags": body[2:].hex()}
    elif body.startswith(b"JE"):
        fields = [x.decode("latin-1").strip() for x in re.split(rb"\x00+", body[3:])]
        while fields and not fields[-1]:
            fields.pop()
        out = {"kind": "display", "line": body[2:3].decode("latin-1"),
               "fields": fields,
               "message": fields[3] if len(fields) > 3 else ""}
    elif not body:
        out = {"kind": "empty"}
    else:
        out = {"kind": "unknown", "hex": body.hex()}
    out["seq"] = seq
    return out
