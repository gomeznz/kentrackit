"""Small geometry helpers (WGS84 lat/lon, distances in metres)."""
import math

R = 6371008.8


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _local_xy(lat0, lat, lon):
    """Equirectangular projection to metres, good enough over a few km."""
    k = math.cos(math.radians(lat0))
    return math.radians(lon) * R * k, math.radians(lat) * R


def point_in_polygon(lat, lon, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def distance_to_polygon_edge(lat, lon, pts):
    px, py = _local_xy(lat, lat, lon)
    best = float("inf")
    for i in range(len(pts)):
        ax, ay = _local_xy(lat, *pts[i])
        bx, by = _local_xy(lat, *pts[(i + 1) % len(pts)])
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def inside(fence, lat, lon, buffer_m=0.0):
    """True if (lat, lon) is inside the fence, or within buffer_m outside its edge."""
    g = fence["geometry"]
    if fence["shape"] == "circle":
        c = g["center"]
        return haversine(lat, lon, c[0], c[1]) <= g["radius"] + buffer_m
    pts = g["points"]
    if len(pts) < 3:
        return False
    if point_in_polygon(lat, lon, pts):
        return True
    return buffer_m > 0 and distance_to_polygon_edge(lat, lon, pts) <= buffer_m


def valid_geometry(shape, g):
    try:
        if shape == "circle":
            lat, lon = float(g["center"][0]), float(g["center"][1])
            r = float(g["radius"])
            ok = -90 <= lat <= 90 and -180 <= lon <= 180 and 5 <= r <= 100000
            return ok and {"center": [lat, lon], "radius": round(r, 1)}
        if shape == "polygon":
            pts = [[float(a), float(b)] for a, b in g["points"]]
            ok = 3 <= len(pts) <= 500 and all(-90 <= a <= 90 and -180 <= b <= 180 for a, b in pts)
            return ok and {"points": [[round(a, 7), round(b, 7)] for a, b in pts]}
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return False
