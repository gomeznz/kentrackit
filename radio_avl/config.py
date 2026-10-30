"""Configuration: defaults, load/save, validation, password hashing."""
import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re

log = logging.getLogger("radio-avl")

BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
TILE_SOURCES = {
    "osm": {"url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; OpenStreetMap contributors", "max_zoom": 19},
    "linz_aerial": {"url": "https://basemaps.linz.govt.nz/v1/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp?api={key}",
                    "attribution": "&copy; LINZ CC BY 4.0", "max_zoom": 20},
    "custom": {"url": "", "attribution": "", "max_zoom": 19},
    "none": {"url": "", "attribution": "", "max_zoom": 19},
}

SAT_SOURCES = {
    "esri": {"url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
             "attribution": "Imagery &copy; Esri, Maxar, Earthstar Geographics", "max_zoom": 19},
    "linz_aerial": {"url": TILE_SOURCES["linz_aerial"]["url"],
                    "attribution": TILE_SOURCES["linz_aerial"]["attribution"], "max_zoom": 21},
    "custom": {"url": "", "attribution": "", "max_zoom": 20},
    "none": {"url": "", "attribution": "", "max_zoom": 19},
}

DEFAULTS = {
    "serial": {"port": "/dev/ttyUSB0", "baud": 9600},
    "data_dir": "data",
    "keep_days": 365,
    "web": {"host": "0.0.0.0", "port": 8090, "title": "KenTrackIt",
            "username": "admin", "password_hash": ""},
    "map": {"center": [-46.413, 168.353], "zoom": 12},
    "display": {"stale_after_s": 60, "lost_after_s": 900, "speed_units": "kmh", "trail_min": 30},
    "alerts": {"overdue_min": 10, "speed_limit_kmh": 0, "status_watch": True,
               "emergency_keywords": "emergency, emerg, man down"},
    "reports": {"stop_min": 5, "stop_radius_m": 60},
    "recording": {"enabled": False, "device": "", "rate": 16000, "format": "opus", "bitrate_kbps": 16,
                  "keep_days": 90, "preroll_ms": 300, "hang_ms": 400, "voice_only": True, "min_free_mb": 1000},
    "polling": {"mode": "off", "command": "", "format": "ascii", "units": "",
                "moving_s": 20, "parked_s": 120, "emergency_s": 10,
                "timeout_s": 8, "guard_s": 1.0, "max_per_min": 20},
    "tiles": {"source": "osm", "linz_key": "", "custom_url": "", "custom_attribution": "",
              "satellite": "esri", "sat_custom_url": "", "sat_custom_attribution": "",
              "max_zoom": 18},
    "drop_bad_checksum": True,
}


# ---- passwords -----------------------------------------------------------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(n))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc.hex(), dk)
    except (ValueError, AttributeError):
        return False


# ---- load / save -----------------------------------------------------------
def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _migrate(cfg):
    """Accept configs from v0.1."""
    web = cfg.get("web", {})
    if web.get("password"):
        web["password_hash"] = hash_password(web.pop("password"))
    web.pop("password", None)
    t = cfg.get("tiles", {})
    if "upstream" in t:
        up = t.pop("upstream")
        if up == TILE_SOURCES["osm"]["url"]:
            t.setdefault("source", "osm")
        elif up:
            t.setdefault("source", "custom")
            t.setdefault("custom_url", up)
            t.setdefault("custom_attribution", t.get("attribution", ""))
        else:
            t.setdefault("source", "none")
        t.pop("attribution", None)
    return cfg


def load(path):
    raw = {}
    if path and os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
    elif path:
        log.warning("config %s not found, using defaults", path)
    migrated = "password" in raw.get("web", {}) or "upstream" in raw.get("tiles", {})
    cfg = merge(DEFAULTS, _migrate(raw))
    if path and not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(os.path.dirname(os.path.abspath(path)), cfg["data_dir"])
    if migrated and path:
        try:
            save(path, cfg)
        except OSError as e:
            log.warning("could not update config file: %s", e)
    return cfg


