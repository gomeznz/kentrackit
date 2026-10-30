"""Trip and stop reports built from stored positions."""
from . import geo

GAP_S = 15 * 60          # longer than this without a report = we don't know what happened


def _zone(fences, lat, lon):
    for f in fences:
        if geo.inside(f, lat, lon):
            return f["name"]
    return ""


def _downsample(pts, n=1500):
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    out = [pts[int(i * step)] for i in range(n)]
    out.append(pts[-1])
    return out


def unit_report(store, unit, t0, t1, fences, cfg, with_track=True):
    stop_s = cfg["reports"]["stop_min"] * 60
    radius = cfg["reports"]["stop_radius_m"]
    pts = store.track(unit["id"], t0, t1, limit=200000)
    name = unit.get("alias") or "Unit " + unit["id"].lstrip("UuGg0")
    rep = {"unit": unit["id"], "name": name, "colour": unit.get("colour") or "",
           "points": len(pts), "first": None, "last": None, "distance_km": 0.0,
           "moving_s": 0, "stopped_s": 0, "max_kmh": 0, "stops": [], "trips": [],
           "gaps": 0, "track": []}
    if not pts:
        return rep
    rep["first"], rep["last"] = pts[0]["rx_time"], pts[-1]["rx_time"]

    # 1. Find stops: runs of points that stay within `radius` of the run's first point
    #    for at least `stop_s`.
    stops, anchor = [], 0
    for j in range(1, len(pts) + 1):
        if j < len(pts) and geo.haversine(pts[anchor]["lat"], pts[anchor]["lon"],
                                          pts[j]["lat"], pts[j]["lon"]) <= radius:
            continue
        if pts[j - 1]["rx_time"] - pts[anchor]["rx_time"] >= stop_s:
            stops.append((anchor, j - 1))
        anchor = j

    for a, b in stops:
        lat = sum(p["lat"] for p in pts[a:b + 1]) / (b - a + 1)
        lon = sum(p["lon"] for p in pts[a:b + 1]) / (b - a + 1)
        rep["stops"].append({"start": pts[a]["rx_time"], "end": pts[b]["rx_time"],
                             "duration_s": round(pts[b]["rx_time"] - pts[a]["rx_time"]),
                             "lat": round(lat, 6), "lon": round(lon, 6),
                             "zone": _zone(fences, lat, lon)})

    # 2. Trips are what's between stops.
    bounds, prev_end = [], 0
    for a, b in stops:
        if a > prev_end:
            bounds.append((prev_end, a))
        prev_end = b
    if prev_end < len(pts) - 1:
        bounds.append((prev_end, len(pts) - 1))

    for a, b in bounds:
        km, top, gaps = 0.0, 0.0, 0
        for i in range(a + 1, b + 1):
            p, q = pts[i - 1], pts[i]
            if q["rx_time"] - p["rx_time"] > GAP_S:
                gaps += 1
            km += geo.haversine(p["lat"], p["lon"], q["lat"], q["lon"]) / 1000
            top = max(top, (q["speed_kn"] or 0) * 1.852)
        if km < radius / 1000:           # wander inside a stop area, not a real trip
            continue
        dur = pts[b]["rx_time"] - pts[a]["rx_time"]
        rep["trips"].append({"start": pts[a]["rx_time"], "end": pts[b]["rx_time"],
                             "duration_s": round(dur), "km": round(km, 2), "max_kmh": round(top),
                             "from_zone": _zone(fences, pts[a]["lat"], pts[a]["lon"]),
                             "to_zone": _zone(fences, pts[b]["lat"], pts[b]["lon"]),
                             "gaps": gaps})
        rep["distance_km"] += km
        rep["moving_s"] += dur
        rep["max_kmh"] = max(rep["max_kmh"], round(top))
        rep["gaps"] += gaps

    rep["distance_km"] = round(rep["distance_km"], 2)
    rep["moving_s"] = round(rep["moving_s"])
    rep["stopped_s"] = sum(s["duration_s"] for s in rep["stops"])
    if with_track:
        rep["track"] = _downsample([[round(p["lat"], 6), round(p["lon"], 6), round(p["rx_time"])] for p in pts])
    return rep


def leaderboard(app, t0, t1, allowed=None):
    """Who's been busiest: distance, time moving, talk time and calls per unit, plus an overall score
    (the average of each measure as a share of the fleet's top unit for that measure)."""
    rep = {r["unit"]: r for r in fleet_report(app.store, t0, t1, app.cfg, None, allowed)["units"]}
    voice = app.channel.voice_by_unit(t0, t1)
    rows = []
    for u in app.store.units():
        if allowed is not None and u["id"] not in allowed:
            continue
        r, v = rep.get(u["id"], {}), voice.get(u["id"], {})
        rows.append({"id": u["id"], "name": u["alias"] or "Unit " + u["id"].lstrip("UuGg0"), "colour": u["colour"] or "",
                     "distance_km": r.get("distance_km", 0.0), "moving_s": r.get("moving_s", 0),
                     "talk_s": v.get("talk_s", 0.0), "calls": v.get("calls", 0),
                     "stops": len(r.get("stops", [])), "reports": r.get("points", 0)})
    keys = ("distance_km", "moving_s", "talk_s", "calls")
    tops = {k: max([x[k] for x in rows] + [0]) for k in keys}
    for x in rows:
        parts = [x[k] / tops[k] for k in keys if tops[k] > 0]
        x["score"] = round(100 * sum(parts) / len(keys), 1) if parts else 0.0
    rows.sort(key=lambda x: (-x["score"], x["name"]))
    return {"from": t0, "to": t1, "units": rows, "tops": tops}


def fleet_report(store, t0, t1, cfg, unit_id=None, allowed=None):
    fences = store.geofences()
    units = [u for u in store.units() if (not unit_id or u["id"] == unit_id)
             and (allowed is None or u["id"] in allowed)]
    reps = [unit_report(store, u, t0, t1, fences, cfg, with_track=bool(unit_id)) for u in units]
    reps = [r for r in reps if r["points"]] if not unit_id else reps
    return {"from": t0, "to": t1, "units": reps,
            "totals": {"units": len(reps),
                       "distance_km": round(sum(r["distance_km"] for r in reps), 2),
                       "moving_s": sum(r["moving_s"] for r in reps)}}
