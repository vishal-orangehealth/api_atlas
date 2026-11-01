# API Atlas

Explore a web app in a browser, record its API calls, and generate a chained, runnable
API test suite (pytest + requests, or Postman v2.1). Runs locally; Python + FastAPI backend,
no-build vanilla JS UI.

## Commands

```bash
pip install -e ".[dev]"
playwright install chromium        # once
pytest                             # unit + real headless-browser end-to-end tests (~5s)
atlas                              # UI at http://127.0.0.1:7801
atlas demo                         # demo shop at http://127.0.0.1:8765 to explore
```

- `ATLAS_HEADLESS=1` runs the capture browser headless (tests set this).
- `ATLAS_NEWMAN=/path/to/newman` also runs the generated Postman collection in tests.
- `ATLAS_HOME` overrides the data dir (default `~/.api-atlas`, SQLite `atlas.db`).

## How the pipeline fits together

```
capture.py  Playwright response -> engine/callmodel.build_call -> engine/filter.classify
            -> engine/values.fingerprints (salted hashes of RAW values)
            -> engine/mask.mask_call -> store.add_call (masked only)
server.py   /api/sessions/{id}/analysis -> engine/analysis.analyse(calls)
            -> endpoints (normalize.build_templates), links (deps.find_links), test cases
gen/        pytest_gen.generate / postman_gen.generate render the case IR to files
static/     index.html, app.js, app.css (hash router: #/ and #/s/{id}/{traffic|tests})
demo/app.py Atlas Shop demo used by tests and `atlas demo`
```

Test case IR (built in `analysis.CaseBuilder`): each case has `setup` steps + a `target` step.
Values inside steps are plain JSON, `{"$var": name, "as": "str"|"raw"}`, or
`{"$fmt": ["Bearer ", {"$var": "token"}]}`. Vars are `extract` (from a response),
`env` (masked value -> `ATLAS_*` env var, optional default), `unique` (fresh email/phone/string
for sign-up flows) or `literal`.

## Product rules (don't break these)

- Never store, show, export or log raw secrets. Masking happens before `store.add_call`;
  fingerprint salt lives only in memory (`Recorder.salts`).
- Generated tests must pass on a second run: sign-up values are unique per run, ids/tokens are
  extracted at run time, dynamic fields (ids, uuids, timestamps, tokens) are only type-checked.
- Happy-path cases are selected by default; negative and "without auth" cases are opt-in.
- The dependency graph is used by the generator but has no UI screen in v0.1 (deliberate).
- Keep the UI no-build (no bundler, no framework).

## Roadmap (next)

1. Import Postman collections (v2.0/v2.1) and pytest suites (`ast`), match to endpoints.
2. Coverage badges per endpoint (covered / happy only / not covered) and "export only missing".
3. Run & verify generated tests inside the app.
