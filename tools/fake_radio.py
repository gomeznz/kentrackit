#!/usr/bin/env python3
"""
A pretend Kenwood monitor radio on a virtual serial port, for testing KenTrackIt's polling,
channel log and coverage without real radios.

  python3 tools/fake_radio.py            # prints the port path, e.g. /dev/pts/5
  then set Settings -> Radio connection -> Other path... to that path, and
  Settings -> Polling -> command:  \\x02POLL{num5}\\x03   (this fake radio's made-up poll command)

What it does
  - answers polls for U00112, U00113, U00115 with a position report (they drive around)
  - U00114 is parked in a "dead spot" and never answers
  - north of the depot there's a coverage hole: units driving through it can't be heard
  - every ~40 s someone keys up for a few seconds of voice, with their ID on the display
"""
import datetime
import math
import os
import pty
import random
import re
import select
import time
import tty

CENTRE = (-46.4123, 168.3524)
UNITS = {"U00112": 0.0, "U00113": 2.1, "U00115": 4.2}       # answering units, phase offsets
DEAD = {"U00114"}
seq = 0


def frame(payload):
    global seq
    f = b"\x02" + payload + f"{seq:02d}".encode() + b"\x03"
    seq = (seq + 1) % 100
    return f


def nmea(body):
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"${body}*{cs:02X}"


def dm(v, d):
    v = abs(v)
    a = int(v)
    return f"{a:0{d}d}{(v - a) * 60:07.4f}"


def where(unit, t):
    ph = UNITS[unit] + t / 90.0
    r = 0.012 + 0.004 * math.sin(ph * 0.7)
    return CENTRE[0] + r * math.sin(ph), CENTRE[1] + r * 1.45 * math.cos(ph), ph


def in_hole(unit, t):
    lat, lon, _ = where(unit, t)
    return lat > CENTRE[0] + 0.009 and lon > CENTRE[1] - 0.006


def position(unit, t):
    lat, lon, ph = where(unit, t)
    g = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    s = nmea(f"PKNDS,{g:%H%M%S},A,{dm(lat, 2)},S,{dm(lon, 3)},E,{random.uniform(20, 35):05.1f},"
             f"{(math.degrees(ph) + 90) % 360:05.1f},{g:%d%m%y},22.70,W00,{unit},207,00,")
    return s.encode() + b"\r\n"


def disp(msg):
    return b"JE0Repeater      \x00\x00      \x00\x00ICC\x00\x00" + msg.encode() + b"\x00\x00\x00\x00"


def main():
    master, slave = pty.openpty()
    tty.setraw(slave)
    print(os.ttyname(slave), flush=True)
    buf, next_voice = b"", time.time() + 15
    while True:
        r, _, _ = select.select([master], [], [], 0.2)
        now = time.time()
        if r:
            buf += os.read(master, 1024)
            for m in re.finditer(rb"\x02POLL(\d{5})\x03", buf):
                unit = "U" + m.group(1).decode()
                time.sleep(0.4)
                if unit in DEAD or unit not in UNITS or in_hole(unit, time.time()):
                    continue
                os.write(master, frame(b"S1") + frame(disp("Receive Data")) + frame(position(unit, time.time())))
                time.sleep(0.5)
                os.write(master, frame(disp("Complete")) + frame(b"S0"))
            if b"\x03" in buf:
                buf = buf[buf.rfind(b"\x03") + 1:]
        if now > next_voice:
            who = random.choice(["U00113", "U00112"])
            os.write(master, frame(b"S1") + frame(disp(who)))
            time.sleep(random.uniform(2, 6))
            os.write(master, frame(disp("")) + frame(b"S0"))
            next_voice = time.time() + random.uniform(30, 50)


if __name__ == "__main__":
    main()
