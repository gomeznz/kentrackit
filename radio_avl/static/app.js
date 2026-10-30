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

  // ---- map -----------------------------------------------------------
  const map = L.map("map", { zoomControl: true }).setView(cfg.map.center, cfg.map.zoom);
  if (cfg.tiles) L.tileLayer("/tiles/{z}/{x}/{y}", { maxZoom: cfg.max_zoom, attribution: cfg.attribution }).addTo(map);
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
  const state = (u) => {
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
  const emergencyUnits = () => new Set(alerts.filter((a) => !a.ack_time && a.severity === "critical" && a.unit_id).map((a) => a.unit_id));

  function icon(u) {
    const moving = u.last_speed_kn != null && u.last_speed_kn > 1.5 && u.last_course != null;
    const box = el("div", `unit-icon st-${state(u)}${selected === u.id ? " sel" : ""}${emergencyUnits().has(u.id) ? " emerg" : ""}`);
    const pin = el("div", "pin"); pin.style.background = colour(u);
    if (moving) {
      const a = el("div", "arrow"); a.style.color = colour(u);
      a.style.transform = `rotate(${u.last_course}deg)`;
      box.appendChild(a);
    }
    box.append(pin, el("div", "label", label(u)));
    return L.divIcon({ html: box, className: "", iconSize: [0, 0] });
  }
  const refreshIcons = () => { for (const e of units.values()) if (e.marker) e.marker.setIcon(icon(e.data)); };

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
      parts.push(u.last_valid ? speed(u.last_speed_kn) : "no GPS fix");
      const zn = zoneNames(e.zones); if (zn.length) parts.push(zn.join(", "));
      mid.append(el("div", "name", label(u)), el("div", "sub", parts.join(" · ")));
      li.append(dot, mid, el("div", "age", ago(u.last_seen)));
      li.onclick = () => select(u.id, true);
      ul.appendChild(li);
    }
    const live = [...units.values()].filter((e) => state(e.data) === "live").length;
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
    renderDetail(); renderList();
  }
  function renderDetail() {
    const e = selected && units.get(selected);
    if (!e) return;
    const u = e.data;
    $("d-name").textContent = label(u);
    $("d-id").textContent = u.id;
    $("d-seen").textContent = u.last_seen ? `${ago(u.last_seen)} (${stamp(u.last_seen)})` : "never";
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
      if (!a.ack_time) { const b = el("button", null, "Acknowledge"); b.onclick = () => post(`/api/alerts/${a.id}/ack`, {}); act.appendChild(b); }
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
      KTSound.alarmStart();         // emergencies sound even when chimes are muted
    } else {
      KTSound.alarmStop();
    }
    refreshIcons();
  }
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
      layer.bindTooltip(z.name, { permanent: true, direction: "center", className: "zone-label" });
      layer.on("click", () => { if (tab === "zones" && !drawing) editZone(z); });
      zoneLayer.addLayer(layer);
    }
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
      li.onclick = () => editZone(z, true);
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
    setTimeout(() => { map.invalidateSize(); if (on) fitAll(); }, 250);
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
      $("display").textContent = d.fields.filter(Boolean).join("  ·  ") || "—";
    });
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
