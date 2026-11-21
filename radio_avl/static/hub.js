"use strict";
(async function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const api = async (path, opts = {}) => {
    const r = await fetch("/hub/api" + path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `Error ${r.status}`);
    return j;
  };
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let toastTimer;
  const toast = (msg, err) => { const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3500); };
  const dur = (s) => s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min" : s < 86400 ? Math.floor(s / 3600) + " h" : Math.floor(s / 86400) + " d";
  const ago = (t) => t ? dur(Math.max(0, Math.round(Date.now() / 1000 - t))) + " ago" : "never";
  const when = (t) => t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "–";
  const money = (x) => (+x).toLocaleString("en-NZ", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const monthName = (m) => { const [y, mo] = m.split("-").map(Number); return new Date(y, mo - 1, 1).toLocaleDateString("en-NZ", { month: "long", year: "numeric" }); };
  const thisMonth = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
  const bytes = (n) => n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

  // ---- signed out -------------------------------------------------------------------------
  let STATE = await api("/state");
  if (!STATE.user) {
    if (window.KTWallpaper) KTWallpaper($("wall"));
    $("signin").hidden = false;
    const setup = STATE.setup;
    $("setup-intro").hidden = !setup; $("f-name-wrap").hidden = !setup;
    $("f-btn").textContent = setup ? "Create account" : "Sign in";
    if (setup) $("f-pass").autocomplete = "new-password";
    $("f").onsubmit = async (ev) => {
      ev.preventDefault(); $("f-err").hidden = true; $("f-btn").disabled = true;
      try {
        const body = { username: $("f-user").value.trim(), password: $("f-pass").value };
        if (setup) await post("/setup", Object.assign({ display_name: $("f-name").value.trim() }, body));
        await post("/login", body);
        location.reload();
      } catch (e) { $("f-err").textContent = e.message; $("f-err").hidden = false; $("f-btn").disabled = false; }
    };
    return;
  }

  // ---- signed in ------------------------------------------------------------------------------
  document.body.className = "settings hub-in";
  $("main").hidden = false;
  $("h-title").textContent = STATE.title;
  document.title = STATE.title + " service";
  $("h-me").textContent = STATE.user.display_name || STATE.user.username;
  $("signout").onclick = async () => { await post("/logout"); location.reload(); };

  // companies
  let open = null;                   // slug whose manage form is open
  async function loadCompanies() {
    const { companies } = await api("/companies");
    const list = $("co-list"); list.replaceChildren();
    if (!companies.length) { list.appendChild(el("div", "co-empty", "No companies yet. Add the first one above.")); return; }
    for (const c of companies) list.appendChild(companyRow(c));
  }

  function gatewayBadge(c) {
    const g = c.gateway || {};
    const b = el("span", "gw");
    let text;
    if (!c.enabled) { text = "Disabled"; }
    else if (!g.last) { text = "Gateway not connected yet"; }
    else if (!g.online) { b.classList.add("down"); text = `Gateway offline · last ${ago(g.last)}`; }
    else if (!(g.link || {}).up) { b.classList.add("radio-off"); text = "Gateway online · radio not connected"; }
    else { b.classList.add("on"); text = "Gateway online" + (g.backlog ? ` · catching up (${g.backlog})` : ""); }
    b.append(el("span", "led"), el("span", null, text));
    return b;
  }

  function companyRow(c) {
    const box = el("div", "co" + (c.enabled ? "" : " off"));
    const top = el("div", "co-top");
    top.append(el("span", "co-name", c.name), el("span", "co-slug", c.slug), gatewayBadge(c));
    const acts = el("div", "co-acts");
    const supp = el("button", null, "Open");
    supp.title = "Open this company's KenTrackIt as ASI support (4 hours, recorded in their audit log)";
    supp.disabled = !c.enabled;
    supp.onclick = async () => {
      if (!confirm(`Open ${c.name}'s KenTrackIt as ASI support? This is recorded in their audit log.`)) return;
      const w = window.open("about:blank", "_blank");     // opened now, or the browser blocks it as a pop-up
      try { await post(`/companies/${c.slug}/support`); if (w) w.location = "/"; else location.href = "/"; }
      catch (e) { if (w) w.close(); toast(e.message, true); }
    };
    const man = el("button", null, open === c.slug ? "Close" : "Manage");
    man.onclick = () => { open = open === c.slug ? null : c.slug; loadCompanies(); };
    acts.append(supp, man);
    top.appendChild(acts);
    box.appendChild(top);

    const st = el("div", "co-stats");
    const stat = (label, v, cls) => { const s = el("span", cls); s.append(label + " ", el("b", null, v)); st.appendChild(s); };
    if (c.enabled) {
      stat("Units", String(c.units ?? "–"));
      stat("Users", String(c.users ?? "–"));
      stat("Positions", (c.positions ?? 0).toLocaleString());
      stat("Last radio data", ago(c.last_frame));
      if (c.open_alerts) stat("Open alerts", String(c.open_alerts), "alerts");
    }
    if (c.domains.length) stat("Web address", c.domains.join(", "));
    if (c.billing && c.billing.plan_text) stat("This month so far", `${c.billing.currency} ${money(c.billing.to_date)}`, "bill-line");
    box.appendChild(st);
    if (open === c.slug) { box.appendChild(manageForm(c)); box.appendChild(rateBox(c)); }
    return box;
  }

  function manageForm(c) {
    const f = el("form", "sub-form");
    f.innerHTML = `
      <label>Company name <input name="name" maxlength="80" required></label>
      <label>Its own web addresses (optional) <input name="domains" placeholder="sdc.track.asiltd.co.nz">
        <span class="hint">Point the address at this service (e.g. another Cloudflare tunnel hostname). People on it can only sign in to this company. Separate several with commas.</span></label>
      <label>Notes <input name="notes" maxlength="1000" placeholder="contract, contacts, radios…"></label>
      <label class="check"><input type="checkbox" name="enabled"> Enabled (unticked: its users can't sign in and its gateway is refused; nothing is deleted)</label>
      <div class="status-grid">
        <div><span class="k">Gateway key</span><span class="gk"></span></div>
        <div><span class="k">Gateway</span><span class="gi"></span></div>
        <div><span class="k">Created</span><span class="cr"></span></div>
        <div><span class="k">Data folder</span><span class="df"></span></div>
      </div>
      <div class="actions"><button type="button" class="nk danger">New gateway key</button><button type="submit">Save</button></div>
      <h3>History from a KenTrackIt box</h3>
      <p class="help">Bring in what the site's box recorded before it was connected here: units, positions, calls,
        alerts, zones and user accounts (with their passwords). On the box, use <b>Settings → Download backup</b>, then
        choose that file here. Only history from before this company's hosted data starts is added, so doing it twice
        adds nothing. Recordings stay on the box. Files over 100 MB need the server's own network address
        (Cloudflare limits uploads).</p>
      <div class="row"><input type="file" class="imp-file" accept=".db"><button type="button" class="imp">Import</button></div>
      <p class="help imp-out" hidden></p>`;
    f.name.value = c.name; f.domains.value = c.domains.join(", "); f.notes.value = c.notes || ""; f.enabled.checked = c.enabled;
    const g = c.gateway || {};
    f.querySelector(".gk").textContent = c.token_hint ? `ends in …${c.token_hint}` : "–";
    f.querySelector(".gi").textContent = g.last ? `${g.version || "?"} from ${g.ip || "?"}, ${ago(g.last)}` + (g.port ? ` (${g.port})` : "") : "never connected";
    f.querySelector(".cr").textContent = when(c.created);
    f.querySelector(".df").textContent = "companies/" + c.slug;
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      if (!f.enabled.checked && c.enabled && !confirm(`Disable ${c.name}? Its users are signed out and its gateway is refused until you enable it again.`)) return;
      try { await post(`/companies/${c.slug}`, { name: f.name.value, domains: f.domains.value, notes: f.notes.value, enabled: f.enabled.checked });
            toast("Saved"); open = null; loadCompanies(); loadActivity(); }
      catch (e) { toast(e.message, true); }
    };
    f.querySelector(".imp").onclick = async () => {
      const file = f.querySelector(".imp-file").files[0];
      if (!file) return toast("Choose the backup file from the box first", true);
      if (!confirm(`Import ${file.name} (${bytes(file.size)}) into ${c.name}?`)) return;
      const b = f.querySelector(".imp"), out = f.querySelector(".imp-out");
      b.disabled = true; b.textContent = "Importing…"; out.hidden = true;
      try {
        const r = await api(`/companies/${c.slug}/import`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
        const n = (x) => (+x).toLocaleString();
        let txt = `Imported ${n(r.positions)} positions, ${n(r.calls)} calls, ${n(r.alerts)} alerts and ${n(r.coverage)} coverage points; ` +
                  `${r.units_new} new units, ${r.units_merged} merged; ${r.zones_new} zones added` + (r.zones_same ? ` (${r.zones_same} already here)` : "") + ".";
        if (r.users_new.length) txt += ` Users added (same passwords as on the box): ${r.users_new.join(", ")}.`;
        if (r.users_skipped.length) txt += ` Users not added: ${r.users_skipped.join(", ")}.`;
        out.textContent = txt; out.hidden = false;
        f.querySelector(".imp-file").value = ""; loadActivity();
      } catch (e) { toast(e.message, true); }
      b.disabled = false; b.textContent = "Import";
    };
    f.querySelector(".nk").onclick = async () => {
      if (!confirm(`Issue a new gateway key for ${c.name}? The current key stops working straight away, so the site's Pi needs the new one.`)) return;
      try { const r = await post(`/companies/${c.slug}/token`); showKey(c.name, r.token); loadActivity(); }
      catch (e) { toast(e.message, true); }
    };
    return f;
  }

  // ---- a company's rate --------------------------------------------------------------------------
  const ITEMS = [["radio", "Per radio", "radios"], ["user", "Per user", "users"], ["phone", "Per phone", "phones"]];

  function tierEditor(unit, tiers) {
    const box = el("div", "tiers");
    const rows = [];
    function redraw() {
      box.replaceChildren();
      let lo = 1;
      rows.forEach((r, i) => {
        const last = i === rows.length - 1;
        const line = el("div", "tier");
        const upto = el("input"); upto.type = "number"; upto.min = lo; upto.value = last ? "" : (r.upto ?? "");
        upto.placeholder = "up to"; upto.onchange = () => { r.upto = upto.value === "" ? null : +upto.value; redraw(); };
        const price = el("input"); price.type = "number"; price.min = 0; price.step = "0.01"; price.value = r.price ?? ""; price.placeholder = "price";
        price.oninput = () => { r.price = price.value; };
        const rng = el("span", "rng", rows.length === 1 ? `Each of the ${unit}` : last ? `${unit} ${lo} and up` : `${unit} ${lo} to`);
        line.append(rng);
        if (!last) line.append(upto);
        line.append(el("span", null, "at"), price, el("span", null, "each"));
        const rm = el("button", "link", "remove"); rm.type = "button"; rm.onclick = () => { rows.splice(i, 1); redraw(); };
        line.append(rm);
        box.appendChild(line);
        if (!last) lo = (+r.upto || lo) + 1;
      });
      const add = el("button", "link", rows.length ? "+ add a price tier" : "+ charge per " + unit.replace(/s$/, ""));
      add.type = "button";
      add.onclick = () => {
        if (rows.length) { const prev = rows[rows.length - 1]; prev.upto = prev.upto || (rows.length > 1 ? (+rows[rows.length - 2].upto || 0) + 10 : 10); }
        rows.push({ upto: null, price: "" }); redraw();
      };
      box.appendChild(add);
    }
    for (const t of tiers || []) rows.push({ upto: t.upto, price: t.price });
    redraw();
    box.value = () => rows.map((r, i) => ({ upto: i === rows.length - 1 ? null : r.upto, price: r.price === "" ? null : +r.price }));
    return box;
  }

  function rateBox(c) {
    const box = el("div", "sub-form rate-box");
    box.appendChild(el("h3", null, "Charges"));
    const hist = el("ul", "rate-hist"); box.appendChild(hist);
    const form = el("div");
    box.appendChild(form);
    (async () => {
      const { rates, currency } = await api(`/companies/${c.slug}/rates`);
      const now = thisMonth();
      const active = rates.filter((r) => r.start_month <= now).pop();
      if (!rates.length) hist.appendChild(el("li", null, "Not charged: no rate set."));
      for (const r of rates.slice().reverse()) {
        const li = el("li");
        li.append(el("span", "m", "from " + r.start_month));
        if (r === active) li.append(el("span", "now", "NOW"));
        if (r.start_month > now) li.append(el("span", "soon", "SCHEDULED"));
        li.append(el("span", null, describe(r.plan, currency)), el("span", "by", `${r.set_by || ""} ${new Date(r.set_at * 1000).toLocaleDateString()}`));
        if (r.start_month > now) {
          const x = el("button", "link", "remove"); x.type = "button";
          x.onclick = async () => { try { await api(`/companies/${c.slug}/rates/${r.id}`, { method: "DELETE" }); loadCompanies(); } catch (e) { toast(e.message, true); } };
          li.append(x);
        }
        hist.appendChild(li);
      }
      const base = (active && active.plan) || {};
      const f = el("div");
      f.innerHTML = `
        <div class="two">
          <label>New rate starts <input type="month" class="r-from" required></label>
          <label>Monthly fee (${currency}, excl. GST) <input type="number" class="r-flat" min="0" step="0.01" placeholder="0"></label>
        </div>`;
      const from = f.querySelector(".r-from"); from.value = now; from.min = now;
      f.querySelector(".r-flat").value = base.flat || "";
      const eds = {};
      for (const [key, label, unit] of ITEMS) {
        f.appendChild(el("div", "item-head", label));
        eds[key] = tierEditor(unit, base[key]); f.appendChild(eds[key]);
      }
      const note = el("label"); note.append("Note (shows on the company's statement) ");
      const ni = el("input"); ni.maxLength = 200; ni.value = base.note || ""; note.appendChild(ni); f.appendChild(note);
      const acts = el("div", "actions");
      const off = el("button", "danger", "No charge from then"); off.type = "button";
      const save = el("button", "primary-btn", "Save rate"); save.type = "button";
      const send = async (plan) => {
        try { await post(`/companies/${c.slug}/rates`, { start_month: from.value, plan }); toast(`Rate saved from ${from.value}`); loadCompanies(); loadBilling(); loadActivity(); }
        catch (e) { toast(e.message, true); }
      };
      save.onclick = () => {
        const plan = { flat: +f.querySelector(".r-flat").value || 0, note: ni.value };
        for (const [key] of ITEMS) plan[key] = eds[key].value();
        send(plan);
      };
      off.onclick = () => { if (confirm(`Stop charging ${c.name} from ${from.value}?`)) send(null); };
      acts.append(off, save); f.appendChild(acts);
      f.appendChild(el("p", "help", "A new rate applies from the start of its month; earlier months keep the rate they had. " +
        "Tiers are like tax brackets: with 1-10 at 20 and 11 and up at 15, 13 radios cost 10 × 20 + 3 × 15."));
      form.appendChild(f);
    })().catch((e) => toast(e.message, true));
    return box;
  }

  function describe(plan, cur) {
    if (!plan) return "No charge";
    const parts = [];
    if (plan.flat) parts.push(`${cur} ${money(plan.flat)} a month`);
    for (const [key, , unit] of ITEMS) {
      const t = plan[key]; if (!t || !t.length) continue;
      if (t.length === 1) { parts.push(`${money(t[0].price)} per ${unit.replace(/s$/, "")}`); continue; }
      let lo = 1; const bits = [];
      for (const x of t) { bits.push(`${x.upto ? lo + "-" + x.upto : lo + "+"} at ${money(x.price)}`); lo = (x.upto || 0) + 1; }
      parts.push(`${unit}: ${bits.join(", ")}`);
    }
    return parts.join(" + ");
  }

  // ---- billing ----------------------------------------------------------------------------------------
  let billOpen = null;
  async function loadBilling() {
    const month = $("bill-month").value || thisMonth();
    const b = await api("/billing?month=" + month);
    $("bill-csv").href = "/hub/api/billing.csv?month=" + month;
    $("bc-cur").value = b.currency; $("bc-gst").value = b.gst_pct;
    const tb = $("bill-rows"); tb.replaceChildren();
    const charged = b.statements.filter((s) => s.plan || s.total);
    if (!charged.length) {
      const tr = el("tr"); const td = el("td", "empty", `Nobody is charged for ${monthName(month)}. Set a rate under Manage.`); td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr);
    }
    let sub = 0, tot = 0;
    for (const s of charged) {
      sub += s.subtotal; tot += s.total;
      const tr = el("tr", "co-bill");
      const nm = el("td"); nm.append(el("div", "uname", s.company), el("div", "usub", s.plan_text));
      tr.append(nm, el("td", "n", String(s.usage.radios)), el("td", "n", String(s.usage.users)), el("td", "n", String(s.usage.phones)),
                el("td", "n", money(s.subtotal)), el("td", "n", money(s.total)));
      tr.onclick = () => { billOpen = billOpen === s.slug ? null : s.slug; loadBilling(); };
      tb.appendChild(tr);
      if (billOpen === s.slug) {
        const lr = el("tr", "lines"); const td = el("td"); td.colSpan = 6;
        const t = el("table");
        for (const ln of s.lines) {
          const r = el("tr");
          r.append(el("td", null, ln.item + (ln.detail ? ` (${ln.detail})` : "")), el("td", "n", `${ln.qty} × ${money(ln.price)}`), el("td", "n", money(ln.amount)));
          t.appendChild(r);
        }
        const g = el("tr"); g.append(el("td", null, `GST ${s.gst_pct} %`), el("td"), el("td", "n", money(s.gst))); t.appendChild(g);
        td.appendChild(t);
        if (s.usage.radio_ids.length) td.appendChild(el("p", "help", "Radios heard: " + s.usage.radio_ids.join(", ")));
        lr.appendChild(td); tb.appendChild(lr);
      }
    }
    const foot = $("bill-foot"); foot.replaceChildren();
    if (charged.length) {
      const tr = el("tr");
      const lbl = el("td"); lbl.append(`Total ${b.currency}`); if (month === b.current) lbl.append(" ", el("span", "to-date", "(month so far)"));
      const e1 = el("td"); e1.colSpan = 3;
      tr.append(lbl, e1, el("td", "n", money(sub)), el("td", "n", money(tot))); foot.appendChild(tr);
    }
  }
  $("bill-month").value = thisMonth();
  $("bill-month").max = thisMonth();
  $("bill-month").onchange = () => { billOpen = null; loadBilling(); };
  $("bill-cfg").onsubmit = async (ev) => {
    ev.preventDefault();
    try { await post("/billing/settings", { currency: $("bc-cur").value.trim(), gst_pct: +$("bc-gst").value }); toast("Saved"); loadBilling(); loadActivity(); }
    catch (e) { toast(e.message, true); }
  };

  function showKey(name, token) {
    $("kb-title").textContent = `Gateway key for ${name}`;
    $("kb-url").textContent = location.origin;
    $("kb-key").textContent = token;
    $("keybox").hidden = false;
    $("keybox").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  $("kb-done").onclick = () => { $("keybox").hidden = true; $("kb-key").textContent = ""; };
  // iPhone push key
  let hpKey = "";
  async function loadPush() {
    const p = await api("/push");
    $("hp-kid").value = p.key_id; $("hp-team").value = p.team_id; $("hp-bundle").value = p.bundle_id; $("hp-crit").checked = p.critical;
    $("hp-st").textContent = !p.available ? "Not available: install python3-httpx, python3-h2 and python3-cryptography"
      : !p.key_set ? "No Apple key yet" : p.critical ? "Ready (Critical Alerts)" : "Ready";
    $("hp-phones").textContent = p.phones;
    $("hp-last").textContent = p.last_ok ? when(p.last_ok) : "never";
    $("hp-err").textContent = p.last_error || "none";
    $("hp-rm").hidden = !p.key_set;
    hpKey = ""; $("hp-file").value = "";
  }
  $("hp-file").onchange = async () => {
    const f = $("hp-file").files[0];
    hpKey = f ? await f.text() : "";
    const m = f && f.name.match(/AuthKey_([A-Z0-9]{10})\.p8$/i);
    if (m && !$("hp-kid").value) $("hp-kid").value = m[1].toUpperCase();
  };
  $("push-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { key_id: $("hp-kid").value.trim(), team_id: $("hp-team").value.trim(), bundle_id: $("hp-bundle").value.trim(), critical: $("hp-crit").checked };
    if (hpKey) body.key = hpKey;
    try { await post("/push", body); toast("Saved"); loadPush(); loadActivity(); } catch (e) { toast(e.message, true); }
  };
  $("hp-rm").onclick = async () => {
    if (!confirm("Remove the Apple push key? iPhones stop getting alerts with the app closed.")) return;
    try { await post("/push", { remove_key: true }); loadPush(); loadActivity(); } catch (e) { toast(e.message, true); }
  };
  loadPush().catch(() => {});

  // wall display link
  async function loadDisplay() {
    const r = await api("/display-key");
    $("dk-st").textContent = r.on ? "On (anyone with the link can see the dashboard)" : "Off";
    $("dk-off").hidden = !r.on;
    $("dk-new").textContent = r.on ? "Make a new link" : "Make a display link";
  }
  $("dk-new").onclick = async () => {
    try { const r = await post("/display-key"); $("dk-url").textContent = `${location.origin}/hub/dashboard?key=${r.key}`; $("dk-out").hidden = false; loadDisplay(); loadActivity(); }
    catch (e) { toast(e.message, true); }
  };
  $("dk-off").onclick = async () => {
    if (!confirm("Turn off the display link? Screens using it stop updating.")) return;
    try { await api("/display-key", { method: "DELETE" }); $("dk-out").hidden = true; loadDisplay(); loadActivity(); } catch (e) { toast(e.message, true); }
  };
  loadDisplay().catch(() => {});

  for (const b of document.querySelectorAll("[data-copy]")) {
    b.onclick = async () => {
      try { await navigator.clipboard.writeText($(b.dataset.copy).textContent); toast("Copied"); }
      catch (e) { toast("Select it and copy by hand", true); }
    };
  }

  // add a company
  $("co-add-btn").onclick = () => { $("co-add").hidden = false; $("co-add-btn").hidden = true; $("ca-name").focus(); };
  $("ca-cancel").onclick = () => { $("co-add").reset(); $("co-add").hidden = true; $("co-add-btn").hidden = false; };
  let slugTouched = false;
  $("ca-slug").oninput = () => { slugTouched = true; };
  $("ca-name").oninput = () => {
    if (slugTouched) return;
    $("ca-slug").value = $("ca-name").value.toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9]+/g, "-").replace(/^[^a-z]+|-+$/g, "").slice(0, 30);
  };
  $("co-add").onsubmit = async (ev) => {
    ev.preventDefault();
    const name = $("ca-name").value.trim();
    try {
      const r = await post("/companies", { name, slug: $("ca-slug").value.trim(),
        admin: { display_name: $("ca-aname").value.trim(), username: $("ca-auser").value.trim(), password: $("ca-apass").value } });
      $("co-add").reset(); slugTouched = false; $("co-add").hidden = true; $("co-add-btn").hidden = false;
      showKey(name, r.token); loadCompanies(); loadActivity();
    } catch (e) { toast(e.message, true); }
  };

  // phone app
  async function loadApk() {
    const i = await api("/app");
    $("apk").textContent = i.version_name ? `Version ${i.version_name} (build ${i.version_code}), ${bytes(i.size)}, uploaded ${when(i.uploaded)}` : "No app uploaded yet";
    $("apk-rm").hidden = !i.version_name;
  }
  $("apk-up").onclick = async () => {
    const f = $("apk-file").files[0];
    if (!f) return toast("Choose the KenTrackIt.apk file first", true);
    $("apk-up").disabled = true; $("apk-up").textContent = "Uploading…";
    try { const i = await api("/app/upload", { method: "POST", headers: { "Content-Type": "application/vnd.android.package-archive" }, body: f });
          toast(`Uploaded ${i.version_name}. Phones will be offered the update.`); $("apk-file").value = ""; loadApk(); loadActivity(); }
    catch (e) { toast(e.message, true); }
    $("apk-up").disabled = false; $("apk-up").textContent = "Upload";
  };
  $("apk-rm").onclick = async () => {
    if (!confirm("Remove the app installer? Phones keep the app they have.")) return;
    try { await api("/app", { method: "DELETE" }); loadApk(); } catch (e) { toast(e.message, true); }
  };

  // service settings
  async function loadSettings() {
    const s = await api("/settings");
    $("svc-title").value = s.title; $("svc-tiles").value = s.tiles.source; $("svc-sat").value = s.tiles.satellite;
    $("svc-linz").value = ""; $("svc-linz").placeholder = s.tiles.linz_key_set ? "key saved; enter a new one to replace" : "from basemaps.linz.govt.nz";
  }
  $("svc").onsubmit = async (ev) => {
    ev.preventDefault();
    const tiles = { source: $("svc-tiles").value, satellite: $("svc-sat").value };
    if ($("svc-linz").value.trim()) tiles.linz_key = $("svc-linz").value.trim();
    try { await post("/settings", { web: { title: $("svc-title").value.trim() }, tiles }); toast("Saved"); loadSettings(); loadActivity();
          $("h-title").textContent = $("svc-title").value.trim(); }
    catch (e) { toast(e.message, true); }
  };

  // ASI administrators
  let editingAdmin = null;
  async function loadAdmins() {
    const { admins } = await api("/admins");
    const tb = $("ad-rows"); tb.replaceChildren();
    for (const u of admins) {
      const tr = el("tr", u.enabled ? "" : "off");
      const nm = el("td"); nm.append(el("div", "uname", u.display_name || u.username), el("div", "usub", u.username + (u.id === STATE.user.id ? " · you" : "")));
      const last = el("td", null, u.last_login ? "Signed in " + ago(u.last_login) : "Never signed in");
      const act = el("td", "act");
      const e = el("button", null, "Edit");
      e.onclick = () => { editingAdmin = u; $("ad-form").hidden = false; $("ad-name").value = u.display_name; $("ad-user").value = u.username;
                          $("ad-pass").value = ""; $("ad-pass").placeholder = "leave blank to keep"; $("ad-pass").required = false; };
      act.appendChild(e);
      if (u.id !== STATE.user.id) {
        const d = el("button", "danger", "Remove");
        d.onclick = async () => { if (!confirm(`Remove ${u.username}?`)) return;
          try { await api("/admins/" + u.id, { method: "DELETE" }); loadAdmins(); loadActivity(); } catch (err) { toast(err.message, true); } };
        act.append(" ", d);
      }
      tr.append(nm, last, act); tb.appendChild(tr);
    }
  }
  $("ad-add-btn").onclick = () => { editingAdmin = null; $("ad-form").reset(); $("ad-form").hidden = false; $("ad-pass").required = true; $("ad-pass").placeholder = "at least 8 characters"; $("ad-name").focus(); };
  $("ad-cancel").onclick = () => { $("ad-form").hidden = true; };
  $("ad-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const body = { display_name: $("ad-name").value.trim(), username: $("ad-user").value.trim() };
    if (editingAdmin) body.id = editingAdmin.id;
    if ($("ad-pass").value) body.password = $("ad-pass").value;
    try { await post("/admins", body); $("ad-form").hidden = true; toast("Saved"); loadAdmins(); loadActivity(); }
    catch (e) { toast(e.message, true); }
  };

  async function loadActivity() {
    const rows = await api("/activity");
    const ul = $("activity"); ul.replaceChildren();
    for (const r of rows.slice(0, 60)) {
      const li = el("li"); li.append(el("time", null, when(r.rx_time)), el("span", null, r.detail)); ul.appendChild(li);
    }
  }

  loadCompanies(); loadBilling().catch((e) => toast(e.message, true)); loadApk(); loadSettings(); loadAdmins(); loadActivity();
  setInterval(() => { if (!open && $("co-add").hidden) loadCompanies().catch(() => {}); }, 10000);
})();
