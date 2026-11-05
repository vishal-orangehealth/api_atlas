# API Atlas

Explore a web app in a browser, record its API calls, and generate a chained, runnable
API test suite (pytest + requests, or Postman v2.1). Runs locally; Python + FastAPI backend,
no-build vanilla JS UI.

Phase 2 adds: running the Postman suite for real with Newman, and **project mode** - open or
clone a real repo, match its own Postman collection(s) to what you explored, and turn missing
coverage into a PR against that repo.

## Commands

```bash
pip install -e ".[dev]"
playwright install chromium        # once
pytest                             # unit + real headless-browser end-to-end tests (~15s)
atlas                              # UI at http://127.0.0.1:7801
atlas demo                         # demo shop at http://127.0.0.1:8765 to explore
```

- `ATLAS_HEADLESS=1` runs the capture browser headless (tests set this).
- `ATLAS_NEWMAN=/path/to/newman` pins Newman for both the "Run tests" button and `atlas/project/changes`;
  otherwise `newman` on PATH, then a pinned `npx -y newman@6`, else the UI shows an install hint.
- `ATLAS_HOME` overrides the data dir (default `~/.api-atlas`, SQLite `atlas.db`; cloned projects
  land in `$ATLAS_HOME/projects/`, scratch worktrees in `$ATLAS_HOME/worktrees/<project_id>/`).
- `GITHUB_TOKEN` (env only, never written anywhere) lets "Push & open PR" create the PR via the
  REST API; otherwise it falls back to `gh pr create`, then to a plain compare link.
- `ATLAS_GITHUB_API` overrides the GitHub API base URL - tests point this at a local fake server.

## How the pipeline fits together

```
capture.py  Playwright response -> engine/callmodel.build_call -> engine/filter.classify
            -> engine/values.fingerprints (salted hashes of RAW values)
            -> engine/mask.mask_call -> store.add_call (masked only)
server.py   /api/sessions/{id}/analysis -> engine/analysis.analyse(calls)
            -> endpoints (normalize.build_templates), links (deps.find_links), test cases
gen/        pytest_gen.generate / postman_gen.generate render the case IR to files
engine/newman_report.py  Newman's `--reporters json` output -> plain-English pass/fail per
            folder (used by both the "Run tests" button and the project "changes" preview)
static/     index.html, app.js, app.css (hash router: #/, #/s/{id}/{traffic|tests}, #/p/{id})
demo/app.py Atlas Shop demo used by tests and `atlas demo`
```

Test case IR (built in `analysis.CaseBuilder`): each case has `setup` steps + a `target` step.
Values inside steps are plain JSON, `{"$var": name, "as": "str"|"raw"}`, or
`{"$fmt": ["Bearer ", {"$var": "token"}]}`. Vars are `extract` (from a response),
`env` (masked value -> `ATLAS_*` env var, optional default), `unique` (fresh email/phone/string
for sign-up flows) or `literal`.

### Project mode (`atlas/project/`)

