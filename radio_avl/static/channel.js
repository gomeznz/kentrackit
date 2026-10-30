"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const SVG = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs) => { const e = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); return e; };
  const css = (name) => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();
  const KINDS = [["data", "Data", "--series-data"], ["voice", "Voice", "--series-voice"], ["poll", "Polls", "--series-poll"]];
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  const [cfg, units] = await Promise.all(["/api/config", "/api/units"].map((u) => fetch(u).then((r) => r.json())));
  const names = new Map(units.map((u) => [u.id, u.alias || "Unit " + u.id.replace(/^[A-Za-z]*0*/, "")]));
  const nameOf = (id) => id ? (names.get(id) || id) : "–";
  const canExport = cfg.user.perms.includes("export");
  document.title = "Channel · " + cfg.title;
  $("log-csv").hidden = !canExport;

  const dur = (s) => { s = Math.round(s); if (s < 60) return s + " s"; if (s < 3600) return Math.floor(s / 60) + " min " + (s % 60 ? (s % 60) + " s" : ""); const h = Math.floor(s / 3600); return h + " h " + Math.floor((s % 3600) / 60) + " min"; };
  const tm = (t, withDate) => new Date(t * 1000).toLocaleString([], withDate ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false } : { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  // ---- tooltip --------------------------------------------------------------
  const tip = $("tip");
  const showTip = (ev, nodes) => { tip.replaceChildren(...nodes); tip.hidden = false; moveTip(ev); };
  const moveTip = (ev) => { const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(innerWidth - w - 8, ev.clientX + 14) + "px"; tip.style.top = Math.max(8, ev.clientY - h - 12) + "px"; };
  const hideTip = () => { tip.hidden = true; };

  // ---- range ------------------------------------------------------------------
  let t0, t1, span, currentH = 24;
  function setRange(h) {
    currentH = h;
    const now = Date.now() / 1000;
    if (h === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); t0 = d.getTime() / 1000; } else t0 = now - h * 3600;
    t1 = now; span = t1 - t0;
    for (const b of $("presets").children) b.classList.toggle("on", b.dataset.h === String(h));
    try { localStorage.setItem("kt.channelRange", String(h)); } catch (e) { /* private mode */ }
    load();
  }
  for (const b of $("presets").children) b.onclick = () => setRange(b.dataset.h === "today" ? "today" : +b.dataset.h);

  const bucketFor = (s) => s <= 3700 ? 120 : s <= 2 * 86400 ? 900 : s <= 8 * 86400 ? 3 * 3600 : 12 * 3600;

  // ---- legend ----------------------------------------------------------------------
  $("legend").replaceChildren(...KINDS.map(([k, label, v]) => { const s = el("span"); const i = el("i"); i.style.background = `var(${v})`; s.append(i, label); return s; }));
  const hs = el("span"); const scale = el("span", "heat-scale");
  for (let i = 1; i <= 7; i++) { const b = el("b"); b.style.background = `var(--seq-${i})`; scale.appendChild(b); }
  hs.append("quiet", scale, "busy"); $("heat-legend").appendChild(hs);

  // ---- data ----------------------------------------------------------------------------
  let stats = null;
  async function load() {
    const bucket = bucketFor(span), tz = -new Date().getTimezoneOffset() * 60;
    const q = `from=${t0}&to=${t1}`;
    [stats] = await Promise.all([fetch(`/api/channel/stats?${q}&bucket=${bucket}&tz=${tz}`).then((r) => r.json()), loadLog()]);
    $("log-csv").href = `/api/channel/log.csv?${q}`;
    renderTiles(); renderBars(); renderHeat(); renderUnits();
  }

  function tile(k, v, s) { const d = el("div", "tile"); d.append(el("div", "k", k), el("div", "v", v), el("div", "s", s || "")); return d; }
  function renderTiles() {
    const T = stats.totals, C = stats.counts, P = stats.poller;
    const answered = P.sent ? Math.round(100 * P.answered / P.sent) + "% answered" : (P.mode === "dry_run" ? `${P.dry_run} planned (test mode)` : "polling is off");
    $("tiles").replaceChildren(
      tile("Channel in use", stats.utilisation_pct.toFixed(1) + "%", "of the selected period"),
      tile("Voice", dur(T.voice), `${C.voice} transmission${C.voice === 1 ? "" : "s"}`),
      tile("Data", dur(T.data), `${C.data} position report${C.data === 1 ? "" : "s"}`),
      tile("Polls", String(C.poll), answered),
      tile("Longest", stats.longest ? dur(stats.longest.duration) : "–",
        stats.longest ? `${stats.longest.kind} · ${nameOf(stats.longest.unit_id)} · ${tm(stats.longest.start, span > 86400)}` : ""));
  }

  function renderBars() {
    const box = $("bars"), B = stats.buckets, W = Math.max(box.clientWidth, 320), H = 220;
    const m = { l: 40, r: 8, t: 10, b: 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const pct = (b) => KINDS.map(([k]) => 100 * b[k] / stats.bucket_s);
    const maxPct = Math.max(5, ...B.map((b) => pct(b).reduce((a, c) => a + c, 0)));
    const top = maxPct <= 10 ? Math.ceil(maxPct / 2) * 2 : maxPct <= 50 ? Math.ceil(maxPct / 10) * 10 : 100;
    const y = (v) => m.t + ih - (v / top) * ih;
    const svg = sv("svg", { width: W, height: H, role: "img", "aria-label": "Channel airtime by period" });
    for (let i = 0; i <= 4; i++) {
      const v = top * i / 4;
      svg.append(sv("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: "gridline" }));
      const t = sv("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end", class: "axis-label" }); t.textContent = (top <= 10 ? v.toFixed(1) : Math.round(v)) + "%";
      svg.append(t);
    }
    const bw = iw / B.length, gap = Math.min(2, bw * 0.25), barW = Math.max(1, bw - gap);
    const fmtT = (t) => span > 86400 * 1.5 ? new Date(t * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" }) + (stats.bucket_s < 86400 ? " " + new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "")
      : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const every = Math.ceil(B.length / Math.max(2, Math.floor(iw / 70)));
    B.forEach((b, i) => {
      const x = m.l + i * bw + gap / 2;
      let acc = 0;
      const vals = pct(b);
      const segs = KINDS.map((k, j) => ({ k, v: vals[j] })).filter((s) => s.v > 0);
      segs.forEach((s, j) => {
        const y0 = y(acc), y1 = y(acc + s.v); acc += s.v;
        const hgt = Math.max(0, y0 - y1 - (j < segs.length - 1 ? 2 : 0));        // 2px surface gap between stacked segments
        const r = j === segs.length - 1 ? Math.min(3, barW / 2) : 0;            // rounded data end at the top only
        const path = r && hgt > r
          ? `M${x},${y0} V${y1 + r} Q${x},${y1} ${x + r},${y1} H${x + barW - r} Q${x + barW},${y1} ${x + barW},${y1 + r} V${y0} Z`
          : `M${x},${y0} V${y0 - hgt} H${x + barW} V${y0} Z`;
        svg.append(sv("path", { d: path, fill: css(s.k[2]) }));
      });
      if (i % every === 0) { const t = sv("text", { x: x + barW / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" }); t.textContent = fmtT(b.t); svg.append(t); }
      const hit = sv("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, class: "hit" });
      hit.addEventListener("mousemove", (ev) => {
        const rows = [el("b", null, `${fmtT(b.t)} – ${fmtT(b.t + stats.bucket_s)}`)];
        KINDS.forEach(([k, label, v], j) => { const r = el("div", "row2"); const sw = el("i"); sw.style.background = css(v); r.append(sw, `${label}: ${dur(b[k])} (${vals[j].toFixed(1)}%)`); rows.push(r); });
        showTip(ev, rows);
      });
      hit.addEventListener("mouseleave", hideTip);
      svg.append(hit);
    });
    box.replaceChildren(svg);
  }

  function renderHeat() {
    const box = $("heat"), Hm = stats.heatmap_pct, W = Math.max(box.clientWidth, 320);
    const m = { l: 36, t: 4, b: 20 }, cw = (W - m.l - 4) / 24, ch = Math.min(26, Math.max(16, cw)), H = m.t + ch * 7 + m.b;
    const svg = sv("svg", { width: W, height: H, role: "img", "aria-label": "Channel use by weekday and hour" });
    const vals = Hm.flat().filter((v) => v != null), max = Math.max(1, ...vals);
    const step = (v) => v == null ? null : v <= 0 ? 0 : Math.min(7, 1 + Math.floor(6 * Math.sqrt(v / max)));
    // rows start Monday; the hour labels every 3 h
    for (let d = 0; d < 7; d++) {
      const t = sv("text", { x: m.l - 6, y: m.t + d * ch + ch / 2 + 4, "text-anchor": "end", class: "axis-label" }); t.textContent = DAYS[d]; svg.append(t);
      for (let h = 0; h < 24; h++) {
        const v = Hm[d][h], s = step(v);
        const r = sv("rect", { x: m.l + h * cw, y: m.t + d * ch, width: Math.max(1, cw), height: ch, class: "cell",
          fill: s == null ? "transparent" : css(`--seq-${s}`), opacity: s == null ? 0 : 1 });
        r.addEventListener("mousemove", (ev) => showTip(ev, [el("b", null, `${DAYS[d]} ${String(h).padStart(2, "0")}:00–${String(h + 1).padStart(2, "0")}:00`),
          el("div", null, v == null ? "Not in the selected period" : `Channel in use ${v.toFixed(1)}% of the time`)]));
        r.addEventListener("mouseleave", hideTip);
        svg.append(r);
      }
    }
    for (let h = 0; h < 24; h += 3) { const t = sv("text", { x: m.l + h * cw + cw / 2, y: H - 5, "text-anchor": "middle", class: "axis-label" }); t.textContent = String(h).padStart(2, "0"); svg.append(t); }
    box.replaceChildren(svg);
  }

  function renderUnits() {
    const tb = $("unit-rows"); tb.replaceChildren();
    const ids = [...new Set([...Object.keys(stats.per_unit), ...Object.keys(stats.coverage)])]
      .sort((a, b) => nameOf(a).localeCompare(nameOf(b), undefined, { numeric: true }));
    if (!ids.length) { const tr = el("tr"); const td = el("td", "empty", "No traffic in this period."); td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr); return; }
    for (const id of ids) {
      const p = stats.per_unit[id] || { data: 0, voice: 0, poll: 0, count: 0 }, c = stats.coverage[id];
      const tr = el("tr");
      const cov = el("td", "n");
      if (c && c.heard + c.missed) {
        const pc = 100 * c.heard / (c.heard + c.missed), w = el("span", "cov"), bar = el("span", "bar"), fill = el("b");
        fill.style.width = pc + "%"; bar.appendChild(fill);
        w.append(bar, `${pc.toFixed(0)}%`); w.title = `${c.heard} heard, ${c.missed} missed`; cov.appendChild(w);
      } else cov.textContent = "–";
      tr.append(el("td", null, nameOf(id)), el("td", "n", p.count), el("td", "n", dur(p.data)), el("td", "n", dur(p.voice)),
        el("td", "n", dur(p.poll)), cov);
      tb.appendChild(tr);
    }
  }

  // ---- log --------------------------------------------------------------------------------
  function logRow(r, fresh) {
    const tr = el("tr", fresh ? "new" : "");
    const k = el("span", "kind"); const i = el("i"); i.style.background = `var(${(KINDS.find((x) => x[0] === r.kind) || KINDS[0])[2]})`;
    k.append(i, (KINDS.find((x) => x[0] === r.kind) || [0, r.kind])[1]);
    const kt = el("td"); kt.appendChild(k);
    tr.append(el("td", null, tm(r.start, span > 86400)), el("td", "n", r.duration < 10 ? r.duration.toFixed(1) + " s" : dur(r.duration)),
      kt, el("td", null, nameOf(r.unit_id)), el("td", "muted", r.display || ""));
    return tr;
  }
  async function loadLog() {
    const kind = $("log-kind").value;
    const rows = await (await fetch(`/api/channel/log?from=${t0}&to=${t1 + 3600}&limit=500${kind ? "&kind=" + kind : ""}`)).json();
    const tb = $("log-rows"); tb.replaceChildren();
    if (!rows.length) { const tr = el("tr"); const td = el("td", "empty", "Nothing heard in this period."); td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr); }
    for (const r of rows) tb.appendChild(logRow(r));
  }
  $("log-kind").onchange = loadLog;

  // ---- live ---------------------------------------------------------------------------------
  let onairTimer = null;
  function setOnAir(d) {
    const o = $("onair"); clearInterval(onairTimer);
    if (!d.busy) { o.className = "onair"; o.lastChild.textContent = "Channel clear"; return; }
    o.className = "onair live";
    const upd = () => { o.lastChild.textContent = `On air${d.unit ? " · " + nameOf(d.unit) : ""}${d.kind === "data" ? " (data)" : ""} · ${Math.max(0, Math.round(Date.now() / 1000 - d.since))} s`; };
    upd(); onairTimer = setInterval(upd, 1000);
  }
  const es = new EventSource("/api/stream");
  es.addEventListener("onair", (m) => setOnAir(JSON.parse(m.data)));
  es.addEventListener("tx", (m) => {
    const r = JSON.parse(m.data), kind = $("log-kind").value;
    if (Date.now() / 1000 - t1 > 120 && span > 0) return;          // looking at the past: leave the log alone
    if (kind && r.kind !== kind) return;
    const tb = $("log-rows"); if (tb.querySelector(".empty")) tb.replaceChildren();
    tb.prepend(logRow(r, true));
  });
  es.addEventListener("signed_out", () => { location.href = "/login"; });

  let resizeT; addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (stats) { renderBars(); renderHeat(); } }, 150); });
  setInterval(() => { if (Date.now() / 1000 - t1 < 120) setRange(currentH); }, 60000);   // keep "last N hours" current

  let start = "24";
  try { start = localStorage.getItem("kt.channelRange") || "24"; } catch (e) { /* private mode */ }
  setRange(start === "today" ? "today" : +start);
})();
