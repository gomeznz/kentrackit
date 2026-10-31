#!/usr/bin/env python3
"""
fake_sip.py - a stand-in SIP endpoint (an NXR-1800, a phone, a PBX) for bench testing.
Python standard library only: runs on the Pi, a NUC or a laptop with nothing to install.

It answers calls, records everything it hears to WAV files, and can talk back:

  --echo SECS        parrot: after each over ends, play it back SECS later (like an NXDN parrot radio)
  --play FILE.wav    play a WAV file once the call is answered (any rate, mono or stereo 16-bit)
  --tone             play a 1 kHz test tone for --tone-secs after answering
  --every SECS       repeat the --play / --tone audio every SECS while the call is up

It can also place calls itself, e.g. to imitate the NXR sending group traffic to KenTrackIt:

  --call HOST:PORT [--to NUMBER] [--play FILE | --tone] [--hangup SECS]

Other options: --auth PASSWORD (demand digest auth), --reject CODE (e.g. 486 busy), --codec PCMU|PCMA,
--ring SECS (ring before answering), --max SECS (hang up long calls), --out DIR (where WAVs go).

Examples
  python3 tools/fake_sip.py --port 5070 --echo 0.5              # parrot on 5070; point KenTrackIt's NXR at it
  python3 tools/fake_sip.py --port 5070 --play group_call.wav --every 20
  python3 tools/fake_sip.py --call 192.168.1.20:5064 --play test.wav --hangup 15   # call KenTrackIt
"""
import argparse
import array
import hashlib
import math
import os
import random
import re
import socket
import struct
import sys
import threading
import time
import uuid
import wave

RATE, FRAME = 8000, 160                      # 8 kHz, 20 ms


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---- G.711 (pure Python) -------------------------------------------------------------------------
def _ulaw_dec(u):
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return 0x84 - t if u & 0x80 else t - 0x84


