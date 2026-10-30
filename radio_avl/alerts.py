"""
Alert engine: geofence enter/exit, overdue units, speeding, status changes, emergencies.

Per-unit state (which zones it's in, overdue/speeding flags, last status fields) is kept in
memory and persisted in units.state so a restart doesn't fire a flood of alerts.
"""
import logging
import threading
import time

from . import geo

log = logging.getLogger("radio-avl")

EXIT_BUFFER_M = 25          # GPS wanders ~30 m; must be this far outside to count as leaving
SEVERITY = {"emergency": "critical", "test": "critical", "status": "warn", "overdue": "warn",
            "speed": "warn", "enter": "info", "exit": "info"}


class AlertEngine:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.state = app.store.unit_states()
        self.fences = []
        self.last_display_alert = ("", 0.0)
        self.reload_fences()

    def reload_fences(self):
        with self.lock:
            self.fences = [f for f in self.app.store.geofences() if f["active"]]

    def _st(self, uid):
        return self.state.setdefault(uid, {})

    def _save(self, uid):
        self.app.store.set_unit_state(uid, self.state.get(uid, {}))

    def raise_alert(self, kind, detail, unit_id=None, t=None, lat=None, lon=None, fence_id=None):
        a = self.app.store.add_alert({
            "t": t or self.app.now(), "kind": kind, "severity": SEVERITY.get(kind, "warn"),
            "unit_id": unit_id, "geofence_id": fence_id, "detail": detail, "lat": lat, "lon": lon})
        self.app.store.add_event("alert", f"{kind}: {detail}", unit_id, a["t"])
        self.app.broker.publish("alert", a)
        log.warning("ALERT %s %s %s", kind, unit_id or "", detail)
        return a

    def _auto_ack(self, unit_id, kind, note, t):
        ids = self.app.store.ack_alerts(unit_id=unit_id, kind=kind, note=note, t=t)
        if ids:
            self.app.broker.publish("alert_ack", {"ids": ids, "note": note})

    # ---- triggers ----------------------------------------------------------
    def on_position(self, d, unit_row, t):
        cfg = self.app.cfg["alerts"]
        uid = d["unit"]
        name = unit_row.get("alias") or "Unit " + uid.lstrip("UuGg0")
        with self.lock:
            st = self._st(uid)
            changed = False

            if st.get("overdue"):
                st["overdue"] = False
                changed = True
                self._auto_ack(uid, "overdue", "Reported again", t)

            # Status/unknown fields in the GPS sentence. Their meaning isn't known yet, so any
            # change is surfaced (a status message or emergency may well show up here).
            extra = d.get("extra") or {}
            prev = st.get("extra")
            if cfg["status_watch"] and prev is not None and extra != prev:
                diffs = [f"{k}: {prev.get(k, '')!s} → {v!s}" for k, v in extra.items() if prev.get(k) != v]
                if diffs:
                    self.raise_alert("status", f"{name}: report fields changed ({'; '.join(diffs)})",
                                     uid, t, d.get("lat"), d.get("lon"))
            if extra != prev:
                st["extra"] = extra
                changed = True

            if d["valid"]:
                lat, lon = d["lat"], d["lon"]
                prev_in = set(st["fences"]) if "fences" in st else None
                now_in = set()
                for f in self.fences:
                    was_in = prev_in is not None and f["id"] in prev_in
                    if geo.inside(f, lat, lon, EXIT_BUFFER_M if was_in else 0):
                        now_in.add(f["id"])
                if prev_in is not None:
                    for f in self.fences:
                        if f["id"] in now_in - prev_in and f["alert_enter"]:
                            self.raise_alert("enter", f"{name} entered {f['name']}", uid, t, lat, lon, f["id"])
                        elif f["id"] in prev_in - now_in and f["alert_exit"]:
                            self.raise_alert("exit", f"{name} left {f['name']}", uid, t, lat, lon, f["id"])
                if prev_in != now_in:
                    st["fences"] = sorted(now_in)
                    changed = True

                limit = cfg["speed_limit_kmh"]
                kmh = (d["speed_kn"] or 0) * 1.852
                if limit and kmh > limit and not st.get("speeding"):
                    st["speeding"] = True
                    changed = True
                    self.raise_alert("speed", f"{name} at {kmh:.0f} km/h (limit {limit})", uid, t, lat, lon)
                elif st.get("speeding") and (not limit or kmh < limit - 5):
                    st["speeding"] = False
                    changed = True

            if changed:
                self._save(uid)
        return {"fences": self.state.get(uid, {}).get("fences", [])}

    def on_display(self, d, t):
        """The monitor radio's display text. Kenwood radios pop up the alert on screen."""
        text = " ".join(x for x in d["fields"] if x)
        low = text.lower()
        kws = [k.strip().lower() for k in self.app.cfg["alerts"]["emergency_keywords"].split(",") if k.strip()]
        if not any(k in low for k in kws):
            return
        last_text, last_t = self.last_display_alert
        if text == last_text and t - last_t < 60:
            return
        self.last_display_alert = (text, t)
        # Try to work out which unit it is from the text (e.g. "EMERGENCY U00113" or "113")
        unit_id, lat, lon = None, None, None
        tokens = {tok.strip(".,:;()").upper() for tok in text.split()}
        for u in self.app.store.units():
            names = {u["id"].upper(), u["id"].lstrip("UuGg0"), (u["alias"] or "").upper()}
            if tokens & {n for n in names if n}:
                unit_id, lat, lon = u["id"], u["last_lat"], u["last_lon"]
                break
        self.raise_alert("emergency", f"Radio display: {text}", unit_id, t, lat, lon)

    def check_overdue(self):
        mins = self.app.cfg["alerts"]["overdue_min"]
        if not mins:
            return
        now = self.app.now()
        for u in self.app.store.units():
            if not u["last_seen"] or now - u["last_seen"] < mins * 60:
                continue
            with self.lock:
                st = self._st(u["id"])
                if st.get("overdue"):
                    continue
                st["overdue"] = True
                self._save(u["id"])
            name = u["alias"] or "Unit " + u["id"].lstrip("UuGg0")
            self.raise_alert("overdue", f"{name} hasn't reported for {int((now - u['last_seen']) // 60)} min",
                             u["id"], now, u["last_lat"], u["last_lon"])

    def unit_fences(self):
        with self.lock:
            return {uid: st.get("fences", []) for uid, st in self.state.items()}
