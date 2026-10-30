"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
                   "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];

  const cfg = await (await fetch("/api/config")).json();
  const skew = cfg.now * 1000 - Date.now();          // server clock minus browser clock
  const now = () => (Date.now() + skew) / 1000;
  document.title = cfg.title; $("title").textContent = cfg.title;

  // ---- map -----------------------------------------------------------
  const map = L.map("map", { zoomControl: true }).setView(cfg.map.center, cfg.map.zoom);
  if (cfg.tiles) {
    L.tileLayer("/tiles/{z}/{x}/{y}", { maxZoom: cfg.max_zoom, attribution: cfg.attribution }).addTo(map);
  }
  L.control.scale({ imperial: false }).addTo(map);

  const units = new Map();      // id -> {data, marker}
  let selected = null;
  let fitted = false;

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

  function icon(u) {
    const moving = u.last_speed_kn != null && u.last_speed_kn > 1.5 && u.last_course != null;
    const el = document.createElement("div");
    el.className = `unit-icon st-${state(u)}${selected === u.id ? " sel" : ""}`;
    const pin = document.createElement("div");
    pin.className = "pin"; pin.style.background = colour(u);
    if (moving) {
      const a = document.createElement("div");
      a.className = "arrow"; a.style.color = colour(u);
      a.style.transform = `rotate(${u.last_course}deg)`;
      el.appendChild(a);
    }
    const lab = document.createElement("div");
    lab.className = "label"; lab.textContent = label(u);
    el.append(pin, lab);
    return L.divIcon({ html: el, className: "", iconSize: [0, 0] });
  }

  function upsert(u) {
    let e = units.get(u.id);
    if (!e) { e = { data: u, marker: null }; units.set(u.id, e); }
    e.data = u;
    if (u.last_lat != null) {
      const ll = [u.last_lat, u.last_lon];
      if (!e.marker) {
        e.marker = L.marker(ll, { icon: icon(u) }).addTo(map).on("click", () => select(u.id));
      } else {
        e.marker.setLatLng(ll); e.marker.setIcon(icon(u));
      }
    }
  }

  // ---- unit list -----------------------------------------------------
  function renderList() {
    const f = $("filter").value.trim().toLowerCase();
    const list = [...units.values()].map((e) => e.data)
      .filter((u) => !f || label(u).toLowerCase().includes(f) || u.id.toLowerCase().includes(f))
      .sort((a, b) => label(a).localeCompare(label(b), undefined, { numeric: true }));
    const ul = $("units"); ul.replaceChildren();
    for (const u of list) {
      const li = document.createElement("li");
      li.className = `st-${state(u)}${selected === u.id ? " sel" : ""}`;
      const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = colour(u);
      const mid = document.createElement("div");
      const n = document.createElement("div"); n.className = "name"; n.textContent = label(u);
      const s = document.createElement("div"); s.className = "sub";
      s.textContent = u.alias ? `ID ${u.id}` : (u.last_valid ? speed(u.last_speed_kn) : "no GPS fix");
      if (u.alias) s.textContent += " · " + (u.last_valid ? speed(u.last_speed_kn) : "no GPS fix");
      mid.append(n, s);
      const age = document.createElement("div"); age.className = "age"; age.textContent = ago(u.last_seen);
      li.append(dot, mid, age);
      li.onclick = () => select(u.id, true);
      ul.appendChild(li);
    }
    const live = [...units.values()].filter((e) => state(e.data) === "live").length;
    $("unit-count").textContent = `${live} live / ${units.size}`;
  }
  $("filter").oninput = renderList;

  // ---- detail panel --------------------------------------------------
  function select(id, pan) {
    selected = id;
    const e = units.get(id);
    if (!e) return;
    $("detail").hidden = false;
    for (const x of units.values()) if (x.marker) x.marker.setIcon(icon(x.data));
    if (pan && e.marker) map.panTo(e.marker.getLatLng());
    $("d-alias").value = e.data.alias || "";
    $("d-colour").value = colour(e.data);
    renderDetail(); renderList();
  }
  function renderDetail() {
    const e = selected && units.get(selected);
    if (!e) return;
    const u = e.data;
    $("d-name").textContent = label(u);
    $("d-id").textContent = u.id;
    $("d-seen").textContent = u.last_seen ? `${ago(u.last_seen)} (${stamp(u.last_seen)})` : "never";
    $("d-motion").textContent = u.last_valid || u.last_lat != null
      ? `${speed(u.last_speed_kn)} · ${u.last_course != null ? Math.round(u.last_course) + "°" : "–"}`
        + (u.last_valid ? "" : " (last report had no fix)") : "–";
    $("d-pos").textContent = u.last_lat != null ? `${u.last_lat.toFixed(5)}, ${u.last_lon.toFixed(5)}` : "–";
  }
  $("d-close").onclick = () => {
    selected = null; $("detail").hidden = true; clearTrack();
    for (const x of units.values()) if (x.marker) x.marker.setIcon(icon(x.data));
    renderList();
  };
  $("d-edit").onsubmit = async (ev) => {
    ev.preventDefault();
    if (!selected) return;
    const r = await fetch("/api/units/" + encodeURIComponent(selected), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias: $("d-alias").value.trim(), colour: $("d-colour").value }),
    });
    if (r.ok) { upsert(await r.json()); renderList(); renderDetail(); }
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
    clearTrack(true);
    $("play").hidden = false;
    if (!track.length) { $("p-info").textContent = "No positions in that period."; return; }
    const pts = track.map((p) => [p.lat, p.lon]);
    trackLine = L.polyline(pts, { color: colour(units.get(selected).data), weight: 4, opacity: .8 }).addTo(map);
    if (pts.length > 1) map.fitBounds(trackLine.getBounds(), { padding: [40, 40], maxZoom: 17 });
    playMarker = L.circleMarker(pts[0], { radius: 7, color: "#fff", weight: 3,
      fillColor: colour(units.get(selected).data), fillOpacity: 1 }).addTo(map);
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

  // ---- activity log ----------------------------------------------------
  function logLine(t, text) {
    const li = document.createElement("li");
    const tm = document.createElement("time"); tm.textContent = clock(t);
    const sp = document.createElement("span"); sp.textContent = text;
    li.append(tm, sp);
    const ul = $("log"); ul.prepend(li);
    while (ul.children.length > 60) ul.lastChild.remove();
  }

  // ---- initial load + live updates -------------------------------------
  for (const u of await (await fetch("/api/units")).json()) upsert(u);
  renderList();
  const withPos = [...units.values()].filter((e) => e.marker);
  if (withPos.length) {
    map.fitBounds(L.featureGroup(withPos.map((e) => e.marker)).getBounds(), { padding: [60, 60], maxZoom: 15 });
    fitted = true;
  }
  for (const ev of (await (await fetch("/api/events?limit=20")).json()).reverse()) {
    logLine(ev.rx_time, (ev.unit_id ? ev.unit_id + ": " : "") + ev.detail);
  }

  function connect() {
    const es = new EventSource("/api/stream");
    es.addEventListener("position", (m) => {
      const d = JSON.parse(m.data);
      upsert(d.unit);
      if (!fitted && units.get(d.unit.id).marker) { map.setView(units.get(d.unit.id).marker.getLatLng(), 15); fitted = true; }
      renderList();
      if (selected === d.unit.id) {
        renderDetail();
        if (trackLine && d.valid && !playing) trackLine.addLatLng([d.lat, d.lon]);
      }
      logLine(d.rx_time, `${label(d.unit)} ${d.valid ? "reported · " + speed(d.speed_kn) : "reported, no GPS fix"}`);
    });
    es.addEventListener("unit_removed", (m) => {
      const id = JSON.parse(m.data).id, e = units.get(id);
      if (e && e.marker) map.removeLayer(e.marker);
      units.delete(id);
      if (selected === id) $("d-close").click();
      renderList();
    });
    es.addEventListener("unit", (m) => { upsert(JSON.parse(m.data)); renderList(); renderDetail(); });
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
    es.onerror = () => { $("led-link").className = "led down"; $("led-link").title = "Lost connection to the Pi"; };
  }
  connect();

  setInterval(() => {               // keep ages and live/stale colours current
    for (const e of units.values()) if (e.marker) e.marker.setIcon(icon(e.data));
    renderList(); renderDetail();
  }, 5000);
})();
