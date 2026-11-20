"""
Bring a KenTrackIt box's history into a company on the hosted service.

The input is the box's own backup (Settings -> Download backup on the box, an avl.db file).
Everything from before the company's hosted data starts is copied in: positions, calls,
alerts, coverage and the event log. Units are merged, zones are added by name, and the box's
user accounts come across with their passwords unless the username is already taken on the
service. Rows from after the hosted data starts are left out, so nothing is counted twice,
and importing the same backup again adds nothing.

Recordings (sound files) stay on the box; their calls come across without the audio.
"""
import sqlite3

from .auth import SUPPORT_USER

SQLITE_MAGIC = b"SQLite format 3\x00"


class ImportError_(Exception):
    pass


def _cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def check_file(path):
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise ImportError_("That isn't a KenTrackIt backup (not a database file)")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ImportError_("The backup file is damaged")
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"units", "positions"} <= tables:
            raise ImportError_("That isn't a KenTrackIt backup (no units or positions in it)")
    except sqlite3.DatabaseError as e:
        raise ImportError_(f"Can't read the backup: {e}")
    finally:
        src.close()


def merge(app, path, username_taken):
    """Merge the backup at `path` into a company's App. username_taken(name) -> True if the
    name is used elsewhere on the service. Returns a summary dict."""
    check_file(path)
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db, lock = app.store.db, app.store.lock
    out = {"units_new": 0, "units_merged": 0, "positions": 0, "calls": 0, "alerts": 0, "coverage": 0,
           "events": 0, "zones_new": 0, "zones_same": 0, "users_new": [], "users_skipped": [], "cutoff": None}
    try:
        with lock, db:
            # the hosted data starts here; older rows come from the box
            starts = [r[0] for r in (
                db.execute("SELECT MIN(rx_time) FROM positions").fetchone(),
                db.execute("SELECT MIN(start) FROM transmissions").fetchone(),
                db.execute("SELECT MIN(t) FROM alerts").fetchone()) if r[0] is not None]
            cutoff = min(starts) if starts else float("inf")
            out["cutoff"] = None if cutoff == float("inf") else cutoff

            # units
            ucols = [c for c in _cols(src, "units") if c in set(_cols(db, "units"))]
            have = {r["id"]: r for r in db.execute("SELECT * FROM units")}
            for u in src.execute("SELECT * FROM units"):
                cur = have.get(u["id"])
                if cur is None:
                    db.execute(f"INSERT INTO units ({','.join(ucols)}) VALUES ({','.join('?' * len(ucols))})",
                               [u[c] for c in ucols])
                    out["units_new"] += 1
                    continue
                upd = {}
                for c in ("alias", "colour", "notes"):
                    if c in ucols and not cur[c] and u[c]:
                        upd[c] = u[c]
                if u["first_seen"] and (cur["first_seen"] is None or u["first_seen"] < cur["first_seen"]):
                    upd["first_seen"] = u["first_seen"]
                if u["reports"]:
                    upd["reports"] = (cur["reports"] or 0) + u["reports"]
                if cur["last_seen"] is None and u["last_seen"]:
                    for c in ("last_seen", "last_lat", "last_lon", "last_speed_kn", "last_course",
                              "last_fix_time", "last_valid"):
                        upd[c] = u[c]
                if upd:
                    db.execute(f"UPDATE units SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                               list(upd.values()) + [u["id"]])
                out["units_merged"] += 1

            def copy(table, tcol, rename=None, fix=None):
                if table not in tables:
                    return 0
                cols = [c for c in _cols(src, table) if c != "id" and c in set(_cols(db, table))]
                n = 0
                sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                for r in src.execute(f"SELECT {','.join(cols)} FROM {table} WHERE {tcol} < ?", (cutoff,)):
                    row = dict(zip(cols, r))
                    if fix:
                        fix(row)
                    db.execute(sql, [row[c] for c in cols])
                    n += 1
                return n

            # zones by name, so alerts can point at them
            zone_map = {}
            if "geofences" in tables:
                gcols = [c for c in _cols(src, "geofences") if c != "id" and c in set(_cols(db, "geofences"))]
                by_name = {r[1].lower(): r[0] for r in db.execute("SELECT id, name FROM geofences")}
                for g in src.execute("SELECT * FROM geofences"):
                    if g["name"].lower() in by_name:
                        zone_map[g["id"]] = by_name[g["name"].lower()]
                        out["zones_same"] += 1
                        continue
                    cur = db.execute(f"INSERT INTO geofences ({','.join(gcols)}) VALUES ({','.join('?' * len(gcols))})",
                                     [g[c] for c in gcols])
                    zone_map[g["id"]] = by_name[g["name"].lower()] = cur.lastrowid
                    out["zones_new"] += 1

            def fix_alert(row):
                if row.get("geofence_id") is not None:
                    row["geofence_id"] = zone_map.get(row["geofence_id"])

            def fix_call(row):
                if "audio" in row:
                    row["audio"] = None         # the sound files stay on the box

            out["positions"] = copy("positions", "rx_time")
            out["calls"] = copy("transmissions", "start", fix=fix_call)
            out["alerts"] = copy("alerts", "t", fix=fix_alert)
            out["coverage"] = copy("coverage", "t")
            out["events"] = copy("events", "rx_time")

            # people, with the passwords they already use
            if "users" in tables:
                cols = [c for c in _cols(src, "users") if c != "id" and c in set(_cols(db, "users"))]
                for u in src.execute("SELECT * FROM users"):
                    name = u["username"]
                    if name.lower() == SUPPORT_USER:
                        continue
                    if db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (name,)).fetchone():
                        out["users_skipped"].append(f"{name} (already in this company)")
                        continue
                    if username_taken(name):
                        out["users_skipped"].append(f"{name} (used by another company)")
                        continue
                    db.execute(f"INSERT INTO users ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [u[c] for c in cols])
                    out["users_new"].append(name)
    finally:
        src.close()
    app.alerts.reload_fences()
    return out


def describe(s):
    bits = [f"{s['units_new']} new units", f"{s['units_merged']} merged", f"{s['positions']:,} positions",
            f"{s['calls']:,} calls", f"{s['alerts']:,} alerts", f"{s['zones_new']} zones"]
    if s["users_new"]:
        bits.append("users " + ", ".join(s["users_new"]))
    return ", ".join(bits)
