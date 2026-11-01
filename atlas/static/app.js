/* API Atlas web UI. No build step: plain JS talking to the local API. */
(function () {
  "use strict";

  const $app = document.getElementById("app");
  const $drawer = document.getElementById("drawer");
  const $modal = document.getElementById("modal");
  const $toast = document.getElementById("toast");

  // ------------------------------------------------------------------ utils
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const methodClass = (m) => (["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"].includes(m) ? m : "OTHER");
  const stamp = (m) => `<span class="m ${methodClass(m)}">${esc(m)}</span>`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      const msg = (data && (data.error || data.detail)) || `Request failed (${res.status})`;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  let toastTimer;
  function toast(msg) {
    $toast.textContent = msg;
    $toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $toast.classList.remove("show"), 2600);
  }

  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  }

  // Render JSON with masked values highlighted
  function prettyJson(value) {
    if (value === null || value === undefined) return "";
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    return esc(text).replace(/&lt;masked:([a-z_]+)&gt;/g, '<span class="masked">&lt;masked:$1&gt;</span>');
  }

  // ------------------------------------------------------------------ router
  let cleanup = null;
  function route() {
    if (cleanup) { cleanup(); cleanup = null; }
    closeDrawer(); closeModal(); removeBar();
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    if (parts[0] === "s" && parts[1]) renderSession(parts[1], parts[2] || null);
    else renderHome();
    $app.focus({ preventScroll: true });
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------ home
  async function renderHome() {
    $app.innerHTML = `
      <section class="start">
        <h1>Explore your app once. Get a working API test suite.</h1>
        <p class="lede">Enter the address of the app you're testing. A browser opens; use the app the way a user would.
        Every API call is recorded, connected and turned into tests you can run with pytest or Postman.</p>
        <form class="start-form" id="startForm">
          <input id="url" name="url" aria-label="App address" placeholder="https://staging.your-app.com" autocomplete="url" required>
          <button class="btn primary" type="submit" id="startBtn">Start exploring</button>
        </form>
        <div class="form-error" id="startErr" hidden></div>
        <p class="hint">No app handy? Run <code>atlas demo</code> in a terminal and explore <code>http://127.0.0.1:8765</code>.</p>
      </section>
      <div class="steps" aria-label="How it works">
        <div><b><span class="n">1</span>Explore</b><span>Click through a real flow: log in, add to cart, check out.</span></div>
        <div><b><span class="n">2</span>Review</b><span>See each endpoint, what it depends on, and the tests suggested for it.</span></div>
        <div><b><span class="n">3</span>Export</b><span>Download pytest or Postman. Tokens and ids are chained for you.</span></div>
      </div>
      <h2>Explorations</h2>
      <div id="sessions"><p class="empty">Loading…</p></div>`;

    const form = document.getElementById("startForm");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("startBtn");
      const err = document.getElementById("startErr");
      err.hidden = true;
      btn.disabled = true; btn.textContent = "Opening browser…";
      try {
        const s = await api("/api/sessions", { method: "POST", body: { url: form.url.value } });
        location.hash = `#/s/${s.id}`;
      } catch (ex) {
        err.textContent = ex.message; err.hidden = false;
        btn.disabled = false; btn.textContent = "Start exploring";
      }
    });

    try {
      const sessions = await api("/api/sessions");
      const box = document.getElementById("sessions");
      if (!box) return;
      if (!sessions.length) {
        box.innerHTML = `<p class="empty">Nothing explored yet. Your explorations will be listed here.</p>`;
        return;
      }
      box.innerHTML = `<table class="list"><thead><tr><th>Name</th><th>API calls</th><th>When</th><th></th></tr></thead><tbody>
        ${sessions.map((s) => `<tr class="link" data-id="${esc(s.id)}">
          <td><b>${esc(s.name)}</b><div class="sub mono">${esc(s.target_url)}</div></td>
          <td>${s.call_count}</td>
          <td class="sub">${ago(s.created_at)}</td>
          <td style="text-align:right">${s.status === "recording" ? '<span class="status rec">Recording</span>' : ""}</td>
        </tr>`).join("")}</tbody></table>`;
      box.querySelectorAll("tr.link").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/s/${tr.dataset.id}`; }));
    } catch (ex) {
      document.getElementById("sessions").innerHTML = `<p class="empty">Couldn't load explorations: ${esc(ex.message)}</p>`;
    }
  }

  // ------------------------------------------------------------------ session
  async function renderSession(sid, tab) {
    let session;
    try { session = await api(`/api/sessions/${sid}`); }
    catch (ex) { $app.innerHTML = `<p class="empty">This exploration doesn't exist any more. <a href="#/">Back to start</a></p>`; return; }

    const state = { sid, session, calls: session.calls, tab: tab || (session.status === "recording" ? "traffic" : "tests"), analysis: null, current: null, fresh: new Set() };
    let ws = null;
    if (session.status === "recording") ws = openSocket(state);
    cleanup = () => { if (ws) ws.close(); };
    drawSession(state);
  }

  function openSocket(state) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/sessions/${state.sid}`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "call") {
        state.calls.push(msg.call);
        state.fresh.add(msg.call.id);
        state.analysis = null;
        if (state.tab === "traffic") drawTraffic(state); else drawHeader(state);
      } else if (msg.type === "status") {
        state.session.status = msg.status;
        if (msg.error) toast(msg.error);
        if (msg.status === "stopped") { state.tab = "tests"; ws.close(); drawSession(state); toast("Exploration saved. Review the tests below."); }
        else drawHeader(state);
      }
    };
    return ws;
  }

  function drawSession(state) {
    $app.innerHTML = `<div id="shead"></div><div class="tabs" role="tablist" id="tabs"></div><div id="pane"></div>`;
    drawHeader(state);
    drawTabs(state);
    if (state.tab === "traffic") drawTraffic(state); else drawTests(state);
  }

  function drawHeader(state) {
    const s = state.session;
    const visible = state.calls.filter((c) => !c.hidden).length;
    const rec = s.status === "recording";
    const el = document.getElementById("shead");
    if (!el) return;
    el.innerHTML = `<div class="shead">
      <div>
        <h1><input id="sname" value="${esc(s.name)}" aria-label="Exploration name"></h1>
        <div class="meta">
          ${rec ? '<span class="status rec">Recording</span>' : '<span class="status">Saved</span>'}
          <span class="mono">${esc(s.target_url)}</span>
          <span>${plural(visible, "API call")}</span>
        </div>
      </div>
      <div class="actions">
        ${rec ? '<button class="btn primary" id="stopBtn">Stop exploring</button>' : ""}
        <a class="btn quiet" href="/api/sessions/${state.sid}/export/har" download>Download HAR</a>
        <button class="btn quiet danger" id="delBtn">Delete</button>
      </div></div>`;
    const name = document.getElementById("sname");
    name.addEventListener("change", async () => {
      try { state.session = { ...state.session, ...(await api(`/api/sessions/${state.sid}`, { method: "PATCH", body: { name: name.value } })) }; toast("Renamed"); }
      catch (ex) { toast(ex.message); }
    });
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); });
    const stop = document.getElementById("stopBtn");
    if (stop) stop.addEventListener("click", async () => {
      stop.disabled = true; stop.textContent = "Stopping…";
      try { await api(`/api/sessions/${state.sid}/stop`, { method: "POST" }); state.session.status = "stopped"; state.tab = "tests"; drawSession(state); }
      catch (ex) { toast(ex.message); stop.disabled = false; stop.textContent = "Stop exploring"; }
    });
    document.getElementById("delBtn").addEventListener("click", async () => {
      if (!confirm("Delete this exploration and everything recorded in it?")) return;
      await api(`/api/sessions/${state.sid}`, { method: "DELETE" });
      location.hash = "#/";
    });
  }

  function drawTabs(state) {
    const visible = state.calls.filter((c) => !c.hidden).length;
    const tabs = [["traffic", "Traffic", visible], ["tests", "Endpoints & tests", null]];
    const el = document.getElementById("tabs");
    el.innerHTML = tabs.map(([id, label, n]) =>
      `<button role="tab" aria-selected="${state.tab === id}" data-tab="${id}">${label}${n !== null ? `<span class="count">${n}</span>` : ""}</button>`).join("");
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      state.tab = b.dataset.tab;
      history.replaceState(null, "", `#/s/${state.sid}/${state.tab}`);
      drawTabs(state);
      if (state.tab === "traffic") drawTraffic(state); else drawTests(state);
    }));
  }

  // ------------------------------------------------------------------ traffic tab
  function drawTraffic(state) {
    drawHeader(state);
    const pane = document.getElementById("pane");
    if (!pane) return;
    const rec = state.session.status === "recording";
    const visible = state.calls.filter((c) => !c.hidden);
    const hidden = state.calls.filter((c) => c.hidden);
    const hiddenHosts = [...new Set(hidden.map((c) => c.host))];
    let html = "";
    if (hiddenHosts.length) {
      html += `<div class="notice">${plural(hidden.length, "call")} to other servers ${hidden.length === 1 ? "is" : "are"} hidden:
        ${hiddenHosts.map((h) => `<button class="btn" data-host="${esc(h)}">Include ${esc(h)}</button>`).join(" ")}</div>`;
    }
    if (!visible.length) {
      html += `<div class="waiting">${rec
        ? "<b>Recording.</b> Use the app in the browser window that just opened. Log in, open a few pages, submit a form. API calls will appear here as they happen. Analytics and tracking calls are left out."
        : "Nothing was recorded. Start a new exploration and use the app in the browser window that opens."}</div>`;
    } else {
      html += `<ol class="route" aria-label="Recorded API calls">${visible.map((c) => {
        const err = c.status >= 400 || !c.status;
        return `<li class="stop ${err ? "err" : ""} ${state.fresh.has(c.id) ? "new" : ""}" data-id="${c.id}" tabindex="0">
          <span class="dot"></span>${stamp(c.method)}
          <span class="path">${esc(c.path)}</span>
          <span class="code">${c.status ?? "—"}</span>
          <span class="ms">${c.duration_ms != null ? Math.round(c.duration_ms) + " ms" : ""}</span></li>`;
      }).join("")}</ol>
      <p class="route-end">${rec ? "Keep exploring. When you've covered the flow, press <b>Stop exploring</b>." : `${plural(visible.length, "call")} recorded.`}</p>`;
    }
    pane.innerHTML = html;
    state.fresh.clear();
    pane.querySelectorAll(".stop").forEach((li) => {
      const open = () => openCall(state.sid, Number(li.dataset.id));
      li.addEventListener("click", open);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    });
    pane.querySelectorAll("[data-host]").forEach((b) => b.addEventListener("click", async () => {
      const hosts = [...state.session.allowed_hosts, b.dataset.host];
      try {
        state.session = { ...state.session, ...(await api(`/api/sessions/${state.sid}/hosts`, { method: "PUT", body: { allowed_hosts: hosts } })) };
        const fresh = await api(`/api/sessions/${state.sid}`);
        state.calls = fresh.calls; state.analysis = null;
        drawTabs(state); drawTraffic(state); toast(`Included ${b.dataset.host}`);
      } catch (ex) { toast(ex.message); }
    }));
    const tabs = document.querySelector('#tabs [data-tab="traffic"] .count');
    if (tabs) tabs.textContent = visible.length;
  }

  async function openCall(sid, id) {
    let c;
    try { c = await api(`/api/sessions/${sid}/calls/${id}`); } catch (ex) { toast(ex.message); return; }
    const kv = (obj) => {
      const rows = Array.isArray(obj) ? obj : Object.entries(obj || {});
      if (!rows.length) return '<p class="empty">None</p>';
      return `<table class="kv">${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${prettyJson(String(v))}</td></tr>`).join("")}</table>`;
    };
    const body = (kind, b) => kind === "none" || b == null ? '<p class="empty">No body</p>' : `<pre class="code">${prettyJson(b)}</pre>`;
    $drawer.innerHTML = `<header>${stamp(c.method)}<span class="path">${esc(c.path)}</span><span class="mono">${c.status ?? ""}</span>
      <button class="btn quiet" id="closeDrawer" aria-label="Close">Close</button></header>
      <div class="body">
        ${c.query.length ? `<h4>Query</h4>${kv(c.query)}` : ""}
        <h4>Request headers</h4>${kv(c.req_headers)}
        <h4>Request body</h4>${body(c.req_kind, c.req_body)}
        <h4>Response headers</h4>${kv(c.resp_headers)}
        <h4>Response body</h4>${body(c.resp_kind, c.resp_body)}
      </div>`;
    $drawer.hidden = false;
    document.getElementById("closeDrawer").addEventListener("click", closeDrawer);
    document.getElementById("closeDrawer").focus();
  }
  function closeDrawer() { $drawer.hidden = true; $drawer.innerHTML = ""; }

  // ------------------------------------------------------------------ tests tab
  async function drawTests(state) {
    drawHeader(state);
    const pane = document.getElementById("pane");
    if (!state.calls.filter((c) => !c.hidden).length) {
      pane.innerHTML = `<div class="waiting">${state.session.status === "recording"
        ? "<b>Nothing recorded yet.</b> Use the app in the browser window; endpoints and tests will show up here."
        : "Nothing was recorded in this exploration, so there's nothing to test yet."}</div>`;
      removeBar();
      return;
    }
    if (!state.analysis) {
      pane.innerHTML = `<p class="empty">Working out endpoints and tests…</p>`;
      try { state.analysis = await api(`/api/sessions/${state.sid}/analysis`); }
      catch (ex) { pane.innerHTML = `<p class="empty">Couldn't analyse this exploration: ${esc(ex.message)}</p>`; return; }
    }
    const a = state.analysis;
    if (!state.current || !a.endpoints.find((e) => e.key === state.current)) state.current = a.endpoints[0] && a.endpoints[0].key;
    const multiHost = a.hosts.length > 1;
    const counts = {};
    a.cases.forEach((c) => { counts[c.endpoint] = counts[c.endpoint] || [0, 0]; counts[c.endpoint][1]++; if (c.selected) counts[c.endpoint][0]++; });

    pane.innerHTML = `
      ${state.session.status === "recording" ? '<div class="notice info">Still recording. This view updates when you come back to it; stop exploring when you are done.</div>' : ""}
      <div class="split">
        <nav class="eplist" aria-label="Endpoints">${a.endpoints.map((e) => {
          const [sel, all] = counts[e.key] || [0, 0];
          return `<button class="ep" data-key="${esc(e.key)}" aria-current="${e.key === state.current}">
            ${stamp(e.method)}<span class="p">${multiHost ? `<span class="sub">${esc(e.host)}</span>` : ""}${esc(e.template)}</span>
            <span class="c ${sel ? "has" : ""}" title="${sel} of ${all} tests selected">${sel}/${all}</span></button>`;
        }).join("")}</nav>
        <div class="detail" id="detail"></div>
      </div>`;
    pane.querySelectorAll(".ep").forEach((b) => b.addEventListener("click", () => {
      state.current = b.dataset.key;
      pane.querySelectorAll(".ep").forEach((x) => x.setAttribute("aria-current", x.dataset.key === state.current));
      drawDetail(state);
    }));
    drawDetail(state);
    drawBar(state);
  }

  const KIND_LABEL = { happy: "Happy path", no_auth: "Security", missing_field: "Validation", wrong_type: "Validation", empty_body: "Validation", invalid_id: "Not found" };
  const KIND_WHY = {
    happy: "Replays the call with its setup steps and checks the status and response shape.",
    no_auth: "Sends the call without credentials. Fails if the endpoint answers anyway, which is worth a look.",
    missing_field: "Leaves out one field. Expects a 4xx validation error.",
    wrong_type: "Sends a value of the wrong type. Expects a 4xx validation error.",
    empty_body: "Sends an empty body. Expects a 4xx validation error.",
    invalid_id: "Uses an id that doesn't exist. Expects a 4xx.",
  };

  function drawDetail(state) {
    const a = state.analysis;
    const e = a.endpoints.find((x) => x.key === state.current);
    const box = document.getElementById("detail");
    if (!e || !box) return;
    const cases = a.cases.filter((c) => c.endpoint === e.key);
    const fields = (e.response_fields || []).filter((f) => f.path.length <= 3).slice(0, 40);
    const authText = { bearer: "Bearer token", header: "API key header", cookie: "Session cookie" }[e.auth] || "No auth seen";

    box.innerHTML = `
      <h3>${stamp(e.method)}<span>${esc(e.template)}</span></h3>
      <div class="facts">
        <span>${plural(e.call_ids.length, "call")} recorded</span>
        <span>Status ${e.statuses.map(esc).join(", ")}</span>
        <span>${authText}</span>
        ${e.issues_token ? "<span>Gives out the token other calls use</span>" : ""}
      </div>

      <div class="section">
        <h4>Tests</h4>
        <div class="case-tools">
          <button class="btn quiet" data-bulk="1">Select all</button>
          <button class="btn quiet" data-bulk="0">Clear</button>
        </div>
        <div class="cases">${cases.map((c) => `
          <div class="case" data-id="${c.id}">
            <input type="checkbox" ${c.selected ? "checked" : ""} aria-label="Include this test">
            <div>
              <input class="t" value="${esc(c.title)}" aria-label="Test name">
              <div class="why"><span class="kind ${c.kind}">${KIND_LABEL[c.kind] || c.kind}</span>${KIND_WHY[c.kind] || ""}
              ${c.setup.length ? `<br><span title="${esc(c.setup.join("\n"))}">Runs ${plural(c.setup.length, "setup step")} first, ending with ${esc(c.setup[c.setup.length - 1])}.</span>` : ""}</div>
            </div>
            <label class="exp">${c.expect.status !== undefined
              ? `expects <input type="number" min="100" max="599" value="${c.expect.status}" aria-label="Expected status">`
              : `expects ${c.expect.status_in ? c.expect.status_in.join(" or ") : c.expect.status_range.join("–")}`}</label>
          </div>`).join("")}</div>
      </div>

      <div class="section">
        <h4>Depends on</h4>
        ${e.depends_on.length
          ? `<ul class="deps">${e.depends_on.map((d) => `<li>${d.kind === "reuse" ? "Reuses" : "Takes"} <code>${esc(d.field)}</code> from ${stamp(d.endpoint.split(" ")[0])} <code>${esc(d.endpoint.split(" ").slice(1).join(" ").replace(/^[^/]*/, ""))}</code> as ${esc(d.used_in)}</li>`).join("")}</ul>`
          : '<p class="help">Nothing. This call works on its own.</p>'}
      </div>

      <div class="section">
        <h4>Response fields</h4>
        <p class="help">Dynamic fields (ids, timestamps, tokens) are only type-checked, never compared to recorded values.</p>
        ${fields.length ? `<table class="fields">${fields.map((f) => `<tr>
          <td>${esc(f.path.join("."))}</td>
          <td class="ty">${esc(f.types.join(" | "))}${f.format ? `<span class="tag">${esc(f.format)}</span>` : ""}${f.dynamic ? '<span class="tag dyn">dynamic</span>' : ""}${!f.required ? '<span class="tag">optional</span>' : ""}</td>
        </tr>`).join("")}</table>` : '<p class="help">No JSON body.</p>'}
        <details class="sample"><summary>Show a recorded call</summary><div id="sample"></div></details>
      </div>`;

    box.querySelectorAll(".case").forEach((row) => {
      const id = row.dataset.id;
      const c = cases.find((x) => x.id === id);
      row.querySelector('input[type="checkbox"]').addEventListener("change", async (ev) => {
        c.selected = ev.target.checked;
        await saveCase(state, id, { selected: c.selected });
      });
      const t = row.querySelector(".t");
      t.addEventListener("change", async () => { c.title = t.value || c.default_title; await saveCase(state, id, { title: c.title }); });
      t.addEventListener("keydown", (ev) => { if (ev.key === "Enter") t.blur(); });
      const ex = row.querySelector(".exp input");
      if (ex) ex.addEventListener("change", async () => {
        const n = parseInt(ex.value, 10);
        if (n >= 100 && n <= 599) { c.expect = { status: n }; await saveCase(state, id, { expect_status: n }); }
      });
    });
    box.querySelectorAll("[data-bulk]").forEach((b) => b.addEventListener("click", async () => {
      const on = b.dataset.bulk === "1";
      cases.forEach((c) => { c.selected = on; });
      try { await api(`/api/sessions/${state.sid}/cases/select`, { method: "POST", body: { case_ids: cases.map((c) => c.id), selected: on } }); }
      catch (ex) { toast(ex.message); }
      refreshCounts(state); drawDetail(state);
    }));
    box.querySelector("details.sample").addEventListener("toggle", async (ev) => {
      if (!ev.target.open) return;
      const c = await api(`/api/sessions/${state.sid}/calls/${e.sample_call_id}`);
      document.getElementById("sample").innerHTML =
        `${c.req_kind !== "none" ? `<pre class="code">${prettyJson(c.req_body)}</pre>` : ""}<pre class="code">${prettyJson(c.resp_body)}</pre>`;
    }, { once: true });
  }

  async function saveCase(state, id, patch) {
    try { await api(`/api/sessions/${state.sid}/cases/${id}`, { method: "PATCH", body: patch }); }
    catch (ex) { toast(ex.message); }
    refreshCounts(state);
  }

  function refreshCounts(state) {
    const a = state.analysis;
    const counts = {};
    a.cases.forEach((c) => { counts[c.endpoint] = counts[c.endpoint] || [0, 0]; counts[c.endpoint][1]++; if (c.selected) counts[c.endpoint][0]++; });
    document.querySelectorAll(".ep").forEach((b) => {
      const [sel, all] = counts[b.dataset.key] || [0, 0];
      const el = b.querySelector(".c"); el.textContent = `${sel}/${all}`; el.classList.toggle("has", sel > 0);
    });
    const selected = a.cases.filter((c) => c.selected);
    a.selected_count = selected.length;
    a.needs_env = [...new Set(selected.flatMap((c) => c.needs_env))].sort();
    drawBar(state);
  }

  // ------------------------------------------------------------------ export bar
  function removeBar() { const b = document.getElementById("bar"); if (b) b.remove(); }
  function drawBar(state) {
    removeBar();
    const a = state.analysis;
    const bar = document.createElement("div");
    bar.className = "bar"; bar.id = "bar";
    const n = a.selected_count;
    bar.innerHTML = `<div class="sum"><b>${plural(n, "test")}</b> selected across ${plural(a.endpoints.length, "endpoint")}.
      ${a.needs_env.length ? `<span class="env">Before running, set ${a.needs_env.map((e) => `<code>${esc(e)}</code>`).join(" ")}.</span>` : "No secrets to set."}</div>
      <button class="btn" id="previewBtn" ${n ? "" : "disabled"}>Preview code</button>
      <a class="btn" ${n ? `href="/api/sessions/${state.sid}/export/postman"` : 'aria-disabled="true"'} download>Download Postman</a>
      <a class="btn primary" ${n ? `href="/api/sessions/${state.sid}/export/pytest"` : 'aria-disabled="true"'} download>Download pytest</a>`;
    document.body.appendChild(bar);
    document.getElementById("previewBtn").addEventListener("click", () => preview(state, "pytest"));
  }

  async function preview(state, fmt) {
    let files;
    try { files = await api(`/api/sessions/${state.sid}/preview/${fmt}`); } catch (ex) { toast(ex.message); return; }
    const names = Object.keys(files);
    let current = names[0];
    const draw = () => {
      $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Generated code">
        <header><h3>Generated ${fmt === "pytest" ? "pytest suite" : "Postman collection"}</h3>
          <button class="btn quiet" data-fmt="pytest" ${fmt === "pytest" ? "disabled" : ""}>pytest</button>
          <button class="btn quiet" data-fmt="postman" ${fmt === "postman" ? "disabled" : ""}>Postman</button>
          <button class="btn quiet" id="copyBtn">Copy</button>
          <button class="btn quiet" id="closeModal">Close</button></header>
        <div class="files" role="tablist">${names.map((nm) => `<button role="tab" aria-selected="${nm === current}" data-file="${esc(nm)}">${esc(nm)}</button>`).join("")}</div>
        <pre class="code">${esc(files[current])}</pre></div>`;
      $modal.hidden = false;
      $modal.querySelectorAll("[data-file]").forEach((b) => b.addEventListener("click", () => { current = b.dataset.file; draw(); }));
      $modal.querySelectorAll("[data-fmt]").forEach((b) => b.addEventListener("click", () => preview(state, b.dataset.fmt)));
      document.getElementById("closeModal").addEventListener("click", closeModal);
      document.getElementById("copyBtn").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(files[current]); toast(`Copied ${current}`); } catch (e) { toast("Couldn't copy. Select the text instead."); }
      });
    };
    draw();
    document.getElementById("closeModal").focus();
  }
  function closeModal() { $modal.hidden = true; $modal.innerHTML = ""; }
  $modal.addEventListener("click", (e) => { if (e.target === $modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });

  route();
})();
