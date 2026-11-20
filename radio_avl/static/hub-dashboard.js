"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const SVG = "http://www.w3.org/2000/svg";
  const key = new URLSearchParams(location.search).get("key") || "";
  if (key) document.body.classList.add("kiosk");
  const n = (x) => (+x || 0).toLocaleString("en-NZ");
  const ago = (t, now) => {
    if (!t) return "never";
    const s = Math.max(0, Math.round(now - t));
    return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
  };
  const dur = (s) => s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : `${Math.floor(s / 86400)} d ${Math.floor(s % 86400 / 3600)} h`;
  const gb = (b) => b == null ? "–" : b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : (b / 1e6).toFixed(0) + " MB";

  const STATES = {
    live: "LIVE", quiet: "QUIET", no_radio: "RADIO DOWN", offline: "GATEWAY OFFLINE",
    never: "NOT CONNECTED", disabled: "DISABLED",
  };
  const ORDER = { offline: 0, no_radio: 1, live: 2, quiet: 3, never: 4, disabled: 5 };
  const SUB = {
    live: "Radio data coming in",
    quiet: "Gateway and radio connected; nothing heard for 5 min (radios off?)",
    no_radio: "Gateway online, but its monitor radio isn't connected",
    offline: "No word from the gateway",
    never: "Gateway hasn't connected yet",
    disabled: "Switched off on the service",
  };

  // clock
  function tick() {
    const d = new Date();
    $("clock").textContent = d.toLocaleTimeString("en-NZ", { hour: "2-digit", minute: "2-digit", hour12: false });
    $("date").textContent = d.toLocaleDateString("en-NZ", { weekday: "long", day: "numeric", month: "long" });
  }
  tick(); setInterval(tick, 1000);

  // full screen, and keep the screen awake on a wall display
  $("fs").onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {});
  document.addEventListener("keydown", (e) => { if (e.key === "f" || e.key === "F") $("fs").click(); });
  let lock = null;
  async function stayAwake() { try { if ("wakeLock" in navigator && !lock) { lock = await navigator.wakeLock.request("screen"); lock.onrelease = () => (lock = null); } } catch (e) { /* not allowed here */ } }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) stayAwake(); });
  stayAwake();

  // bar sparkline with a hover readout. One series: the card title names it, so no legend.
  const tip = el("div", "tip"); tip.hidden = true; document.body.appendChild(tip);
  function bars(values, label) {
    const W = 300, H = 46, gap = 2;
    const max = Math.max(...values, 1);
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    const bw = W / values.length;
    values.forEach((v, i) => {
      const h = v ? Math.max(2, (v / max) * (H - 2)) : 0;
      const hit = document.createElementNS(SVG, "rect");
      hit.setAttribute("class", "hit"); hit.setAttribute("x", i * bw); hit.setAttribute("y", 0);
      hit.setAttribute("width", bw); hit.setAttribute("height", H);
      const b = document.createElementNS(SVG, "rect");
      b.setAttribute("class", "bar"); b.setAttribute("x", i * bw + gap / 2); b.setAttribute("width", Math.max(1, bw - gap));
      b.setAttribute("y", H - h); b.setAttribute("height", h); b.setAttribute("rx", Math.min(2, (bw - gap) / 2));
      const show = (e) => { tip.textContent = label(i, v); tip.hidden = false; tip.style.left = e.clientX + "px"; tip.style.top = e.clientY + "px"; };
      hit.addEventListener("mousemove", show); b.addEventListener("mousemove", show);
      hit.addEventListener("mouseleave", () => (tip.hidden = true)); b.addEventListener("mouseleave", () => (tip.hidden = true));
      svg.append(hit, b);
    });
    const base = document.createElementNS(SVG, "line");
    base.setAttribute("class", "base"); base.setAttribute("x1", 0); base.setAttribute("x2", W); base.setAttribute("y1", H - 0.5); base.setAttribute("y2", H - 0.5);
    svg.appendChild(base);
    return svg;
  }

  function chart(title, total, values, axis, label, emptyText) {
    const c = el("div", "chart");
    const k = el("div", "k"); k.append(el("span", null, title), el("b", null, total)); c.appendChild(k);
    if (!values.length || !values.some((v) => v)) c.appendChild(el("div", "none", emptyText));
    else c.appendChild(bars(values, label));
    const ax = el("div", "axis"); ax.append(el("span", null, axis[0]), el("span", null, axis[1])); c.appendChild(ax);
    return c;
  }

  function stat(k, v, small, cls) {
    const s = el("div", "stat" + (cls ? " " + cls : ""));
    s.title = { Radios: "Radios heard in the last 15 minutes, of all the company's radios",
                Packets: "Radio packets a minute, averaged over the last 5 minutes",
                "Fixes today": "Position reports since midnight" }[k] || "";
    const vv = el("div", "v", v); if (small) vv.append(" ", el("small", null, small));
    s.append(el("div", "k", k), vv);
    return s;
  }

  function tile(k, v, s, cls, small) {
    const t = el("div", "tile" + (cls ? " " + cls : ""));
    const vv = el("div", "v", v); if (small) vv.append(" ", el("small", null, small));
    t.append(el("div", "k", k), vv, el("div", "s", s || ""));
    return t;
  }

  function card(c, d) {
    const emerg = (c.emergencies || []).length;
    const box = el("article", `co ${c.state}` + (emerg ? " emerg" : ""));
    const head = el("div", "co-head");
    const pill = el("span", "pill"); pill.append(el("i"), STATES[c.state] || c.state);
    head.append(el("div", "co-name", c.name), pill);
    box.appendChild(head);
    const sub = el("div", "co-sub");
    if (c.state === "disabled") sub.textContent = SUB.disabled;
    else { sub.append(SUB[c.state] + " · last radio data "); sub.append(el("b", null, ago(c.last_frame, d.now))); }
    box.appendChild(sub);
    if (c.state === "disabled") return box;

    const st = el("div", "stats");
    st.append(
      stat("Radios", n(c.heard_15m), `/ ${n(c.units_total)}`),
      stat("Packets", c.packets_rate.toLocaleString("en-NZ", { maximumFractionDigits: 1 }), "/min"),
      stat("Fixes today", n(c.positions_today)),
      emerg ? stat("Emergency", String(emerg), "open", "bad")
            : stat("Alerts", n(Object.values(c.alerts || {}).reduce((a, b) => a + b, 0)), "", (c.alerts || {}).warn ? "warn" : ""));
    box.appendChild(st);

    const h = new Date(d.now * 1000).getHours();
    const charts = el("div", "charts");
    const pk = c.packets_60m || [];
    charts.append(
      chart("Packets per minute", `${n(pk.reduce((a, b) => a + b, 0))} / hour`, pk,
            [`${pk.length || 60} min ago`, "now"], (i, v) => `${pk.length - i} min ago: ${v} packets`, "No radio packets in the last hour"),
      chart("Positions per hour", `${n(c.positions_hourly.reduce((a, b) => a + b, 0))} / 24 h`, c.positions_hourly,
            ["24 h ago", "now"], (i, v) => { const hh = (h - 23 + i + 48) % 24; return `${String(hh).padStart(2, "0")}:00: ${n(v)} positions`; },
            "No positions in the last 24 hours"));
    box.appendChild(charts);

    const g = c.gateway || {};
    const f = el("div", "co-foot");
    const bit = (k, v) => { const s = el("span"); s.append(k + " ", el("b", null, v)); f.appendChild(s); };
    bit("Gateway", g.last ? (g.version ? `v${g.version}, ` : "") + ago(g.last, d.now) : "never");
    if (g.backlog) bit("Catching up", `${n(g.backlog)} waiting`);
    bit("Radios today", n(c.heard_today));
    bit("Calls today", n(c.calls_today));
    if (c.phones_15m) bit("Phones", n(c.phones_15m));
    if (emerg) bit("Emergency", c.emergencies.map((e) => e.alias || e.unit).join(", "));
    if (c.billing) bit("Month so far", `${c.billing.currency} ${(+c.billing.to_date).toFixed(2)}`);
    box.appendChild(f);
    return box;
  }

  function render(d) {
    document.title = `${d.title} status`;
    $("title").textContent = d.title;
    const T = d.totals;

    const em = d.companies.filter((c) => (c.emergencies || []).length);
    $("emergency").hidden = !em.length;
    if (em.length) {
      $("emergency").replaceChildren(el("span", null, "⚠ EMERGENCY"),
        el("span", "what", em.map((c) => `${c.name}: ${c.emergencies.map((e) => e.alias || e.unit).join(", ")}`).join(" · ")));
    }

    $("tiles").replaceChildren(
      tile("Companies live", String(T.live), `${T.gateways_online} of ${T.companies} gateways online`, "", `/ ${T.companies}`),
      tile("Radios heard", n(T.heard_15m), `in the last 15 min, of ${n(T.units)} known`),
      tile("Packets / min", T.packets_rate.toLocaleString("en-NZ", { maximumFractionDigits: 1 }), "average over the last 5 min"),
      tile("Positions today", n(T.positions_today), "since midnight"),
      tile("Calls today", n(T.calls_today), "voice calls since midnight"),
      T.emergencies ? tile("Emergencies", String(T.emergencies), "open now", "alarm")
                    : tile("Open alerts", n(T.alerts_open), T.alerts_open ? "waiting for someone" : "nothing waiting"));

    const comps = d.companies.slice().sort((a, b) =>
      ((b.emergencies || []).length - (a.emergencies || []).length) || (ORDER[a.state] - ORDER[b.state]) || a.name.localeCompare(b.name));
    const grid = $("grid");
    grid.replaceChildren(...comps.map((c) => card(c, d)));
    if (!comps.length) grid.replaceChildren(el("div", "empty", "No companies yet. Add one on the admin page."));

    const H = d.host;
    const parts = [
      ["Server up", dur(d.now - d.started)],
      ["Load", H.load == null ? "–" : `${H.load.toFixed(2)} (${H.cpus || "?"} CPUs)`, H.load != null && H.cpus && H.load > H.cpus],
      ["Memory free", H.mem_total ? `${gb(H.mem_free)} of ${gb(H.mem_total)}` : "–", H.mem_total && H.mem_free / H.mem_total < 0.1],
      ["Disk free", H.disk_total ? `${gb(H.disk_free)} of ${gb(H.disk_total)}` : "–", H.disk_total && H.disk_free / H.disk_total < 0.1],
      ["Databases", gb(H.data_bytes)],
      ["Version", d.version],
    ];
    $("host").replaceChildren(...parts.map(([k, v, bad]) => { const s = el("span", bad ? "bad" : ""); s.append(k + " ", el("b", null, v)); return s; }));
    if (d.now - d.started < 3600) $("host").appendChild(el("span", null, "Packet counts start from when the server started."));
  }

  let lastOk = 0;
  async function load() {
    try {
      const r = await fetch("/hub/api/dashboard" + (key ? "?key=" + encodeURIComponent(key) : ""), { cache: "no-store" });
      if (r.status === 401) {
        $("error").hidden = false;
        $("error").textContent = key ? "This display link has been turned off. Make a new one on the admin page." : "Sign in on the admin page first.";
        if (!key) setTimeout(() => (location.href = "/hub"), 2500);
        return;
      }
      const d = await r.json();
      render(d);
      lastOk = Date.now();
      $("error").hidden = true;
    } catch (e) {
      $("error").hidden = false;
      $("error").textContent = "Can't reach the service. Retrying…";
    }
  }
  setInterval(() => { $("updated").textContent = lastOk ? `updated ${Math.round((Date.now() - lastOk) / 1000)} s ago` : "connecting…"; }, 1000);
  load(); setInterval(load, 10000);
})();
