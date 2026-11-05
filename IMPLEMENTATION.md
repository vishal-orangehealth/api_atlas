# API Atlas — Implementation Notes

This is a from-the-ground-up explanation of what has been built, how the pieces fit
together, and why each design decision was made the way it was. It complements
`CLAUDE.md` (which is the terse, "don't break this" reference) with the fuller story.

Status at time of writing: **108 passed, 1 skipped** (`pytest`). No AI/LLM is used
anywhere in this codebase — every step described below is deterministic Python.

---

## 1. What the product does, end to end

1. You point a Chromium browser (via Playwright) at a web app and click around it
   normally — log in, create something, view a list, etc.
2. Every network response the app makes is captured, stripped of secrets, and stored.
3. You hit "Analyze" and the engine figures out:
   - which calls are the *same endpoint* hit with different parameters (`/products/1`,
     `/products/2` → `/products/{id}`),
   - which calls *depend on* earlier calls (a login's token used by later requests),
   - and from that, a set of runnable test **cases** — a happy path per endpoint, plus
     opt-in negative cases (missing field, wrong type, invalid id, no auth).
4. You export that as a **pytest + requests** suite or a **Postman v2.1 collection**.
5. (Phase 2) You can also run the suite for real via **Newman**, open an existing
   project's own Postman collection and get **coverage** against what you explored, and
   turn gaps into a **pull request** against that repo.
6. (Latest addition) If a project already has its own Postman collection, you can skip
   exploration entirely and get coverage + run results straight from the collection.

---

## 2. Capture pipeline (Phase 1)

```
capture.py              Playwright page.on("response") listener
  -> engine/callmodel.build_call     turn a raw Playwright response into a plain dict
                                      (method, url, path, query, headers, body, status, ts)
  -> engine/filter.classify          decide keep / hide (noise) for this call
  -> engine/values.fingerprints      salted SHA-256 hash of every leaf value, RAW values
                                      never leave this step
  -> engine/mask.mask_call           replace secrets/PII in the call body with
                                      <masked:kind> placeholders
  -> store.add_call                  only the masked call + its fingerprints are persisted
```

### `engine/filter.py` — noise classification
Browsers fire a lot of calls that have nothing to do with the app's own API: analytics
beacons, Next.js dev-server internals, telemetry pings, etc. `classify()` hides these by
default so they don't pollute endpoint lists or (more importantly) the dependency graph.
`NOISE_PATH` is a regex of known noise path prefixes (`/collect`, `/beacon`, `/__webpack_hmr`,
`/__nextjs_*`, …). A call can also be hidden for host or response-type reasons (non-JSON,
off-origin).

A session's noise filter is **not frozen at capture time** — `/api/sessions/{id}/reclassify`
re-runs `classify()` against already-stored calls and flips their hidden flag. This matters
because real-world apps surprise you (a new dev-tool path you hadn't seen), and re-filtering
after the fact removes those calls from analysis/links without re-capturing the whole session.

### `engine/values.py` — fingerprinting, never raw values
Every leaf value in a call (body fields recursively, headers, cookies, query params, path
segments) becomes:

```python
{"loc": {"side": "req"|"resp", "part": "body"|"header"|"cookie"|"query"|"path", "path": [...]},
 "h": sha256(salt + canonical(value))[:20],
 "weak": bool,   # short value (e.g. id "5") - only linked when both ends look like ids
 "idk": bool}    # the field NAME looks like an identifier (id, _id, uuid, slug, key, code, ref)
```

- `canonical()` filters out boolean/None, long strings (>4000 chars, likely blobs), and a
  `STOPWORDS` set (`"true"`, `"json"`, `"admin"`, …) so trivial/common values never create
  false links.
- The salt (`Recorder.salts`, per session) lives only in memory for the capture's lifetime —
  it is **never persisted**, so even if the database leaked, fingerprints can't be reversed
  or compared across sessions.
- `IGNORED_REQ_HEADERS` / `IGNORED_RESP_HEADERS` strip transport-level headers (`user-agent`,
  `content-length`, `etag`, CORS headers, …) that would otherwise spam the link graph.

### `engine/mask.py` — secret masking
Runs on the call *before* it's stored. Detects emails, phone numbers, JWTs, bearer tokens,
card/PAN-looking numbers, and generic high-entropy secrets, replacing them with
`<masked:kind>` placeholders. This is the one hard product rule that's checked by multiple
tests: **raw secret values must never reach `store.add_call`, logs, or the UI.**

---

## 3. Analysis pipeline (Phase 1)

```
server.py  GET /api/sessions/{id}/analysis
  -> engine/analysis.analyse(calls)
       -> engine/deps.find_links(calls)        value-flow dependency graph
       -> engine/normalize.build_templates      group calls into endpoint templates
       -> CaseBuilder                           turn endpoints + links into test case IR
```

### `engine/deps.py` — API chaining (the core "how do calls relate" logic)
This is the piece that makes the generated tests *runnable as a chain* instead of a flat
list of isolated requests. Full write-up is in the conversation above; summary:

- Walking calls in capture order, every request-side fingerprint hash is checked against
  hashes seen so far.
- If it matches an **earlier request's** value → `"reuse"` link (e.g. the same email typed
  into signup, then login).
- Else if it matches an **earlier response's** value → `"response"` link (e.g. a login
  response's token used as a later `Authorization` header; a created resource's id used in
  a later `/resource/{id}` path).
