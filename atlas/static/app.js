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
    else if (parts[0] === "p" && parts[1]) renderProject(parts[1]);
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
      <h2 class="sechead">Projects <button class="btn quiet small" id="openProjectBtn">Open project folder</button></h2>
      <div id="projects"><p class="empty">Loading…</p></div>
      <h2>Explorations</h2>
      <div id="sessions"><p class="empty">Loading…</p></div>`;

    document.getElementById("openProjectBtn").addEventListener("click", () => openFolderPicker());
    loadProjects();

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

  // ------------------------------------------------------------------ projects: open/clone
  async function loadProjects() {
    const box = document.getElementById("projects");
    try {
      const projects = await api("/api/projects");
      if (!box) return;
      if (!projects.length) { box.innerHTML = `<p class="empty">No projects opened yet.</p>`; return; }
      box.innerHTML = `<table class="list"><thead><tr><th>Name</th><th>Path</th><th>Branch</th></tr></thead><tbody>
        ${projects.map((p) => `<tr class="link" data-id="${esc(p.id)}">
          <td><b>${esc(p.name)}</b>${p.remote ? `<div class="sub mono">${esc(p.remote)}</div>` : ""}</td>
          <td class="sub mono">${esc(p.path)}</td>
          <td class="sub">${esc(p.default_branch || "")}</td>
        </tr>`).join("")}</tbody></table>`;
      box.querySelectorAll("tr.link").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/p/${tr.dataset.id}`; }));
    } catch (ex) {
      box.innerHTML = `<p class="empty">Couldn't load projects: ${esc(ex.message)}</p>`;
    }
  }

  async function openFolderPicker(path) {
    let listing;
    try { listing = await api(`/api/fs/list${path ? `?path=${encodeURIComponent(path)}` : ""}`); }
    catch (ex) { toast(ex.message); return; }
    $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Open project folder">
      <header><h3>Open project folder</h3><button class="btn quiet" id="closeModal">Close</button></header>
      <div class="picker">
        <div class="picker-path mono">${esc(listing.path)}</div>
        <div class="picker-list">
          ${listing.parent ? `<button class="picker-row" data-path="${esc(listing.parent)}">.. (up)</button>` : ""}
          ${listing.entries.map((e) => `<button class="picker-row" data-path="${esc(e.path)}">${e.is_repo ? "\u{1F4C1}" : "\u{1F4C2}"} ${esc(e.name)}</button>`).join("") || '<p class="empty">No subfolders here</p>'}
        </div>
        <button class="btn primary" id="useFolderBtn">Open "${esc(listing.path.split("/").pop() || listing.path)}"</button>
      </div>
      <div class="picker-divider">or clone from GitHub</div>
      <form class="cloneform" id="cloneForm">
        <input name="clone_url" placeholder="https://github.com/org/repo.git" required>
        <button class="btn" type="submit">Clone</button>
      </form></div>`;
    $modal.hidden = false;
    document.getElementById("closeModal").addEventListener("click", closeModal);
    $modal.querySelectorAll(".picker-row").forEach((b) => b.addEventListener("click", () => openFolderPicker(b.dataset.path)));
    document.getElementById("useFolderBtn").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const p = await api("/api/projects", { method: "POST", body: { path: listing.path } });
        closeModal(); location.hash = `#/p/${p.id}`;
      } catch (ex) { toast(ex.message); e.target.disabled = false; }
    });
    document.getElementById("cloneForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button");
      btn.disabled = true; btn.textContent = "Cloning…";
      try {
        const p = await api("/api/projects", { method: "POST", body: { clone_url: e.target.clone_url.value } });
        closeModal(); location.hash = `#/p/${p.id}`;
      } catch (ex) { toast(ex.message); btn.disabled = false; btn.textContent = "Clone"; }
    });
  }

  // ------------------------------------------------------------------ project
  async function renderProject(pid) {
    let p;
    try { p = await api(`/api/projects/${pid}`); }
    catch (ex) { $app.innerHTML = `<p class="empty">This project doesn't exist any more. <a href="#/">Back to start</a></p>`; return; }
    const state = { pid, project: p };
    $app.innerHTML = `<div class="shead">
      <div><h1>${esc(p.name)}</h1>
        <div class="meta"><span class="mono">${esc(p.path)}</span>
          ${p.git && p.git.remote ? `<span class="mono">${esc(p.git.remote)}</span>` : ""}
          ${p.git && p.git.default_branch ? `<span>branch <code>${esc(p.git.default_branch)}</code></span>` : ""}</div></div>
      <div class="actions">
        <button class="btn primary" id="exploreBtn">Explore a flow for this project</button>
        <button class="btn quiet danger" id="delProjectBtn">Remove</button>
      </div></div>
      <form class="exploreinline" id="exploreForm" hidden>
        <input id="exploreUrl" placeholder="https://staging.your-app.com" autocomplete="url" required>
        <button class="btn primary" type="submit">Start exploring</button>
        <button class="btn quiet" type="button" id="exploreCancel">Cancel</button>
      </form>
      <div id="scanbox"></div>
      <div id="selfcoveragebox"></div>
      <div id="coveragebox"></div>`;
    document.getElementById("delProjectBtn").addEventListener("click", async () => {
      if (!confirm("Remove this project from Atlas? Files on disk are not touched.")) return;
      await api(`/api/projects/${pid}`, { method: "DELETE" });
      location.hash = "#/";
    });
    const exploreForm = document.getElementById("exploreForm");
    document.getElementById("exploreBtn").addEventListener("click", () => {
      exploreForm.hidden = false;
      document.getElementById("exploreUrl").focus();
    });
    document.getElementById("exploreCancel").addEventListener("click", () => { exploreForm.hidden = true; });
    exploreForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector('button[type="submit"]');
      btn.disabled = true; btn.textContent = "Opening browser…";
      try {
        const s = await api("/api/sessions", { method: "POST", body: { url: document.getElementById("exploreUrl").value } });
        sessionStorage.setItem("atlas_return_to_project", pid);
        sessionStorage.setItem("atlas_return_session_id", s.id);
        location.hash = `#/s/${s.id}`;
      } catch (ex) { toast(ex.message); btn.disabled = false; btn.textContent = "Start exploring"; }
    });
    drawScan(state);
  }

  function drawScan(state) {
    const p = state.project;
    const box = document.getElementById("scanbox");
    if (p.missing) { box.innerHTML = `<p class="empty">This folder no longer exists on disk.</p>`; return; }
    box.innerHTML = `<h2>Scan</h2>
      <div class="scangrid">
        <div><b>Collections</b>${p.collections.length ? `<ul>${p.collections.map((c) => `<li class="mono">${esc(c.path)}</li>`).join("")}</ul>` : '<p class="empty">None found</p>'}</div>
        <div><b>Environments</b>${p.environments.length ? `<ul>${p.environments.map((e) => `<li class="mono">${esc(e.path)}</li>`).join("")}</ul>` : '<p class="empty">None found</p>'}</div>
        <div><b>Newman command</b>${p.newman_command ? `<code>${esc(p.newman_command)}</code>` : '<p class="empty">Not detected</p>'}</div>
      </div>`;
    if (p.collections.length) { drawSelfCoverage(state); drawCoveragePicker(state); }
    else { document.getElementById("selfcoveragebox").innerHTML = ""; document.getElementById("coveragebox").innerHTML = ""; }
  }

  // ------------------------------------------------------------------ project: a collection's own tests, no exploration needed
  function drawSelfCoverage(state) {
    const p = state.project;
    const box = document.getElementById("selfcoveragebox");
    box.innerHTML = `<h2>This collection's own tests</h2>
      <p class="hint">Works with no exploration - coverage and runs come straight from the collection file itself.</p>
      <div class="coveragepicker">
        <label>Collection <select id="selfCollSel">${p.collections.map((c) => `<option value="${esc(c.path)}">${esc(c.name)}</option>`).join("")}</select></label>
        ${p.environments.length ? `<label>Environment <select id="selfEnvSel"><option value="">(none)</option>${p.environments.map((e) => `<option value="${esc(e.path)}">${esc(e.path)}</option>`).join("")}</select></label>` : ""}
      </div>
      <div id="selfCovResult"></div>`;
    const refresh = async () => {
      const coll = document.getElementById("selfCollSel").value;
      const res = document.getElementById("selfCovResult");
      res.innerHTML = `<p class="empty">Loading…</p>`;
      try {
        const data = await api(`/api/projects/${state.pid}/self-coverage?collection=${encodeURIComponent(coll)}`);
        drawSelfCoverageResult(res, state, coll, data);
      } catch (ex) { res.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; }
    };
    document.getElementById("selfCollSel").addEventListener("change", refresh);
    refresh();
  }

  function drawSelfCoverageResult(el, state, collPath, data) {
    el.innerHTML = `<div class="covsummary">
        <span class="covpill ho">${data.summary.happy_only} happy only</span>
        <span class="covpill han">${data.summary.happy_and_negative} happy + negative</span>
        <button class="btn primary small" id="runCollBtn">Run this collection</button>
      </div>
      <p class="hint">Style: base URL var <code>${esc(data.style.base_url_var)}</code>, token var
        <code>${esc(data.style.token_var || "none detected")}</code>, variables set on
        <code>${esc(data.style.variable_scope)}</code>.</p>
      <table class="list"><thead><tr><th>Endpoint</th><th>Coverage</th></tr></thead><tbody>
        ${data.coverage.map((c) => `<tr><td class="mono">${stamp(c.method)} ${esc(c.template)}</td>
          <td><span class="covpill ${BADGE_CLASS[c.badge]}">${BADGE_LABEL[c.badge]}</span></td></tr>`).join("")}
      </tbody></table>
      <div id="runCollResult"></div>`;
    document.getElementById("runCollBtn").addEventListener("click", () => runCollection(state, collPath));
  }

  async function runCollection(state, collPath) {
    const btn = document.getElementById("runCollBtn");
    const res = document.getElementById("runCollResult");
    const envSel = document.getElementById("selfEnvSel");
    btn.disabled = true; btn.textContent = "Running…";
    res.innerHTML = `<p class="empty">Running with Newman…</p>`;
    const params = new URLSearchParams({ collection: collPath });
    if (envSel && envSel.value) params.set("environment", envSel.value);
    let result;
    try { result = await api(`/api/projects/${state.pid}/run-collection?${params}`, { method: "POST", body: {} }); }
    catch (ex) { res.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; btn.disabled = false; btn.textContent = "Run this collection"; return; }
    btn.disabled = false; btn.textContent = "Run this collection";
    drawInlineNewmanResults(res, null, result);
  }

  async function drawCoveragePicker(state) {
    const box = document.getElementById("coveragebox");
    let sessions;
    try { sessions = await api("/api/sessions"); } catch (ex) { box.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; return; }
    if (!sessions.length) {
      box.innerHTML = `<h2>Add tests from an exploration</h2><p class="empty">Nothing explored yet. Click "Explore a flow for this project" above to record one.</p>`;
      return;
    }
    box.innerHTML = `<h2>Add tests from an exploration</h2>
      <div class="coveragepicker">
        <label>Collection <select id="collSel">${state.project.collections.map((c) => `<option value="${esc(c.path)}">${esc(c.name)}</option>`).join("")}</select></label>
        <label>Exploration <select id="sessSel"><option value="">Choose one…</option>${sessions.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("")}</select></label>
      </div>
      <div id="covResult"></div>`;
    const refresh = async () => {
      const coll = document.getElementById("collSel").value;
      const sid = document.getElementById("sessSel").value;
      const res = document.getElementById("covResult");
      if (!sid) { res.innerHTML = ""; return; }
      res.innerHTML = `<p class="empty">Loading…</p>`;
      try {
        const [data, analysis] = await Promise.all([
          api(`/api/projects/${state.pid}/coverage?collection=${encodeURIComponent(coll)}&sid=${sid}`),
          api(`/api/sessions/${sid}/analysis`),
        ]);
        const sessionName = document.querySelector(`#sessSel option[value="${sid}"]`)?.textContent || "flow";
        drawCoverageAndFlow(res, state, sid, coll, data, analysis, sessionName);
      } catch (ex) { res.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; }
    };
    document.getElementById("collSel").addEventListener("change", refresh);
    document.getElementById("sessSel").addEventListener("change", refresh);

    // coming back from "Explore a flow for this project" - auto-pick what was just recorded
    if (sessionStorage.getItem("atlas_return_to_project") === state.pid) {
      const returnSid = sessionStorage.getItem("atlas_return_session_id");
      sessionStorage.removeItem("atlas_return_to_project");
      sessionStorage.removeItem("atlas_return_session_id");
      if (returnSid && sessions.some((s) => s.id === returnSid)) {
        document.getElementById("sessSel").value = returnSid;
        refresh();
      }
    }
  }

  const BADGE_LABEL = { not_covered: "Not covered", happy_only: "Happy path only", happy_and_negative: "Happy + negative" };
  const BADGE_CLASS = { not_covered: "nc", happy_only: "ho", happy_and_negative: "han" };

  // ------------------------------------------------------------------ project: view, select, run, PR
  function drawCoverageAndFlow(el, state, sid, collPath, data, analysis, sessionName) {
    const byEndpoint = {};
    data.coverage.forEach((c) => { byEndpoint[c.endpoint] = c.badge; });
    // only pre-check a case when its endpoint isn't in the collection at all - "happy_only"
    // already has a matching request (coverage matches by method+path shape, not body), so
    // pre-checking it too would default to suggesting a near-duplicate add. The user can still
    // check it manually to add negative coverage; the per-case "Already in collection" badge
    // tells them what's already there.
    const defaultChecked = (c) => c.selected && (byEndpoint[c.endpoint] || "not_covered") === "not_covered";
    const envOptions = state.project.environments.map((e) => `<option value="${esc(e.path)}">${esc(e.path)}</option>`).join("");

    el.innerHTML = `
      <div class="covsummary">
        <span class="covpill nc">${data.summary.not_covered} not covered</span>
        <span class="covpill ho">${data.summary.happy_only} happy only</span>
        <span class="covpill han">${data.summary.happy_and_negative} happy + negative</span>
      </div>
      <p class="hint">Style: base URL var <code>${esc(data.style.base_url_var)}</code>, token var
        <code>${esc(data.style.token_var || "none detected")}</code>, variables set on
        <code>${esc(data.style.variable_scope)}</code>.</p>
      <details class="endpointlist"><summary>View all ${data.coverage.length} endpoints in <code>${esc(collPath)}</code></summary>
        <table class="list"><thead><tr><th>Endpoint</th><th>Coverage</th></tr></thead><tbody>
          ${data.coverage.map((c) => `<tr><td class="mono">${stamp(c.method)} ${esc(c.template)}</td>
            <td><span class="covpill ${BADGE_CLASS[c.badge]}">${BADGE_LABEL[c.badge]}</span></td></tr>`).join("")}
        </tbody></table>
      </details>

      <h3>API calls from "${esc(sessionName)}"</h3>
      <p class="hint">Check the ones that are part of this flow - uncheck any to leave them out of the run and the PR.
        Click a row to see the actual generated request(s).</p>
      <div class="caselist">
        ${analysis.cases.map((c) => `<div class="caserow">
          <div class="caserow-top">
            <label class="caserow-check"><input type="checkbox" data-case="${esc(c.id)}" ${defaultChecked(c) ? "checked" : ""}></label>
            <button type="button" class="caserow-info" data-expand="${esc(c.id)}" aria-expanded="false">
              <span class="kind ${esc(c.kind)}">${esc(KIND_LABEL[c.kind] || c.kind)}</span>
              <span class="ct">${esc(c.title)}</span>
              <span class="covpill ${BADGE_CLASS[byEndpoint[c.endpoint] || "not_covered"]}">${BADGE_LABEL[byEndpoint[c.endpoint] || "not_covered"]}</span>
              <span class="chev">▾</span>
            </button>
          </div>
          <div class="casedetail" id="detail-${esc(c.id)}" hidden></div>
        </div>`).join("")}
      </div>

      <fieldset class="target">
        <legend>When you open a PR</legend>
        <label><input type="radio" name="mode" value="existing" checked> Add a new folder to <code>${esc(collPath)}</code></label>
        <label><input type="radio" name="mode" value="new"> Create a new collection file</label>
        <div id="newFields" class="newfields" hidden>
          <input id="newName" placeholder="Collection name">
          <input id="newPath" placeholder="Folder in the repo (blank = root)">
        </div>
        ${state.project.environments.length ? `<label class="envpick">Add missing env vars to <select id="envSel"><option value="">(none)</option>${envOptions}</select></label>` : ""}
        <label>Flow name (used for the branch and PR title) <input id="flowName" value="${esc(sessionName)}"></label>
      </fieldset>

      <div class="flowactions">
        <button class="btn" id="runFlowBtn">Run flow</button>
        <button class="btn primary" id="previewChangeBtn">Preview &amp; open PR</button>
      </div>
      <div id="runResult"></div>
      <div id="changeResult"></div>`;

    el.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", () => {
      document.getElementById("newFields").hidden = r.value !== "new" || !r.checked;
    }));
    el.querySelectorAll("[data-expand]").forEach((btn) => btn.addEventListener("click", () => {
      const caseId = btn.dataset.expand;
      const detail = document.getElementById(`detail-${caseId}`);
      detail.hidden = !detail.hidden;
      btn.setAttribute("aria-expanded", String(!detail.hidden));
      if (!detail.hidden && !detail.dataset.loaded) loadCaseScript(state, sid, collPath, caseId, analysis, detail);
    }));
    document.getElementById("runFlowBtn").addEventListener("click", () => runFlow(sid, analysis));
    document.getElementById("previewChangeBtn").addEventListener("click", () => previewChange(state, sid, collPath));
  }

  async function loadCaseScript(state, sid, collPath, caseId, analysis, detail) {
    detail.innerHTML = `<p class="empty">Loading script…</p>`;
    try {
      const data = await api(`/api/projects/${state.pid}/case-script?sid=${sid}&case_id=${caseId}&collection=${encodeURIComponent(collPath)}`);
      detail.innerHTML = `<button class="btn small" id="runCase-${esc(caseId)}">Run this</button>
        <div class="runresult" id="runResult-${esc(caseId)}"></div>
        ${data.items.map(scriptItemHtml).join("")}`;
      detail.dataset.loaded = "1";
      const runBtn = document.getElementById(`runCase-${caseId}`);
      const runRes = document.getElementById(`runResult-${caseId}`);
      runBtn.addEventListener("click", () => runCasesInline(sid, analysis, [caseId], runRes, runBtn, "Run this"));
    } catch (ex) { detail.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; }
  }

  const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];

  function scriptItemHtml(item) {
    const req = item.request;
    const headerLines = (req.header || []).map((h) => `${h.key}: ${h.value}`).join("\n");
    const body = req.body && req.body.raw ? req.body.raw : "";
    const pre = (item.event || []).find((e) => e.listen === "prerequest");
    const test = (item.event || []).find((e) => e.listen === "test");
    const scriptParts = [];
    if (pre) scriptParts.push("// prerequest script\n" + pre.script.exec.join("\n"));
    if (test) scriptParts.push("// test script\n" + test.script.exec.join("\n"));
    return `<div class="scriptitem" data-item-id="${esc(item.id)}">
      <div class="scriptitem-head">
        <span class="mono">${esc(item.name)}</span>
        <span class="existbadge ${item.already_in_collection ? "exists" : "new"}">${item.already_in_collection ? "Already in collection" : "New"}</span>
      </div>
      <p class="hint">You can edit the request below before running - your changes are used only for the run, not saved.</p>
      <div class="reqedit">
        <div class="reqedit-line">
          <select data-f="method">${HTTP_METHODS.map((m) => `<option ${m === req.method ? "selected" : ""}>${m}</option>`).join("")}</select>
          <input data-f="url" class="mono" value="${esc(req.url.raw)}">
        </div>
        <label>Headers<textarea data-f="headers" class="mono" rows="2" placeholder="Key: value">${esc(headerLines)}</textarea></label>
        <label>Body<textarea data-f="body" class="mono" rows="6">${esc(body)}</textarea></label>
      </div>
      ${scriptParts.length ? `<pre class="code small">${esc(scriptParts.join("\n\n"))}</pre>` : ""}
    </div>`;
  }

  function collectRequestOverrides(root) {
    const overrides = {};
    root.querySelectorAll(".scriptitem[data-item-id]").forEach((el) => {
      overrides[el.dataset.itemId] = {
        method: el.querySelector('[data-f="method"]').value,
        url: el.querySelector('[data-f="url"]').value,
        headers: el.querySelector('[data-f="headers"]').value,
        body: el.querySelector('[data-f="body"]').value,
      };
    });
    return overrides;
  }

  function checkedCaseIds() {
    return [...document.querySelectorAll('.caselist [data-case]:checked')].map((el) => el.dataset.case);
  }

  function runFlow(sid, analysis) {
    const caseIds = checkedCaseIds();
    if (!caseIds.length) { toast("Select at least one API call to run."); return; }
    runCasesInline(sid, analysis, caseIds, document.getElementById("runResult"),
      document.getElementById("runFlowBtn"), "Run flow");
  }

  function neededEnvFor(analysis, caseIds) {
    const wanted = new Set(caseIds);
    const needed = new Set();
    analysis.cases.filter((c) => wanted.has(c.id)).forEach((c) => c.needs_env.forEach((e) => needed.add(e)));
    return [...needed].sort();
  }

  // Prompts for any ATLAS_* values the picked cases need (never written to disk), then runs
  // them with Newman and renders the result into resultEl. Shared by "Run flow" and the
  // per-case "Run this" button in the script preview, so neither one skips the prompt.
  function runCasesInline(sid, analysis, caseIds, resultEl, btn, idleLabel) {
    const needed = neededEnvFor(analysis, caseIds);
    const overrides = collectRequestOverrides(document);
    const go = async (env) => {
      if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
      resultEl.innerHTML = `<p class="empty">Running with Newman…</p>`;
      let result;
      try { result = await api(`/api/sessions/${sid}/newman`, { method: "POST", body: { case_ids: caseIds, env, overrides } }); }
      catch (ex) { resultEl.innerHTML = `<p class="empty">${esc(ex.message)}</p>`; }
      if (btn) { btn.disabled = false; btn.textContent = idleLabel; }
      if (result) drawInlineNewmanResults(resultEl, sid, result);
    };
    if (needed.length) drawEnvPromptModal(needed, go);
    else go({});
  }

  function drawEnvPromptModal(needed, onSubmit) {
    $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Values needed to run">
      <header><h3>Before running…</h3><button class="btn quiet" id="closeModal">Close</button></header>
      <form class="envform" id="envForm">
        <p class="hint">These are used only for this run &mdash; never written to disk. Leave blank to see what fails.</p>
        ${needed.map((e) => `<label>${esc(e)}<input type="password" name="${esc(e)}" autocomplete="off"></label>`).join("")}
        <button class="btn primary" type="submit">Run</button>
      </form></div>`;
    $modal.hidden = false;
    document.getElementById("closeModal").addEventListener("click", closeModal);
    document.getElementById("envForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const env = {};
      for (const el of e.target.elements) if (el.name) env[el.name] = el.value;
      closeModal();
      onSubmit(env);
    });
  }

  async function previewChange(state, sid, collPath) {
    const caseIds = checkedCaseIds();
    if (!caseIds.length) { toast("Select at least one API call to add."); return; }
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const flowName = document.getElementById("flowName").value || "New tests";
    const envSel = document.getElementById("envSel");
    const target = mode === "existing"
      ? { mode, collection: collPath, env: envSel ? envSel.value || null : null }
      : { mode, collection_name: document.getElementById("newName").value || flowName,
          new_path: document.getElementById("newPath").value || "" };
    const btn = document.getElementById("previewChangeBtn");
    btn.disabled = true; btn.textContent = "Checking out a branch and generating the diff…";
    const res = document.getElementById("changeResult");
    res.innerHTML = "";
    let change;
    try { change = await api(`/api/projects/${state.pid}/changes`, { method: "POST",
      body: { sid, case_ids: caseIds, target, flow_name: flowName } }); }
    catch (ex) { toast(ex.message); btn.disabled = false; btn.textContent = "Preview & open PR"; return; }
    btn.remove();
    drawChangeResult(res, state, change);
  }

  function drawChangeResult(el, state, change) {
    const covLine = change.coverage_before
      ? `Coverage: ${change.coverage_before.happy_only + change.coverage_before.happy_and_negative}/${change.coverage_before.total} → ${change.coverage_after.happy_only + change.coverage_after.happy_and_negative}/${change.coverage_after.total} endpoints`
      : "";
    el.innerHTML = `
      <p><b>Branch</b> <code>${esc(change.branch)}</code> from <code>${esc(change.base_branch)}</code></p>
      ${covLine ? `<p>${esc(covLine)}</p>` : ""}
      ${change.needs_env.length ? `<p class="hint">Env vars to fill: ${change.needs_env.map((e) => `<code>${esc(e)}</code>`).join(" ")}</p>` : ""}
      ${change.warnings.map((w) => `<p class="hint warn">⚠ ${esc(w)}</p>`).join("")}
      <details open><summary>Diff</summary><pre class="code small">${esc(change.diff)}</pre></details>
      ${change.has_remote
        ? `<button class="btn primary" id="pushPrBtn">Push &amp; open PR</button>`
        : `<p class="hint">This project has no git remote, so it can't be pushed. The branch and commit are ready locally in a worktree.</p>`}`;
    const pushBtn = document.getElementById("pushPrBtn");
    if (pushBtn) pushBtn.addEventListener("click", async () => {
      pushBtn.disabled = true; pushBtn.textContent = "Pushing…";
      try {
        const result = await api(`/api/projects/${state.pid}/changes/${change.change_id}/submit`, { method: "POST", body: {} });
        if (result.ok) {
          el.insertAdjacentHTML("beforeend", `<p>✓ <a href="${esc(result.url)}" target="_blank" rel="noopener">${esc(result.url)}</a></p>`);
        } else if (result.url) {
          el.insertAdjacentHTML("beforeend", `<p>Pushed, but couldn't open a PR automatically${result.error ? ` (${esc(result.error)})` : ""}. <a href="${esc(result.url)}" target="_blank" rel="noopener">Open a PR manually</a></p>`);
        } else {
          toast(result.error || "Couldn't open a PR.");
        }
      } catch (ex) { toast(ex.message); }
      pushBtn.remove();
    });
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
    drawPane(state);
  }

  function drawPane(state) {
    if (state.tab === "traffic") drawTraffic(state);
    else if (state.tab === "graph") drawGraph(state);
    else drawTests(state);
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
        ${!rec && sessionStorage.getItem("atlas_return_to_project")
          ? `<a class="btn primary" href="#/p/${esc(sessionStorage.getItem("atlas_return_to_project"))}">Back to project</a>` : ""}
        ${!rec ? `<button class="btn quiet" id="reclassifyBtn" title="Re-apply the noise filter to already-recorded calls, e.g. after an API Atlas update">Clean up noise</button>` : ""}
        <a class="btn quiet" href="/api/sessions/${state.sid}/export/har" download>Download HAR</a>
        <button class="btn quiet danger" id="delBtn">Delete</button>
      </div></div>`;
    const reclassifyBtn = document.getElementById("reclassifyBtn");
    if (reclassifyBtn) reclassifyBtn.addEventListener("click", async () => {
      reclassifyBtn.disabled = true; reclassifyBtn.textContent = "Checking…";
      try {
        const r = await api(`/api/sessions/${state.sid}/reclassify`, { method: "POST", body: {} });
        toast(r.changed ? `Hid ${plural(r.changed, "call")} that's now recognised as noise.` : "Nothing to clean up.");
        if (r.changed) renderSession(state.sid, state.tab);
      } catch (ex) { toast(ex.message); reclassifyBtn.disabled = false; reclassifyBtn.textContent = "Clean up noise"; }
    });
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
    const tabs = [["traffic", "Traffic", visible], ["tests", "Endpoints & tests", null], ["graph", "Dependency graph", null]];
    const el = document.getElementById("tabs");
    el.innerHTML = tabs.map(([id, label, n]) =>
      `<button role="tab" aria-selected="${state.tab === id}" data-tab="${id}">${label}${n !== null ? `<span class="count">${n}</span>` : ""}</button>`).join("");
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      state.tab = b.dataset.tab;
      history.replaceState(null, "", `#/s/${state.sid}/${state.tab}`);
      drawTabs(state);
      drawPane(state);
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

  // ------------------------------------------------------------------ graph tab
  // Draws the dependency links analysis.py already computes (CaseBuilder/find_links
  // feed the generator with these; this is the first UI for them) as a layered
  // left-to-right flow: endpoints with no incoming link start the leftmost column,
  // each consumer sits one column to the right of its producer.
  async function drawGraph(state) {
    drawHeader(state);
    const pane = document.getElementById("pane");
    if (!state.calls.filter((c) => !c.hidden).length) {
      pane.innerHTML = `<div class="waiting">Nothing recorded yet.</div>`;
      removeBar();
      return;
    }
    if (!state.analysis) {
      pane.innerHTML = `<p class="empty">Working out endpoints and tests…</p>`;
      try { state.analysis = await api(`/api/sessions/${state.sid}/analysis`); }
      catch (ex) { pane.innerHTML = `<p class="empty">Couldn't analyse this exploration: ${esc(ex.message)}</p>`; return; }
    }
    removeBar();
    const a = state.analysis;
    if (!a.endpoints.length) { pane.innerHTML = `<p class="empty">No endpoints yet.</p>`; return; }

    const byKey = {};
    a.endpoints.forEach((e) => { byKey[e.key] = e; });
    // One edge per (producer, consumer) pair; a pair can have several dependent
    // fields, so keep all of them for the tooltip but draw a single line.
    const edgeMap = new Map();
    a.endpoints.forEach((e) => {
      (e.depends_on || []).forEach((d) => {
        if (!byKey[d.endpoint]) return; // producer endpoint not in this view (shouldn't happen)
        const k = `${d.endpoint}>${e.key}`;
        if (!edgeMap.has(k)) edgeMap.set(k, { from: d.endpoint, to: e.key, kinds: new Set(), fields: [] });
        const edge = edgeMap.get(k);
        edge.kinds.add(d.kind);
        edge.fields.push(`${d.kind === "reuse" ? "reuses" : "takes"} ${d.field} as ${d.used_in}`);
      });
    });
    const edges = [...edgeMap.values()];
    const connected = new Set();
    edges.forEach((ed) => { connected.add(ed.from); connected.add(ed.to); });
    const standalone = a.endpoints.filter((e) => !connected.has(e.key));

    if (!edges.length) {
      pane.innerHTML = `<p class="help" style="margin-bottom:20px">No call here uses a value from another one — every endpoint works on its own.</p>
        <div class="gstandalone">${a.endpoints.map((e) => graphChip(e)).join("")}</div>`;
      wireGraphClicks(pane, state);
      return;
    }

    // Longest-path layering needs a DAG, but two calls can each reuse a value
    // the other one sent (a retry/polling pair, for instance), so this can
    // have real cycles. Collapse each strongly-connected component (Tarjan)
    // into one column first — that condensation is always acyclic — then
    // layer that; nodes with no principled order just share a column instead
    // of fighting over which one goes "first".
    const order = [...connected];
    const adj = {};
    edges.forEach((ed) => { (adj[ed.from] = adj[ed.from] || []).push(ed.to); });
    const comp = stronglyConnected(order, adj);
    const condIncoming = {};
    const seenCondEdge = new Set();
    edges.forEach((ed) => {
      const cf = comp.get(ed.from), ct = comp.get(ed.to);
      if (cf === ct) return;
      const ck = `${cf}>${ct}`;
      if (seenCondEdge.has(ck)) return;
      seenCondEdge.add(ck);
      (condIncoming[ct] = condIncoming[ct] || []).push(cf);
    });
    const compIds = [...new Set(order.map((n) => comp.get(n)))];
    const compLevel = {};
    for (let pass = 0; pass < compIds.length + 1; pass++) {
      let changed = false;
      compIds.forEach((id) => {
        const ins = condIncoming[id] || [];
        const want = ins.length ? Math.max(...ins.map((p) => (compLevel[p] || 0))) + 1 : 0;
        if ((compLevel[id] || 0) < want) { compLevel[id] = want; changed = true; }
      });
      if (!changed) break; // the condensation is acyclic, so this always settles
    }
    const level = {};
    order.forEach((key) => { level[key] = compLevel[comp.get(key)] || 0; });

    // A plain array indexed by level can end up sparse (e.g. a level with no
    // node of its own), and Math.max(...sparse.map(...)) silently yields NaN
    // on the holes — build every column explicitly instead.
    const maxLevel = Math.max(0, ...[...connected].map((key) => level[key] || 0));
    const columns = Array.from({ length: maxLevel + 1 }, () => []);
    connected.forEach((key) => { columns[level[key] || 0].push(key); });
    // A long reuse chain (e.g. a token that rotates on every call) can need
    // dozens of columns; shrink them to fit instead of forcing huge horizontal
    // scrolling, down to a floor where the truncated label + tooltip still work.
    const COL_W = Math.max(90, Math.min(250, 1900 / columns.length));
    const NODE_W = Math.max(70, COL_W - 20);
    const ROW_H = 58, PAD = 20;
    const pos = {};
    columns.forEach((keys, col) => {
      keys.forEach((key, row) => { pos[key] = { x: PAD + col * COL_W, y: PAD + row * ROW_H }; });
    });
    const width = PAD * 2 + (columns.length - 1) * COL_W + NODE_W;
    const height = PAD * 2 + Math.max(...columns.map((c) => c.length)) * ROW_H;

    const edgeSvg = edges.map((ed) => {
      const p1 = pos[ed.from], p2 = pos[ed.to];
      if (!p1 || !p2) return "";
      const x1 = p1.x + NODE_W, y1 = p1.y + 20, x2 = p2.x, y2 = p2.y + 20;
      const mx = (x1 + x2) / 2;
      const dashed = ed.kinds.has("response") ? "" : ' stroke-dasharray="4 4"';
      return `<path data-from="${esc(ed.from)}" data-to="${esc(ed.to)}"
        d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none"${dashed}><title>${esc(ed.fields.join("; "))}</title></path>`;
    }).join("");

    // Per node: which variable ties it to which neighbour, in each direction —
    // same info find_links feeds the generator, just shown instead of only
    // driving test setup steps behind the scenes.
    const outOf = {}, into = {};
    edges.forEach((ed) => {
      (outOf[ed.from] = outOf[ed.from] || []).push(ed);
      (into[ed.to] = into[ed.to] || []).push(ed);
    });

    pane.innerHTML = `
      <p class="help" style="margin-bottom:16px">Which call's response or request field feeds which other call. Solid = takes a value
        from a response; dashed = reuses a value it sent itself earlier. Hover a call to see the variables; click it to open it under
        "Endpoints & tests".</p>
      <div class="graph" style="width:${width}px;height:${height}px">
        <svg class="graph-edges" width="${width}" height="${height}">${edgeSvg}</svg>
        ${[...connected].map((key) => graphNode(byKey[key], pos[key], NODE_W)).join("")}
      </div>
      <div class="section" id="gdetail"><h4>Chained by</h4><p class="help">Hover a call above to see which variables connect it to the others.</p></div>
      ${standalone.length ? `<div class="section"><h4>Work on their own</h4><div class="gstandalone">
        ${standalone.map((e) => graphChip(e)).join("")}</div></div>` : ""}`;
    wireGraphClicks(pane, state);
    wireGraphHover(pane, byKey, outOf, into);
  }

  function wireGraphHover(pane, byKey, outOf, into) {
    const svg = pane.querySelector(".graph-edges");
    const detail = document.getElementById("gdetail");
    pane.querySelectorAll(".gnode").forEach((n) => {
      const key = n.dataset.key;
      const show = () => {
        svg.querySelectorAll("path").forEach((p) => p.classList.toggle("hi", p.dataset.from === key || p.dataset.to === key));
        const rows = [
          ...(into[key] || []).map((ed) => `<li>From ${stamp(byKey[ed.from].method)} <code>${esc(byKey[ed.from].template)}</code>: ${ed.fields.map(esc).join("; ")}</li>`),
          ...(outOf[key] || []).map((ed) => `<li>To ${stamp(byKey[ed.to].method)} <code>${esc(byKey[ed.to].template)}</code>: ${ed.fields.map(esc).join("; ")}</li>`),
        ];
        detail.innerHTML = `<h4>Chained by — ${stamp(byKey[key].method)}<code>${esc(byKey[key].template)}</code></h4>
          <ul class="deps">${rows.join("")}</ul>`;
      };
      n.addEventListener("mouseenter", show);
      n.addEventListener("focus", show);
      n.addEventListener("mouseleave", () => svg.querySelectorAll("path.hi").forEach((p) => p.classList.remove("hi")));
    });
  }

  function graphNode(e, p, nodeW) {
    return `<button class="gnode" data-key="${esc(e.key)}" style="left:${p.x}px;top:${p.y}px;width:${nodeW}px" title="${esc(e.template)}">
      ${stamp(e.method)}<span>${esc(e.template)}</span></button>`;
  }

  function graphChip(e) {
    return `<button class="gchip" data-key="${esc(e.key)}">${stamp(e.method)}<span>${esc(e.template)}</span></button>`;
  }

  function wireGraphClicks(pane, state) {
    pane.querySelectorAll("[data-key]").forEach((b) => b.addEventListener("click", () => {
      state.current = b.dataset.key;
      state.tab = "tests";
      history.replaceState(null, "", `#/s/${state.sid}/tests`);
      drawTabs(state);
      drawPane(state);
    }));
  }

  // Tarjan's algorithm: returns a Map from node -> id of the strongly-connected
  // component it belongs to (a lone node with no cycle through it gets its own id).
  function stronglyConnected(nodes, adj) {
    let index = 0, nextComp = 0;
    const stack = [], onStack = new Set(), idx = new Map(), low = new Map(), comp = new Map();
    const visit = (v) => {
      idx.set(v, index); low.set(v, index); index++;
      stack.push(v); onStack.add(v);
      (adj[v] || []).forEach((w) => {
        if (!idx.has(w)) { visit(w); low.set(v, Math.min(low.get(v), low.get(w))); }
        else if (onStack.has(w)) { low.set(v, Math.min(low.get(v), idx.get(w))); }
      });
      if (low.get(v) === idx.get(v)) {
        const id = nextComp++;
        let w;
        do { w = stack.pop(); onStack.delete(w); comp.set(w, id); } while (w !== v);
      }
    };
    nodes.forEach((n) => { if (!idx.has(n)) visit(n); });
    return comp;
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
  function requireSelection(state) {
    if (state.analysis.selected_count) return true;
    toast("Select at least one test first.");
    return false;
  }
  function drawBar(state) {
    removeBar();
    const a = state.analysis;
    const bar = document.createElement("div");
    bar.className = "bar"; bar.id = "bar";
    const n = a.selected_count;
    bar.innerHTML = `<div class="sum"><b>${plural(n, "test")}</b> selected across ${plural(a.endpoints.length, "endpoint")}.
      ${a.needs_env.length ? `<span class="env">Before running, set ${a.needs_env.map((e) => `<code>${esc(e)}</code>`).join(" ")}.</span>` : "No secrets to set."}</div>
      <button class="btn" id="previewBtn">Preview code</button>
      <button class="btn" id="runBtn">Run tests</button>
      <a class="btn" id="dlPostman" href="/api/sessions/${state.sid}/export/postman" download>Download Postman</a>
      <a class="btn primary" id="dlPytest" href="/api/sessions/${state.sid}/export/pytest" download>Download pytest</a>`;
    document.body.appendChild(bar);
    document.getElementById("previewBtn").addEventListener("click", () => { if (requireSelection(state)) preview(state, "pytest"); });
    document.getElementById("runBtn").addEventListener("click", () => { if (requireSelection(state)) runNewman(state); });
    for (const [elId, fmt] of [["dlPostman", "postman"], ["dlPytest", "pytest"]]) {
      document.getElementById(elId).addEventListener("click", (e) => { if (!requireSelection(state)) e.preventDefault(); });
    }
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
  // ------------------------------------------------------------------ run with newman
  function runNewman(state) {
    const needed = state.analysis.needs_env;
    if (needed.length) drawEnvForm(state, needed);
    else doRunNewman(state, {});
  }

  function drawEnvForm(state, needed) {
    $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Values needed to run">
      <header><h3>Before running…</h3><button class="btn quiet" id="closeModal">Close</button></header>
      <form class="envform" id="envForm">
        <p class="hint">These are used only for this run &mdash; never written to disk. Leave blank to see what fails.</p>
        ${needed.map((e) => `<label>${esc(e)}<input type="password" name="${esc(e)}" autocomplete="off"></label>`).join("")}
        <button class="btn primary" type="submit">Run tests</button>
      </form></div>`;
    $modal.hidden = false;
    document.getElementById("closeModal").addEventListener("click", closeModal);
    document.getElementById("envForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const env = {};
      for (const el of e.target.elements) if (el.name) env[el.name] = el.value;
      doRunNewman(state, env);
    });
  }

  async function doRunNewman(state, env) {
    $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Running tests">
      <header><h3>Running the Postman collection with Newman…</h3>
        <button class="btn quiet" id="closeModal">Close</button></header>
      <pre class="code">Starting…</pre></div>`;
    $modal.hidden = false;
    document.getElementById("closeModal").addEventListener("click", closeModal);
    let result;
    try { result = await api(`/api/sessions/${state.sid}/newman`, { method: "POST", body: { env } }); }
    catch (ex) {
      $modal.querySelector("pre.code").textContent = ex.message;
      $modal.querySelector("h3").textContent = "Couldn't run the tests";
      return;
    }
    drawNewmanResults(state, result);
  }

  function _newmanRows(result) {
    return result.cases.map((c) => `
      <div class="runcase ${c.ok ? "pass" : "fail"}">
        <div class="runcase-head"><span class="rc-icon">${c.ok ? "✓" : "✗"}</span><b>${esc(c.title)}</b>
          <span class="kind ${esc(c.kind)}">${esc(KIND_LABEL[c.kind] || c.kind)}</span></div>
        ${c.reasons.map((r, i) => `
          <div class="reason">
            ${r.step ? `<div class="reason-step">${r.is_target === false ? "Setup call" : r.is_target === true ? "This call" : "Request"}: <b>${esc(r.step)}</b></div>` : ""}
            <div>${esc(r.plain)}${r.hint ? `: ${esc(r.hint)}` : ""}</div>
            ${r.is_target === false ? `<div class="hint">This is an earlier step in the chain failing, not the request being tested - fix this first, the later failure is probably just a side effect.</div>` : ""}
            ${r.kind === "status" && r.is_target !== false ? `<button class="btn quiet small" data-case="${esc(c.id)}" data-status="${r.got_status}" data-reason="${i}">Expect ${r.got_status} instead</button>` : ""}
            ${r.request ? `<details><summary>Request sent</summary>
              <div class="mono small">${esc(r.request.method)} ${esc(r.request.path)}</div>
              ${r.request.snippet ? `<pre class="code small">${esc(r.request.snippet)}</pre>` : ""}
            </details>` : ""}
            ${r.snippet ? `<details><summary>Response</summary><pre class="code small">${esc(r.snippet)}</pre></details>` : ""}
          </div>`).join("")}
        ${c.checks.length ? `<div class="checklist">
          ${c.checks.map((ch) => `<div class="checkrow ${ch.ok ? "pass" : "fail"}">${ch.ok ? "✓" : "✗"} ${esc(ch.name)}</div>`).join("")}
        </div>` : ""}
        ${c.request ? `<details class="respdetail"><summary>Request sent (${esc(c.request.method)})</summary>
          <div class="mono small">${esc(c.request.path)}</div>
          ${c.request.snippet ? `<pre class="code small">${esc(c.request.snippet)}</pre>` : ""}
        </details>` : ""}
        ${c.response ? `<details class="respdetail"><summary>Response${c.response.status ? ` (${c.response.status})` : ""}</summary>
          ${c.response.headers.length ? `<pre class="code small">${esc(c.response.headers.map((h) => `${h.key}: ${h.value}`).join("\n"))}</pre>` : ""}
          ${c.response.snippet ? `<pre class="code small">${esc(c.response.snippet)}</pre>` : ""}
        </details>` : ""}
      </div>`).join("");
  }

  function drawNewmanResults(state, result) {
    $modal.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-label="Test run results">
      <header><h3>${result.ok ? "✓" : "✗"} ${result.passed}/${result.total} test folders passed</h3>
        <button class="btn quiet" id="closeModal">Close</button></header>
      <div class="runresults">${_newmanRows(result)}</div></div>`;
    $modal.hidden = false;
    document.getElementById("closeModal").addEventListener("click", closeModal);
    document.getElementById("closeModal").focus();
    $modal.querySelectorAll("[data-case]").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      await saveCase(state, btn.dataset.case, { expect_status: Number(btn.dataset.status) });
      btn.textContent = "Updated — re-run to confirm";
    }));
  }

  function drawInlineNewmanResults(el, sid, result) {
    el.innerHTML = `<div class="runheader">${result.ok ? "✓" : "✗"} ${result.passed}/${result.total} test folders passed</div>
      <div class="runresults inline">${_newmanRows(result)}</div>`;
    if (!sid) { el.querySelectorAll("[data-case]").forEach((btn) => btn.remove()); return; }
    el.querySelectorAll("[data-case]").forEach((btn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/api/sessions/${sid}/cases/${btn.dataset.case}`, { method: "PATCH", body: { expect_status: Number(btn.dataset.status) } });
        btn.textContent = "Updated — re-run to confirm";
      } catch (ex) { toast(ex.message); btn.disabled = false; }
    }));
  }
  function closeModal() { $modal.hidden = true; $modal.innerHTML = ""; }
  $modal.addEventListener("click", (e) => { if (e.target === $modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });

  route();
})();
