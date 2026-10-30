"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2",
                   "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];
  const colourOf = (id, c) => { if (c) return c; let h = 0; for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };

  const [cfg, settings, units] = await Promise.all(["/api/config", "/api/settings", "/api/units"].map((u) => fetch(u).then((r) => r.json())));
  $("r-title").textContent = cfg.title; document.title = cfg.title + " report";
  $("r-stopmin").textContent = settings.reports.stop_min; $("r-stoprad").textContent = settings.reports.stop_radius_m;
  const kmh = settings.display.speed_units !== "knots";
  const spd = (k) => kmh ? `${k} km/h` : `${(k / 1.852).toFixed(0)} kn`;
  const hm = (s) => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`; };
  const tm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" });
  const dt = (t, multiDay) => t ? (multiDay ? new Date(t * 1000).toLocaleDateString([], { day: "numeric", month: "short" }) + " " : "") + tm(t) : "–";
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  for (const u of units) { const o = el("option", null, u.alias ? `${u.alias} (${u.id})` : u.id); o.value = u.id; $("r-unit").appendChild(o); }

  function preset(p) {
    const d = new Date(); let a = new Date(d), b = new Date(d);
    if (p === "yesterday") { a.setDate(a.getDate() - 1); b = new Date(a); }
    if (p === "7d") a.setDate(a.getDate() - 6);
    $("r-from").value = iso(a); $("r-to").value = iso(b);
    for (const x of $("presets").children) x.classList.toggle("on", x.dataset.p === p);
  }
  for (const b of $("presets").children) b.onclick = () => { preset(b.dataset.p); run(); };

  const range = () => {
    const a = new Date($("r-from").value + "T00:00"), b = new Date($("r-to").value + "T00:00");
    b.setDate(b.getDate() + 1);
    return [a.getTime() / 1000, b.getTime() / 1000 - 1];
  };

  let map = null, mapLayers = null;
  async function run() {
    let [t0, t1] = range();
    if (!(t1 > t0)) return;
    const multiDay = t1 - t0 > 86400;
    $("r-period").textContent = new Date(t0 * 1000).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" }) +
      (multiDay ? " to " + new Date(t1 * 1000).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "");
    $("r-csv").href = `/api/report.csv?from=${t0}&to=${t1}`;
    const rep = await (await fetch(`/api/report?from=${t0}&to=${t1}`)).json();

    const T = rep.totals;
    $("totals").replaceChildren(...[[T.units, "units reporting"], [T.distance_km.toFixed(1) + " km", "total distance"], [hm(T.moving_s), "total moving"]]
      .map(([v, k]) => { const d = el("div"); d.append(el("b", null, v), el("span", null, k)); return d; }));
    const tb = $("fleet-rows"); tb.replaceChildren();
    if (!rep.units.length) { const tr = el("tr"); const td = el("td", "empty", "No positions in this period."); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
    for (const r of rep.units) {
      const tr = el("tr", "click"); tr.dataset.unit = r.unit;
      const name = el("td"); const dot = el("span", "dot"); dot.style.background = colourOf(r.unit, r.colour);
      name.append(dot, document.createTextNode(r.name));
      tr.append(name, el("td", null, dt(r.first, multiDay)), el("td", null, dt(r.last, multiDay)),
        el("td", "n", r.distance_km.toFixed(1) + " km"), el("td", "n", hm(r.moving_s)), el("td", "n", hm(r.stopped_s)),
        el("td", "n", r.stops.length), el("td", "n", r.trips.length), el("td", "n", spd(r.max_kmh)));
      tr.onclick = () => showUnit(r.unit, t0, t1, multiDay);
      tb.appendChild(tr);
    }
    const want = $("r-unit").value;
    if (want) showUnit(want, t0, t1, multiDay); else $("unit").hidden = true;
  }

  async function showUnit(id, t0, t1, multiDay) {
    for (const tr of $("fleet-rows").children) tr.classList.toggle("sel", tr.dataset.unit === id);
    const rep = await (await fetch(`/api/report?from=${t0}&to=${t1}&unit=${encodeURIComponent(id)}`)).json();
    const r = rep.units[0];
    if (!r) return;
    $("unit").hidden = false;
    $("u-name").textContent = `${r.name} (${r.unit})`;
    $("u-totals").replaceChildren(...[[r.distance_km.toFixed(1) + " km", "distance"], [hm(r.moving_s), "moving"], [hm(r.stopped_s), "stopped"],
      [r.stops.length, "stops"], [spd(r.max_kmh), "top speed"]].map(([v, k]) => { const d = el("div"); d.append(el("b", null, v), el("span", null, k)); return d; }));

    if (!map) {
      map = L.map("u-map").setView(cfg.map.center, cfg.map.zoom);
      KTBasemap(map, cfg);
      L.control.scale({ imperial: false }).addTo(map);
      mapLayers = L.layerGroup().addTo(map);
      const zones = await (await fetch("/api/geofences")).json();
      for (const z of zones.filter((z) => z.active)) {
        const st = { color: z.colour, weight: 1.5, fillOpacity: .06 };
        (z.shape === "circle" ? L.circle(z.geometry.center, { radius: z.geometry.radius, ...st }) : L.polygon(z.geometry.points, st))
          .bindTooltip(z.name, { permanent: true, direction: "center", className: "zone-label" }).addTo(map);
      }
    }
    mapLayers.clearLayers();
    const c = colourOf(r.unit, r.colour);
    if (r.track.length) {
      const line = L.polyline(r.track.map((p) => [p[0], p[1]]), { color: c, weight: 4, opacity: .85 }).addTo(mapLayers);
      map.fitBounds(line.getBounds(), { padding: [30, 30], maxZoom: 16 });
      L.circleMarker(r.track[0], { radius: 6, color: "#fff", weight: 2, fillColor: "#16a34a", fillOpacity: 1 }).bindTooltip("Start " + tm(r.track[0][2])).addTo(mapLayers);
      const last = r.track[r.track.length - 1];
      L.circleMarker(last, { radius: 6, color: "#fff", weight: 2, fillColor: "#dc2626", fillOpacity: 1 }).bindTooltip("End " + tm(last[2])).addTo(mapLayers);
    }
    r.stops.forEach((s, i) => L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "stop-marker", html: String(i + 1) }) })
      .bindTooltip(`Stop ${i + 1}: ${tm(s.start)}–${tm(s.end)} (${hm(s.duration_s)})${s.zone ? " · " + s.zone : ""}`).addTo(mapLayers));
    setTimeout(() => map.invalidateSize(), 50);

    const items = [...r.stops.map((s, i) => ({ ...s, kind: "stop", n: i + 1 })), ...r.trips.map((t) => ({ ...t, kind: "trip" }))]
      .sort((a, b) => a.start - b.start);
    const tb = $("u-rows"); tb.replaceChildren();
    if (!items.length) { const tr = el("tr"); const td = el("td", "empty", "No positions in this period."); td.colSpan = 7; tr.appendChild(td); tb.appendChild(tr); }
    for (const it of items) {
      const tr = el("tr", it.kind + (it.gaps ? " gap" : ""));
      const k = el("td", "kind");
      if (it.kind === "stop") k.appendChild(el("span", "stop-num", it.n)); else k.appendChild(el("span", "trip-ico", "→"));
      const where = it.kind === "stop" ? (it.zone || `${it.lat.toFixed(4)}, ${it.lon.toFixed(4)}`)
        : `${it.from_zone || "…"} → ${it.to_zone || "…"}` + (it.gaps ? " (gap in reports)" : "");
      tr.append(k, el("td", null, dt(it.start, multiDay)), el("td", null, dt(it.end, multiDay)), el("td", "n", hm(it.duration_s)),
        el("td", null, where), el("td", "n", it.kind === "trip" ? it.km.toFixed(1) + " km" : ""), el("td", "n", it.kind === "trip" ? spd(it.max_kmh) : ""));
      if (it.kind === "stop") { tr.classList.add("click"); tr.onclick = () => map.setView([it.lat, it.lon], 17); }
      tb.appendChild(tr);
    }
    if ($("r-unit").value !== id) $("r-unit").value = id;
  }

  $("r-run").onclick = run;
  $("r-unit").onchange = run;
  $("r-print").onclick = () => window.print();

  // Opened from the map's "Trip report" link: ?unit=&from=&to=
  const qs = new URLSearchParams(location.search);
  if (qs.get("from")) {
    const a = new Date(+qs.get("from") * 1000), b = new Date(+qs.get("to") * 1000);
    $("r-from").value = iso(a); $("r-to").value = iso(b);
    if (qs.get("unit")) $("r-unit").value = qs.get("unit");
  } else preset("today");
  run();
})();
