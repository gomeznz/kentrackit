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