def _alaw_dec(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = t + 8 if seg == 0 else t + 0x108
    if seg > 1:
        t <<= seg - 1
    return t if a & 0x80 else -t


def _ulaw_enc(x):
    sign = 0x80 if x < 0 else 0
    x = min(abs(x), 32635) + 0x84
    exp = max(0, min(7, x.bit_length() - 8))
    return ~(sign | (exp << 4) | ((x >> (exp + 3)) & 0x0F)) & 0xFF


def _alaw_enc(x):
    mask = 0xD5 if x >= 0 else 0x55
    x = (x >> 3) if x >= 0 else ((-x - 1) >> 3)
    for seg, end in enumerate((0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)):
        if x <= end:
            break
    else:
        return 0x7F ^ mask
    aval = (seg << 4) | ((x >> 1) & 0x0F if seg < 2 else (x >> seg) & 0x0F)
    return aval ^ mask


DEC = {0: [_ulaw_dec(i) for i in range(256)], 8: [_alaw_dec(i) for i in range(256)]}
_ENC_U = bytes(_ulaw_enc(v - 32768) for v in range(65536))
_ENC_A = bytes(_alaw_enc(v - 32768) for v in range(65536))
CODECS = {"PCMU": 0, "PCMA": 8}


def encode(pt, pcm):
    t = _ENC_U if pt == 0 else _ENC_A
    return bytes(t[s + 32768] for s in pcm)


def decode(pt, payload):
    d = DEC[pt]
    return array.array("h", (d[b] for b in payload))


def rms(a):
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def db(v):
    return 20 * math.log10(v / 32768) if v > 1 else -99.0


# ---- audio sources ----------------------------------------------------------------------------
def load_wav(path):
    """Any 16-bit WAV -> 8 kHz mono array (simple averaging resample; fine for test audio)."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit(f"{path}: only 16-bit WAV files, please")
        ch, rate = w.getnchannels(), w.getframerate()
        a = array.array("h", w.readframes(w.getnframes()))
    if ch > 1:
        a = array.array("h", (sum(a[i:i + ch]) // ch for i in range(0, len(a), ch)))
    if rate != RATE:
        step = rate / RATE
        a = array.array("h", (a[int(i * step)] for i in range(int(len(a) / step))))
    return a


def tone(secs, freq=1000, level_db=-10):
    amp = 32767 * 10 ** (level_db / 20)
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(int(secs * RATE))))


def frames(a):
    for i in range(0, len(a) - FRAME + 1, FRAME):
        yield a[i:i + FRAME]


# ---- SIP messages ----------------------------------------------------------------------------------
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}


class Msg:
    def __init__(self, data):
        head, _, self.body = data.decode("utf-8", "replace").partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.first = lines[0]
        self.headers = []
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k = k.strip().lower()
                self.headers.append((COMPACT.get(k, k), v.strip()))
        p = self.first.split(" ", 2)
        self.is_response = p[0].startswith("SIP/")
        self.method = None if self.is_response else p[0]
        self.uri = None if self.is_response else p[1]
        self.status = int(p[1]) if self.is_response else None

    def get(self, k, default=""):
        for n, v in self.headers:
            if n == k:
                return v
        return default

    def all(self, k):
        return [v for n, v in self.headers if n == k]

    @property
    def cseq(self):
        n, m = self.get("cseq", "0 X").split()
        return int(n), m


def tag():
    return uuid.uuid4().hex[:10]


def local_ip_for(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sdp(ip, port, pt):
    name = "PCMU" if pt == 0 else "PCMA"
    sid = int(time.time())
    return (f"v=0\r\no=fake_sip {sid} {sid} IN IP4 {ip}\r\ns=fake_sip\r\nc=IN IP4 {ip}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP {pt} 101\r\na=rtpmap:{pt} {name}/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\na=ptime:20\r\na=sendrecv\r\n")


def parse_sdp(body):
    ip = port = None
    pts = []
    for ln in body.splitlines():
        if ln.startswith("c=IN IP4 "):
            ip = ln.split()[2]
        elif ln.startswith("m=audio "):
            f = ln.split()
            port, pts = int(f[1]), [int(x) for x in f[3:] if x.isdigit()]
    return ip, port, pts


# ---- one call's audio ------------------------------------------------------------------------------
class Call:
    def __init__(self, ep, call_id, pt, remote, direction):
        self.ep, self.a = ep, ep.args
        self.call_id, self.pt, self.remote, self.direction = call_id, pt, remote, direction
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(100):
            self.rtp_port = random.randrange(20000, 30000, 2)
            try:
                self.sock.bind(("0.0.0.0", self.rtp_port))
                break
            except OSError:
                continue
        self.sock.settimeout(0.2)
        self.up = threading.Event()
        self.ended = threading.Event()
        self.out = []                               # frames waiting to be sent
        self.olock = threading.Lock()
        self.rx = array.array("h")
        self.packets = self.dtmf = 0
        self.over, self.last_voice = None, 0.0
        self.started = time.time()
        self.peer = ""                              # From/To display for logs

    def start(self):
        self.up.set()
        for fn in (self._recv, self._send, self._talkback):
            threading.Thread(target=fn, daemon=True).start()

    def queue(self, a, label):
        n = len(a) // FRAME
        log(f"  ▶ playing {label}: {n * FRAME / RATE:.1f} s")
        with self.olock:
            self.out.extend(frames(a))

    def _recv(self):
        thr = 10 ** (self.a.vad_db / 20) * 32768
        while not self.ended.is_set():
            try:
                pkt, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                self._end_over()
                continue
            except OSError:
                break
            if len(pkt) < 12 or pkt[0] >> 6 != 2:
                continue
            cc, ext, pt = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[1] & 0x7F
            off = 12 + 4 * cc
            if ext and len(pkt) >= off + 4:
                off += 4 + 4 * struct.unpack(">H", pkt[off + 2:off + 4])[0]
            payload = pkt[off:]
            self.packets += 1
            if pt == 101:
                if len(payload) >= 4 and payload[1] & 0x80:
                    self.dtmf += 1
                    log(f"  DTMF {'0123456789*#ABCD'[payload[0]] if payload[0] < 16 else payload[0]}")
                continue
            if pt not in DEC:
                continue
            a = decode(pt, payload)
            self.rx.extend(a)
            loud = rms(a) > thr
            if loud:
                if self.over is None:
                    self.over = array.array("h")
                    log(f"  ◀ hearing audio ({db(rms(a)):.0f} dBFS)")
                self.last_voice = time.time()
            if self.over is not None:
                self.over.extend(a)
                if not loud:
                    self._end_over()

    def _end_over(self):
        if self.over is not None and time.time() - self.last_voice > self.a.gap:
            over, self.over = self.over, None
            secs = len(over) / RATE
            log(f"  ◀ over ended: {secs:.1f} s, level {db(rms(over)):.0f} dBFS")
            if self.a.echo is not None:
                trim = int((self.a.gap - 0.05) * RATE)
                body = over[:max(FRAME, len(over) - trim)]
                threading.Timer(self.a.echo, self.queue, (body, "echo")).start()

    def _send(self):
        seq, ts, ssrc = random.randrange(65536), random.randrange(1 << 32), random.randrange(1 << 32)
        silence = array.array("h", [0] * FRAME)
        nxt = time.monotonic()
        first = True
        while not self.ended.is_set():
            with self.olock:
                fr = self.out.pop(0) if self.out else None
            if fr is None and not self.a.silence:
                first = True
                time.sleep(0.02)
                nxt = time.monotonic()
                continue
            hdr = struct.pack(">BBHII", 0x80, (0x80 if first else 0) | self.pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
            first = False
            try:
                self.sock.sendto(hdr + encode(self.pt, fr if fr is not None else silence), self.remote)
            except OSError:
                pass
            seq += 1
            ts += FRAME
            nxt += 0.02
            time.sleep(max(0, nxt - time.monotonic()))

    def _talkback(self):
        src = self.ep.source()
        if src is None:
            return
        a, label = src
        time.sleep(self.a.delay)
        while not self.ended.is_set():
            self.queue(a, label)
            if not self.a.every:
                break
            if self.ended.wait(self.a.every):
                break

    def finish(self, why):
        if self.ended.is_set():
            return
        self.ended.set()
        self.sock.close()
        secs = time.time() - self.started
        path = None
        if self.rx and self.a.out:
            os.makedirs(self.a.out, exist_ok=True)
            path = os.path.join(self.a.out, f"call-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.started))}-{self.call_id[:6]}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                w.writeframes(self.rx.tobytes())
        log(f"■ call ended ({why}) after {secs:.1f} s: {self.packets} RTP packets, {len(self.rx) / RATE:.1f} s of audio"
            + (f", {self.dtmf} DTMF" if self.dtmf else "") + (f" → {path}" if path else ""))


# ---- the SIP endpoint ---------------------------------------------------------------------------------
class Endpoint:
    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", args.port))
        self.calls = {}
        self.nonce = uuid.uuid4().hex
        self.pt = CODECS[args.codec]
        self._wav = load_wav(args.play) if args.play else None

    def source(self):
        if self._wav is not None:
            return self._wav, os.path.basename(self.args.play)
        if self.args.tone:
            return tone(self.args.tone_secs), f"{self.args.tone_secs:g} s 1 kHz tone"
        return None

    def send(self, text, addr):
        self.sock.sendto(text.encode(), addr)

    def reply(self, req, addr, code, reason, to_tag=None, extra=(), body=""):
        h = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.all("via")]
        to = req.get("to")
        if to_tag and ";tag=" not in to:
            to += f";tag={to_tag}"
        h += [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.get('call-id')}", f"CSeq: {req.get('cseq')}",
              "User-Agent: fake_sip", *extra]
        if body:
            h.append("Content-Type: application/sdp")
        self.send("\r\n".join(h) + f"\r\nContent-Length: {len(body)}\r\n\r\n" + body, addr)

    def _authorised(self, req, addr):
        if not self.args.auth:
            return True
        hdr = req.get("authorization")
        if hdr:
            f = dict(re.findall(r'(\w+)="?([^",]*)"?', hdr))
            ha1 = hashlib.md5(f"{f.get('username')}:fake_sip:{self.args.auth}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{req.method}:{f.get('uri')}".encode()).hexdigest()
            if f.get("qop"):
                want = hashlib.md5(f"{ha1}:{self.nonce}:{f.get('nc')}:{f.get('cnonce')}:{f.get('qop')}:{ha2}".encode()).hexdigest()
            else:
                want = hashlib.md5(f"{ha1}:{self.nonce}:{ha2}".encode()).hexdigest()
            if f.get("response") == want:
                return True
            log(f"  auth failed for {f.get('username')!r}")
        self.reply(req, addr, 401, "Unauthorized",
                   extra=[f'WWW-Authenticate: Digest realm="fake_sip", nonce="{self.nonce}", algorithm=MD5, qop="auth"'])
        return False

    def run(self):
        a = self.args
        log(f"fake SIP endpoint on udp/{a.port} ({a.codec})"
            + (f", parrot after {a.echo:g} s" if a.echo is not None else "")
            + (f", plays {a.play}" if a.play else ", plays a tone" if a.tone else "")
            + (f" every {a.every:g} s" if a.every else "") + (", needs auth" if a.auth else "")
            + (f", rejects with {a.reject}" if a.reject else "") + (f"; recordings → {a.out}" if a.out else ""))
        threading.Thread(target=self._watch, daemon=True).start()
        while True:
            data, addr = self.sock.recvfrom(65535)
            if not data.strip():
                continue
            try:
                self.handle(Msg(data), addr)
            except Exception as e:      # noqa: BLE001 - keep serving whatever arrives
                log(f"  ! couldn't handle a message from {addr[0]}: {e}")

    def _watch(self):
        while True:
            time.sleep(1)
            for cid, c in list(self.calls.items()):
                if self.args.max and time.time() - c.started > self.args.max and not c.ended.is_set():
                    self.bye(c)
                    c.finish(f"--max {self.args.max:g} s")
                if c.ended.is_set():
                    self.calls.pop(cid, None)

    # ----- inbound -----
    def handle(self, m, addr):
        if m.is_response:
            return self.on_response(m, addr)
        cid = m.get("call-id")
        if m.method == "INVITE":
            c = self.calls.get(cid)
            if c:                                    # re-INVITE / retransmission: same answer
                return self.reply(m, addr, 200, "OK", c.local_tag, [f"Contact: <sip:fake@{c.local_ip}:{self.args.port}>"],
                                  sdp(c.local_ip, c.rtp_port, c.pt))
            log(f"☎ INVITE {m.uri} from {m.get('from')[:60]} ({addr[0]}:{addr[1]})")
            if not self._authorised(m, addr):
                return
            if self.args.reject:
                log(f"  rejecting with {self.args.reject}")
                return self.reply(m, addr, self.args.reject, "Rejected for testing", tag())
            ip, port, pts = parse_sdp(m.body)
            pt = self.pt if self.pt in pts else next((p for p in pts if p in DEC), None)
            if pt is None or not port:
                log("  no G.711 in the offer: 488")
                return self.reply(m, addr, 488, "Not Acceptable Here", tag())
            c = Call(self, cid, pt, (ip or addr[0], port), "in")
            c.local_tag, c.local_ip = tag(), local_ip_for(addr[0])
            c.inv, c.addr = m, addr
            self.calls[cid] = c
            self.reply(m, addr, 100, "Trying")
            if self.args.ring:
                self.reply(m, addr, 180, "Ringing", c.local_tag)
                time.sleep(self.args.ring)
            self.reply(m, addr, 200, "OK", c.local_tag, [f"Contact: <sip:fake@{c.local_ip}:{self.args.port}>"],
                       sdp(c.local_ip, c.rtp_port, pt))
            log(f"  answered: {'PCMU' if pt == 0 else 'PCMA'}, RTP {c.local_ip}:{c.rtp_port} ⇄ {c.remote[0]}:{c.remote[1]}")
            c.start()
        elif m.method == "ACK":
            pass
        elif m.method == "BYE":
            self.reply(m, addr, 200, "OK")
            c = self.calls.pop(cid, None)
            if c:
                c.finish("they hung up")
        elif m.method == "CANCEL":
            self.reply(m, addr, 200, "OK")
            c = self.calls.pop(cid, None)
            if c:
                c.finish("cancelled")
        elif m.method == "REGISTER":
            if self._authorised(m, addr):
                log(f"  REGISTER from {m.get('from')[:60]}: OK")
                self.reply(m, addr, 200, "OK", tag(), [f"Contact: {m.get('contact')}", "Expires: 300"])
        elif m.method in ("OPTIONS", "INFO", "UPDATE", "NOTIFY"):
            self.reply(m, addr, 200, "OK")
        else:
            self.reply(m, addr, 501, "Not Implemented")

    # ----- outbound (--call) -----
    def call(self, host, port, to_user):
        a = self.args
        target = (socket.gethostbyname(host), port)
        ip = local_ip_for(target[0])
        cid, ftag, branch = uuid.uuid4().hex + "@fake_sip", tag(), "z9hG4bK" + tag()
        c = Call(self, cid, self.pt, None, "out")
        c.local_ip, c.local_tag = ip, ftag
        self.calls[cid] = c
        uri = f"sip:{to_user + '@' if to_user else ''}{host}:{port}"
        c.uri, c.target, c.cseq = uri, target, 1
        body = sdp(ip, c.rtp_port, self.pt)
        c.invite = "\r\n".join([
            f"INVITE {uri} SIP/2.0", f"Via: SIP/2.0/UDP {ip}:{a.port};branch={branch};rport", "Max-Forwards: 70",
            f'From: "fake_sip" <sip:fake@{ip}:{a.port}>;tag={ftag}', f"To: <{uri}>", f"Call-ID: {cid}", "CSeq: 1 INVITE",
            f"Contact: <sip:fake@{ip}:{a.port}>", "User-Agent: fake_sip", "Content-Type: application/sdp",
            f"Content-Length: {len(body)}", "", body])
        log(f"☎ calling {uri}")
        self.send(c.invite, target)
        if a.hangup:
            def later():
                if not c.up.wait(30) or c.ended.wait(a.hangup):
                    return
                self.bye(c)
                c.finish(f"--hangup {a.hangup:g} s")
            threading.Thread(target=later, daemon=True).start()
        return c

    def on_response(self, r, addr):
        c = self.calls.get(r.get("call-id"))
        n, method = r.cseq
        if not c or method != "INVITE":
            return
        if r.status < 200:
            log(f"  {r.first[8:]}")
            return
        if r.status >= 300:
            log(f"  call refused: {r.first[8:]}")
            c.finish(f"refused {r.status}")
            self.calls.pop(c.call_id, None)
            return
        c.remote_tag = re.search(r"tag=([^;>\s]+)", r.get("to")).group(1)
        c.to_hdr = r.get("to")
        ack = "\r\n".join([f"ACK {c.uri} SIP/2.0", f"Via: SIP/2.0/UDP {c.local_ip}:{self.args.port};branch=z9hG4bK{tag()};rport",
                           "Max-Forwards: 70", f'From: "fake_sip" <sip:fake@{c.local_ip}:{self.args.port}>;tag={c.local_tag}',
                           f"To: {c.to_hdr}", f"Call-ID: {c.call_id}", f"CSeq: {n} ACK", "Content-Length: 0", "", ""])
        self.send(ack, c.target)
        if c.up.is_set():
            return
        ip, port, pts = parse_sdp(r.body)
        c.remote = (ip or addr[0], port)
        if pts and pts[0] in DEC:
            c.pt = pts[0]
        log(f"  answered: RTP {c.local_ip}:{c.rtp_port} ⇄ {c.remote[0]}:{c.remote[1]}")
        c.start()

    def bye(self, c):
        if c.direction == "out":
            c.cseq += 1
            text = "\r\n".join([f"BYE {c.uri} SIP/2.0", f"Via: SIP/2.0/UDP {c.local_ip}:{self.args.port};branch=z9hG4bK{tag()};rport",
                                "Max-Forwards: 70", f'From: "fake_sip" <sip:fake@{c.local_ip}:{self.args.port}>;tag={c.local_tag}',
                                f"To: {getattr(c, 'to_hdr', '<' + c.uri + '>')}", f"Call-ID: {c.call_id}", f"CSeq: {c.cseq} BYE",
                                "Content-Length: 0", "", ""])
            self.send(text, c.target)
        else:                                       # we answered: swap From/To, send to their Contact
            m = c.inv
            to = m.get("to") + ("" if ";tag=" in m.get("to") else f";tag={c.local_tag}")
            ruri = re.search(r"<([^>]+)>", m.get("contact") or "") or None
            ruri = ruri.group(1) if ruri else m.get("from").split("<")[-1].split(">")[0]
            text = "\r\n".join([f"BYE {ruri} SIP/2.0", f"Via: SIP/2.0/UDP {c.local_ip}:{self.args.port};branch=z9hG4bK{tag()};rport",
                                 "Max-Forwards: 70", f"From: {to}", f"To: {m.get('from')}", f"Call-ID: {c.call_id}",
                                 "CSeq: 1 BYE", "Content-Length: 0", "", ""])
            self.send(text, c.addr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=5070, help="SIP port to listen on (default 5070)")
    p.add_argument("--out", default="fake_sip_calls", help="folder for recordings ('' to not save)")
    p.add_argument("--codec", choices=CODECS, default="PCMU")
    p.add_argument("--echo", type=float, metavar="SECS", help="parrot each over back after SECS")
    p.add_argument("--play", metavar="WAV", help="play this WAV after answering")
    p.add_argument("--tone", action="store_true", help="play a 1 kHz tone after answering")
    p.add_argument("--tone-secs", type=float, default=3.0)
    p.add_argument("--delay", type=float, default=1.0, help="seconds after answering before --play/--tone")
    p.add_argument("--every", type=float, metavar="SECS", help="repeat --play/--tone every SECS")
    p.add_argument("--silence", action="store_true", help="send silence when idle (continuous RTP, like some PBXs)")
    p.add_argument("--auth", metavar="PASSWORD", help="demand digest auth (any username)")
    p.add_argument("--reject", type=int, metavar="CODE", help="reject every call with this status, e.g. 486")
    p.add_argument("--ring", type=float, default=0.5, help="ring this long before answering")
    p.add_argument("--max", type=float, help="end calls longer than this many seconds")
    p.add_argument("--vad-db", type=float, default=-45, help="level that counts as someone talking (dBFS)")
    p.add_argument("--gap", type=float, default=0.6, help="silence that ends an over (s)")
    p.add_argument("--call", metavar="HOST:PORT", help="place a call to this address instead of only waiting")
    p.add_argument("--to", default="", help="number/user to dial with --call")
    p.add_argument("--hangup", type=float, metavar="SECS", help="with --call: hang up after SECS")
    a = p.parse_args()
    ep = Endpoint(a)
    if a.call:
        host, _, port = a.call.rpartition(":")
        threading.Thread(target=ep.run, daemon=True).start()
        time.sleep(0.2)
        c = ep.call(host, int(port), a.to)
        try:
            while not c.ended.wait(0.5):
                pass
            time.sleep(0.5)
        except KeyboardInterrupt:
            ep.bye(c)
            c.finish("Ctrl-C")
        return
    try:
        ep.run()
    except KeyboardInterrupt:
        for c in list(ep.calls.values()):
            c.finish("Ctrl-C")


if __name__ == "__main__":
    main()
