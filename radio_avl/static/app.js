"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const store = { get: (k, d) => { try { const v = localStorage.getItem("kt." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
                  set: (k, v) => { try { localStorage.setItem("kt." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
                   "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];
  const post = (url, body, method = "POST") => fetch(url, { method, headers: { "Content-Type": "application/json" },
    body: body == null ? undefined : JSON.stringify(body) }).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.status); return j; });

  const cfg = await (await fetch("/api/config")).json();
  const skew = cfg.now * 1000 - Date.now();          // server clock minus browser clock
  const now = () => (Date.now() + skew) / 1000;
  document.title = cfg.title; $("title").textContent = cfg.title; $("w-title").textContent = cfg.title;
  const me = cfg.user, can = (p) => me.perms.includes(p);
  const fullName = me.display_name || me.username;
  $("me-name").textContent = fullName.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  $("t-me").title = `${fullName} (${me.role_label}): my account and sign out`;
  $("t-settings").hidden = !can("settings");
  $("t-reports").hidden = !can("reports");
  $("t-channel").hidden = !can("reports");
  $("t-console").hidden = !can("listen");
  $("d-poll").hidden = !can("poll") || cfg.polling_mode === "off";
  $("d-edit").hidden = !can("units");
  $("p-csv").hidden = !can("export");
  $("p-report").hidden = !can("reports");
  $("a-ack-all").hidden = !can("ack");
  $("z-add-circle").hidden = $("z-add-poly").hidden = !can("zones");
  $("em-ack").hidden = !can("ack"); $("em-silence").hidden = can("ack");

  // ---- map -----------------------------------------------------------
  const map = L.map("map", { zoomControl: true }).setView(cfg.map.center, cfg.map.zoom);
  KTBasemap(map, cfg);
  L.control.scale({ imperial: false }).addTo(map);
  const zoneLayer = L.layerGroup().addTo(map);
  const trailLayer = L.layerGroup().addTo(map);

  const units = new Map();      // id -> {data, marker, zones}
  let zones = [];
  let alerts = [];
  let selected = null, following = false, fitted = false, lastUserMove = 0;
  map.on("dragstart zoomstart", (e) => { if (e.hard !== false) lastUserMove = Date.now(); });
  map.on("dragstart", () => { if (following) setFollow(false); });

  const label = (u) => u.alias || "Unit " + u.id.replace(/^[A-Za-z]*0*/, "");
  const colour = (u) => {
    if (u.colour) return u.colour;
    let h = 0; for (const c of u.id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return PALETTE[h % PALETTE.length];
  };
  const callOnly = (u) => !u.last_seen && !!u.last_call;        // heard by caller ID, never sent GPS
  const state = (u) => {
    if (callOnly(u)) return now() - u.last_call < cfg.lost_after_s ? "voice" : "lost";
    if (!u.last_seen) return "lost";
    const age = now() - u.last_seen;
    return age < cfg.stale_after_s ? "live" : age < cfg.lost_after_s ? "stale" : "lost";
  };
  const ago = (t) => {
    if (!t) return "never";
    const s = Math.max(0, Math.round(now() - t));
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  };
  const speed = (kn) => kn == null ? "–" :
    cfg.speed_units === "knots" ? kn.toFixed(1) + " kn" : Math.round(kn * 1.852) + " km/h";
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
  const stamp = (t) => new Date(t * 1000).toLocaleString([], { hour12: false });
  const zoneNames = (ids) => (ids || []).map((id) => (zones.find((z) => z.id === id) || {}).name).filter(Boolean);
  let onair = { busy: false };
  const talking = (id) => onair.busy && onair.unit === id;
  const emergencyUnits = () => new Set(alerts.filter((a) => !a.ack_time && a.severity === "critical" && a.unit_id).map((a) => a.unit_id));

  function icon(u) {
    const moving = u.last_speed_kn != null && u.last_speed_kn > 1.5 && u.last_course != null;
    const box = el("div", `unit-icon st-${state(u)}${selected === u.id ? " sel" : ""}${emergencyUnits().has(u.id) ? " emerg" : ""}${talking(u.id) ? " talking" : ""}`);
    const pin = el("div", "pin"); pin.style.background = colour(u);
    if (moving) {
      const a = el("div", "arrow"); a.style.color = colour(u);
      a.style.transform = `rotate(${u.last_course}deg)`;
      box.appendChild(a);
    }
    box.append(pin);
    return L.divIcon({ html: box, className: "", iconSize: [0, 0] });
  }
  const refreshIcons = () => { for (const e of units.values()) if (e.marker) e.marker.setIcon(icon(e.data)); scheduleLabels(); };

  // ---- unit labels ---------------------------------------------------------
  // Each label is placed in the nearest clear spot around its unit (away from other labels,
  // unit markers and zone names) and joined back to the unit with a leader line.
  const SVGNS = "http://www.w3.org/2000/svg";
  const labelLayer = el("div", "unit-labels");
  const leaders = document.createElementNS(SVGNS, "svg"); leaders.setAttribute("class", "leaders");
  map.getContainer().append(leaders, labelLayer);
  const labelEls = new Map();            // unit id -> {node, text, w, h, wall}
  let labelsQueued = false;
  function scheduleLabels() {
    if (labelsQueued) return;
    labelsQueued = true;
    requestAnimationFrame(layoutLabels);
  }
  map.on("move zoomend resize viewreset", scheduleLabels);
  map.on("zoomstart", () => { labelLayer.classList.add("hiding"); leaders.classList.add("hiding"); });
  map.on("zoomend", () => { labelLayer.classList.remove("hiding"); leaders.classList.remove("hiding"); });

  const ANGLES = [-40, -140, 40, 140, 0, 180, -90, 90].map((d) => d * Math.PI / 180);
  const RADII = [14, 30, 52, 80, 115];
  const overlap = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) *
                            Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));

  function layoutLabels() {
    labelsQueued = false;
    const box = map.getContainer().getBoundingClientRect(), W = box.width, H = box.height;
    const em = emergencyUnits(), isWall = document.body.classList.contains("wall");
    const visible = [...units.values()].filter((e) => e.marker).map((e) => {
      const p = map.latLngToContainerPoint(e.marker.getLatLng());
      return { e, p };
    }).filter(({ p }) => p.x > -150 && p.y > -60 && p.x < W + 150 && p.y < H + 60);
    const rank = ({ e }) => (selected === e.data.id ? 0 : em.has(e.data.id) ? 1 : state(e.data) === "live" ? 2 : state(e.data) === "stale" ? 3 : 4);
    visible.sort((a, b) => rank(a) - rank(b) || label(a.e.data).localeCompare(label(b.e.data), undefined, { numeric: true }));

    // things to keep clear of: every unit marker, and the zone name labels
    const obstacles = visible.map(({ p }) => ({ x: p.x - 12, y: p.y - 12, w: 24, h: 24 }));
    for (const z of map.getContainer().querySelectorAll(".zone-label")) {
      const r = z.getBoundingClientRect();
      if (r.width) obstacles.push({ x: r.left - box.left - 3, y: r.top - box.top - 2, w: r.width + 6, h: r.height + 4 });
    }

    const seen = new Set(), placed = [];
    leaders.replaceChildren();
    for (const { e, p } of visible) {
      const u = e.data, id = u.id, text = label(u);
      seen.add(id);
      let L1 = labelEls.get(id);
      if (!L1) {
        const node = el("div", "ulabel");
        node.onclick = () => select(id, false);
        labelLayer.appendChild(node);
        L1 = { node }; labelEls.set(id, L1);
      }
      if (L1.text !== text || L1.wall !== isWall) {
        L1.node.textContent = text; L1.text = text; L1.wall = isWall;
        L1.node.style.transform = "translate(-9999px,0)";
        L1.w = L1.node.offsetWidth; L1.h = L1.node.offsetHeight;
      }
      L1.node.className = `ulabel st-${state(u)}${selected === id ? " sel" : ""}${em.has(id) ? " emerg" : ""}${talking(id) ? " talking" : ""}`;
      L1.node.style.borderLeftColor = colour(u);
      const w = L1.w, h = L1.h;

      let best = null, bestCost = Infinity;
      search:
      for (const r of RADII) {
        for (const a of ANGLES) {
          const cx = p.x + Math.cos(a) * (r + w / 2), cy = p.y + Math.sin(a) * (r + h / 2);
          const rect = { x: cx - w / 2, y: cy - h / 2, w, h };
          let cost = 0;
          for (const o of obstacles) if (o.x !== p.x - 12 || o.y !== p.y - 12) cost += overlap(rect, o);
          for (const o of placed) cost += overlap(rect, o) * 3;
          if (rect.x < 2 || rect.y < 2 || rect.x + w > W - 2 || rect.y + h > H - 2) cost += w * h * 0.5;
          cost += r * 0.5;                                    // prefer close to the unit
          if (cost < bestCost) { bestCost = cost; best = rect; }
          if (cost <= r * 0.5) break search;                 // completely clear: take it
        }
      }
      placed.push(best);
      L1.node.style.transform = `translate(${Math.round(best.x)}px, ${Math.round(best.y)}px)`;

      // leader from the edge of the marker to the nearest point of the label
      const qx = Math.max(best.x, Math.min(p.x, best.x + w)), qy = Math.max(best.y, Math.min(p.y, best.y + h));
      const dx = qx - p.x, dy = qy - p.y, d = Math.hypot(dx, dy);
      if (d > 11) {
        const line = document.createElementNS(SVGNS, "line");
        line.setAttribute("x1", p.x + dx / d * 10); line.setAttribute("y1", p.y + dy / d * 10);
        line.setAttribute("x2", qx); line.setAttribute("y2", qy);
        line.setAttribute("stroke", em.has(id) ? "#dc2626" : colour(u));
        line.setAttribute("class", "st-" + state(u));
        leaders.appendChild(line);
        const dot = document.createElementNS(SVGNS, "circle");
        dot.setAttribute("cx", qx); dot.setAttribute("cy", qy); dot.setAttribute("r", 2.2);
        dot.setAttribute("fill", em.has(id) ? "#dc2626" : colour(u)); dot.setAttribute("class", "st-" + state(u));
        leaders.appendChild(dot);
      }
    }
    for (const [id, L1] of labelEls) if (!seen.has(id)) { L1.node.remove(); labelEls.delete(id); }
  }

  function upsert(u, z) {
    let e = units.get(u.id);
    if (!e) { e = { data: u, marker: null, zones: [] }; units.set(u.id, e); }
    e.data = u;
    if (z) e.zones = z;
    if (u.last_lat != null) {
      const ll = [u.last_lat, u.last_lon];
      if (!e.marker) e.marker = L.marker(ll, { icon: icon(u) }).addTo(map).on("click", () => select(u.id));
      else { e.marker.setLatLng(ll); e.marker.setIcon(icon(u)); }
    }
    scheduleLabels();
  }

  // ---- tabs ------------------------------------------------------------
  let tab = "units";
  function showTab(t) {
    tab = t;
    for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === t);
    for (const n of ["units", "zones", "alerts"]) $("tab-" + n).hidden = n !== t;
    $("detail").hidden = t !== "units" || !selected;
    if (t !== "zones") cancelDraw();
  }
  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => showTab(b.dataset.tab);

  // ---- unit list -----------------------------------------------------
  function renderList() {
    const f = $("filter").value.trim().toLowerCase();
    const em = emergencyUnits();
    const list = [...units.values()]
      .filter((e) => !f || label(e.data).toLowerCase().includes(f) || e.data.id.toLowerCase().includes(f))
      .sort((a, b) => label(a.data).localeCompare(label(b.data), undefined, { numeric: true }));
    const ul = $("units"); ul.replaceChildren();
    for (const e of list) {
      const u = e.data;
      const li = el("li", `st-${state(u)}${selected === u.id ? " sel" : ""}${em.has(u.id) ? " emerg" : ""}`);
      const dot = el("span", "dot"); dot.style.background = colour(u);
      const mid = el("div");
      const parts = [];
      if (u.alias) parts.push("ID " + u.id);
      parts.push(callOnly(u) ? "no GPS · calls only" : u.last_valid ? speed(u.last_speed_kn) : "no GPS fix");
      const zn = zoneNames(e.zones); if (zn.length) parts.push(zn.join(", "));
      mid.append(el("div", "name", label(u)), el("div", "sub", parts.join(" · ")));
      const heard = Math.max(u.last_seen || 0, u.last_call || 0) || null;
      li.append(dot, mid, el("div", "age", (callOnly(u) ? "📞 " : "") + ago(heard)));
      li.onclick = () => select(u.id, true);
      ul.appendChild(li);
    }
    const live = [...units.values()].filter((e) => state(e.data) === "live" || state(e.data) === "voice").length;
    $("unit-count").textContent = `${live}/${units.size}`;
    $("w-stats").textContent = `${live} live of ${units.size}` + (openAlerts().length ? ` · ${openAlerts().length} alert${openAlerts().length > 1 ? "s" : ""}` : "");
  }
  $("filter").oninput = renderList;

  // ---- detail panel --------------------------------------------------
  function select(id, pan) {
    selected = id;
    const e = units.get(id);
    if (!e) return;
    if (tab !== "units") showTab("units");
    $("detail").hidden = false;
    refreshIcons();
    if (pan && e.marker) map.panTo(e.marker.getLatLng());
    $("d-alias").value = e.data.alias || "";
    $("d-colour").value = colour(e.data);
    if (following) setFollow(true);
    renderDetail(); renderList(); loadCalls();
  }
  // ---- recent calls for the selected unit (voice recordings) --------------------
  const canListen = can("listen") && cfg.recording;
  async function loadCalls() {
    $("d-calls-wrap").hidden = !canListen || !selected;
    if (!canListen || !selected) return;
    const id = selected;
    const rows = await (await fetch(`/api/channel/log?unit=${encodeURIComponent(id)}&kind=voice&audio=1&limit=5&from=${now() - 7 * 86400}&to=${now() + 60}`)).json();
    if (selected !== id) return;
    const ul = $("d-calls"); ul.replaceChildren();
    if (!rows.length) { ul.appendChild(el("li", "empty", "No recorded calls in the last 7 days.")); return; }
    for (const r of rows) {
      const li = el("li");
      const b = el("button", "play-btn" + (KTPlayer.current() === r.id && KTPlayer.playing() ? " on" : ""), "▶");
      b.dataset.tx = r.id; b.title = "Play this call";
      b.onclick = () => KTPlayer.play(r.id, `${label(units.get(id).data)} · ${stamp(r.start)}`);
      li.append(b, el("time", null, stamp(r.start)), el("span", "grow"), el("span", "muted", `${Math.round(r.audio_s || r.duration)} s`));
      ul.appendChild(li);
    }
  }
  KTPlayer.onChange((id, state) => {
    for (const b of document.querySelectorAll(".play-btn")) b.classList.toggle("on", +b.dataset.tx === id && state === "playing");
  });
  function renderDetail() {
    const e = selected && units.get(selected);
    if (!e) return;
    const u = e.data;
    $("d-name").textContent = label(u);
    $("d-id").textContent = u.id;
    $("d-seen").textContent = u.last_seen ? `${ago(u.last_seen)} (${stamp(u.last_seen)})` : "never: this radio hasn't sent GPS";
    $("d-follow").hidden = u.last_lat == null;
    $("d-call").textContent = u.last_call ? `${ago(u.last_call)} (${stamp(u.last_call)})` : "–";
    $("d-motion").textContent = u.last_lat != null
      ? `${speed(u.last_speed_kn)} · ${u.last_course != null ? Math.round(u.last_course) + "°" : "–"}`
        + (u.last_valid ? "" : " (last report had no fix)") : "–";
    $("d-pos").textContent = u.last_lat != null ? `${u.last_lat.toFixed(5)}, ${u.last_lon.toFixed(5)}` : "–";
    $("d-zone").textContent = zoneNames(e.zones).join(", ") || "–";
  }
  function setFollow(on) {
    following = on && !!selected;
    $("d-follow").setAttribute("aria-pressed", following);
    $("d-follow").textContent = following ? "Following ✓" : "Follow";
    const e = following && units.get(selected);
    if (e && e.marker) map.setView(e.marker.getLatLng(), Math.max(map.getZoom(), 15));
  }
  $("d-follow").onclick = () => setFollow(!following);
  $("d-poll").onclick = async () => {
    if (!selected) return;
    $("d-poll").disabled = true; $("d-poll-result").textContent = "Polling…";
    try { const r = await post(`/api/units/${encodeURIComponent(selected)}/poll`, {});
          if (r.mode === "dry_run") $("d-poll-result").textContent = "Queued (test mode: nothing is sent)"; }
    catch (e) { $("d-poll-result").textContent = e.message; }
    setTimeout(() => { $("d-poll").disabled = false; }, 3000);
  };
  $("d-close").onclick = () => {
    selected = null; setFollow(false); $("detail").hidden = true; clearTrack();
    refreshIcons(); renderList();
  };
  $("d-edit").onsubmit = async (ev) => {
    ev.preventDefault();
    if (!selected) return;
    try { upsert(await post("/api/units/" + encodeURIComponent(selected), { alias: $("d-alias").value.trim(), colour: $("d-colour").value })); renderList(); renderDetail(); }
    catch (e) { toast({ severity: "warn", detail: "Couldn't save: " + e.message }); }
  };

  // ---- history / playback ---------------------------------------------
  let track = null, trackLine = null, playMarker = null, playing = null;
  const toLocalInput = (t) => {
    const d = new Date(t * 1000); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  };
  async function loadTrack(t0, t1) {
    if (!selected) return;
    $("t-from").value = toLocalInput(t0); $("t-to").value = toLocalInput(t1);
    const q = `unit=${encodeURIComponent(selected)}&from=${t0}&to=${t1}`;
    track = await (await fetch("/api/track?" + q)).json();
    $("p-csv").href = "/api/export.csv?" + q;
    $("p-report").href = `/reports?${q}`;
    clearTrack(true);
    $("play").hidden = false;
    if (!track.length) { $("p-info").textContent = "No positions in that period."; return; }
    setFollow(false);
    const pts = track.map((p) => [p.lat, p.lon]);
    const c = colour(units.get(selected).data);
    trackLine = L.polyline(pts, { color: c, weight: 4, opacity: .8 }).addTo(map);
    if (pts.length > 1) map.fitBounds(trackLine.getBounds(), { padding: [40, 40], maxZoom: 17 });
    playMarker = L.circleMarker(pts[0], { radius: 7, color: "#fff", weight: 3, fillColor: c, fillOpacity: 1 }).addTo(map);
    $("p-slider").max = track.length - 1; $("p-slider").value = 0;
    showPoint(0);
  }
  function showPoint(i) {
    const p = track[i]; if (!p) return;
    playMarker.setLatLng([p.lat, p.lon]);
    $("p-info").textContent = `${stamp(p.rx_time)} · ${speed(p.speed_kn)} · point ${i + 1} of ${track.length}`;
  }
  function clearTrack(keepPanel) {
    stopPlay();
    if (trackLine) map.removeLayer(trackLine);
    if (playMarker) map.removeLayer(playMarker);
    trackLine = playMarker = null;
    if (!keepPanel) { track = null; $("play").hidden = true; }
    for (const b of $("ranges").children) b.classList.toggle("on", false);
  }
  function stopPlay() { if (playing) clearTimeout(playing); playing = null; $("p-play").textContent = "▶"; }
  function step() {
    const s = $("p-slider"), i = +s.value;
    if (!track || i >= track.length - 1) return stopPlay();
    s.value = i + 1; showPoint(i + 1);
    const dt = (track[i + 1].rx_time - track[i].rx_time) / +$("p-speed").value;
    playing = setTimeout(step, Math.min(Math.max(dt * 1000, 30), 2000));
  }
  $("p-play").onclick = () => {
    if (playing) return stopPlay();
    if (!track || !track.length) return;
    if (+$("p-slider").value >= track.length - 1) $("p-slider").value = 0;
    $("p-play").textContent = "❚❚"; step();
  };
  $("p-slider").oninput = (e) => { stopPlay(); showPoint(+e.target.value); };
  $("p-clear").onclick = () => clearTrack();
  for (const b of $("ranges").children) {
    b.onclick = () => {
      const t1 = now(); loadTrack(t1 - b.dataset.h * 3600, t1);
      setTimeout(() => { for (const x of $("ranges").children) x.classList.toggle("on", x === b); });
    };
  }
  $("t-load").onclick = () => {
    const a = Date.parse($("t-from").value) / 1000, b = Date.parse($("t-to").value) / 1000;
    if (a && b && b > a) loadTrack(a, b);
  };

  // ---- trails ------------------------------------------------------------
  let trailsOn = store.get("trails", true);
  const trails = new Map();   // unit -> {line, pts:[[lat,lon,t]]}
  async function loadTrails() {
    trailLayer.clearLayers(); trails.clear();
    if (!trailsOn) return;
    const data = await (await fetch("/api/trails?minutes=" + cfg.trail_min)).json();
    for (const [id, pts] of Object.entries(data)) addTrail(id, pts);
  }
  function addTrail(id, pts) {
    const e = units.get(id);
    const line = L.polyline(pts.map((p) => [p[0], p[1]]), { color: e ? colour(e.data) : "#888", weight: 3, opacity: .55, interactive: false });
    trailLayer.addLayer(line);
    trails.set(id, { line, pts });
  }
  function extendTrail(id, lat, lon, t) {
    if (!trailsOn) return;
    let tr = trails.get(id);
    if (!tr) { addTrail(id, [[lat, lon, t]]); return; }
    tr.pts.push([lat, lon, t]);
    const cutoff = now() - cfg.trail_min * 60;
    while (tr.pts.length && tr.pts[0][2] < cutoff) tr.pts.shift();
    tr.line.setLatLngs(tr.pts.map((p) => [p[0], p[1]]));
  }
  function setTrails(on) {
    trailsOn = on; store.set("trails", on);
    $("t-trails").setAttribute("aria-pressed", on);
    loadTrails();
  }
  $("t-trails").onclick = () => setTrails(!trailsOn);

  // ---- coverage overlay --------------------------------------------------------
  const covLayer = L.layerGroup();
  let covOn = store.get("coverage", false), covDays = store.get("coverageDays", 7), covT = null;
  const cellFor = (z) => z >= 16 ? 75 : z >= 15 ? 100 : z >= 14 ? 150 : z >= 13 ? 250 : z >= 12 ? 400 : z >= 11 ? 700 : 1200;
  // diverging: red (missed) <-> gray midpoint <-> blue (heard)
  const covColour = (pct) => {
    const stops = [[0, [208, 59, 59]], [50, [160, 160, 155]], [100, [42, 120, 214]]];
    const [a, b] = pct <= 50 ? [stops[0], stops[1]] : [stops[1], stops[2]];
    const f = (pct - a[0]) / (b[0] - a[0]);
    return `rgb(${a[1].map((v, i) => Math.round(v + (b[1][i] - v) * f)).join(",")})`;
  };
  async function loadCoverage() {
    covLayer.clearLayers();
    if (!covOn) return;
    const t1 = now(), t0 = t1 - covDays * 86400;
    const d = await (await fetch(`/api/coverage?from=${t0}&to=${t1}&cell=${cellFor(map.getZoom())}`)).json();
    const maxN = Math.max(1, ...d.cells.map((c) => c.heard + c.missed));
    for (const c of d.cells) {
      const n = c.heard + c.missed, col = covColour(c.pct);
      L.rectangle([[c.s, c.w], [c.n, c.e]], { stroke: false, fillColor: col, fillOpacity: 0.18 + 0.4 * Math.min(1, Math.sqrt(n / maxN)), interactive: true })
        .bindTooltip(`${c.pct.toFixed(0)}% heard · ${c.heard} heard, ${c.missed} missed${c.inferred ? ` (${c.inferred} inferred)` : ""}`, { sticky: true })
        .addTo(covLayer);
    }
    $("cov-note").textContent = d.samples ? `${d.samples.toLocaleString()} samples, ${d.cell_m} m squares` : "No samples yet in this period";
  }
  function setCoverage(on) {
    covOn = on; store.set("coverage", on);
    $("t-coverage").setAttribute("aria-pressed", on);
    $("cov-legend").hidden = !on;
    if (on) { covLayer.addTo(map); covLayer.eachLayer(() => {}); } else map.removeLayer(covLayer);
    loadCoverage();
  }
  $("t-coverage").onclick = () => setCoverage(!covOn);
  for (const b of document.querySelectorAll("#cov-legend .cov-range button")) {
    b.classList.toggle("on", +b.dataset.d === covDays);
    b.onclick = () => { covDays = +b.dataset.d; store.set("coverageDays", covDays);
      for (const x of document.querySelectorAll("#cov-legend .cov-range button")) x.classList.toggle("on", x === b); loadCoverage(); };
  }
  map.on("zoomend", () => { if (covOn) { clearTimeout(covT); covT = setTimeout(loadCoverage, 250); } });
  setInterval(() => { if (covOn) loadCoverage(); }, 120000);
  setCoverage(covOn);

  // ---- sound -------------------------------------------------------------
  let soundOn = store.get("sound", true);
  const setSound = (on) => { soundOn = on; store.set("sound", on); $("t-sound").setAttribute("aria-pressed", on); };
  $("t-sound").onclick = () => { KTSound.unlock(); setSound(!soundOn); };
  setSound(soundOn);
  const unlockOnce = () => { KTSound.unlock(); $("sound-hint").hidden = true; updateEmergency(); };
  document.addEventListener("pointerdown", unlockOnce, { once: true });
  document.addEventListener("keydown", unlockOnce, { once: true });

  // ---- toasts --------------------------------------------------------------
  function toast(a) {
    const t = el("div", "toast sev-" + a.severity);
    t.append(el("strong", null, a.kind ? a.kind.toUpperCase() : ""), el("span", null, a.detail));
    if (a.lat != null) { t.classList.add("click"); t.onclick = () => map.setView([a.lat, a.lon], Math.max(map.getZoom(), 15)); }
    $("toasts").prepend(t);
    while ($("toasts").children.length > 4) $("toasts").lastChild.remove();
    setTimeout(() => t.classList.add("out"), 9000);
    setTimeout(() => t.remove(), 9600);
  }

  // ---- alerts ----------------------------------------------------------------
  const openAlerts = () => alerts.filter((a) => !a.ack_time);
  const unitName = (id) => { const e = id && units.get(id); return e ? label(e.data) : (id || ""); };
  function renderAlerts() {
    const open = openAlerts();
    $("alert-count").hidden = !open.length;
    $("alert-count").textContent = open.length;
    $("alert-count").className = "badge" + (open.some((a) => a.severity === "critical") ? " crit" : "");
    $("alerts-summary").textContent = open.length ? `${open.length} to acknowledge` : "Nothing to acknowledge";
    $("a-ack-all").disabled = !open.length;
    const ul = $("alerts"); ul.replaceChildren();
    const list = [...open, ...alerts.filter((a) => a.ack_time).slice(0, 40)];
    if (!list.length) ul.appendChild(el("li", "empty", "No alerts yet."));
    for (const a of list) {
      const li = el("li", `alert sev-${a.severity}${a.ack_time ? " acked" : ""}`);
      const head = el("div", "a-head");
      head.append(el("span", "a-kind", a.kind), el("time", null, stamp(a.t)));
      const body = el("div", "a-body", a.detail);
      const act = el("div", "a-act");
      if (a.lat != null) { const b = el("button", null, "Show"); b.onclick = () => { map.setView([a.lat, a.lon], Math.max(map.getZoom(), 16)); if (a.unit_id && units.has(a.unit_id)) select(a.unit_id); }; act.appendChild(b); }
      if (!a.ack_time && can("ack")) { const b = el("button", null, "Acknowledge"); b.onclick = () => post(`/api/alerts/${a.id}/ack`, {}); act.appendChild(b); }
      else if (!a.ack_time) act.appendChild(el("span", "muted", "Waiting for a dispatcher"));
      else act.appendChild(el("span", "muted", "✓ " + (a.ack_note || "acknowledged") + " " + clock(a.ack_time)));
      li.append(head, body, act);
      ul.appendChild(li);
    }
    renderList();
    updateEmergency();
  }
  $("a-ack-all").onclick = () => post("/api/alerts/ack-all", {});

  function updateEmergency() {
    const crit = openAlerts().filter((a) => a.severity === "critical");
    $("emergency").hidden = !crit.length;
    document.body.classList.toggle("in-emergency", crit.length > 0);
    if (crit.length) {
      const a = crit[0];
      $("em-detail").textContent = (a.kind === "test" ? "TEST · " : "") + a.detail.replace(/^Radio display:\s*/, "") +
        (crit.length > 1 ? ` (+${crit.length - 1} more)` : "");
      $("em-time").textContent = stamp(a.t);
      $("em-show").hidden = a.lat == null;
      if (!KTSound.unlocked()) $("sound-hint").hidden = false;
      if (!crit.every((x) => silenced.has(x.id))) KTSound.alarmStart();   // sounds even when chimes are muted
    } else {
      KTSound.alarmStop();
    }
    refreshIcons();
  }
  const silenced = new Set();
  $("em-silence").onclick = () => { for (const a of openAlerts()) silenced.add(a.id); KTSound.alarmStop(); };
  $("em-ack").onclick = () => {
    for (const a of openAlerts().filter((x) => x.severity === "critical")) post(`/api/alerts/${a.id}/ack`, { note: "Emergency acknowledged" });
  };
  $("em-show").onclick = () => {
    const a = openAlerts().find((x) => x.severity === "critical" && x.lat != null);
    if (a) { map.setView([a.lat, a.lon], 16); if (a.unit_id && units.has(a.unit_id)) select(a.unit_id); }
  };

  // ---- zones -------------------------------------------------------------------
  function renderZones() {
    zoneLayer.clearLayers();
    for (const z of zones) {
      const style = { color: z.colour, weight: 2, opacity: z.active ? .9 : .4, fillOpacity: z.active ? .08 : .03, dashArray: z.active ? null : "4 6" };
      const layer = z.shape === "circle" ? L.circle(z.geometry.center, { radius: z.geometry.radius, ...style })
        : L.polygon(z.geometry.points, style);
      layer.bindTooltip(document.createTextNode(z.name), { permanent: true, direction: "center", className: "zone-label" });
      layer.on("click", () => { if (tab === "zones" && !drawing && can("zones")) editZone(z); });
      zoneLayer.addLayer(layer);
    }
    scheduleLabels();
    $("zone-count").textContent = zones.length || "";
    const ul = $("zones"); ul.replaceChildren();
    if (!zones.length) ul.appendChild(el("li", "empty", "No zones yet. Add a circle (e.g. a depot) or draw an area."));
    for (const z of zones) {
      const li = el("li", "zone" + (z.active ? "" : " off"));
      const sw = el("span", "dot"); sw.style.background = z.colour;
      const mid = el("div");
      const flags = [z.shape === "circle" ? `circle ${Math.round(z.geometry.radius)} m` : `area, ${z.geometry.points.length} points`];
      if (z.alert_enter) flags.push("enter"); if (z.alert_exit) flags.push("leave"); if (!z.active) flags.push("off");
      mid.append(el("div", "name", z.name), el("div", "sub", flags.join(" · ")));
      const inside = [...units.values()].filter((e) => e.zones.includes(z.id)).length;
      li.append(sw, mid, el("div", "age", inside ? `${inside} in` : ""));
      li.onclick = () => { if (can("zones")) editZone(z, true); else if (z.shape === "circle") map.setView(z.geometry.center, 16); else map.fitBounds(z.geometry.points); };
      ul.appendChild(li);
    }
  }

  // drawing state
  let drawing = null;   // {mode:'circle'|'polygon', id, shape, center, radius, points, preview}
  const hint = (t) => { $("z-hint").hidden = !t; $("z-hint").textContent = t || ""; };
  function clearPreview() { if (drawing && drawing.preview) { map.removeLayer(drawing.preview); drawing.preview = null; } }
  function drawPreview() {
    clearPreview();
    const c = $("z-colour").value;
    if (drawing.shape === "circle" && drawing.center) drawing.preview = L.circle(drawing.center, { radius: drawing.radius, color: c, weight: 2, dashArray: "6 6" }).addTo(map);
    if (drawing.shape === "polygon" && drawing.points.length) {
      drawing.preview = (drawing.capturing ? L.polyline(drawing.points, { color: c, weight: 2, dashArray: "6 6" }) : L.polygon(drawing.points, { color: c, weight: 2, dashArray: "6 6" })).addTo(map);
    }
  }
  function startDraw(shape, existing) {
    cancelDraw();
    drawing = { shape, id: existing ? existing.id : null, center: null, radius: 200, points: [], capturing: true, preview: null };
    document.body.classList.add("drawing");
    $("z-edit").hidden = true;
    if (shape === "circle") hint("Click the map where the centre of the zone should be.");
    else hint("Click points around the area. Click the first point again, or double-click, to finish. Backspace removes the last point.");
  }
  function cancelDraw() {
    clearPreview(); drawing = null; hint(""); $("z-edit").hidden = true;
    document.body.classList.remove("drawing");
    zoneLayer.eachLayer((l) => l.setStyle && l.setStyle({ opacity: l.options.opacity }));
  }
  function openEditor(z) {
    $("z-edit").hidden = false;
    $("z-name").value = z ? z.name : ""; $("z-colour").value = z ? z.colour : "#2563eb";
    $("z-enter").checked = z ? z.alert_enter : true; $("z-exit").checked = z ? z.alert_exit : true;
    $("z-active").checked = z ? z.active : true;
    $("z-delete").hidden = !z;
    $("z-radius-wrap").hidden = drawing.shape !== "circle";
    $("z-radius").value = Math.round(drawing.radius);
    if (!z) $("z-name").focus();
  }
  function finishCapture() {
    if (drawing.shape === "polygon" && drawing.points.length < 3) return;
    drawing.capturing = false;
    hint(drawing.shape === "circle" ? "Click the map to move the centre. Set the radius below." : "");
    drawPreview();
    openEditor(drawing.id ? zones.find((z) => z.id === drawing.id) : null);
  }
  function editZone(z, pan) {
    showTab("zones");
    startDraw(z.shape, z);
    if (z.shape === "circle") { drawing.center = z.geometry.center; drawing.radius = z.geometry.radius; }
    else drawing.points = z.geometry.points.map((p) => p.slice());
    finishCapture();
    if (pan && drawing.preview) map.fitBounds(drawing.preview.getBounds(), { padding: [60, 60], maxZoom: 17 });
  }
  map.on("click", (ev) => {
    if (!drawing) return;
    const ll = [ev.latlng.lat, ev.latlng.lng];
    if (drawing.shape === "circle") {
      drawing.center = ll; drawPreview();
      if (drawing.capturing) finishCapture();
      return;
    }
    if (!drawing.capturing) return;
    if (drawing.points.length >= 3) {
      const first = map.latLngToContainerPoint(drawing.points[0]);
      if (first.distanceTo(ev.containerPoint) < 12) return finishCapture();
    }
    drawing.points.push(ll); drawPreview();
  });
  map.on("dblclick", (ev) => { if (drawing && drawing.capturing && drawing.shape === "polygon") { L.DomEvent.stop(ev); finishCapture(); } });
  document.addEventListener("keydown", (ev) => {
    if (!drawing) return;
    if (ev.key === "Escape") cancelDraw();
    if (ev.key === "Backspace" && drawing.capturing && drawing.points.length && document.activeElement.tagName !== "INPUT") { drawing.points.pop(); drawPreview(); ev.preventDefault(); }
  });
  map.doubleClickZoom.disable();
  $("z-add-circle").onclick = () => startDraw("circle");
  $("z-add-poly").onclick = () => startDraw("polygon");
  $("z-cancel").onclick = cancelDraw;
  $("z-radius").oninput = () => { drawing.radius = Math.max(5, +$("z-radius").value || 5); drawPreview(); };
  $("z-colour").oninput = () => drawPreview();
  $("z-redraw").onclick = () => {
    const keep = drawing; const z = zones.find((x) => x.id === keep.id);
    startDraw(keep.shape, z || null);
    drawing.radius = keep.radius;
  };
  $("z-delete").onclick = async () => {
    const z = zones.find((x) => x.id === drawing.id);
    if (!z || !confirm(`Delete zone "${z.name}"?`)) return;
    await post("/api/geofences/" + z.id, null, "DELETE");
    cancelDraw();
  };
  $("z-edit").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { name: $("z-name").value.trim(), shape: drawing.shape, colour: $("z-colour").value,
      alert_enter: $("z-enter").checked, alert_exit: $("z-exit").checked, active: $("z-active").checked,
      geometry: drawing.shape === "circle" ? { center: drawing.center, radius: drawing.radius } : { points: drawing.points } };
    try {
      await post("/api/geofences" + (drawing.id ? "/" + drawing.id : ""), body);
      cancelDraw();
    } catch (e) { hint("Couldn't save: " + e.message); }
  };

  // ---- wall display ------------------------------------------------------------
  let wall = false;
  function setWall(on, fullscreen) {
    wall = on;
    document.body.classList.toggle("wall", on);
    $("wallbar").hidden = !on;
    if (on && fullscreen && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    setTimeout(() => { map.invalidateSize(); if (on) fitAll(); scheduleLabels(); }, 250);
  }
  function fitAll() {
    const live = [...units.values()].filter((e) => e.marker && state(e.data) !== "lost");
    const use = live.length ? live : [...units.values()].filter((e) => e.marker);
    if (use.length) map.fitBounds(L.featureGroup(use.map((e) => e.marker)).getBounds(), { padding: [80, 80], maxZoom: 15 });
  }
  $("t-wall").onclick = () => setWall(true, true);
  $("w-exit").onclick = () => setWall(false);
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && wall && new URLSearchParams(location.search).get("wall") !== "1") setWall(false); });
  setInterval(() => { $("w-clock").textContent = new Date().toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" }); }, 1000);
  setInterval(() => { if (wall && !following && Date.now() - lastUserMove > 120000) fitAll(); }, 60000);

  // ---- activity log ----------------------------------------------------
  function logLine(t, text) {
    const li = el("li");
    li.append(el("time", null, clock(t)), el("span", null, text));
    const ul = $("log"); ul.prepend(li);
    while (ul.children.length > 60) ul.lastChild.remove();
  }

  // ---- initial load ----------------------------------------------------
  const [u0, z0, a0, ev0] = await Promise.all(["/api/units", "/api/geofences", "/api/alerts?limit=200", "/api/events?limit=20"]
    .map((u) => fetch(u).then((r) => r.json())));
  zones = z0; alerts = a0;
  for (const u of u0) upsert(u, (u.state && u.state.fences) || []);
  renderZones(); renderList(); renderAlerts();
  const withPos = [...units.values()].filter((e) => e.marker);
  if (withPos.length) { fitAll(); fitted = true; }
  for (const ev of ev0.reverse()) logLine(ev.rx_time, (ev.unit_id ? ev.unit_id + ": " : "") + ev.detail);
  setTrails(trailsOn);
  if (new URLSearchParams(location.search).get("wall") === "1") setWall(true, false);

  // ---- live updates ----------------------------------------------------
  function connect() {
    const es = new EventSource("/api/stream");
    es.addEventListener("position", (m) => {
      const d = JSON.parse(m.data);
      upsert(d.unit, d.zones);
      const e = units.get(d.unit.id);
      if (!fitted && e.marker) { map.setView(e.marker.getLatLng(), 15); fitted = true; }
      if (d.valid) extendTrail(d.unit.id, d.lat, d.lon, d.rx_time);
      renderList();
      if (selected === d.unit.id) {
        renderDetail();
        if (trackLine && d.valid && !playing) trackLine.addLatLng([d.lat, d.lon]);
        if (following && e.marker) map.panTo(e.marker.getLatLng());
      }
      logLine(d.rx_time, `${label(d.unit)} ${d.valid ? "reported · " + speed(d.speed_kn) : "reported, no GPS fix"}`);
    });
    es.addEventListener("unit", (m) => { upsert(JSON.parse(m.data)); renderList(); renderDetail(); });
    es.addEventListener("unit_removed", (m) => {
      const id = JSON.parse(m.data).id, e = units.get(id);
      if (e && e.marker) map.removeLayer(e.marker);
      units.delete(id);
      scheduleLabels();
      if (selected === id) $("d-close").click();
      renderList();
    });
    es.addEventListener("alert", (m) => {
      const a = JSON.parse(m.data);
      if (alerts.some((x) => x.id === a.id)) return;
      alerts.unshift(a);
      renderAlerts();
      if (a.severity !== "critical") {
        toast(a);
        if (soundOn) (a.severity === "warn" ? KTSound.warn() : KTSound.chime());
      }
      logLine(a.t, `${a.kind.toUpperCase()}: ${a.detail}`);
    });
    es.addEventListener("alert_ack", (m) => {
      const d = JSON.parse(m.data);
      for (const a of alerts) if (d.ids.includes(a.id)) { a.ack_time = now(); a.ack_note = d.note || ""; }
      renderAlerts();
    });
    es.addEventListener("geofences", (m) => { zones = JSON.parse(m.data); renderZones(); renderList(); });
    let onairTick = null;
    es.addEventListener("onair", (m) => {
      const prev = onair.unit;
      onair = JSON.parse(m.data);
      clearInterval(onairTick);
      const disp = $("display");
      if (onair.busy) {
        const who = onair.unit && units.has(onair.unit) ? label(units.get(onair.unit).data) : (onair.name || onair.unit || "");
        const upd = () => { disp.textContent = `● ON AIR${who ? " · " + who : ""}${onair.kind === "data" ? " (data)" : ""} · ${Math.max(0, Math.round(now() - onair.since))} s`; };
        disp.classList.add("onair"); upd(); onairTick = setInterval(upd, 1000);
      } else disp.classList.remove("onair");
      if (prev !== onair.unit || !onair.busy) refreshIcons();
    });
    es.addEventListener("tx", (m) => {
      const t = JSON.parse(m.data);
      if (t.kind !== "voice") return;
      const who = t.unit_id && units.has(t.unit_id) ? label(units.get(t.unit_id).data) : "Someone";
      logLine(t.start, `${who} talked for ${t.duration < 10 ? t.duration.toFixed(1) : Math.round(t.duration)} s`);
    });
    es.addEventListener("recording", (m) => {
      const d = JSON.parse(m.data);
      if (selected && d.unit_id === selected) loadCalls();
    });
    es.addEventListener("poll", (m) => {
      const d = JSON.parse(m.data);
      if (selected !== d.unit) return;
      const msg = { sent: "Poll sent…", answered: `Answered in ${d.rtt} s`, missed: "No answer", dry_run: "Test mode: would poll now" }[d.result];
      if (msg) $("d-poll-result").textContent = msg;
    });
    es.addEventListener("channel", (m) => {
      const d = JSON.parse(m.data);
      $("led-chan").className = "led" + (d.busy ? " busy" : "");
      $("led-chan").title = d.busy ? "Channel busy" : "Channel clear";
    });
    es.addEventListener("link", (m) => {
      const d = JSON.parse(m.data);
      $("led-link").className = "led " + (d.up ? "up" : "down");
      $("led-link").title = (d.up ? "Radio link up: " : "Radio link down: ") + d.detail;
    });
    es.addEventListener("display", (m) => {
      const d = JSON.parse(m.data);
      if (onair.busy) return;
      $("display").textContent = d.fields.filter(Boolean).join("  ·  ") || "—";
    });
    es.addEventListener("signed_out", () => { location.href = "/login"; });
    es.addEventListener("event", (m) => { const d = JSON.parse(m.data); logLine(d.t, d.kind + ": " + d.detail); });
    es.onopen = async () => {          // after a reconnect, catch up on anything missed
      const [a, z] = await Promise.all([fetch("/api/alerts?limit=200").then((r) => r.json()), fetch("/api/geofences").then((r) => r.json())]);
      alerts = a; zones = z; renderZones(); renderAlerts();
    };
    es.onerror = () => { $("led-link").className = "led down"; $("led-link").title = "Lost connection to the Pi"; };
  }
  connect();

  setInterval(() => { refreshIcons(); renderList(); renderDetail(); }, 5000);
})();