- `_ok()` guards weak/short values (ids like `5`) so they only link when both ends plausibly
  look like identifiers — otherwise two unrelated short numbers would wrongly "chain."
- Cookies are a special case: the link points to the response that **first** set the cookie
  (`Set-Cookie`), since that's the one that matters for "where did this session come from,"
  not the most recent responder.

### `engine/normalize.py` — endpoint templating
Groups calls like `/products/1`, `/products/2`, `/products/42` into one endpoint template
`/products/{id}` by detecting which path segments vary across calls and which don't (and
cross-checking against the link graph — a segment is only treated as a parameter if
something feeds it, or it "looks like" a param: numeric, uuid, slug-ish).

### `CaseBuilder` (`engine/analysis.py`) — turning links into runnable steps
- `chain(target_id)` walks the link graph backwards from a target call to collect every
  setup call it transitively depends on, in capture order — this becomes the case's
  `setup` list.
- Each link becomes a named **variable**:
  - `response` link → `{"kind": "extract", "from": <call>, "source": "body"|"header", "path": [...]}`
    — pulled out of the actual HTTP response at run time, not baked in.
  - `reuse` link, masked value → `{"kind": "env", "env": "ATLAS_X", "default": ...}` — comes
    from an environment variable so secrets aren't embedded in the generated test.
  - `reuse` link, sign-up field (email/phone/username on a `register`/`signup` endpoint) →
    `{"kind": "unique", ...}` — generates a fresh value every run so re-running the suite
    doesn't collide with "email already exists."
  - Anything else → `{"kind": "literal", "value": ...}`.
  - Cookie-based links are skipped — the HTTP client's session/cookie jar carries them
    naturally; no variable needed.
- Every place that originally held a linked literal value gets rewritten into a reference:
  `{"$var": "token"}`, or `{"$fmt": ["Bearer ", {"$var": "token"}]}` for prefixed headers.
- `_assertions()` builds the target step's response assertions from the **shape** inferred
  across all captured calls to that endpoint+status (`engine/schema.py`): stable, non-dynamic
  scalar fields get an `equals` check; everything else (ids, timestamps, uuids, tokens) is
  only **type-checked**, specifically so re-running the suite against fresh data doesn't fail
  on a value that was never meant to be stable.
- `cases_for(ep)` builds the opt-in negative cases from the happy-path flow: per-field
  `missing_field` / `wrong_type` (dropping or corrupting up to 6/4 body keys), `empty_body`,
  per-path-param `invalid_id`, and (if the endpoint is always authenticated) a `no_auth` case
  that strips auth headers/cookies and expects a 401/403.

---

## 4. Generators (Phase 1)

`gen/pytest_gen.py` and `gen/postman_gen.py` both render the same case IR — they don't
duplicate any of the chaining/masking/assertion logic above, just the output format.

- **pytest**: one `test_*` function per case, `requests.Session()` per test, setup steps run
  first (asserting only status), target step runs last and is the one actually asserted.
  Env-var variables map to `os.environ.get("ATLAS_X", default)`; unique variables generate a
  fresh value per test run (timestamp/uuid-suffixed).
