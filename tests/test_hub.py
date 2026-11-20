"""Hosted service: companies kept apart, gateway data, sign-in routing, and a Pi's uplink
surviving an outage. Runs real servers on local ports."""
import http.cookiejar
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radio_avl import config as config_mod
from radio_avl import hub as H
from radio_avl.main import App


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def nmea(body):
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"${body}*{cs:02X}"


def frame(unit, lat, seq, status="207"):
    s = nmea(f"PKNDS,010203,A,{lat},S,16820.0000,E,010.0,090.0,201126,22.70,W00,{unit},{status},00,")
    return b"\x02" + s.encode() + b"\r\n" + f"{seq % 100:02d}".encode() + b"\x03"


class Client:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", "Origin": self.base}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.op.open(r) as resp:
                raw = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw


def wait(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def make_hub():
    port = free_port()
    hub = H.Hub(tempfile.mkdtemp(), "127.0.0.1", port)
    srv = H.serve(hub)
    base = f"http://127.0.0.1:{port}"
    asi = Client(base)
    assert asi("POST", "/hub/api/setup", {"username": "alex", "password": "password123"})[0] == 200
    assert asi("POST", "/hub/api/login", {"username": "alex", "password": "password123"})[0] == 200
    return hub, srv, base, asi


def add(asi, name, slug, user, pw):
    code, b = asi("POST", "/hub/api/companies", {"name": name, "slug": slug, "admin": {"username": user, "password": pw}})
    assert code == 200, b
    return b["token"]


def post_gw(base, token, chunks, stream="s1"):
    c = Client(base)
    return c("POST", "/api/gateway/frames", {"stream": stream, "link": {"up": True, "detail": "test"}, "chunks": chunks},
             {"Authorization": "Bearer " + token, "Origin": ""})


def test_companies_are_kept_apart():
    hub, srv, base, asi = make_hub()
    try:
        t1 = add(asi, "Council", "council", "cadmin", "council123")
        t2 = add(asi, "Acme", "acme", "aadmin", "acme12345")
        now = time.time()
        assert post_gw(base, t1, [[1, now, frame("U00112", "4624.0000", 1).hex()]])[1]["accepted"] == 1
        assert post_gw(base, t1, [[1, now, frame("U00112", "4624.0000", 1).hex()]])[1]["accepted"] == 0  # retry
        assert post_gw(base, t2, [[1, now, frame("U00500", "4600.0000", 1, "224").hex()]])[1]["accepted"] == 1
        assert post_gw(base, "gw_" + "x" * 40, [])[0] == 401

        c, a = Client(base), Client(base)
        assert c("POST", "/api/login", {"username": "cadmin", "password": "council123"})[0] == 200
        assert a("POST", "/api/login", {"username": "aadmin", "password": "acme12345"})[0] == 200
        assert [u["id"] for u in c("GET", "/api/units")[1]] == ["U00112"]
        assert [u["id"] for u in a("GET", "/api/units")[1]] == ["U00500"]
        assert c("GET", "/api/alerts")[1] == []
        assert [x["kind"] for x in a("GET", "/api/alerts")[1]] == ["emergency"]
        # the other company's unit isn't reachable by asking for it
        assert c("GET", "/api/track?unit=U00500")[1] == []

        # usernames are unique across the service; the support name is reserved
        assert "already used" in c("POST", "/api/users", {"username": "AADMIN", "password": "x" * 10, "role": "viewer"})[1]["error"]
        assert "reserved" in c("POST", "/api/users", {"username": "asi-support", "password": "x" * 10, "role": "viewer"})[1]["error"]

        # the site's hardware isn't a company admin's to change
        assert c("POST", "/api/settings", {"serial": {"port": "/dev/ttyUSB1"}})[0] == 400
        assert c("POST", "/api/settings", {"web": {"port": 9000}})[0] == 400
        assert c("POST", "/api/settings", {"web": {"title": "SDC"}})[0] == 200
        assert c("POST", "/api/restart", {})[0] == 404
        assert c("GET", "/api/settings")[1]["hosted"] is True

        # a session cookie only ever opens its own company
        assert hub.app("council").auth.session_user("acme~whatever") is None

        # support session, then disabling a company shuts it out
        assert asi("POST", "/hub/api/companies/acme/support", {})[0] == 200
        assert asi("GET", "/api/me")[1]["username"] == "asi-support"
        assert asi("POST", "/hub/api/companies/acme", {"enabled": False})[0] == 200
        assert a("GET", "/api/units")[0] == 401
        assert post_gw(base, t2, [])[0] == 403
        assert Client(base)("POST", "/api/login", {"username": "aadmin", "password": "acme12345"})[0] == 403
        # still unique while disabled
        assert "already used" in c("POST", "/api/users", {"username": "aadmin", "password": "x" * 10, "role": "viewer"})[1]["error"]
    finally:
        hub.shutdown()
        srv.shutdown()


def test_company_web_address():
    hub, srv, base, asi = make_hub()
    try:
        add(asi, "Council", "council", "cadmin", "council123")
        add(asi, "Acme", "acme", "aadmin", "acme12345")
        assert asi("POST", "/hub/api/companies/council", {"domains": "council.track.example.nz"})[0] == 200
        assert asi("POST", "/hub/api/companies/acme", {"domains": ["council.track.example.nz"]})[0] == 400
        on_council = Client(base)
        h = {"X-Forwarded-Host": "council.track.example.nz"}
        assert on_council("GET", "/api/auth-status", headers=h)[1]["title"] == "Council"
        # someone from another company can't sign in on the council's address
        assert on_council("POST", "/api/login", {"username": "aadmin", "password": "acme12345"}, h)[0] == 401
        assert on_council("POST", "/api/login", {"username": "cadmin", "password": "council123"}, h)[0] == 200
    finally:
        hub.shutdown()
        srv.shutdown()


def test_pi_uplink_survives_an_outage():
    hub, srv, base, asi = make_hub()
    token = add(asi, "Council", "council", "cadmin", "council123")
    d = tempfile.mkdtemp()
    cfg = config_mod.merge(config_mod.DEFAULTS, {"data_dir": d, "uplink": {"enabled": True, "url": base, "token": token}})
    pi = App(cfg, None, "replay")
    pi.link(True, "/dev/ttyUSB0 @ 9600")
    threading.Thread(target=pi.uplink.run, daemon=True).start()
    council = hub.app("council")
    try:
        for i in range(5):
            pi.uplink.rx(frame("U00112", f"46{20 + i:02d}.0000", i), time.time())
        assert wait(lambda: council.store.counts()["positions"] == 5)
        assert wait(lambda: pi.uplink.status["state"] == "connected")
        assert council.stats["link_up"] is True

        # the service goes away: the Pi can't reach it
        pi.cfg = config_mod.merge(pi.cfg, {"uplink": {"url": f"http://127.0.0.1:{free_port()}"}})
        for i in range(5, 12):
            pi.uplink.rx(frame("U00113", f"46{20 + i:02d}.0000", i), time.time())
        assert wait(lambda: pi.uplink.status["state"] == "error")
        assert wait(lambda: os.path.exists(pi.uplink.spool_path))
        assert pi.uplink.backlog() == 7
        assert council.store.counts()["positions"] == 5

        # and comes back
        pi.cfg = config_mod.merge(pi.cfg, {"uplink": {"url": base}})
        pi.uplink.restart.set()
        assert wait(lambda: council.store.counts()["positions"] == 12, 30)
        assert wait(lambda: not os.path.exists(pi.uplink.spool_path))
        time.sleep(1.5)
        assert council.store.counts()["positions"] == 12      # nothing sent twice
        assert pi.uplink.backlog() == 0
    finally:
        pi.stop.set()
        hub.shutdown()
        srv.shutdown()


def test_first_admin_only_from_the_local_network():
    port = free_port()
    hub = H.Hub(tempfile.mkdtemp(), "127.0.0.1", port)
    srv = H.serve(hub)
    try:
        c = Client(f"http://127.0.0.1:{port}")
        body = {"username": "mallory", "password": "password123"}
        assert c("POST", "/hub/api/setup", body, {"CF-Connecting-IP": "203.0.113.9"})[0] == 403
        assert hub.auth.needs_setup()
        assert c("POST", "/hub/api/setup", body)[0] == 200
        assert c("POST", "/hub/api/setup", body)[0] == 409
    finally:
        hub.shutdown()
        srv.shutdown()


def test_no_overdue_alerts_while_the_gateway_is_offline():
    hub, srv, base, asi = make_hub()
    try:
        tok = add(asi, "Council", "council", "cadmin", "council123")
        app = hub.app("council")
        app.cfg["alerts"]["overdue_min"] = 1
        old = time.time() - 600
        post_gw(base, tok, [[1, old, frame("U00112", "4624.0000", 1).hex()]])
        with hub.lock, hub.db:                       # the gateway went quiet a while ago
            hub.db.execute("UPDATE companies SET gw_last=? WHERE slug='council'", (time.time() - 300,))
        app.alerts.check_overdue()
        assert app.store.alerts() == []
        post_gw(base, tok, [])                        # back, nothing waiting: the unit really is overdue
        app.alerts.check_overdue()
        assert [a["kind"] for a in app.store.alerts()] == ["overdue"]
    finally:
        hub.shutdown()
        srv.shutdown()


def test_billing():
    from radio_avl import billing
    hub, srv, base, asi = make_hub()
    try:
        tok = add(asi, "Council", "council", "cadmin", "council123")
        add(asi, "Acme", "acme", "aadmin", "acme12345")               # no rate: never charged
        this = billing.month_of(time.time())
        nxt = billing.add_months(this, 1)
        plan = {"flat": 50, "radio": [{"upto": 2, "price": 20}, {"price": 15}], "user": [{"price": 2.5}], "phone": [{"price": 5}]}
        assert asi("POST", "/hub/api/companies/council/rates", {"start_month": this, "plan": plan})[0] == 200
        bad = asi("POST", "/hub/api/companies/council/rates", {"start_month": this, "plan": {"radio": [{"upto": 5, "price": 1}, {"upto": 3, "price": 1}, {"price": 1}]}})
        assert bad[0] == 400 and "higher" in bad[1]["error"]
        now = time.time()
        post_gw(base, tok, [[i + 1, now, frame(u, "4624.0000", i).hex()] for i, u in enumerate(["U00101", "U00102", "U00103"])])
        c = Client(base)
        c("POST", "/api/login", {"username": "cadmin", "password": "council123"})
        c("POST", "/api/users", {"username": "viewer1", "password": "viewer12345", "role": "viewer"})
        c("POST", "/api/mobile/position", {"lat": -46.4, "lon": 168.35})               # a phone
        # 3 radios: 2 x 20 + 1 x 15; 2 users x 2.50; 1 phone x 5; + 50 = 115, GST 17.25
        st = asi("GET", "/hub/api/billing?month=" + this)[1]
        council = next(x for x in st["statements"] if x["slug"] == "council")
        acme = next(x for x in st["statements"] if x["slug"] == "acme")
        assert council["usage"]["radios"] == 3 and council["usage"]["users"] == 2 and council["usage"]["phones"] == 1
        assert (council["subtotal"], council["gst"], council["total"]) == (115.0, 17.25, 132.25)
        assert acme["total"] == 0 and acme["plan"] is None
        csv = asi("GET", "/hub/api/billing.csv?month=" + this)[1].decode()
        assert "Council" in csv and "Acme" not in csv and "U00101 U00102 U00103" in csv

        # a cheaper rate from next month; this month keeps the old one
        assert asi("POST", "/hub/api/companies/council/rates", {"start_month": nxt, "plan": {"flat": 30}})[0] == 200
        assert hub.rate_for("council", this)[0]["flat"] == 50 and hub.rate_for("council", nxt)[0]["flat"] == 30
        # the company sees its own usage and what's coming
        b = c("GET", "/api/billing")[1]
        assert b["enabled"] and b["current"]["total"] == 132.25 and b["scheduled"][0]["start_month"] == nxt
        assert Client(base)("GET", "/api/billing")[0] == 401
        # removing the scheduled change
        rid = [r["id"] for r in hub.rates("council") if r["start_month"] == nxt][0]
        assert asi("DELETE", f"/hub/api/companies/council/rates/{rid}")[0] == 200
        cur = [r["id"] for r in hub.rates("council")][0]
        assert asi("DELETE", f"/hub/api/companies/council/rates/{cur}")[0] == 400     # already in use

        # usage is kept when the company prunes its own history
        hub.update_usage("council")
        app = hub.app("council")
        with app.store.lock, app.store.db:
            app.store.db.execute("DELETE FROM positions")
        hub.update_usage("council")
        assert hub.usage("council", this)["radios"] == 3
        # a closed month doesn't change any more
        hub.update_usage("council", this, final=True)
        post_gw(base, tok, [[9, time.time(), frame("U00109", "4624.0000", 9).hex()]])
        hub.update_usage("council")
        assert hub.usage("council", this)["radios"] == 3
    finally:
        hub.shutdown()
        srv.shutdown()


def test_graduated_tiers():
    from radio_avl import billing
    tiers = billing.validate_plan({"radio": [{"upto": 10, "price": 20}, {"upto": 50, "price": 15}, {"price": 10}]})["radio"]
    assert [(g["qty"], g["price"]) for g in billing.graduated(60, tiers)] == [(10, 20), (40, 15), (10, 10)]
    assert billing.graduated(0, tiers) == []
    assert billing.statement(None, {"radios": 5})["total"] == 0
    s = billing.statement(billing.validate_plan({"radio": [{"price": 0.333}]}), {"radios": 3}, 15)
    assert s["subtotal"] == 0.99 and s["gst"] == 0.15


def test_reset_password_from_the_command_line():
    from radio_avl.auth import reset_password
    hub, srv, base, asi = make_hub()
    try:
        add(asi, "Council", "council", "cadmin", "council123")
        out = []
        assert reset_password(os.path.join(hub.data_dir, "hub.db"), "ALEX", "brand-new-pw", out.append) == 0
        assert Client(base)("POST", "/hub/api/login", {"username": "alex", "password": "brand-new-pw"})[0] == 200
        assert asi("GET", "/hub/api/companies")[0] == 401                # signed out everywhere
        db = os.path.join(hub.data_dir, "companies", "council", "avl.db")
        assert reset_password(db, "cadmin", "council-new", out.append) == 0
        assert Client(base)("POST", "/api/login", {"username": "cadmin", "password": "council-new"})[0] == 200
        assert reset_password(db, "nobody", "whatever12", out.append) == 1 and "cadmin" in out[-1]
        assert reset_password(db, "cadmin", "short", out.append) == 1
    finally:
        hub.shutdown()
        srv.shutdown()


def test_import_history_from_a_box():
    import sqlite3
    hub, srv, base, asi = make_hub()
    try:
        tok = add(asi, "Council", "council", "cadmin", "council123")
        add(asi, "Acme", "acme", "aadmin", "acme12345")
        # the box, with three weeks of history, its people and a zone
        d = tempfile.mkdtemp()
        box = App(config_mod.merge(config_mod.DEFAULTS, {"data_dir": d}), None, "replay")
        box.auth.save_user({"username": "scott", "display_name": "Scott", "password": "scottpass1", "role": "admin"})
        box.auth.save_user({"username": "aadmin", "password": "collides1", "role": "viewer"})
        box.auth.save_user({"username": "cadmin", "password": "collides2", "role": "viewer"})
        zid = box.store.save_geofence({"name": "Depot", "shape": "circle", "geometry": {"center": [-46.41, 168.35], "radius": 200},
                                       "colour": "#2563eb", "alert_enter": 1, "alert_exit": 1, "active": 1})["id"]
        old = time.time() - 21 * 86400
        for i in range(30):
            box.handle(frame("U00112" if i % 2 else "U00113", f"46{20 + i % 9:02d}.0000", i)[1:-1], old + i * 60)
        with box.store.lock, box.store.db:
            box.store.db.execute("UPDATE units SET alias='Truck 12' WHERE id='U00112'")
            box.store.db.execute("INSERT INTO alerts (t, kind, severity, unit_id, geofence_id, detail) VALUES (?,?,?,?,?,?)",
                                 (old + 100, "enter", "info", "U00112", zid, "entered Depot"))
            box.store.db.execute("INSERT INTO transmissions (start, end, duration, kind, unit_id, audio) VALUES (?,?,?,?,?,?)",
                                 (old + 200, old + 205, 5, "voice", "U00112", "voice/x.opus"))
        bak = os.path.join(d, "backup.db")
        src = sqlite3.connect(bak)
        box.store.db.backup(src)
        src.close()
        # the hosted company already has today's data from the gateway
        post_gw(base, tok, [[1, time.time(), frame("U00112", "4624.0000", 1).hex()]])
        council = hub.app("council")
        assert council.store.counts()["positions"] == 1

        r = hub.import_box("council", bak, "alex")
        assert r["positions"] == 30 and r["units_new"] == 1 and r["units_merged"] == 1
        assert r["alerts"] == 1 and r["calls"] == 1 and r["zones_new"] == 1
        with council.store.lock:
            gid = council.store.db.execute("SELECT geofence_id FROM alerts").fetchone()[0]
        assert [f["name"] for f in council.store.geofences() if f["id"] == gid] == ["Depot"]
        assert r["users_new"] == ["scott"]
        assert sorted(r["users_skipped"]) == ["aadmin (used by another company)", "cadmin (already in this company)"]
        assert council.store.counts()["positions"] == 31
        units = {u["id"]: u for u in council.store.units()}
        assert units["U00112"]["alias"] == "Truck 12"
        assert units["U00112"]["last_seen"] > time.time() - 60          # still today's position
        with council.store.lock:
            assert council.store.db.execute("SELECT audio FROM transmissions").fetchone()[0] is None
        # the box's people sign in with the passwords they know
        assert Client(base)("POST", "/api/login", {"username": "scott", "password": "scottpass1"})[0] == 200
        # importing again adds nothing
        r2 = hub.import_box("council", bak, "alex")
        assert r2["positions"] == 0 and r2["alerts"] == 0 and r2["users_new"] == []
        # not a backup
        junk = os.path.join(d, "junk.db")
        open(junk, "wb").write(b"hello")
        try:
            hub.import_box("council", junk, "alex")
            assert False
        except H.HubError as e:
            assert "isn't a KenTrackIt backup" in str(e)
        # through the page
        with open(bak, "rb") as f:
            raw = f.read()
        req = urllib.request.Request(base + "/hub/api/companies/council/import", data=raw, method="POST",
                                     headers={"Content-Type": "application/octet-stream", "Origin": base})
        resp = asi.op.open(req)
        assert json.loads(resp.read())["positions"] == 0
    finally:
        hub.shutdown()
        srv.shutdown()
