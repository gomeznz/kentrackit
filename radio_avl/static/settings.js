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
    $("web-host").value = S.web.host;
    if (!$("web-host").value) {          // a host not in the list
      const o = document.createElement("option"); o.value = o.textContent = S.web.host;
      $("web-host").appendChild(o); $("web-host").value = S.web.host;
    }
    $("web-user").value = S.web.username; $("web-pass").value = "";
    $("clear-pass-wrap").hidden = !S.web.password_set; $("web-clear-pass").checked = false;
    $("port-check").textContent = ""; $("port-check").className = "hint";
  }

  function fillMap() {
    const t = S.tiles;
    $("tile-source").value = t.source;
    $("tile-linz").value = ""; $("tile-linz").placeholder = t.linz_key_set ? "key saved; enter a new one to replace" : "from basemaps.linz.govt.nz";
    $("tile-url").value = t.custom_url || ""; $("tile-attr").value = t.custom_attribution || "";
    $("tile-maxzoom").value = t.max_zoom;
    showTileFields();
    mini.setView(S.map.center, S.map.zoom);
    pendingView = null; $("view-text").textContent = viewText(S.map.center, S.map.zoom);
  }
  function showTileFields() {
    $("linz-wrap").hidden = $("tile-source").value !== "linz_aerial";
    $("custom-wrap").hidden = $("tile-source").value !== "custom";
  }
  $("tile-source").onchange = showTileFields;

  function fillDisplay() {
    $("disp-stale").value = S.display.stale_after_s;
    $("disp-lost").value = Math.round(S.display.lost_after_s / 60);
    $("disp-speed").value = S.display.speed_units;
    $("disp-trail").value = S.display.trail_min;
  }
  function fillAlerts() {
    $("al-overdue").value = S.alerts.overdue_min;
    $("al-speed").value = S.alerts.speed_limit_kmh;
    $("al-keywords").value = S.alerts.emergency_keywords;
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
    $("sys-ver").textContent = s.version;
    $("sys-host").textContent = s.hostname;
    $("sys-up").textContent = dur(s.uptime_s);
    $("sys-clock").textContent = Math.abs(s.clock_offset_s) > 0
      ? `Pi clock was out by ${s.clock_offset_s} s; corrected from GPS` : "OK";
    $("sys-cfg").textContent = s.config_path + (s.config_writable ? "" : " (read-only!)");
    $("sys-data").textContent = s.data_dir;

    const w = [];
    if (!S.web.password_set) w.push("No password is set: anyone on the network can open this page and change settings. Set one under Web access.");
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
  if (cfgPublic.tiles) L.tileLayer("/tiles/{z}/{x}/{y}", { maxZoom: cfgPublic.max_zoom }).addTo(mini);
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
      const w = { title: $("web-title").value, port: +$("web-port").value, host: $("web-host").value,
                  username: $("web-user").value };
      if ($("web-clear-pass").checked) w.clear_password = true;
      else if ($("web-pass").value) w.new_password = $("web-pass").value;
      return { web: w };
    },
    map: () => {
      const t = { source: $("tile-source").value, custom_url: $("tile-url").value,
                  custom_attribution: $("tile-attr").value, max_zoom: +$("tile-maxzoom").value };
      if ($("tile-linz").value.trim()) t.linz_key = $("tile-linz").value.trim();
      const p = { tiles: t };
      if (pendingView) p.map = pendingView;
      return p;
    },
    display: () => ({ display: { stale_after_s: +$("disp-stale").value,
      lost_after_s: Math.round(+$("disp-lost").value * 60), speed_units: $("disp-speed").value,
      trail_min: +$("disp-trail").value } }),
    alerts: () => ({ alerts: { overdue_min: +$("al-overdue").value, speed_limit_kmh: +$("al-speed").value,
      emergency_keywords: $("al-keywords").value, status_watch: $("al-status").checked } }),
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

  // ---- load ----------------------------------------------------------------------
  function fillAll() { fillSerial(); fillWeb(); fillMap(); fillDisplay(); fillAlerts(); fillData(); fillStatus(); }
  async function refresh() {
    try { SYS = await api("/api/system"); fillStatus(); } catch (e) { /* offline briefly */ }
  }
  [S, SYS] = await Promise.all([api("/api/settings"), api("/api/system")]);
  fillAll();
  fillUnits();
  setInterval(refresh, 5000);
})();
