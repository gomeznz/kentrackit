"""iPhone push alerts: Apple's signed token, who gets what, and tidying up dead phones."""
import base64
import json
import os
import sys
import tempfile
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radio_avl import config as config_mod
from radio_avl import push as push_mod
from radio_avl.main import App

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class FakeApple:
    def __init__(self):
        self.sent = []
        self.reply = {}                      # device token -> (status, reason)

    def __call__(self, req):
        tok = req.url.path.rsplit("/", 1)[-1]
        self.sent.append({"host": req.url.host, "token": tok, "headers": dict(req.headers), "body": json.loads(req.content)})
        status, reason = self.reply.get(tok, (200, ""))
        return httpx.Response(status, json={"reason": reason} if reason else None)


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture()
def box():
    d = tempfile.mkdtemp()
    cfg = config_mod.merge(config_mod.DEFAULTS, {"data_dir": d})
    cfg = config_mod.apply_patch(cfg, {"push": {"key": PEM, "key_id": "ABC123DEFG", "team_id": "TEAM123456"}})
    app = App(cfg, None, "replay")
    fake = FakeApple()
    app.apns.transport = httpx.MockTransport(fake)
    return app, fake


def signed_in(app, username, units=None, role="admin"):
    u = app.auth.save_user({"username": username, "password": "password123", "role": role, "units": units})
    tok = app.auth.create_session(u, True, "127.0.0.1", "KenTrackItApp/1.0 (iOS 18)")
    return u, tok


def test_apples_signed_token(box):
    app, fake = box
    u, s = signed_in(app, "alex")
    app.push.register(u, s, TOKEN_A, "development")
    assert app.push.test(u) == ["sent"]
    req = fake.sent[0]
    assert req["host"] == "api.sandbox.push.apple.com"
    assert req["headers"]["apns-topic"] == "nz.co.asiltd.kentrackit" and req["headers"]["apns-push-type"] == "alert"
    jwt = req["headers"]["authorization"].split(" ", 1)[1]
    head, body, sig = jwt.split(".")
    assert json.loads(b64d(head)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(b64d(body))["iss"] == "TEAM123456"
    raw = b64d(sig)
    KEY.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    assert req["body"]["aps"]["interruption-level"] == "time-sensitive"     # no Critical Alerts approval yet


def test_who_gets_alerts(box):
    app, fake = box
    a, sa = signed_in(app, "alex")
    b, sb = signed_in(app, "driver", units=["U00200"], role="viewer")
    app.push.register(a, sa, TOKEN_A, "production")
    app.push.register(b, sb, TOKEN_B, "production", other_alerts=False)
    alert = app.store.add_alert({"t": time.time(), "kind": "emergency", "severity": "critical", "unit_id": "U00112",
                                 "detail": "Emergency: Truck 12"})
    app.push.on_alert(alert)
    assert [x["token"] for x in fake.sent] == [TOKEN_A]                     # the driver can't see U00112
    assert fake.sent[0]["host"] == "api.push.apple.com"
    assert fake.sent[0]["headers"]["apns-collapse-id"] == f"alert-{alert['id']}"
    assert fake.sent[0]["body"]["aps"]["sound"] == "alarm.wav"
    fake.sent.clear()
    warn = app.store.add_alert({"t": time.time(), "kind": "overdue", "severity": "warn", "unit_id": "U00200", "detail": "overdue"})
    app.push.on_alert(warn)
    assert [x["token"] for x in fake.sent] == [TOKEN_A]                     # the driver turned other alerts off
    fake.sent.clear()
    info = app.store.add_alert({"t": time.time(), "kind": "enter", "severity": "info", "unit_id": "U00200", "detail": "x"})
    app.push.on_alert(info)
    assert fake.sent == []
    # acknowledging replaces it on the phones
    app.push.on_ack([alert["id"]], "Dealt with by Sam")
    assert {x["body"]["aps"]["alert"]["title"] for x in fake.sent} == {"Acknowledged"}
    assert fake.sent[0]["headers"]["apns-collapse-id"] == f"alert-{alert['id']}"


def test_signed_out_and_dead_phones(box):
    app, fake = box
    a, sa = signed_in(app, "alex")
    app.push.register(a, sa, TOKEN_A, "production")
    assert len(app.push.phones()) == 1
    app.auth.logout(sa)
    assert app.push.phones() == []                                           # signed out: no more alerts
    a2 = app.auth.create_session(a, True, "1.2.3.4", "x")
    app.push.register(a, a2, TOKEN_A, "production")
    fake.reply[TOKEN_A] = (410, "Unregistered")                              # app deleted from the phone
    app.push.on_alert(app.store.add_alert({"t": time.time(), "kind": "emergency", "severity": "critical", "detail": "x"}))
    with app.store.lock:
        assert app.store.db.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0


def test_critical_alerts_and_settings(box):
    app, fake = box
    app.cfg = config_mod.apply_patch(app.cfg, {"push": {"critical": True}})
    assert app.cfg["push"]["key"] == PEM.strip() + "\n"                      # kept when not sent again
    a, sa = signed_in(app, "alex")
    app.push.register(a, sa, TOKEN_A, "production")
    app.push.on_alert(app.store.add_alert({"t": time.time(), "kind": "emergency", "severity": "critical", "detail": "x"}))
    aps = fake.sent[0]["body"]["aps"]
    assert aps["interruption-level"] == "critical" and aps["sound"]["critical"] == 1
    assert config_mod.public(app.cfg)["push"] == {"key_id": "ABC123DEFG", "team_id": "TEAM123456",
                                                  "bundle_id": "nz.co.asiltd.kentrackit", "critical": True, "key_set": True}
    for bad in ({"key": "hello"}, {"key_id": "short"}, {"team_id": "toolongteamid"}, {"bundle_id": "bad id!"}):
        with pytest.raises(config_mod.Invalid):
            config_mod.apply_patch(app.cfg, {"push": bad})
    with pytest.raises(push_mod.PushError):
        app.push.register(a, sa, "not-a-token", "production")


def test_hosted_service_shares_one_key():
    sys.path.insert(0, os.path.dirname(__file__))
    from test_hub import make_hub, add
    hub, srv, base, asi = make_hub()
    try:
        add(asi, "Council", "council", "cadmin", "council123")
        assert asi("POST", "/hub/api/push", {"key": "nope"})[0] == 400
        r = asi("POST", "/hub/api/push", {"key": PEM, "key_id": "ABC123DEFG", "team_id": "TEAM123456"})[1]
        assert r["key_set"] and "key" not in r
        council = hub.app("council")
        assert council.apns is hub.apns and council.apns.configured()
        fake = FakeApple()
        hub.apns.transport = httpx.MockTransport(fake)
        u = council.auth.by_name("cadmin")
        s = council.auth.create_session(u, True, "1.2.3.4", "x")
        council.push.register(u, s, TOKEN_A, "production")
        council.push.on_alert(council.store.add_alert({"t": time.time(), "kind": "emergency", "severity": "critical",
                                                       "unit_id": "U00112", "detail": "Emergency: U00112"}))
        assert fake.sent[0]["body"]["aps"]["alert"]["subtitle"] == "Council"
        # a company can't change the service's key
        c = __import__("test_hub").Client(base)
        c("POST", "/api/login", {"username": "cadmin", "password": "council123"})
        assert c("POST", "/api/settings", {"push": {"critical": True}})[0] == 400
        assert c("GET", "/api/push")[1]["configured"] is True
    finally:
        hub.shutdown()
        srv.shutdown()
