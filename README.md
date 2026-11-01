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
3. **Export.** Download pytest or Postman. Each test runs its own setup chain (sign up → log in →
   create cart → place order) so it passes on its own, every run:
   - sign-up flows get a fresh unique email/phone each run
   - masked values for existing accounts are read from env vars (`ATLAS_EMAIL`, `ATLAS_PASSWORD`, …)
   - ids and tokens are extracted from responses at run time, never hard-coded
   - dynamic fields are only type-checked, never compared to recorded values

## What's in v0.1

| Area | Included |
|---|---|
| Capture | Playwright browser, noise filter, host allowlist, live traffic view, HAR export |
| Understanding | endpoint templating, dependency + auth detection (used by the generator) |
| Safety | masking before storage, salted value hashes (salt never written to disk) |
| Tests | happy path, missing field, wrong type, empty body, unknown id, without auth |
| Output | pytest + requests suite, Postman v2.1 collection + environment |

Not yet: importing existing suites, coverage overlay, contract diff, RestAssured/k6 output,
in-app test runs, AI suggestions.

## Develop

```bash
pip install -e ".[dev]"
pytest              # unit tests + a real headless-browser capture of the demo app
```

Set `ATLAS_NEWMAN=/path/to/newman` (or put `newman` on PATH) to also run the generated Postman
collection in the test suite. Data lives in `~/.api-atlas` (override with `ATLAS_HOME`).

## Layout

```
atlas/
  capture.py        Playwright recorder: filter → fingerprint → mask → store
  server.py         FastAPI: REST + websocket + UI
  store.py          SQLite (masked data only)
  engine/           filter, mask, values (fingerprints), deps (links), normalize, schema, analysis
  gen/              pytest_gen, postman_gen
  static/           web UI (no build step)
  demo/             Atlas Shop demo app
```