A **project** (`store.py`'s `projects` table) is just a path on disk plus its scanned git remote/
default branch - opened from an existing folder or cloned with `git clone` (never `pipx`-style
magic; always a real `.git` the rest of the pipeline can `fetch`/`worktree` against).

```
postman_parse.py  tolerant v2.0/v2.1 reader: walk_items/requests_in (method + path segments,
                   {{var}} and :param both treated as a wildcard segment), item_index (maps a
                   generated request's id back to its case, for Newman report parsing)
scan.py           find_postman_files, detect_newman_command (package.json / CI configs),
                   git_info (remote, default branch) - all read-only, GIT_TERMINAL_PROMPT=0
coverage.py       match by METHOD + path SHAPE only (ignores host and param names - Postman
                   rarely names a variable the way our analysis does); badge is a heuristic:
                   >1 matching request for a shape => "happy + negative", else "happy only"
style.py          detect_style: base URL variable name, token variable, and whether the repo's
                   own scripts write chain variables via pm.collectionVariables vs pm.environment
style_adapt.py     adapt_items: rename our {{baseUrl}} to the repo's own var, prefix every one of
                   OUR chain variables with atlas_ (collision-proof), and rescope
                   pm.collectionVariables.set(...) -> pm.environment.set(...) when the repo's
                   style calls for it. Renames are precise (the exact {{var}} and the exact
                   pm.collectionVariables.set("name", first-arg shape), never a blind text
                   replace - a body field that happens to share a chain variable's name,
                   e.g. body["token"], is never touched.
edit.py            format-preserving-ish file edits: detect indent + trailing newline, append
                   (new folder / new variables / new env values), re-serialize, diff old vs new.
                   Not a full format-preserving JSON editor - for a file shaped like a standard
                   Postman export (true of both Postman's own export and our generator) the
                   round-trip is byte-identical outside the append; a file that round-trips
                   messily sets unrelated_changes=True so the UI can warn instead of hiding it.
git_ops.py         fetch / create_worktree (new branch `atlas/<flow>-<timestamp>` from
                   origin/<default-branch>, into a scratch dir - never touches the user's own
                   checkout) / diff (stages, so new files show up too) / commit_all / push.
                   GIT_TERMINAL_PROMPT=0 everywhere, so a command needing interactive auth fails
                   fast instead of hanging.
github_pr.py       open_pr: GITHUB_TOKEN REST API, then `gh pr create`, then a compare link.
                   Never stores a token; ATLAS_GITHUB_API lets tests swap in a fake server.
```

`server.py` wires these into `POST /api/projects/{pid}/changes` (preview: worktree + edit + diff +
Newman-on-the-new-folder-only + coverage before/after, all local, nothing pushed) and
`POST /api/projects/{pid}/changes/{change_id}/submit` (the only endpoint that commits, pushes and
opens a PR - gated behind its own explicit button in the UI, never implied by a preview).

## Product rules (don't break these)

- Never store, show, export or log raw secrets. Masking happens before `store.add_call`;
  fingerprint salt lives only in memory (`Recorder.salts`).
- Generated tests must pass on a second run: sign-up values are unique per run, ids/tokens are
  extracted at run time, dynamic fields (ids, uuids, timestamps, tokens) are only type-checked.
- Happy-path cases are selected by default; negative and "without auth" cases are opt-in.
- The dependency graph is used by the generator but has no UI screen in v0.1 (deliberate).
- Keep the UI no-build (no bundler, no framework).
- Project mode never touches the user's own checkout (always a separate `git worktree`), never
  pushes to `main`/the default branch, and never edits or deletes an existing request - only
  appends a new folder, new collection variables, or new (empty-valued) environment values.
- Never write a GitHub token (or any secret) to disk; it's read from `GITHUB_TOKEN` per call.

## Roadmap (next)

1. **Project mode for all frameworks, not just Postman.** Today `atlas/project/*.py` (coverage,
   style, edit, the changes->PR flow) is entirely Postman-collection-specific - a pytest+requests
   suite exported via `gen/pytest_gen.py` gets none of it: no coverage matching against an
   existing repo's own pytest tests, no duplicate-detection, no PR workflow. Needs a pytest-suite
   reader (`ast`-based, mirroring `postman_parse.py`'s `requests_in()`/`unit_index()`) and an
   `edit.py` equivalent that appends new `test_*` functions instead of Postman JSON.
2. **Idempotency-key support.** `UNIQUE_KEY_RE` / `_value_spec()` in `engine/analysis.py` don't
   recognize an idempotency-key field - it gets captured as a static `literal`, so a second run
   of the generated suite replays the exact same key and can legitimately get a different
   response (a cached/duplicate result) than the first run, making the test flaky or wrong
   against idempotency-enforcing endpoints. Needs a pattern match (`idempotency-key`, header or
   body) treated like a `unique` value - fresh per run, same as sign-up emails - so chained
   scripts stay correct on repeat runs instead of silently reusing a stale key.
3. "Export only missing" - generate/add just the not-yet-covered endpoints in one click.
4. A UI for the dependency graph; nested-folder-aware coverage (today a folder's own name isn't
   used, only the request shapes inside it).