- **Postman v2.1**: one collection item per case (nested setup + target requests), plus a
  companion environment file for `ATLAS_*` variables. `item_index(cases)` builds a reverse
  map from a rendered item's Postman `id` back to `{case_id, is_target}` — this is what lets
  `engine/newman_report.py` turn a Newman JSON report back into "which case passed/failed."
  `case_items(case)` exposes just one case's items (used by the "view actual script" UI
  feature, see §6).

---

## 5. Phase 2, Step 1 — Run & verify with Newman

Generating a suite is only useful if you can tell whether it actually passes. `server.py`
shells out to Newman (`ATLAS_NEWMAN` env override → `newman` on PATH → pinned
`npx -y newman@6` → else a clear "install Newman" hint, never a silent failure) against the
rendered collection, with `--reporters json` so the result is machine-readable.

### `engine/newman_report.py` — turning Newman JSON into plain English
- `plain_reason()` rewrites Postman's assertion-error text ("expected response to have status
  code 201 but got 409") into "Expected 201, got 409" — the raw message is also usually fine,
  but status-code mismatches are the overwhelming majority of failures and deserve a clean
  one-liner.
- `body_hint()` picks a likely-useful field (`detail`, `message`, …) out of a failing
  response body, so "why did this fail" doesn't require opening a response viewer.
- `_mask_snippet()` re-masks the response body shown in a failure snippet — a passing or
  failing Newman run still touches live secrets in transit, and the UI must never show them,
  even in a debug/failure view.
- `summarize(report, cases, item_index)` groups Newman's per-request executions back up to
  the case level using `item_index`, and — this was a direct fix from live testing — captures
  the target step's **response (status, masked headers, masked body snippet) and the list of
  assertions that ran**, for every case regardless of pass/fail. Originally only failures got
  this detail; a passing 200 showed nothing, which made it impossible to sanity-check what a
  "pass" actually returned.
- `summarize_collection(report, unit_index)` is the project-mode sibling: when you run a
  collection Atlas didn't generate (no case ids of ours), results are grouped by the
  collection's own **top-level folder** instead (via `postman_parse.unit_index`).
- **Step-level failure attribution.** A case's `setup` chain can be several calls deep before
  the target request even runs; without knowing *which* call in that chain actually broke, a
  downstream 401 ("missing token") looks unrelated to the upstream 500 ("login crashed") that
  caused it — this was the exact confusion a live QA run surfaced. `postman_gen.item_index()`
  and `postman_parse.unit_index()` now carry a `step_label` / `step_no` (or `request_name` for
  a third-party collection) per rendered item, and `summarize()` / `summarize_collection()`
  attach `step`, `step_no`, and `is_target` to every failure reason, sorted so the earliest
  broken step is always listed first. The UI (`app.js`) renders this as `Setup call: POST
  /api/auth/login` vs. `This call: GET /api/orders/{id}`, with a hint telling you to fix the
  setup failure first since the target failure is probably just a side effect.
- **The request that actually triggered the failure, not just the response.** Knowing a call
  returned 404 doesn't tell you *which* path param or payload caused it — `_request_summary()`
  reads Newman's `execution.request` (already variable-resolved, i.e. the real bytes that went
  over the wire: `/api/v1/retails/9999`, not `/api/v1/retails/{retail_id}`), masks it the same
  way responses are masked, and attaches it to every failure reason and to the target step's
  own summary. The UI shows this as a "Request sent" block right above "Response" on every
  failing reason line.

---

## 6. UX iteration driven by live testing

A chunk of this build was fixing real friction found by testing against a live QA/staging
app (`orangehealth.dev`), not speculative polish:

- **"Run flow" never prompted for required env vars.** The session page's "Run tests" already
  asked for `ATLAS_*` values via a form; the project page's "Run flow" button skipped straight
  to Newman with no values, silently failing. Fixed by extracting the shared
  `neededEnvFor()` / `drawEnvPromptModal()` / `runCasesInline()` logic in `app.js` so both
  entry points — and the later per-case "Run this" button — go through the same prompt-then-run
  path. Verified live: with the prompt filled in, a failure changed from "set X in environment"
  to a real downstream network error, confirming the value was actually used.
- **Case detail was invisible.** Clicking a case in the list didn't expand to show anything;
  for endpoints with multiple cases (happy + several negatives) there was no way to tell them
  apart. Fixed with an expandable `.caserow` / `.casedetail` UI showing the case's title, kind,
  and (after a run) its checks/response.