def save(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def tile_settings(cfg):
    """-> (upstream url template or '', attribution, cache key)."""
    t = cfg["tiles"]
    src = t.get("source", "osm")
    if src == "linz_aerial":
        if not t.get("linz_key"):
            return "", TILE_SOURCES[src]["attribution"], src
        return TILE_SOURCES[src]["url"].replace("{key}", t["linz_key"]), TILE_SOURCES[src]["attribution"], src
    if src == "custom":
        url = t.get("custom_url", "")
        key = "custom-" + hashlib.sha1(url.encode()).hexdigest()[:8]
        return url, t.get("custom_attribution", ""), key
    if src == "none":
        return "", "", "none"
    return TILE_SOURCES["osm"]["url"], TILE_SOURCES["osm"]["attribution"], "osm"


def sat_settings(cfg):
    """-> (upstream url template or '', attribution, cache key, max zoom) for satellite imagery."""
    t = cfg["tiles"]
    src = t.get("satellite", "esri")
    meta = SAT_SOURCES.get(src, SAT_SOURCES["none"])
    if src == "linz_aerial":
        url = meta["url"].replace("{key}", t["linz_key"]) if t.get("linz_key") else ""
        return url, meta["attribution"], "sat-linz", meta["max_zoom"]
    if src == "custom":
        url = t.get("sat_custom_url", "")
        return url, t.get("sat_custom_attribution", ""), "sat-custom-" + hashlib.sha1(url.encode()).hexdigest()[:8], meta["max_zoom"]
    if src == "esri":
        return meta["url"], meta["attribution"], "sat-esri", meta["max_zoom"]
    return "", "", "sat-none", meta["max_zoom"]


def public(cfg):
    """Config as shown in the settings page (no secrets)."""
    out = copy.deepcopy(cfg)
    out["web"]["password_set"] = bool(out["web"].pop("password_hash", ""))
    out["tiles"]["linz_key_set"] = bool(out["tiles"].get("linz_key"))
    out["tiles"]["linz_key"] = ""
    return out


# ---- validation ------------------------------------------------------------
class Invalid(ValueError):
    pass


def _num(v, lo, hi, name, integer=False):
    try:
        x = int(v) if integer else float(v)
        if integer and float(v) != int(v):
            raise ValueError
    except (TypeError, ValueError):
        raise Invalid(f"{name} must be a number")
    if not lo <= x <= hi:
        raise Invalid(f"{name} must be between {lo} and {hi}")
    return x


def apply_patch(cfg, patch):
    """Return a new validated config with `patch` applied. Raises Invalid."""
    new = copy.deepcopy(cfg)

    s = patch.get("serial")
    if s is not None:
        if "port" in s:
            p = str(s["port"]).strip()
            if not re.fullmatch(r"/dev/[A-Za-z0-9_./:+-]{1,200}", p) or ".." in p:
                raise Invalid("Serial port must be a /dev/... path")
            new["serial"]["port"] = p
        if "baud" in s:
            b = _num(s["baud"], 0, 10**7, "Baud rate", True)
            if b not in BAUDS:
                raise Invalid("Baud rate must be one of " + ", ".join(map(str, BAUDS)))
            new["serial"]["baud"] = b

    w = patch.get("web")
    if w is not None:
        if "host" in w:
            h = str(w["host"]).strip()
            try:
                ipaddress.ip_address(h)
            except ValueError:
                raise Invalid("Listen address must be an IP address, e.g. 0.0.0.0")
            new["web"]["host"] = h
        if "port" in w:
            new["web"]["port"] = _num(w["port"], 1024, 65535, "Web port", True)
        if "title" in w:
            t = str(w["title"]).strip()
            if not 1 <= len(t) <= 60:
                raise Invalid("Title must be 1-60 characters")
            new["web"]["title"] = t
        if "username" in w:
            u = str(w["username"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", u):
                raise Invalid("Username: 1-32 letters, numbers, . _ -")
            new["web"]["username"] = u
        if w.get("clear_password"):
            new["web"]["password_hash"] = ""
        elif w.get("new_password"):
            pw = str(w["new_password"])
            if len(pw) < 6:
                raise Invalid("Password must be at least 6 characters")
            new["web"]["password_hash"] = hash_password(pw)

    m = patch.get("map")
    if m is not None:
        if "center" in m:
            c = m["center"]
            if not isinstance(c, (list, tuple)) or len(c) != 2:
                raise Invalid("Map centre must be [lat, lon]")
            new["map"]["center"] = [round(_num(c[0], -90, 90, "Latitude"), 6),
                                    round(_num(c[1], -180, 180, "Longitude"), 6)]
        if "zoom" in m:
            new["map"]["zoom"] = _num(m["zoom"], 1, 19, "Zoom", True)

    d = patch.get("display")
    if d is not None:
        if "stale_after_s" in d:
            new["display"]["stale_after_s"] = _num(d["stale_after_s"], 10, 86400, "Stale after", True)
        if "lost_after_s" in d:
            new["display"]["lost_after_s"] = _num(d["lost_after_s"], 20, 30 * 86400, "Lost after", True)
        if "speed_units" in d:
            if d["speed_units"] not in ("kmh", "knots"):
                raise Invalid("Speed units must be kmh or knots")
            new["display"]["speed_units"] = d["speed_units"]
        if "trail_min" in d:
            new["display"]["trail_min"] = _num(d["trail_min"], 1, 1440, "Trail length", True)
        if new["display"]["lost_after_s"] <= new["display"]["stale_after_s"]:
            raise Invalid("'Lost after' must be longer than 'stale after'")

    t = patch.get("tiles")
    if t is not None:
        if "source" in t:
            if t["source"] not in TILE_SOURCES:
                raise Invalid("Unknown map source")
            new["tiles"]["source"] = t["source"]
        if t.get("linz_key"):
            k = str(t["linz_key"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", k):
                raise Invalid("LINZ API key doesn't look right")
            new["tiles"]["linz_key"] = k
        if "custom_url" in t:
            u = str(t["custom_url"]).strip()
            if u and not (re.match(r"^https?://", u) and all(x in u for x in ("{z}", "{x}", "{y}"))):
                raise Invalid("Custom tile URL must be http(s) and contain {z}, {x} and {y}")
            new["tiles"]["custom_url"] = u
        if "custom_attribution" in t:
            new["tiles"]["custom_attribution"] = str(t["custom_attribution"])[:200]
        if "max_zoom" in t:
            new["tiles"]["max_zoom"] = _num(t["max_zoom"], 10, 22, "Max zoom", True)
        if "satellite" in t:
            if t["satellite"] not in SAT_SOURCES:
                raise Invalid("Unknown satellite source")
            new["tiles"]["satellite"] = t["satellite"]
        if "sat_custom_url" in t:
            u = str(t["sat_custom_url"]).strip()
            if u and not (re.match(r"^https?://", u) and all(x in u for x in ("{z}", "{x}", "{y}"))):
                raise Invalid("Custom satellite URL must be http(s) and contain {z}, {x} and {y}")
            new["tiles"]["sat_custom_url"] = u
        if "sat_custom_attribution" in t:
            new["tiles"]["sat_custom_attribution"] = str(t["sat_custom_attribution"])[:200]
        if new["tiles"]["satellite"] == "linz_aerial" and not new["tiles"]["linz_key"]:
            raise Invalid("LINZ aerial needs an API key (free from basemaps.linz.govt.nz)")
        if new["tiles"]["satellite"] == "custom" and not new["tiles"]["sat_custom_url"]:
            raise Invalid("Enter a custom satellite tile URL")
        if new["tiles"]["source"] == "linz_aerial" and not new["tiles"]["linz_key"]:
            raise Invalid("LINZ aerial needs an API key (free from basemaps.linz.govt.nz)")
        if new["tiles"]["source"] == "custom" and not new["tiles"]["custom_url"]:
            raise Invalid("Enter a custom tile URL")

    a = patch.get("alerts")
    if a is not None:
        if "overdue_min" in a:
            new["alerts"]["overdue_min"] = _num(a["overdue_min"], 0, 1440, "Overdue after", True)
        if "speed_limit_kmh" in a:
            new["alerts"]["speed_limit_kmh"] = _num(a["speed_limit_kmh"], 0, 300, "Speed limit", True)
        if "status_watch" in a:
            new["alerts"]["status_watch"] = bool(a["status_watch"])
        if "emergency_keywords" in a:
            new["alerts"]["emergency_keywords"] = str(a["emergency_keywords"])[:300]

    r = patch.get("reports")
    if r is not None:
        if "stop_min" in r:
            new["reports"]["stop_min"] = _num(r["stop_min"], 1, 240, "Stop length", True)
        if "stop_radius_m" in r:
            new["reports"]["stop_radius_m"] = _num(r["stop_radius_m"], 15, 1000, "Stop radius", True)

    rc = patch.get("recording")
    if rc is not None:
        R = new["recording"]
        if "enabled" in rc:
            R["enabled"] = bool(rc["enabled"])
        if "device" in rc:
            d = str(rc["device"]).strip()
            if d and not (d == "test" or re.fullmatch(r"[A-Za-z0-9_:=,.\-]{1,80}", d)):
                raise Invalid("Sound card should look like plughw:CARD=Device,DEV=0")
            R["device"] = d
        if "rate" in rc:
            r = _num(rc["rate"], 8000, 48000, "Sample rate", True)
            if r not in (8000, 16000, 24000, 48000):
                raise Invalid("Sample rate must be 8000, 16000, 24000 or 48000")
            R["rate"] = r
        if "format" in rc:
            if rc["format"] not in ("opus", "wav"):
                raise Invalid("Format must be opus or wav")
            R["format"] = rc["format"]
        for k, lo, hi, name in (("bitrate_kbps", 6, 64, "Bitrate"), ("keep_days", 0, 3650, "Keep recordings"),
                                ("preroll_ms", 0, 2000, "Pre-roll"), ("hang_ms", 0, 3000, "Hang time"),
                                ("min_free_mb", 100, 100000, "Minimum free disk")):
            if k in rc:
                R[k] = _num(rc[k], lo, hi, name, True)
        if "voice_only" in rc:
            R["voice_only"] = bool(rc["voice_only"])
        if R["enabled"] and not R["device"]:
            raise Invalid("Pick the sound card the monitor radio's audio is plugged into")

    pl = patch.get("polling")
    if pl is not None:
        P = new["polling"]
        if "mode" in pl:
            if pl["mode"] not in ("off", "dry_run", "live"):
                raise Invalid("Polling mode must be off, dry_run or live")
            P["mode"] = pl["mode"]
        if "format" in pl:
            if pl["format"] not in ("ascii", "hex"):
                raise Invalid("Command format must be ascii or hex")
            P["format"] = pl["format"]
        if "command" in pl:
            P["command"] = str(pl["command"])[:300]
        if "units" in pl:
            ids = [x.strip() for x in str(pl["units"]).split(",") if x.strip()]
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", x) for x in ids):
                raise Invalid("Units to poll: comma-separated unit IDs, e.g. U00112, U00113")
            P["units"] = ", ".join(ids)
        for k, lo, hi, name in (("moving_s", 5, 3600, "Moving interval"), ("parked_s", 10, 86400, "Parked interval"),
                                ("emergency_s", 3, 600, "Emergency interval"), ("timeout_s", 2, 60, "Reply timeout"),
                                ("max_per_min", 1, 60, "Max polls per minute")):
            if k in pl:
                P[k] = _num(pl[k], lo, hi, name, True)
        if "guard_s" in pl:
            P["guard_s"] = round(_num(pl["guard_s"], 0, 10, "Clear-channel wait"), 1)
        if P["mode"] == "live":
            if not P["command"].strip():
                raise Invalid("Enter the poll command before switching polling to live")
            from .poller import build_command
            try:
                build_command(P, "U00112", 1)
            except Exception as e:
                raise Invalid(f"The poll command can't be built: {e}")

    if "keep_days" in patch:
        new["keep_days"] = _num(patch["keep_days"], 0, 3650, "Keep history", True)
    if "drop_bad_checksum" in patch:
        new["drop_bad_checksum"] = bool(patch["drop_bad_checksum"])
    return new
