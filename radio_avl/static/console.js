"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];
  const colour = (u) => { if (u.colour) return u.colour; let h = 0; for (const c of u.id) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };
  const label = (u) => u.alias || "Unit " + u.id.replace(/^[A-Za-z]*0*/, "");
  const now = () => Date.now() / 1000;
  const ago = (t) => { if (!t) return "never"; const s = Math.max(0, now() - t); return s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`; };
  const hhmm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const [cfg, unitList, me, alertList] = await Promise.all(["/api/config", "/api/units", "/api/me", "/api/alerts?open=1&limit=50"]
    .map((u) => fetch(u).then((r) => r.json())));
  let T = await (await fetch("/api/console")).json();
  $("site").textContent = "· " + cfg.title; document.title = "Dispatch · " + cfg.title;
  const units = new Map(unitList.map((u) => [u.id, u]));
  let alerts = alertList;
  let selected = null, mode = "group", onair = { busy: false }, linkUp = true, query = "";
  const canListen = me.perms.includes("listen");

  // ---- units ---------------------------------------------------------------------------------
  const emergencyUnits = () => new Set(alerts.filter((a) => !a.ack_time && a.severity === "critical" && a.unit_id).map((a) => a.unit_id));
  function renderUnits() {
    const g = el("button", "row group" + (mode === "group" && !selected ? " sel" : ""));
    g.append(el("span", "dot"), el("div"), el("span", "ago", "group"));
    g.children[1].append(el("div", "nm", T.group || "All units"), el("div", "id", "Everyone on the channel"));
    g.onclick = () => { selected = null; setMode("group"); };
    $("group-row").replaceChildren(g);
    const em = emergencyUnits(), stale = cfg.stale_after_s || 60, lost = cfg.lost_after_s || 900;
    const rows = [...units.values()].filter((u) => !query || (label(u) + " " + u.id).toLowerCase().includes(query))
      .sort((a, b) => (em.has(b.id) - em.has(a.id)) || label(a).localeCompare(label(b)));
    const ul = $("unit-list"); ul.replaceChildren();
    if (!rows.length) ul.appendChild(el("li", "empty", query ? "No units match." : "No units yet."));
    for (const u of rows) {
      const li = el("li");
      const b = el("button", "row");
      if (u.id === selected) b.classList.add("sel");
      if (onair.busy && onair.unit === u.id) b.classList.add("talking");
      if (em.has(u.id)) b.classList.add("emerg-u");
      if (now() - Math.max(u.last_seen || 0, u.last_call || 0) > lost) b.classList.add("stale");
      const dot = el("span", "dot"); dot.style.background = colour(u);
      const mid = el("div"); mid.append(el("div", "nm", label(u)), el("div", "id", u.id));
      const heard = Math.max(u.last_seen || 0, u.last_call || 0) || null;
      b.append(dot, mid, el("span", "ago", em.has(u.id) ? "🚨" : (u.last_seen ? "" : "📞 ") + ago(heard)));
      b.onclick = () => { selected = u.id; if (!T.private) mode = "group"; renderAll(); };
      b.ondblclick = () => { selected = u.id; if (T.private) setMode("unit"); };
      li.appendChild(b); ul.appendChild(li);
    }
  }
  $("q").oninput = () => { query = $("q").value.trim().toLowerCase(); renderUnits(); };

  // ---- target -----------------------------------------------------------------------------
  function setMode(m) {
    if (m === "unit" && (!T.private || !selected)) return;
    mode = m; renderAll();
  }
  for (const b of $("mode").children) b.onclick = () => setMode(b.dataset.m);
  const target = () => (mode === "unit" && selected ? { kind: "unit", unit: selected } : { kind: "group" });
  function renderTarget() {
    const u = selected && units.get(selected);
    if (mode === "unit" && u) {
      $("t-kicker").textContent = "Private call"; $("t-name").textContent = label(u);
      $("t-sub").textContent = `${u.id} · last heard ${ago(u.last_seen)} ago · only this radio hears you`;
    } else {
      $("t-kicker").textContent = "Group call"; $("t-name").textContent = T.group || "All units";
      $("t-sub").textContent = u ? `${label(u)} selected · everyone on the channel hears you` : "Everyone on the channel hears you";
    }
    for (const b of $("mode").children) { const on = b.dataset.m === mode; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); }
    const priv = $("mode").children[1];
    priv.disabled = !T.private || !selected;
    $("why").textContent = !T.private ? (T.path === "nxr_sip" ? "Private calls aren't available through the NXR (it's set to one group)." : "Private calls need the radio's dial command: Settings → Dispatch console.")
      : !selected ? "Pick a unit for a private call." : "";
  }

  // ---- talk state, PTT ---------------------------------------------------------------------------
  let ptt = "idle";           // idle | asking | tx
  let txStart = 0, txTimer = null, wantUp = false;
  function blockedReason() {
    if (!T.enabled) return "The dispatch console is turned off (Settings → Dispatch console).";
    if (!me.perms.includes("talk")) return "Your account can listen but not talk.";
    if (!window.isSecureContext) return "Talking needs a secure page (see below).";
    if (!T.ready) return T.detail;
    if (T.floor && T.floor.user_id !== me.id) return `${T.floor.name} is talking…`;
    if (T.channel_busy && ptt === "idle") return "Channel busy: wait for it to clear";
    if (!ws || ws.readyState !== 1) return "Connecting…";
    return "";
  }
  function renderPtt() {
    const b = $("ptt"), why = blockedReason();
    b.classList.toggle("tx", ptt === "tx"); b.classList.toggle("wait", ptt === "asking");
    b.disabled = ptt === "idle" && !!why;
    $("ptt-lbl").textContent = ptt === "tx" ? "ON AIR" : ptt === "asking" ? "Keying…" : why && T.floor && T.floor.user_id !== me.id ? "Busy" : "Hold to talk";
    const st = $("status");
    if (ptt === "tx") { st.textContent = `Talking to ${mode === "unit" && selected ? label(units.get(selected) || { id: selected }) : T.group}`; st.className = "status ok"; }
    else if (why) { st.textContent = why; st.className = "status bad"; }
    else { st.textContent = `Ready · ${T.detail}`; st.className = "status"; }
    $("limit-arc").parentNode.classList.toggle("on", ptt === "tx");
  }
  function pttDown() {
    if (ptt !== "idle" || $("ptt").disabled) return;
    wantUp = false; ptt = "asking"; renderPtt();
    ensureAudio(true).then(() => {
      if (wantUp) { ptt = "idle"; renderPtt(); return; }
      ws.send(JSON.stringify({ type: "ptt", on: true, target: target() }));
    }).catch((e) => { ptt = "idle"; renderPtt(); $("status").textContent = e.message; $("status").className = "status bad"; });
  }
  function pttUp() {
    wantUp = true;
    if (ptt === "tx") { ws.send(JSON.stringify({ type: "ptt", on: false })); endTx(); }
  }
  function startTx() {
    ptt = "tx"; txStart = now(); renderPtt();
    clearInterval(txTimer);
    txTimer = setInterval(() => {
      const e = now() - txStart, left = Math.max(0, T.max_s - e);
      $("ptt-timer").textContent = `${Math.floor(e / 60)}:${String(Math.floor(e % 60)).padStart(2, "0")}` + (left < 10 ? ` · ${Math.ceil(left)}s left` : "");
      $("limit-arc").style.strokeDashoffset = String(301.6 * (1 - Math.min(1, e / T.max_s)));
    }, 200);
    if (wantUp) pttUp();
  }
  function endTx() { ptt = "idle"; clearInterval(txTimer); $("ptt-timer").textContent = ""; $("limit-arc").style.strokeDashoffset = "301.6"; renderPtt(); }

  const pb = $("ptt");
  pb.addEventListener("pointerdown", (e) => { e.preventDefault(); pb.setPointerCapture(e.pointerId); pttDown(); });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) pb.addEventListener(ev, pttUp);
  pb.addEventListener("contextmenu", (e) => e.preventDefault());
  pb.addEventListener("keydown", (e) => { if ((e.key === "Enter") && !e.repeat) { e.preventDefault(); pttDown(); } });
  pb.addEventListener("keyup", (e) => { if (e.key === "Enter") pttUp(); });
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    e.preventDefault(); pttDown();
  });
  document.addEventListener("keyup", (e) => { if (e.code === "Space" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); pttUp(); } });
  window.addEventListener("blur", pttUp);                 // switching windows mid-over releases the button

  // ---- audio -----------------------------------------------------------------------------------
  let ctx = null, player = null, mic = null, micReady = null;
  async function ensureAudio(withMic) {
    if (!ctx) {
      ctx = new AudioContext();
      await ctx.audioWorklet.addModule("/static/console-audio.js");
      player = new AudioWorkletNode(ctx, "kt-player", { outputChannelCount: [2] });
      player.connect(ctx.destination);
      player.port.onmessage = (e) => { $("rx-meter").style.width = Math.min(100, e.data.peak * 140) + "%"; };
      player.port.postMessage({ vol: +$("vol").value });
    }
    if (ctx.state === "suspended") await ctx.resume();
    if (withMic && !mic) {
      micReady = micReady || (async () => {
        if (!navigator.mediaDevices) throw new Error("This browser can't use the microphone here");
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } })
          .catch(() => { throw new Error("Microphone blocked: allow it in the browser's address bar, then try again"); });
        const node = new AudioWorkletNode(ctx, "kt-mic");
        const sink = ctx.createGain(); sink.gain.value = 0;
        ctx.createMediaStreamSource(stream).connect(node); node.connect(sink); sink.connect(ctx.destination);
        node.port.onmessage = (e) => {
          $("mic-meter").style.width = (ptt === "tx" ? Math.min(100, e.data.peak * 140) : 0) + "%";
          if (ptt === "tx" && ws && ws.readyState === 1) ws.send(e.data.pcm);
        };
        mic = node;
      })();
      await micReady;
    }
  }
  $("vol").oninput = () => player && player.port.postMessage({ vol: +$("vol").value });
  let listening = false;
  $("listen").hidden = !canListen;
  $("listen").onclick = async () => {
    listening = !listening;
    if (listening) await ensureAudio(false);
    else if (player) player.port.postMessage({ flush: true });
    $("listen").setAttribute("aria-pressed", listening); $("listen").textContent = listening ? "🔊 Listening" : "🔇 Listen";
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "listen", on: listening }));
    if (!listening) $("rx-meter").style.width = "0";
  };

  // ---- WebSocket ------------------------------------------------------------------------------------
  let ws = null, backoff = 1000;
  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/console/ws");
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { backoff = 1000; if (listening) ws.send(JSON.stringify({ type: "listen", on: true })); renderPtt(); };
    ws.onmessage = (m) => {
      if (typeof m.data !== "string") { if (player && listening) player.port.postMessage({ pcm: m.data }, [m.data]); return; }
      const d = JSON.parse(m.data);
      if (d.type === "ptt") {
        if (d.on) startTx();
        else { if (ptt !== "idle") endTx(); if (d.error) { $("status").textContent = d.error; $("status").className = "status bad"; } }
      }
    };
    ws.onclose = () => { if (ptt !== "idle") endTx(); renderPtt(); setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000); };
  }
  connect();

  if (!window.isSecureContext) {
    const n = $("secure-note"); n.hidden = false;
    n.append("Browsers only allow the microphone on secure (https) pages. Use the Cloudflare address");
    if (T.https_port) { const a = el("a", null, `https://${location.hostname}:${T.https_port}/console`); a.href = a.textContent; n.append(", or ", a); }
    else n.append(", or turn on HTTPS under Settings → Web access");
    n.append(". You can still listen here.");
  }

  // ---- recent calls ------------------------------------------------------------------------------------
  const callsEl = $("calls");
  function callRow(r, fresh) {
    const li = el("li", "call " + r.kind + (fresh ? " new" : "")); li.dataset.id = r.id;
    const u = r.unit_id && units.get(r.unit_id);
    let who = r.kind === "dispatch" ? (r.display || "Dispatch") : (u ? label(u) : r.display || r.unit_id || "Unknown caller");
    let btn;
    if (r.has_audio && canListen) {
      btn = el("button", "play", "▶"); btn.title = "Play"; btn.onclick = () => KTPlayer.play(r.id, `${who} · ${hhmm(r.start)}`);
    } else btn = el("span", "noaud", "·");
    const mid = el("div", "who"); mid.append(el("b", null, who), el("span", null, `${r.kind === "dispatch" ? "Dispatch" : "Voice"} · ${Math.round(r.duration)} s${r.unit_id && r.kind === "voice" ? " · " + r.unit_id : ""}`));
    li.append(btn, mid, el("span", "tm", hhmm(r.start)));
    return li;
  }
  async function loadCalls() {
    const rows = (await (await fetch(`/api/channel/log?from=${Math.floor(now() - 86400)}&to=${Math.floor(now() + 60)}&limit=200`)).json())
      .filter((r) => r.kind === "voice" || r.kind === "dispatch").slice(0, 60);
    callsEl.replaceChildren(...(rows.length ? rows.map((r) => callRow(r)) : [el("li", "empty", "No calls in the last 24 hours.")]));
  }
  KTPlayer.onChange && KTPlayer.onChange((id, st) => {
    for (const b of callsEl.querySelectorAll(".play")) { const on = b.closest(".call").dataset.id == id && st === "playing"; b.classList.toggle("playing", on); b.textContent = on ? "❚❚" : "▶"; }
  });

  // ---- emergencies ---------------------------------------------------------------------------------------
  function renderEmerg() {
    const open = alerts.filter((a) => !a.ack_time && a.severity === "critical");
    $("emerg").hidden = !open.length;
    if (!open.length) return;
    const a = open[0], u = a.unit_id && units.get(a.unit_id);
    $("emerg-title").textContent = `EMERGENCY · ${u ? label(u) : a.unit_id || "unknown unit"}`;
    $("emerg-detail").textContent = `${a.detail} · ${hhmm(a.t)}` + (open.length > 1 ? ` · and ${open.length - 1} more` : "");
    $("emerg-talk").hidden = !a.unit_id;
    $("emerg-talk").onclick = () => { selected = a.unit_id; mode = T.private ? "unit" : "group"; renderAll(); $("ptt").focus(); };
    $("emerg-ack").hidden = !T.can_ack;
    $("emerg-ack").onclick = () => fetch(`/api/alerts/${a.id}/ack`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note: "from the dispatch console" }) });
  }

  // ---- live events -------------------------------------------------------------------------------------
  function renderOnair() {
    const p = $("p-chan"), inc = $("incoming");
    const me_tx = T.floor && T.floor.user_id === me.id;
    if (onair.busy && !me_tx) {
      const u = onair.unit && units.get(onair.unit);
      const who = u ? label(u) : onair.name || onair.unit || "";
      p.className = "pill live"; p.lastChild.textContent = who ? `On air · ${who}` : (onair.kind === "data" ? "Data" : "On air");
      inc.hidden = onair.kind === "data"; $("incoming-text").textContent = `${who || "Someone"} is talking`;
    } else if (T.floor && !me_tx) {
      p.className = "pill live"; p.lastChild.textContent = `Dispatch · ${T.floor.name}`; inc.hidden = true;
    } else { p.className = "pill ok"; p.lastChild.textContent = "Channel clear"; inc.hidden = true; }
  }
  function renderPath() {
    const p = $("p-path");
    p.className = "pill " + (!T.enabled ? "" : T.ready ? "ok" : "bad");
    p.lastChild.textContent = !T.enabled ? "Talk off" : { k5tra: "K5TRA", nxr_sip: "NXR SIP", test: "Test mode" }[T.path] || T.path;
    p.title = T.detail;
  }
  function renderLink() { const p = $("p-link"); p.className = "pill " + (linkUp ? "ok" : "bad"); p.lastChild.textContent = linkUp ? "Radio link" : "No radio link"; }
  function renderAll() { renderUnits(); renderTarget(); renderPtt(); renderOnair(); renderPath(); renderLink(); renderEmerg(); }

  const es = new EventSource("/api/stream");
  es.addEventListener("open", async () => { const d = await (await fetch("/api/console")).json(); if (!T.t || d.t >= T.t) { T = Object.assign(T, d); renderAll(); } });
  es.addEventListener("talk", (m) => { const d = JSON.parse(m.data); if (T.t && d.t && d.t < T.t) return; T = Object.assign(T, d); if (!T.private && mode === "unit") mode = "group"; renderAll(); });
  es.addEventListener("onair", (m) => { onair = JSON.parse(m.data); renderOnair(); renderUnits(); });
  es.addEventListener("link", (m) => { linkUp = JSON.parse(m.data).up; renderLink(); });
  es.addEventListener("unit", (m) => { const u = JSON.parse(m.data); units.set(u.id, u); renderUnits(); });
  es.addEventListener("position", (m) => { const d = JSON.parse(m.data); units.set(d.unit.id, d.unit); });
  es.addEventListener("alert", (m) => { alerts.unshift(JSON.parse(m.data)); renderEmerg(); renderUnits(); });
  es.addEventListener("alert_ack", (m) => { const d = JSON.parse(m.data); for (const a of alerts) if (d.ids.includes(a.id)) a.ack_time = now(); renderEmerg(); renderUnits(); });
  es.addEventListener("tx", (m) => {
    const r = JSON.parse(m.data);
    if (r.kind !== "voice" && r.kind !== "dispatch") return;
    callsEl.querySelector(".empty")?.remove();
    callsEl.prepend(callRow(Object.assign({ has_audio: false }, r), true));
  });
  es.addEventListener("recording", (m) => {
    const d = JSON.parse(m.data), li = callsEl.querySelector(`[data-id="${d.tx_id}"]`);
    if (li) loadCalls();
  });
  es.addEventListener("signed_out", () => { location.href = "/login?next=/console"; });

  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" }); }, 1000);
  setInterval(renderUnits, 15000);
  renderAll();
  loadCalls();
})();