- **"Why are we even considering this API?"** — traced to the noise filter missing
  `/__nextjs_original-stack-frames` and similar Next.js dev-server internals. Fixed in
  `NOISE_PATH`, then exposed `/api/sessions/{id}/reclassify` so already-captured sessions
  could be cleaned up without re-recording.
- **A noise call was quietly corrupting the dependency graph.** The browser sends the same
  session cookie on every request, including noise calls. Before reclassification removes
  them, the link detector could treat the *first* call carrying a cookie value — even if it
  was pure noise — as the "producer," chaining real calls to a meaningless dependency. Fixed by
  the same reclassify step: hidden/noise calls are excluded from `calls()` entirely, so they
  never enter `analyse()` or `find_links()` in the first place.
- **"Show actual scripts, not just 'depends on'."** The project "changes" flow originally just
  listed dependency labels (`"depends on: POST /login (field token)"`). Replaced with a real
  script viewer (`loadCaseScript()` / `scriptItemHtml()`) showing the literal rendered request
  — method, URL, headers, body — matching what Postman itself would show, with an `.existbadge`
  marker when a request already exists in the target collection.
- **Requests became editable.** Once real scripts were visible, the natural next ask was to
  let the user tweak them before running — method dropdown, URL input, headers/body textareas,
  all pre-filled. `collectRequestOverrides()` on the frontend feeds a `RequestOverride` map into
  `RunIn.overrides`, applied server-side by `_apply_request_overrides()` just before the
  collection is handed to Newman — the stored/generated collection itself is never mutated,
  only the copy that gets run.
- **"Don't auto-run tests on every change preview."** Previewing a project "change" (a new
  folder, new variables) originally ran Newman automatically every time, which was slow and
  noisy for something that's just a diff preview. Removed — Newman only runs when the user
  explicitly asks.
- **`ATLAS_COOKIE` — "why is this required?"** Investigated and explained rather than silently
  removed: it only appears when a captured request authenticates via cookie and *no* request in
  the chain produced that cookie itself (i.e. it's an existing browser session, not something
  Atlas can regenerate) — see `_needs_cookie()` in `analysis.py`. Kept as-is; this is correct
  behavior, not a bug.
- **Project "changes" flow was defaulting to suggest near-duplicate requests.** Coverage
  matches by method + path shape only, not body (see §7), so an endpoint already badged
  `"happy only"` still has a real matching request sitting in the collection. The case
  checklist's `defaultChecked` predicate pre-checked every `selected` case regardless of its
  coverage badge, so the default "Preview change" action would propose adding a second,
  near-identical request for anything already covered once. Fixed: a case is now pre-checked
  only when its endpoint is `"not_covered"`; an already-`"happy_only"` endpoint can still be
  checked by hand to add negative coverage, but is no longer suggested by default.

---

## 7. Phase 2, Step 2 — Project mode

