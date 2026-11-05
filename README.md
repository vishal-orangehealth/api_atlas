# API Atlas

**Explore your app once. Get a working API test suite.**

API Atlas opens a browser, records the API calls your app makes while you use it, works out how
they connect (login token → every call, `orderId` → `/orders/{id}`), and generates a chained test
suite you can run straight away with **pytest** or **Postman/Newman**.

Everything runs on your machine. Passwords, tokens, cookies, emails, phone numbers, card numbers
and PAN are masked *before* anything is saved.

## Quick start

```bash
pipx install .                 # or: pip install .
playwright install chromium    # once, if you don't have Chrome installed
atlas                          # opens http://127.0.0.1:7801
```

No app to try it on? Start the demo shop in another terminal and explore `http://127.0.0.1:8765`:

```bash
atlas demo
```

## How it works

1. **Explore.** Enter your app's URL. A browser opens; use the app like a user would.
   Analytics/tracking calls, static files and preflights are left out. Calls to other servers are
   hidden until you include their host.
2. **Review.** Each endpoint shows what it depends on, its response fields (dynamic ones like ids
   and timestamps are marked) and suggested tests. Happy-path tests are selected; validation,
   not-found and "without auth" checks are there to opt into. Rename tests or change the expected
   status inline.
3. **Export, or run it for real.** Download pytest or Postman, or just click **Run tests**:
   it runs the Postman collection with Newman right there and shows pass/fail per test folder in
   plain English ("Expected 201, got 409: email already exists"), with a masked response snippet
   and a one-click "expect 409 instead" fix for a status mismatch. Every test runs its own setup
   chain (sign up → log in → create cart → place order) so it passes on its own, every run:
   - sign-up flows get a fresh unique email/phone each run
   - masked values for existing accounts are read from env vars (`ATLAS_EMAIL`, `ATLAS_PASSWORD`, …) -
     if any are missing, Run tests asks for them first and passes them as `--env-var`, never to disk
   - ids and tokens are extracted from responses at run time, never hard-coded
   - dynamic fields are only type-checked, never compared to recorded values
4. **Or turn it into a PR against your real suite.** Open (or clone) the repo your team's Postman
   collection actually lives in. API Atlas scans it for collections/environments, matches their
   requests to what you explored by method + path shape, and shows a coverage badge per endpoint
   (not covered / happy path only / happy + negative). Pick which of the recorded calls to add,
   preview the change - a real `git diff`, Newman run on just the new folder, coverage before/after -
   then push a branch and open a PR. It never touches your own checkout (always a scratch
   `git worktree`), never pushes to your default branch, and never edits or deletes an existing
   request. New chain variables are prefixed `atlas_` and match the repo's own naming/scope
   convention, so they never collide with the team's.

## What's in v0.2

| Area | Included |
|---|---|
| Capture | Playwright browser, noise filter, host allowlist, live traffic view, HAR export |
| Understanding | endpoint templating, dependency + auth detection (used by the generator) |
| Safety | masking before storage, salted value hashes (salt never written to disk) |
| Tests | happy path, missing field, wrong type, empty body, unknown id, without auth |
| Output | pytest + requests suite, Postman v2.1 collection + environment |
| Run & verify | Newman run in-app, plain-English failures, one-click status fix |
| Project mode | open/clone a repo, scan its Postman files, coverage badges, style-matched PRs |

Not yet: importing existing pytest suites, "export only missing", contract diff, RestAssured/k6
output, AI suggestions.

## Develop

```bash
pip install -e ".[dev]"
pytest              # unit tests + a real headless-browser capture of the demo app
```

Set `ATLAS_NEWMAN=/path/to/newman` to pin Newman (otherwise `newman` on PATH, then a pinned
`npx newman@6`). Data lives in `~/.api-atlas` (override with `ATLAS_HOME`); set `GITHUB_TOKEN` to
let "Push & open PR" create the PR via the GitHub API instead of falling back to `gh`/a compare
link - it's read from the environment only, never written anywhere.

## Layout

```
atlas/
  capture.py        Playwright recorder: filter → fingerprint → mask → store
  server.py         FastAPI: REST + websocket + UI
  store.py          SQLite (masked data only; also the `projects` table for project mode)
  engine/           filter, mask, values (fingerprints), deps (links), normalize, schema, analysis,
                     newman_report (Newman's JSON report -> plain-English pass/fail)
  gen/              pytest_gen, postman_gen
  project/          postman_parse, scan, coverage, style, style_adapt, edit, git_ops, github_pr -
                     see CLAUDE.md for how they fit together
  static/           web UI (no build step)
  demo/             Atlas Shop demo app
```
