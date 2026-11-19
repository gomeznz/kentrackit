"""
Gateway uplink: this Pi sends what the monitor radio says to a company on the hosted KenTrackIt
service, while still doing everything locally (map, LCD, recording, talk-back).

The raw serial bytes go up exactly as they were read, each chunk with its receive time and a
sequence number, over HTTPS POSTs that also work through Cloudflare. If the service can't be
reached the chunks are spooled to disk and sent, in order and with their original times, when
it comes back. The service ignores sequence numbers it has already had, so nothing is counted
twice after a retry.
"""
import collections
import http.client
import json
import logging
import os
import secrets
import ssl
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("radio-avl")

BATCH = 400                    # chunks per POST
MAX_SPOOL = 50 * 1024 * 1024   # bytes of backlog kept on disk while the service is unreachable
MAX_QUEUE = 2000               # chunks kept in memory before they go to the spool
HEARTBEAT_S = 10               # check in this often even when the radio is quiet
RESERVE = 10000                # sequence numbers reserved per write of the state file


class Uplink:
    def __init__(self, app):
        self.app = app
        self.restart = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.q = collections.deque()
        d = app.cfg["data_dir"]
        self.spool_path = os.path.join(d, "uplink-spool.jsonl")
        self.state_path = os.path.join(d, "uplink-state.json")
        self.conn = None
        self.conn_key = None
        st = self._load_state()
        self.stream = st["stream"]
        self.seq = st["reserved"]        # never reuse a number, even after a crash
        self.reserved = self.seq
        self._reserve()
        self.status = {"state": "off", "detail": "", "last_ok": None, "company": "", "sent": 0,
                       "backlog": 0, "dropped": 0}

    # ---- sequence numbers ---------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if isinstance(st.get("stream"), str) and isinstance(st.get("reserved"), int):
                return st
        except (OSError, ValueError):
            pass
        return {"stream": secrets.token_hex(8), "reserved": 0}

    def _reserve(self):
        self.reserved = self.seq + RESERVE
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"stream": self.stream, "reserved": self.reserved}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("uplink: can't save state: %s", e)

    # ---- from the serial reader -------------------------------------------------
    def enabled(self):
        u = self.app.cfg["uplink"]
        return bool(u["enabled"] and u["url"] and u["token"])

    def rx(self, data, t):
        if not self.enabled():
            return
        with self.lock:
            self.seq += 1
            if self.seq >= self.reserved:
                self._reserve()
            self.q.append([self.seq, round(t, 3), data.hex()])
            if len(self.q) > MAX_QUEUE:
                self._spool_queue()
        self.wake.set()

    def status_changed(self):
        self.wake.set()

    # ---- spool ---------------------------------------------------------------------
    def _spool_queue(self):
        """Move the in-memory queue to the end of the spool file (caller holds the lock)."""
        if not self.q:
            return
        try:
            size = os.path.getsize(self.spool_path) if os.path.exists(self.spool_path) else 0
            if size > MAX_SPOOL:
                self.status["dropped"] += len(self.q)
                self.q.clear()
                return
            with open(self.spool_path, "a") as f:
                for c in self.q:
                    f.write(json.dumps(c) + "\n")
            self.q.clear()
        except OSError as e:
            log.warning("uplink: can't write spool: %s", e)

    def flush(self):
        """On shutdown: keep what hasn't been sent yet."""
        with self.lock:
            self._spool_queue()

    def _spooled(self, n):
        try:
            with open(self.spool_path) as f:
                out = []
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
                    if len(out) >= n:
                        break
                return out
        except FileNotFoundError:
            return []

    def _drop_spooled(self, last_seq):
        """Remove spooled chunks up to and including last_seq."""
        try:
            with open(self.spool_path) as f:
                keep = [line for line in f if self._seq_of(line) > last_seq]
            if keep:
                tmp = self.spool_path + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(keep)
                os.replace(tmp, self.spool_path)
            else:
                os.unlink(self.spool_path)
        except FileNotFoundError:
            pass

    @staticmethod
    def _seq_of(line):
        try:
            return json.loads(line)[0]
        except (ValueError, IndexError, TypeError):
            return -1

    def backlog(self):
        n = len(self.q)
        try:
            with open(self.spool_path) as f:
                n += sum(1 for _ in f)
        except FileNotFoundError:
            pass
        return n

    # ---- sending ----------------------------------------------------------------------
    def _connection(self):
        u = urlparse(self.app.cfg["uplink"]["url"])
        key = (u.scheme, u.netloc)
        if self.conn is None or self.conn_key != key:
            if self.conn:
                self.conn.close()
            if u.scheme == "https":
                self.conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=20,
                                                        context=ssl.create_default_context())
            else:
                self.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=20)
            self.conn_key = key
        return self.conn

    def _post(self, chunks):
        st = self.app.stats
        body = json.dumps({
            "stream": self.stream, "version": self.app.VERSION,
            "link": {"up": st["link_up"], "detail": st["link_detail"], "port": st.get("serial_port")},
            "backlog": max(0, self.backlog() - len(chunks)), "chunks": chunks,
        }).encode()
        c = self._connection()
        try:
            c.request("POST", "/api/gateway/frames", body, {
                "Content-Type": "application/json", "User-Agent": f"KenTrackItGateway/{self.app.VERSION}",
                "Authorization": "Bearer " + self.app.cfg["uplink"]["token"]})
            r = c.getresponse()
            data = r.read()
        except Exception:
            c.close()
            self.conn = None
            raise
        try:
            reply = json.loads(data or b"{}")
        except ValueError:
            reply = {}
        return r.status, reply

    def _set(self, state, detail=""):
        changed = (state, detail) != (self.status["state"], self.status["detail"])
        self.status["state"], self.status["detail"] = state, detail
        if changed:
            self.app.broker.publish("uplink", self.info())
            if state in ("connected", "error", "refused"):
                self.app.store.add_event("uplink", f"hosted service: {state}" + (f" ({detail})" if detail else ""))

    def info(self):
        return dict(self.status, enabled=self.enabled(), url=self.app.cfg["uplink"]["url"], backlog=self.backlog())

    def run(self):
        while not self.app.stop.is_set():
            self.restart.clear()
            if not self.enabled():
                self._set("off")
                self.restart.wait(2)
                continue
            self._set("connecting")
            backoff, last_post = 2, 0.0
            while not self.app.stop.is_set() and not self.restart.is_set() and self.enabled():
                self.wake.wait(max(0.0, HEARTBEAT_S - (time.time() - last_post)))
                time.sleep(0.3)                     # gather a few chunks into one POST
                self.wake.clear()
                from_spool = True
                chunks = self._spooled(BATCH)
                if not chunks:
                    from_spool = False
                    with self.lock:
                        chunks = list(self.q)[:BATCH]
                if not chunks and time.time() - last_post < HEARTBEAT_S - 0.5:
                    continue
                try:
                    code, reply = self._post(chunks)
                except Exception as e:
                    code, reply = 0, {"error": str(e) or e.__class__.__name__}
                last_post = time.time()
                if code == 200:
                    backoff = 2
                    self.status.update(last_ok=self.app.now(), company=reply.get("company", ""))
                    self.status["sent"] += len(chunks)
                    if chunks:
                        if from_spool:
                            self._drop_spooled(chunks[-1][0])
                        else:
                            with self.lock:
                                for _ in range(min(len(chunks), len(self.q))):
                                    self.q.popleft()
                    self._set("connected", reply.get("company", ""))
                    if self.backlog():
                        self.wake.set()             # more to send: go straight on
                    continue
                # keep the data for later
                with self.lock:
                    self._spool_queue()
                if code in (401, 403):
                    self._set("refused", reply.get("error", "the service refused this gateway key"))
                    backoff = 60
                else:
                    self._set("error", reply.get("error") or f"HTTP {code}")
                    backoff = min(backoff * 2, 60)
                if self.restart.wait(backoff):
                    break
                self.wake.set()
            if self.conn:
                self.conn.close()
                self.conn = None
