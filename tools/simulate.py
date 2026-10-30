#!/usr/bin/env python3
"""
Generate a fake capture (same format as nxdn_listen.py raw logs) with a small fleet driving
around Invercargill, for demos and testing without radios.

  python3 tools/simulate.py --hours 3 > demo.log
  python3 -m radio_avl --config test.json --replay demo.log

Includes: stops, a speeding run to Bluff, a unit that goes silent (overdue), a status-field
change and an EMERGENCY pop-up on the monitor radio's display.
"""
import argparse
import datetime
import math
import random

STX, ETX = b"\x02", b"\x03"
DEPOT = (-46.41232, 168.35238)

# (lat, lon, dwell_minutes_after_arriving, speed_kmh_to_get_here)
ROUTES = {
    "U00112": [(DEPOT[0], DEPOT[1], 20, 0), (-46.4290, 168.3470, 25, 45), (-46.3990, 168.3650, 15, 50),
               (DEPOT[0], DEPOT[1], 40, 45), (-46.4130, 168.3900, 20, 50), (DEPOT[0], DEPOT[1], 999, 50)],
    "U00113": [(DEPOT[0], DEPOT[1], 10, 0), (-46.4050, 168.3300, 30, 40), (-46.6000, 168.3340, 30, 112),
               (-46.4050, 168.3300, 20, 95), (DEPOT[0], DEPOT[1], 999, 45)],
    "U00114": [(-46.4300, 168.3600, 5, 0), (-46.4200, 168.3750, 60, 35), (-46.4300, 168.3600, 999, 35)],
    "U00115": [(-46.3950, 168.3450, 15, 0), (-46.4100, 168.3500, 20, 40), (-46.4250, 168.3300, 999, 40)],
    "U00116": [(DEPOT[0] + 0.0004, DEPOT[1] - 0.0006, 999, 0)],
}
SILENT_AFTER = {"U00115": 0.6}           # fraction of the run after which this unit stops reporting
STATUS_CHANGE = {"U00114": 0.5}          # fraction at which field 14 changes 00 -> 05


def plan(route, t0):
    """-> list of (t_start, t_end, lat0, lon0, lat1, lon1) legs, stationary legs have same ends."""
    from radio_avl.geo import haversine
    legs, t = [], t0
    for i, (lat, lon, dwell, kmh) in enumerate(route):
        if i:
            plat, plon = route[i - 1][0], route[i - 1][1]
            dist = haversine(plat, plon, lat, lon) * 1.25        # roads aren't straight
            dur = dist / (kmh / 3.6)
            legs.append((t, t + dur, plat, plon, lat, lon, kmh))
            t += dur
        legs.append((t, t + dwell * 60, lat, lon, lat, lon, 0))
        t += dwell * 60
    return legs


def where(legs, t):
    for a, b, la0, lo0, la1, lo1, kmh in legs:
        if a <= t <= b:
            f = 0 if b == a else (t - a) / (b - a)
            course = math.degrees(math.atan2((lo1 - lo0) * math.cos(math.radians(la0)), la1 - la0)) % 360
            return la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f, kmh, course if kmh else 0
    a, b, la0, lo0, la1, lo1, _ = legs[-1]
    return la1, lo1, 0, 0


def nmea(body):
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"${body}*{cs:02X}"


def dm(v, deg_digits):
    v = abs(v)
    d = int(v)
    return f"{d:0{deg_digits}d}{(v - d) * 60:07.4f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=3)
    ap.add_argument("--interval", type=float, default=19, help="seconds between polls of a unit")
    ap.add_argument("--end", help="ISO local end time (default: now)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    random.seed(a.seed)

    end = datetime.datetime.fromisoformat(a.end) if a.end else datetime.datetime.now()
    t_end = end.timestamp()
    t0 = t_end - a.hours * 3600
    legs = {u: plan(r, t0) for u, r in ROUTES.items()}
    units = list(ROUTES)
    seq = 0
    emergency_at = t0 + a.hours * 3600 * 0.8

    def frame(payload):
        nonlocal seq
        f = STX + payload + f"{seq:02d}".encode() + ETX
        seq = (seq + 1) % 100
        return f

    def line(t, data):
        ts = datetime.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in data)
        print(f"{ts} {data.hex(' ')}  |{asc}|")

    disp = lambda msg: b"JE0Repeater      \x00\x00      \x00\x00ICC\x00\x00" + msg.encode() + b"\x00\x00\x00\x00"
    t, i, emergency_done = t0, 0, False
    step = a.interval / len(units)
    while t < t_end:
        u = units[i % len(units)]
        i += 1
        frac = (t - t0) / (t_end - t0)
        if not emergency_done and t >= emergency_at:
            emergency_done = True
            line(t, frame(b"S1") + frame(disp("EMERGENCY  U00113")))
            line(t + 3, frame(disp("")) + frame(b"S0"))
        if frac > SILENT_AFTER.get(u, 2):
            t += step
            continue
        lat, lon, kmh, course = where(legs[u], t)
        jitter = 6 / 111000
        lat += random.uniform(-jitter, jitter)
        lon += random.uniform(-jitter, jitter) * 1.45
        kmh = max(0, kmh + random.uniform(-4, 4)) if kmh else random.choice([0, 0, 0, 1.2, 2.1])
        status = "05" if frac > STATUS_CHANGE.get(u, 2) else "00"
        g = datetime.datetime.fromtimestamp(t - 1, datetime.timezone.utc)
        s = nmea(f"PKNDS,{g:%H%M%S},A,{dm(lat, 2)},{'S' if lat < 0 else 'N'},{dm(lon, 3)},E,"
                 f"{kmh / 1.852:05.1f},{course:05.1f},{g:%d%m%y},22.70,W00,{u},207,{status},")
        data = (frame(b"S1") + frame(b"JA\xd0\x80\x88\x82") + frame(disp("Receive Data")) +
                frame(s.encode() + b"\r\n") + frame(disp("Complete")) + frame(b"S0"))
        line(t, data)
        t += step


if __name__ == "__main__":
    main()