A **project** (`store.py`'s `projects` table) is a path on disk plus its scanned git remote and
default branch — either an existing local folder or a fresh `git clone` into
`$ATLAS_HOME/projects/`. Nothing magic: it's a real `.git` the rest of the pipeline can
`fetch`/`worktree` against.

- **`project/scan.py`** — read-only repo inspection: `find_postman_files` (collections +
  environments by filename/content heuristics), `detect_newman_command` (checks
  `package.json` scripts and common CI configs for how the repo itself runs Newman),
  `git_info` (remote + default branch, via `git config --get remote.origin.url` —
  specifically *not* `git remote get-url origin`, which applies `insteadOf` URL rewrites and
  would misreport the remote in any environment using them). `GIT_TERMINAL_PROMPT=0`
  everywhere so a command needing interactive auth fails fast instead of hanging the UI.
- **`project/postman_parse.py`** — tolerant v2.0/v2.1 collection reader. `requests_in()` walks
  nested folders, extracting method + path segments from either URL shape (object or raw
  string), and flags `{{var}}` / `:param` segments as wildcards. `unit_index()` maps a
  collection's own request ids back to a "unit" (its top-level folder, or itself if bare) —
  this is what makes `summarize_collection()` possible for arbitrary third-party collections.
- **`project/coverage.py`** — matches Atlas's own endpoint templates against a collection's
  requests by **method + path shape only**, deliberately ignoring host and param names (a
  real-world Postman collection essentially never names its path variables the way our own
  analysis does, so matching names would under-match constantly). Badge is a simple heuristic:
  more than one matching request for a shape → `"happy + negative"`, otherwise `"happy only"`;
  zero matches → `"not covered"`. `collection_endpoints()` additionally synthesizes an
  endpoint list **straight from a collection's own requests** (deduped by shape) — this is the
  hook that lets coverage run with zero exploration (see §9).
- **`project/style.py`** — detects a repo's own Postman conventions: its base-URL variable
  name, its auth-token variable name, and whether its own scripts write chain variables via
  `pm.collectionVariables.set(...)` or `pm.environment.set(...)`. Generated additions should
  look like they belong, not like a foreign tool dropped files in.
- **`project/style_adapt.py`** — `adapt_items()` applies that detected style to Atlas's
  generated requests before they're added to someone else's collection: renames `{{baseUrl}}`
  to the repo's own variable, prefixes every one of *our* chain variables with `atlas_`
  (collision-proof against the repo's existing variables), and rescopes
  `pm.collectionVariables.set(...)` → `pm.environment.set(...)` when that's the repo's style.
  These renames are precise — the exact `{{var}}` token and the exact
  `pm.collectionVariables.set("name", ...)` first-argument shape — never a blind text
  find-and-replace, so a request body field that happens to share a chain variable's name
  (e.g. `body["token"]`) is never touched.
- **`project/edit.py`** — format-preserving-ish file edits: detects the file's indent width
  and trailing-newline convention, appends the new folder / variables / env values, and
  re-serializes. For a file shaped like a standard Postman export (true of both Postman's own
  export and Atlas's own generator output) the round-trip is byte-identical outside the
  appended content; a file that round-trips messily sets `unrelated_changes=True` so the diff
  UI can warn instead of silently hiding unrelated noise. **Only appends** — new folder, new
  collection variables, new (empty-valued) environment keys. It never edits or deletes an
  existing request.

---

## 8. Phase 2, Step 3 — Changes → verify → PR

`POST /api/projects/{pid}/changes` (preview): creates a scratch `git worktree` (new branch
`atlas/<flow>-<timestamp>` off `origin/<default-branch>`, in `$ATLAS_HOME/worktrees/<project_id>/`
— **never** the user's own checkout), applies `edit.py`'s appends, computes the diff and the
coverage before/after, and (only when explicitly asked, not automatically — see §6) runs Newman
against the new folder. Everything here is local; nothing is pushed.

`POST /api/projects/{pid}/changes/{change_id}/submit` is the **only** endpoint that commits,
pushes, and opens a PR — deliberately gated behind its own explicit UI button, never implied
by a preview.

- **`project/git_ops.py`** — `fetch` / `create_worktree` / `diff` (stages first, so new files
  show up in the diff too) / `commit_all` / `push`. `GIT_TERMINAL_PROMPT=0` throughout.
- **`project/github_pr.py`** — `open_pr()` tries, in order: `GITHUB_TOKEN` env var via the
  REST API, then `gh pr create`, then falls back to a plain GitHub compare link if neither is
  available. The token is read per-call from the environment and **never written to disk**.
  `ATLAS_GITHUB_API` lets tests point this at a local fake server
  (`tests/fake_github.py`, a minimal stdlib `http.server`) instead of hitting real GitHub.
- The UX addition the user specifically asked for mid-build — **select/unselect individual
  APIs from an explored flow before building a change** — lives in the checklist UI
  (`checkedCaseIds` in `app.js`) that feeds `case_ids` into both the Newman run and the
  change-preview request, so a project PR only proposes the endpoints you actually chose.

---

## 9. Latest feature — "this collection's own tests" (no exploration required)

Prompted by the question *"someone doesn't want to explore in Chromium at all — what then?"*
This closes that gap for projects that already own a Postman collection:

- `GET /api/projects/{pid}/self-coverage?collection=...` — uses
  `coverage.collection_endpoints()` to synthesize endpoints **from the collection itself**
  (so "coverage" here just means "how many variants of each shape exist in the collection,"
  without needing any Atlas exploration at all), plus `style.detect_style()`.
- `POST /api/projects/{pid}/run-collection?collection=...&environment=...` — runs that
  collection through Newman as-is (optionally with the project's own environment file and
  `--env-var` overrides), and summarizes the result via `newman_report.summarize_collection()`
  grouped by the collection's own top-level folders (not by Atlas case ids, since there are
  none).
- UI: `drawSelfCoverage()` in `app.js` renders a new "This collection's own tests" panel on the
  project page — a collection + environment picker, coverage badges, and a "Run" button that
  reuses the existing inline Newman-results renderer (guarded for the no-session-id case, since
  there's no Atlas session behind this at all).
- Tests: `test_self_coverage_needs_no_exploration`,
  `test_run_collection_groups_results_by_top_level_folder`,
  `test_run_collection_without_newman_gives_install_hint` in `test_project_api.py`, plus the
  `unit_index` tests in `test_postman_parse.py` and the `collection_endpoints` dedup tests in
  `test_coverage.py`.

---

## 10. Testing approach

- **Real headless-browser end-to-end** (`test_end_to_end.py`): actually launches the demo shop
  (`demo/app.py`), drives it with Playwright (`ATLAS_HEADLESS=1`), captures real traffic,
  and asserts on the generated output — not a mocked pipeline.
- **Unit tests per module**, each targeting one deterministic transformation (fingerprinting,
  link-finding, templating, masking, style detection, format-preserving edits).
- **Fake GitHub** (`tests/fake_github.py`): a minimal stdlib HTTP server standing in for the
  GitHub REST API, so PR-creation logic is tested against real HTTP semantics without ever
  touching a real repo or token.
- **Local bare git remotes** for project/scan/git_ops tests, using the
  `git config url.<path>.insteadOf <url>` trick to simulate a GitHub-shaped remote URL
  pointing at a local bare repo — this is exactly what exposed the `git remote get-url`
  bug in §7 (that command applies `insteadOf` rewrites and returns the *rewritten* URL,
  not the configured one).
- Current count: **108 passed, 1 skipped**, runtime ~15s.

---

## 11. Deliberate non-goals (v0.1/v0.2)

- No AI/LLM anywhere in the pipeline — confirmed by dependency list (`fastapi`, `uvicorn`,
  `playwright`, `pytest`, `httpx`, `requests` only) and by direct code reading. Chaining,
  masking, templating, and assertion generation are all regex/heuristic/hash-based.
  Consequence: it's fully reproducible, but anything the heuristics don't recognize needs a
  new rule, not a better prompt.
- No UI screen for the raw dependency graph (`depends_on` is used internally by the
  generator and the "actual script" viewer, but there's no graph visualization) — deliberate,
  per `CLAUDE.md`.
- No bundler/framework for the frontend — plain `index.html` / `app.js` / `app.css` with a
  hash router (`#/`, `#/s/{id}/{traffic|tests}`, `#/p/{id}`).
- `edit.py` is not a full format-preserving JSON editor — it's good enough for Postman's own
  export shape (and Atlas's own generator output), and honest (`unrelated_changes=True`) when
  a file doesn't round-trip cleanly, rather than silently guessing.

## 12. Roadmap (not yet built)

1. **Project mode for all frameworks, not just Postman.** Everything in §7–9
   (`project/coverage.py`, `project/style.py`, `project/edit.py`, the changes→PR flow,
   "this collection's own tests") is built exclusively around Postman collection JSON. A
   pytest+requests suite exported via `gen/pytest_gen.py` is invisible to all of it: no
   coverage match against a repo's own existing pytest tests, no duplicate-detection (the
   fix in §6 only covers the Postman path), no PR workflow. Closing this needs a pytest-suite
   reader (`ast`-based — walk `test_*` functions, extract the HTTP calls they make, mirroring
   `postman_parse.py`'s `requests_in()` / `unit_index()`) and an `edit.py` equivalent that
   appends new `test_*` functions to an existing file instead of Postman JSON folders.
2. **Idempotency-key support.** Confirmed absent: `UNIQUE_KEY_RE` / `_value_spec()` in
   `engine/analysis.py` only recognize email/phone/username/handle-shaped sign-up fields as
   needing a fresh value per run. An idempotency-key field (header or body) is masked and
   chained like any other value but then gets baked in as a static `literal` — so the second
   run of a generated suite replays the exact same key, and an idempotency-enforcing endpoint
   can legitimately return a cached/duplicate response instead of a fresh one, making the test
   flaky or silently wrong. Fix: recognize the pattern and treat it like a `unique` variable —
   generated fresh per run, the same mechanism sign-up emails already use.
3. "Export only missing" — generate/add just the not-yet-covered endpoints in one click.
4. A UI for the dependency graph; nested-folder-aware coverage (today a folder's own name
   isn't used for matching, only the request shapes inside it).
