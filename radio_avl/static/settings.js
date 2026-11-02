"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
                   "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];
  const autoColour = (id) => { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };
  let S = null, SYS = null;

  const api = async (path, opts = {}) => {
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `Error ${r.status}`);
    return j;
  };
  const post = (path, body) => api(path, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  let toastTimer;
  function toast(msg, err) {
    const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3500);
  }
  const bytes = (n) => n == null ? "–" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB"
    : n < 1073741824 ? (n / 1048576).toFixed(1) + " MB" : (n / 1073741824).toFixed(1) + " GB";
  const dur = (s) => s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min"
    : s < 86400 ? Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min"
    : Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h";
  const when = (t) => t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "–";
  const ago = (t, now) => t ? dur(Math.max(0, Math.round(now - t))) + " ago" : "never";

  // ---- fill forms ------------------------------------------------------
  function fillSerial() {
    const sel = $("serial-port"); sel.replaceChildren();
    for (const p of SYS.serial_ports) {
      const o = document.createElement("option");
      o.value = p.path;
      const name = p.path.replace("/dev/serial/by-id/", "");
      o.textContent = name + (p.description ? ` (${p.description})` : "") +
        (p.path !== p.device ? ` → ${p.device}` : "");
      sel.appendChild(o);
    }
    const c = document.createElement("option"); c.value = "__custom"; c.textContent = "Other path…";
    sel.appendChild(c);
    sel.value = [...sel.options].some((o) => o.value === S.serial.port) ? S.serial.port : "__custom";
    $("serial-custom").value = S.serial.port;
    $("serial-custom-wrap").hidden = sel.value !== "__custom";
    const b = $("serial-baud"); b.replaceChildren();
    for (const v of SYS.bauds) { const o = document.createElement("option"); o.value = o.textContent = v; b.appendChild(o); }
    b.value = S.serial.baud;
  }
  $("serial-port").onchange = () => { $("serial-custom-wrap").hidden = $("serial-port").value !== "__custom"; };

  function fillWeb() {
    $("web-title").value = S.web.title; $("s-title").textContent = S.web.title;
    document.title = S.web.title + " settings";
    $("web-port").value = S.web.port;
    $("web-https").value = S.web.https_port || "";
    $("web-host").value = S.web.host;
    if (!$("web-host").value) {          // a host not in the list
      const o = document.createElement("option"); o.value = o.textContent = S.web.host;
      $("web-host").appendChild(o); $("web-host").value = S.web.host;
    }
    $("port-check").textContent = ""; $("port-check").className = "hint";
  }

  function fillMap() {
    const t = S.tiles;
    $("tile-source").value = t.source;
    $("tile-linz").value = ""; $("tile-linz").placeholder = t.linz_key_set ? "key saved; enter a new one to replace" : "from basemaps.linz.govt.nz";
    $("tile-url").value = t.custom_url || ""; $("tile-attr").value = t.custom_attribution || "";
    $("tile-maxzoom").value = t.max_zoom;
    $("sat-source").value = t.satellite;
    $("sat-linz").value = ""; $("sat-linz").placeholder = t.linz_key_set ? "key saved; enter a new one to replace" : "from basemaps.linz.govt.nz";
    $("sat-url").value = t.sat_custom_url || ""; $("sat-attr").value = t.sat_custom_attribution || "";
    showTileFields();
    mini.setView(S.map.center, S.map.zoom);
    pendingView = null; $("view-text").textContent = viewText(S.map.center, S.map.zoom);
  }
  function showTileFields() {
    const sat = $("sat-source").value, keySet = S && S.tiles.linz_key_set;
    $("sat-linz-wrap").hidden = sat !== "linz_aerial" || $("tile-source").value === "linz_aerial";
    $("sat-custom-wrap").hidden = sat !== "custom";
    $("sat-help-esri").hidden = sat !== "esri" && !(sat === "linz_aerial" && !keySet);
    $("linz-wrap").hidden = $("tile-source").value !== "linz_aerial";
    $("custom-wrap").hidden = $("tile-source").value !== "custom";
  }
  $("tile-source").onchange = showTileFields;
  $("sat-source").onchange = showTileFields;

  function fillDisplay() {
    $("disp-stale").value = S.display.stale_after_s;
    $("disp-lost").value = Math.round(S.display.lost_after_s / 60);
    $("disp-speed").value = S.display.speed_units;
    $("disp-trail").value = S.display.trail_min;
  }
  // ---- voice recording ---------------------------------------------------------------
  let VOICE = null;
  async function loadVoice() {
    try { VOICE = await api("/api/voice"); } catch (e) { return; }
    const v = VOICE.status;
    const st = { off: "Off", listening: "Listening", error: "Problem: " + v.error }[v.state] || v.state;
    $("vr-st").textContent = st + (v.paused_for_disk ? " (paused: disk nearly full)" : "") + (v.state === "listening" && S.recording.format === "opus" && !v.encoder ? " · no Opus encoder, saving WAV" : "");
    $("vr-st").className = v.state === "error" ? "bad-text" : v.state === "listening" ? "ok-text" : "";
    $("vr-usage").textContent = `${VOICE.usage.files} calls, ${bytes(VOICE.usage.bytes)}`;
    meter(v.levels);
  }
  function fillVoice() {
    const R = S.recording;
    $("vr-on").checked = R.enabled; $("vr-fmt").value = R.format; $("vr-keep").value = R.keep_days;
    $("vr-rate").value = R.rate; $("vr-br").value = R.bitrate_kbps; $("vr-pre").value = R.preroll_ms;
    $("vr-hang").value = R.hang_ms; $("vr-free").value = R.min_free_mb; $("vr-voiceonly").checked = R.voice_only;
    $("vr-min").value = R.min_call_s;
    const sel = $("vr-dev"); sel.replaceChildren();
    const opt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.appendChild(o); };
    opt("", "– choose –");
    for (const d of (VOICE && VOICE.devices) || []) opt(d.device, `${d.name} (${d.device})`);
    opt("test", "Test signal (no hardware)");
    opt("__custom", "Other device…");
    sel.value = [...sel.options].some((o) => o.value === R.device) ? R.device : (R.device ? "__custom" : "");
    $("vr-dev-custom").value = R.device; $("vr-dev-custom").hidden = sel.value !== "__custom";
  }
  $("vr-dev").onchange = () => { $("vr-dev-custom").hidden = $("vr-dev").value !== "__custom"; };
  function meter(lv) {
    if (!lv || !lv.fresh) { $("vr-meter").style.width = "0"; $("vr-peak").style.left = "0"; $("vr-level").textContent = "No audio"; return; }
    const pos = (db) => Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    $("vr-meter").style.width = pos(lv.rms_db) + "%";
    $("vr-peak").style.left = pos(lv.peak_db) + "%";
    $("vr-level").textContent = `peak ${lv.peak_db} dB · average ${lv.rms_db} dB` + (lv.peak_db > -1 ? " (clipping: turn it down)" : lv.peak_db < -40 ? " (very quiet)" : "");
  }
  let voiceTimer = null;
  new IntersectionObserver((es) => {
    clearInterval(voiceTimer);
    if (es[0].isIntersecting) voiceTimer = setInterval(loadVoice, 500);
  }).observe($("voice-card"));

  // ---- dispatch console ------------------------------------------------------------
  let CON = null;
  async function loadConsole() {
    try { CON = await api("/api/console/devices"); } catch (e) { return; }
    const st = CON.state;
    $("cn-st").textContent = !st.enabled ? "Off" : (st.ready ? "Ready · " : "Problem: ") + st.detail;
    $("cn-st").className = !st.enabled ? "" : st.ready ? "ok-text" : "bad-text";
  }
  function fillConsole() {
    const C = S.console;
    $("cn-on").checked = C.enabled; $("cn-path").value = C.path; $("cn-group").value = C.group_name;
    $("cn-gpio").value = C.ptt_gpio; $("cn-inv").checked = C.ptt_invert; $("cn-pcmd").value = C.private_command;
    $("cn-pfmt").value = C.private_format; $("cn-pdelay").value = C.private_delay_ms;
    $("cn-host").value = C.nxr_host; $("cn-port").value = C.nxr_port; $("cn-user").value = C.nxr_user;
    $("cn-lport").value = C.sip_local_port; $("cn-auser").value = C.sip_auth_user; $("cn-apass").value = "";
    $("cn-apass").placeholder = C.sip_password_set ? "unchanged" : "none";
    $("cn-codec").value = C.sip_codec; $("cn-hold").value = C.sip_hold_s; $("cn-reg").checked = C.sip_register; $("cn-in").checked = C.sip_inbound;
    $("cn-max").value = C.max_talk_s; $("cn-over").checked = C.talk_over_busy; $("cn-lead").value = C.ptt_lead_ms; $("cn-tail").value = C.ptt_tail_ms;
    const opts = (sel, list, cur, blank) => {
      const e = $(sel); e.replaceChildren();
      const add = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; e.appendChild(o); };
      add("", blank);
      for (const [v, t] of list) add(v, t);
      if (cur && ![...e.options].some((o) => o.value === cur)) add(cur, cur);
      e.value = cur;
    };
    opts("cn-hid", ((CON && CON.hid) || []).map((h) => [h, h]), C.hid_device, "Find it automatically");
    opts("cn-play", [...((CON && CON.playback) || []).map((d) => [d.device, `${d.name} (${d.device})`]), ["test", "Test (no sound card)"]],
         C.playback_device, "Same sound card as voice recording");
    showConsoleFields();
  }
  function showConsoleFields() { $("cn-k5").hidden = $("cn-path").value !== "k5tra"; $("cn-sip").hidden = $("cn-path").value !== "nxr_sip"; }
  $("cn-path").onchange = showConsoleFields;
  let conTimer = null;
  new IntersectionObserver((es) => { clearInterval(conTimer); if (es[0].isIntersecting) { loadConsole(); conTimer = setInterval(loadConsole, 3000); } })
    .observe($("console-card"));

  // ---- front-panel LCD -----------------------------------------------------------
  let LCDI = null;
  const lcdChars = (t) => t.replace(/\x00/g, "Ψ").replace(/\x01/g, "♦");
  async function loadLcd() {
    try { LCDI = await api("/api/lcd"); } catch (e) { return; }
    const st = LCDI.status, C = S.lcd;
    $("lcd-st").textContent = { off: "Off (preview only)", running: `Running on i2c-${C.bus} at 0x${(st.address ?? C.address).toString(16).toUpperCase()}` + (st.address != null && st.address !== C.address ? " (found it there: save that address to make it stick)" : ""),
      error: "Problem: " + st.error }[st.state] || st.state;
    $("lcd-st").className = st.state === "error" ? "bad-text" : st.state === "running" ? "ok-text" : "";
    const pv = $("lcd-preview"); pv.replaceChildren();
    for (let r = 0; r < C.rows; r++) { const d = document.createElement("div"); d.textContent = lcdChars((st.lines[r] || "").padEnd(C.cols).slice(0, C.cols)); pv.appendChild(d); }
    pv.classList.toggle("dark", st.state === "error");
  }
  function fillLcd() {
    const C = S.lcd;
    $("lcd-on").checked = C.enabled; $("lcd-addr").value = "0x" + C.address.toString(16).toUpperCase();
    $("lcd-size").value = `${C.cols}x${C.rows}`; $("lcd-bl").value = C.backlight; $("lcd-rot").value = C.rotate_s;
    $("lcd-flash").checked = C.flash_emergency;
    const sel = $("lcd-bus"); sel.replaceChildren();
    const buses = new Set([...((LCDI && LCDI.buses) || []), C.bus]);
    for (const b of [...buses].sort((a, b) => a - b)) { const o = document.createElement("option"); o.value = b; o.textContent = `i2c-${b}` + (b === 1 ? " (header pins 3 and 5)" : ""); sel.appendChild(o); }
    sel.value = C.bus;
  }
  $("lcd-scan").onclick = async () => {
    $("lcd-scan-res").textContent = "Looking…";
    try {
      const r = (await api(`/api/lcd?scan=1&bus=${$("lcd-bus").value}`)).scan;
      if (r.error) { $("lcd-scan-res").textContent = r.error; return; }
      if (!r.found.length) { $("lcd-scan-res").textContent = "Nothing found. Check the wiring and that I2C is on."; return; }
      $("lcd-addr").value = r.found[0];
      $("lcd-scan-res").textContent = r.found.length > 1 ? `Found ${r.found.join(", ")}. Using ${r.found[0]}.` : `Found it at ${r.found[0]}. Tick the box above and save.`;
    } catch (e) { $("lcd-scan-res").textContent = e.message; }
  };
  let lcdTimer = null;
  new IntersectionObserver((es) => {
    clearInterval(lcdTimer);
    if (es[0].isIntersecting) { loadLcd(); lcdTimer = setInterval(loadLcd, 1000); }
  }).observe($("lcd-card"));

  function fillPolling() {
    const P = S.polling;
    $("pl-mode").value = P.mode; $("pl-cmd").value = P.command; $("pl-fmt").value = P.format; $("pl-units").value = P.units;
    $("pl-moving").value = P.moving_s; $("pl-parked").value = P.parked_s; $("pl-emerg").value = P.emergency_s;
    $("pl-timeout").value = P.timeout_s; $("pl-guard").value = P.guard_s; $("pl-max").value = P.max_per_min;
    plPreview();
  }
  function plPreview() {
    $("pl-live-warn").hidden = $("pl-mode").value !== "live";
    const cmd = $("pl-cmd").value;
    if (!cmd) { $("pl-preview").textContent = ""; return; }
    try {
      let s = cmd.replace(/\{id\}/g, "U00112").replace(/\{num5\}/g, "00112").replace(/\{num\}/g, "112").replace(/\{seq\}/g, "01");
      let bytes;
      if ($("pl-fmt").value === "hex") bytes = (s.replace(/[^0-9a-f]/gi, "").match(/../g) || []).map((h) => parseInt(h, 16));
      else bytes = [...s.replace(/\\x([0-9a-f]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16))).replace(/\\r/g, "\r").replace(/\\n/g, "\n")].map((c) => c.charCodeAt(0));
      $("pl-preview").textContent = "Unit 112 would get: " + bytes.map((b) => b.toString(16).padStart(2, "0")).join(" ");
    } catch (e) { $("pl-preview").textContent = ""; }
  }
  $("pl-cmd").oninput = $("pl-fmt").onchange = $("pl-mode").onchange = plPreview;

  function fillAlerts() {
    $("al-overdue").value = S.alerts.overdue_min;
    $("al-speed").value = S.alerts.speed_limit_kmh;
    $("al-keywords").value = S.alerts.emergency_keywords;
    $("al-emst").value = S.alerts.emergency_status; $("al-nost").value = S.alerts.normal_status;
    $("al-status").checked = !!S.alerts.status_watch;
    $("rp-stopmin").value = S.reports.stop_min;
    $("rp-radius").value = S.reports.stop_radius_m;
  }
  function fillData() {
    $("keep-days").value = S.keep_days;
    $("drop-bad").checked = !!S.drop_bad_checksum;
  }

  // ---- status ----------------------------------------------------------
  function fillStatus() {
    const s = SYS;
    $("st-link").innerHTML = "";
    const span = document.createElement("span");
    span.className = s.link_up ? "ok-text" : "bad-text";
    span.textContent = s.mode === "replay" ? "Replay: " + s.link_detail : (s.link_up ? "Connected" : "Not connected") +
      (s.link_detail ? ` (${s.link_detail})` : "");
    $("st-link").appendChild(span);
    $("st-last").textContent = s.last_frame ? ago(s.last_frame, s.now) : "nothing yet";
    $("st-frames").textContent = `${s.frames} / ${s.positions} since start`;
    $("st-gaps").textContent = s.seq_gaps ? `${s.seq_gaps} (check cable / baud)` : "none";
    $("ports-used").textContent = s.listening_ports.map((p) => p === S.web.port ? p + " (this)" : p).join(", ") || "none found";
    $("tile-usage").textContent = `${s.tiles.files} tiles, ${bytes(s.tiles.bytes)}`;
    $("st-pos").textContent = s.store.positions.toLocaleString() + ` from ${s.store.units} unit${s.store.units === 1 ? "" : "s"}`;
    $("st-oldest").textContent = when(s.store.oldest);
    $("st-db").textContent = bytes(s.db_bytes);
    $("st-disk").textContent = s.disk ? `${bytes(s.disk.free)} of ${bytes(s.disk.total)}` : "–";
    if (s.poller) {
      const P = s.poller;
      $("pl-st-sent").textContent = `${P.sent} / ${P.answered}`;
      $("pl-st-missed").textContent = P.missed + (Object.keys(P.missed_by_unit).length ? " (" + Object.entries(P.missed_by_unit).map(([u, n]) => `${u}: ${n}`).join(", ") + ")" : "");
      $("pl-st-dry").textContent = P.dry_run;
      $("pl-st-last").textContent = P.last ? `${P.last.unit} · ${when(P.last.t)}${P.last.mode === "dry_run" ? " (test)" : ""}` : "–";
    }
    $("sys-ver").textContent = s.version;
    $("sys-host").textContent = s.hostname;
    $("sys-up").textContent = dur(s.uptime_s);
    $("sys-clock").textContent = Math.abs(s.clock_offset_s) > 0
      ? `Pi clock was out by ${s.clock_offset_s} s; corrected from GPS` : "OK";
    $("sys-cfg").textContent = s.config_path + (s.config_writable ? "" : " (read-only!)");
    $("sys-data").textContent = s.data_dir;

    const w = [];
    if (!s.config_writable) w.push(`The service can't write ${s.config_path}, so changes won't be kept. Re-run install.sh.`);
    $("warn").hidden = !w.length; $("warn").textContent = w.join(" ");
  }

  // ---- units table ---------------------------------------------------------
  async function fillUnits() {
    const units = await api("/api/units");
    const tb = $("unit-rows"); tb.replaceChildren();
    if (!units.length) {
      const tr = document.createElement("tr"); const td = document.createElement("td");
      td.colSpan = 5; td.className = "empty"; td.textContent = "No units have reported yet.";
      tr.appendChild(td); tb.appendChild(tr); return;
    }
    for (const u of units) {
      const tr = document.createElement("tr");
      const id = document.createElement("td"); id.className = "id"; id.textContent = u.id;
      const nm = document.createElement("td"); const ni = document.createElement("input");
      ni.value = u.alias || ""; ni.placeholder = "Unit " + u.id.replace(/^[A-Za-z]*0*/, ""); ni.maxLength = 40; nm.appendChild(ni);
      const cl = document.createElement("td"); const ci = document.createElement("input");
      ci.type = "color"; ci.value = u.colour || autoColour(u.id); cl.appendChild(ci);
      const ls = document.createElement("td"); ls.textContent = when(u.last_seen);
      const ac = document.createElement("td"); ac.className = "act";
      const sv = document.createElement("button"); sv.textContent = "Save";
      sv.onclick = async () => {
        try { await post("/api/units/" + encodeURIComponent(u.id), { alias: ni.value.trim(),
          colour: (u.colour || ci.value !== autoColour(u.id)) ? ci.value : "" }); toast(`Saved ${u.id}`); }
        catch (e) { toast(e.message, true); }
      };
      const del = document.createElement("button"); del.textContent = "Delete"; del.className = "danger";
      del.onclick = async () => {
        if (!confirm(`Delete ${u.alias || u.id} and all of its history? This can't be undone.`)) return;
        try { await api("/api/units/" + encodeURIComponent(u.id), { method: "DELETE" }); toast(`Deleted ${u.id}`); fillUnits(); }
        catch (e) { toast(e.message, true); }
      };
      ac.append(sv, " ", del);
      tr.append(id, nm, cl, ls, ac); tb.appendChild(tr);
    }
  }

  // ---- mini map for start view ------------------------------------------------
  const mini = L.map("mini-map", { attributionControl: false });
  const cfgPublic = await api("/api/config");
  KTBasemap(mini, cfgPublic, { noSave: true });
  let pendingView = null;
  const viewText = (c, z) => `${(+c[0]).toFixed(4)}, ${(+c[1]).toFixed(4)} at zoom ${z}`;
  $("use-view").onclick = () => {
    const c = mini.getCenter(); pendingView = { center: [c.lat, c.lng], zoom: mini.getZoom() };
    $("view-text").textContent = "New: " + viewText(pendingView.center, pendingView.zoom) + " (press Save)";
  };

  // ---- port availability check ---------------------------------------------------
  let portTimer;
  $("web-port").oninput = () => {
    clearTimeout(portTimer);
    const port = +$("web-port").value, el = $("port-check");
    if (!port || port === S.web.port) { el.textContent = ""; el.className = "hint"; return; }
    portTimer = setTimeout(async () => {
      try {
        const r = await post("/api/check-port", { port, host: $("web-host").value });
        el.textContent = r.free ? `Port ${port} is free` : `Port ${port} is in use by another program`;
        el.className = "hint " + (r.free ? "ok" : "bad");
      } catch (e) { el.textContent = e.message; el.className = "hint bad"; }
    }, 300);
  };

  // ---- saving ------------------------------------------------------------------
  const builders = {
    serial: () => ({ serial: {
      port: $("serial-port").value === "__custom" ? $("serial-custom").value.trim() : $("serial-port").value,
      baud: +$("serial-baud").value } }),
    web: () => {
      return { web: { title: $("web-title").value, port: +$("web-port").value, host: $("web-host").value,
                      https_port: +$("web-https").value || 0 } };
    },
    map: () => {
      const t = { source: $("tile-source").value, custom_url: $("tile-url").value,
                  custom_attribution: $("tile-attr").value, max_zoom: +$("tile-maxzoom").value };
      t.satellite = $("sat-source").value;
      t.sat_custom_url = $("sat-url").value; t.sat_custom_attribution = $("sat-attr").value;
      const key = $("tile-linz").value.trim() || $("sat-linz").value.trim();
      if (key) t.linz_key = key;
      const p = { tiles: t };
      if (pendingView) p.map = pendingView;
      return p;
    },
    display: () => ({ display: { stale_after_s: +$("disp-stale").value,
      lost_after_s: Math.round(+$("disp-lost").value * 60), speed_units: $("disp-speed").value,
      trail_min: +$("disp-trail").value } }),
    recording: () => ({ recording: { enabled: $("vr-on").checked,
      device: $("vr-dev").value === "__custom" ? $("vr-dev-custom").value.trim() : $("vr-dev").value,
      format: $("vr-fmt").value, keep_days: +$("vr-keep").value, rate: +$("vr-rate").value, bitrate_kbps: +$("vr-br").value,
      preroll_ms: +$("vr-pre").value, hang_ms: +$("vr-hang").value, min_free_mb: +$("vr-free").value,
      voice_only: $("vr-voiceonly").checked, min_call_s: +$("vr-min").value } }),
    console: () => {
      const c = { enabled: $("cn-on").checked, path: $("cn-path").value, group_name: $("cn-group").value.trim(),
        hid_device: $("cn-hid").value, playback_device: $("cn-play").value, ptt_gpio: +$("cn-gpio").value, ptt_invert: $("cn-inv").checked,
        private_command: $("cn-pcmd").value, private_format: $("cn-pfmt").value, private_delay_ms: +$("cn-pdelay").value,
        nxr_host: $("cn-host").value.trim(), nxr_port: +$("cn-port").value, nxr_user: $("cn-user").value.trim(),
        sip_local_port: +$("cn-lport").value, sip_auth_user: $("cn-auser").value.trim(), sip_codec: $("cn-codec").value,
        sip_hold_s: +$("cn-hold").value, sip_register: $("cn-reg").checked, sip_inbound: $("cn-in").checked,
        max_talk_s: +$("cn-max").value, talk_over_busy: $("cn-over").checked, ptt_lead_ms: +$("cn-lead").value, ptt_tail_ms: +$("cn-tail").value };
      if ($("cn-apass").value) c.sip_auth_password = $("cn-apass").value;
      return { console: c };
    },
    lcd: () => ({ lcd: { enabled: $("lcd-on").checked, bus: +$("lcd-bus").value, address: $("lcd-addr").value.trim(),
      size: $("lcd-size").value, backlight: $("lcd-bl").value, rotate_s: +$("lcd-rot").value,
      flash_emergency: $("lcd-flash").checked } }),
    polling: () => ({ polling: { mode: $("pl-mode").value, command: $("pl-cmd").value, format: $("pl-fmt").value,
      units: $("pl-units").value, moving_s: +$("pl-moving").value, parked_s: +$("pl-parked").value,
      emergency_s: +$("pl-emerg").value, timeout_s: +$("pl-timeout").value, guard_s: +$("pl-guard").value,
      max_per_min: +$("pl-max").value } }),
    alerts: () => ({ alerts: { overdue_min: +$("al-overdue").value, speed_limit_kmh: +$("al-speed").value,
      emergency_keywords: $("al-keywords").value, status_watch: $("al-status").checked,
      emergency_status: $("al-emst").value.trim(), normal_status: $("al-nost").value.trim() } }),
    reports: () => ({ reports: { stop_min: +$("rp-stopmin").value, stop_radius_m: +$("rp-radius").value } }),
    data: () => ({ keep_days: +$("keep-days").value, drop_bad_checksum: $("drop-bad").checked }),
  };

  for (const form of document.querySelectorAll("form[data-section]")) {
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const section = form.dataset.section;
      const patch = builders[section]();
      const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
      try {
        const oldPort = S.web.port;
        const r = await post("/api/settings", patch);
        S = r.settings;
        toast(["Saved.", ...r.notes].join(" "));
        if (section === "web" && S.web.port !== oldPort) {
          setTimeout(() => { const u = new URL(location.href); u.port = S.web.port; location.href = u.href; }, 2500);
          return;
        }
        if (section === "map") setTimeout(() => location.reload(), 800);
        if (section === "console") { loadConsole(); setTimeout(loadConsole, 1200); }
        fillAll();
      } catch (e) {
        toast(e.message, true);
      } finally { btn.disabled = false; }
    };
  }

  $("al-test").onclick = async () => {
    if (!confirm("Raise a TEST emergency? Every open map screen will show the red banner and sound the alarm until someone acknowledges it.")) return;
    try { await post("/api/alerts/test", {}); toast("Test emergency raised. Acknowledge it on the map."); } catch (e) { toast(e.message, true); }
  };
  $("tile-clear").onclick = async () => {
    if (!confirm("Delete all saved map tiles? If the Pi is offline the map will be blank until it's back online.")) return;
    await post("/api/tiles/clear", {}); toast("Tile cache cleared"); refresh();
  };
  $("restart").onclick = async () => {
    if (!confirm("Restart the KenTrackIt service? The map will be unavailable for a few seconds.")) return;
    try { await post("/api/restart", {}); } catch (e) { /* connection drops */ }
    toast("Restarting…");
    setTimeout(function wait() {
      fetch("/api/config").then((r) => r.ok ? location.reload() : setTimeout(wait, 1500))
        .catch(() => setTimeout(wait, 1500));
    }, 3000);
  };

  // ---- users ----------------------------------------------------------------------
  let USERS = null, ME = null, editing = null;
  const unitLabel = (id) => { const u = USERS.units.find((x) => x.id === id); return u && u.alias ? `${u.alias} (${id})` : id; };
  async function loadUsers() {
    USERS = await api("/api/users");
    const tb = $("user-rows"); tb.replaceChildren();
    for (const u of USERS.users) {
      const tr = document.createElement("tr"); if (!u.enabled) tr.className = "off";
      const nm = document.createElement("td");
      const b = document.createElement("div"); b.className = "uname"; b.textContent = u.display_name || u.username;
      const s = document.createElement("div"); s.className = "usub"; s.textContent = u.username + (u.enabled ? "" : " · disabled") + (ME && u.id === ME.id ? " · you" : "");
      nm.append(b, s);
      const role = document.createElement("td"); role.textContent = u.role_label;
      const un = document.createElement("td"); un.textContent = u.units === null ? "All" : u.units.length ? u.units.map(unitLabel).join(", ") : "None";
      const last = document.createElement("td"); last.textContent = when(u.last_login);
      const act = document.createElement("td"); act.className = "act";
      const e = document.createElement("button"); e.textContent = "Edit"; e.onclick = () => openUser(u); act.appendChild(e);
      tr.append(nm, role, un, last, act); tb.appendChild(tr);
    }
  }
  function openUser(u) {
    editing = u || null;
    $("u-form").hidden = false; $("u-add-row").hidden = true;
    $("u-form-title").textContent = u ? `Edit ${u.display_name || u.username}` : "New user";
    $("uf-name").value = u ? u.display_name : ""; $("uf-user").value = u ? u.username : "";
    $("uf-role").value = u ? u.role : "viewer";
    $("uf-pass").value = ""; $("uf-pass").required = !u; $("uf-pass").placeholder = u ? "leave blank to keep" : "at least 8 characters";
    $("uf-enabled").checked = u ? u.enabled : true;
    const restricted = u && u.units !== null;
    $("uf-all").checked = !restricted; $("uf-some").checked = restricted;
    const box = $("uf-units"); box.replaceChildren();
    if (!USERS.units.length) { const p = document.createElement("p"); p.className = "help"; p.textContent = "No units have reported yet."; box.appendChild(p); }
    for (const x of USERS.units) {
      const l = document.createElement("label"); l.className = "check";
      const c = document.createElement("input"); c.type = "checkbox"; c.value = x.id; c.checked = restricted && u.units.includes(x.id);
      l.append(c, document.createTextNode(" " + unitLabel(x.id))); box.appendChild(l);
    }
    box.hidden = !restricted;
    $("uf-delete").hidden = !u || (ME && u.id === ME.id);
    $("u-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("uf-name").focus();
  }
  const closeUser = () => { $("u-form").hidden = true; $("u-add-row").hidden = false; editing = null; };
  $("uf-all").onchange = $("uf-some").onchange = () => { $("uf-units").hidden = !$("uf-some").checked; };
  $("u-add").onclick = () => openUser(null);
  $("uf-cancel").onclick = closeUser;
  $("uf-delete").onclick = async () => {
    if (!editing || !confirm(`Delete ${editing.display_name || editing.username}? They'll be signed out straight away.`)) return;
    try { await api("/api/users/" + editing.id, { method: "DELETE" }); toast("User deleted"); closeUser(); loadUsers(); loadAudit(); }
    catch (e) { toast(e.message, true); }
  };
  $("u-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { display_name: $("uf-name").value.trim(), username: $("uf-user").value.trim(), role: $("uf-role").value,
      enabled: $("uf-enabled").checked,
      units: $("uf-some").checked ? [...$("uf-units").querySelectorAll("input:checked")].map((c) => c.value) : null };
    if ($("uf-pass").value) body.password = $("uf-pass").value;
    try {
      await post("/api/users" + (editing ? "/" + editing.id : ""), body);
      toast(editing ? "User updated" : "User added"); closeUser(); loadUsers(); loadAudit();
    } catch (e) { toast(e.message, true); }
  };

  let auditLimit = 30;
  async function loadAudit() {
    const rows = await api("/api/audit?limit=" + auditLimit);
    const ul = $("audit"); ul.replaceChildren();
    for (const r of rows) {
      const li = document.createElement("li");
      const t = document.createElement("time"); t.textContent = when(r.rx_time);
      const d = document.createElement("span"); d.textContent = r.detail;
      li.append(t, d); ul.appendChild(li);
    }
    $("audit-more").hidden = rows.length < auditLimit;
  }
  $("audit-more").onclick = () => { auditLimit += 100; loadAudit(); };

  // ---- raw serial capture -----------------------------------------------------------
  async function loadCaptures() {
    const c = await api("/api/captures");
    const st = c.status;
    $("cap-start").hidden = st.recording; $("cap-stop").hidden = !st.recording; $("cap-min").disabled = st.recording;
    $("cap-status").textContent = st.recording ? `Recording ${st.name} until ${new Date(st.until * 1000).toLocaleTimeString([], { hour12: false })} (${bytes(st.bytes)})` : "";
    const tb = $("cap-files"); tb.replaceChildren();
    for (const f of c.files) {
      const tr = document.createElement("tr");
      const n = document.createElement("td"); n.className = "id"; n.textContent = f.name;
      const sz = document.createElement("td"); sz.textContent = bytes(f.bytes);
      const act = document.createElement("td"); act.className = "act";
      const dl = document.createElement("a"); dl.className = "btn"; dl.textContent = "Download"; dl.href = "/api/captures/" + f.name;
      const del = document.createElement("button"); del.textContent = "Delete"; del.className = "danger";
      del.onclick = async () => { await api("/api/captures/" + f.name, { method: "DELETE" }); loadCaptures(); };
      act.append(dl, " ", del);
      tr.append(n, sz, act); tb.appendChild(tr);
    }
  }
  $("cap-start").onclick = async () => { await post("/api/captures/start", { minutes: +$("cap-min").value }); toast("Capture started"); loadCaptures(); };
  $("cap-stop").onclick = async () => { await post("/api/captures/stop", {}); toast("Capture stopped"); loadCaptures(); };
  setInterval(() => { if (!$("cap-stop").hidden) loadCaptures(); }, 5000);

  // ---- load ----------------------------------------------------------------------
  function fillAll() { fillSerial(); fillWeb(); fillMap(); fillDisplay(); fillVoice(); fillConsole(); fillLcd(); fillPolling(); fillAlerts(); fillData(); fillStatus(); }
  async function refresh() {
    try { SYS = await api("/api/system"); fillStatus(); } catch (e) { /* offline briefly */ }
  }
  [S, SYS] = await Promise.all([api("/api/settings"), api("/api/system")]);
  try { VOICE = await api("/api/voice"); } catch (e) { /* not an admin */ }
  try { LCDI = await api("/api/lcd"); } catch (e) { /* older server */ }
  try { CON = await api("/api/console/devices"); } catch (e) { /* older server */ }
  fillAll();
  fillUnits();
  ME = await api("/api/me");
  $("me-link").textContent = (ME.display_name || ME.username) + " ▾";
  loadUsers(); loadAudit(); loadCaptures();
  setInterval(refresh, 5000);
})();
