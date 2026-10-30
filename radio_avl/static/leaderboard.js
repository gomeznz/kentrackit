"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#c026d3", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#e11d48"];
  const colourOf = (u) => { if (u.colour) return u.colour; let h = 0; for (const c of u.id) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; };
  const store = { get: (k, d) => { try { return localStorage.getItem("kt.lb." + k) ?? d; } catch (e) { return d; } },
                  set: (k, v) => { try { localStorage.setItem("kt.lb." + k, v); } catch (e) { /* private mode */ } } };
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const cfg = await (await fetch("/api/config")).json();
  $("site").textContent = cfg.title; document.title = "Most Active · " + cfg.title;

  const hm = (s) => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h} h ${String(m).padStart(2, "0")} m` : m ? `${m} m ${String(sec).padStart(2, "0")} s` : `${sec} s`; };
  const METRICS = {
    score: { label: "points", fmt: (v) => Math.round(v), unitLbl: "activity score", help: "Overall score: distance, time moving, talk time and calls, each compared with the busiest unit, averaged out of 100." },
    distance_km: { label: "km", fmt: (v) => v.toFixed(v >= 100 ? 0 : 1), unitLbl: "kilometres", help: "Distance driven, measured between position reports." },
    moving_s: { label: "", fmt: hm, unitLbl: "time moving", help: "Time spent moving (between stops of 5 minutes or more)." },
    talk_s: { label: "", fmt: hm, unitLbl: "on air", help: "Total voice talk time on the radio." },
    calls: { label: "calls", fmt: (v) => String(Math.round(v)), unitLbl: "calls", help: "Number of voice transmissions." },
  };
  let metric = store.get("metric", "score"), range = store.get("range", "today"), data = null, cycling = store.get("cycle", "0") === "1", cycleT = null;

  function period() {
    const now = Date.now() / 1000, d = new Date();
    if (range === "today") { d.setHours(0, 0, 0, 0); return [d.getTime() / 1000, now, "Today"]; }
    const days = +range; return [now - days * 86400, now, `Last ${days} days`];
  }

  // count-up animation
  function countUp(node, to, fmt) {
    if (reduced) { node.textContent = fmt(to); return; }
    const start = performance.now(), dur = 1100;
    const step = (t) => { const f = Math.min(1, (t - start) / dur), e = 1 - Math.pow(1 - f, 3);
      node.textContent = fmt(to * e); if (f < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }

  function render() {
    const M = METRICS[metric];
    for (const b of $("metrics").children) b.classList.toggle("on", b.dataset.m === metric);
    const rows = [...data.units].sort((a, b) => b[metric] - a[metric] || b.score - a.score || a.name.localeCompare(b.name));
    const top = Math.max(1e-9, ...rows.map((r) => r[metric]));
    const active = rows.filter((r) => r[metric] > 0);
    $("empty").hidden = active.length > 0;

    // podium: 2nd, 1st, 3rd
    const pod = $("podium"); pod.replaceChildren();
    [[1, "p2", 0.25], [0, "p1", 0], [2, "p3", 0.45]].forEach(([i, cls, delay]) => {
      const r = rows[i], s = el("div", `step ${cls}`);
      if (!r || r[metric] <= 0) { s.classList.add("vacant"); s.append(el("div", "medal", String(i + 1)), el("div", "name", "—")); pod.appendChild(s); return; }
      if (i === 0) s.appendChild(el("div", "crown", "👑"));
      const uid = el("div", "uid"); const dot = el("i"); dot.style.background = colourOf(r); uid.append(dot, r.id);
      const val = el("div", "val", "0");
      const extra = el("div", "extra");
      for (const [k, txt] of [["distance_km", `${r.distance_km.toFixed(1)} km`], ["moving_s", `${hm(r.moving_s)} moving`], ["talk_s", `${hm(r.talk_s)} on air`], ["calls", `${r.calls} calls`]])
        if (k !== metric) extra.appendChild(el("span", null, txt));
      s.append(el("div", "medal", String(i + 1)), el("div", "name", r.name), uid, val, el("div", "unit-lbl", M.unitLbl), extra);
      s.style.animationDelay = delay + "s";
      pod.appendChild(s);
      requestAnimationFrame(() => s.classList.add("in"));
      setTimeout(() => countUp(val, r[metric], (v) => M.fmt(v) + (M.label && metric !== "calls" ? " " + M.label : "")), delay * 1000 + 250);
    });

    // positions 4+
    const ol = $("ranks"); ol.replaceChildren();
    rows.slice(3).forEach((r, k) => {
      const li = el("li", r[metric] > 0 ? "" : "quiet");
      const who = el("div", "who"); who.append(el("b", null, r.name), el("span", null, r.id));
      const bar = el("div", "bar"); const fill = el("i"); bar.appendChild(fill);
      fill.style.background = `linear-gradient(90deg, ${colourOf(r)}, var(--cyan))`;
      const num = el("div", "num", "0");
      li.append(el("div", "pos", String(k + 4)), who, bar, num);
      li.style.animationDelay = (0.6 + k * 0.07) + "s";
      ol.appendChild(li);
      requestAnimationFrame(() => { li.classList.add("in"); setTimeout(() => { fill.style.width = (100 * r[metric] / top) + "%"; }, 600 + k * 70); });
      setTimeout(() => countUp(num, r[metric], (v) => M.fmt(v) + (M.label && metric !== "calls" && metric !== "score" ? " " + M.label : metric === "score" ? " pts" : "")), 700 + k * 70);
    });
    $("foot").textContent = M.help + "  ·  Updates every minute.";
  }

  async function load() {
    const [t0, t1, label] = period();
    $("period").textContent = label;
    for (const b of $("ranges").children) b.classList.toggle("on", b.dataset.r === range);
    data = await (await fetch(`/api/leaderboard?from=${t0}&to=${t1}`)).json();
    render();
  }

  for (const b of $("metrics").children) b.onclick = () => { metric = b.dataset.m; store.set("metric", metric); setCycle(false); render(); };
  for (const b of $("ranges").children) b.onclick = () => { range = b.dataset.r; store.set("range", range); load(); };

  function setCycle(on) {
    cycling = on; store.set("cycle", on ? "1" : "0"); $("cycle").classList.toggle("on", on);
    clearInterval(cycleT);
    if (on) cycleT = setInterval(() => { const keys = Object.keys(METRICS); metric = keys[(keys.indexOf(metric) + 1) % keys.length]; render(); }, 15000);
  }
  $("cycle").onclick = () => setCycle(!cycling);

  const fs = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(() => {});
  $("fs").onclick = fs;
  document.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") fs();
    const keys = Object.keys(METRICS);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      metric = keys[(keys.indexOf(metric) + (e.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length];
      store.set("metric", metric); render();
    }
  });
  // a window opened from Reports goes full screen on the first click or key press
  if (window.opener && document.documentElement.requestFullscreen) {
    const go = () => { if (!document.fullscreenElement) fs(); };
    document.addEventListener("pointerdown", go, { once: true });
  }

  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }, 1000);
  setInterval(load, 60000);
  setCycle(cycling);
  load();
})();
