"""Decide which captured network calls are real API traffic.

Three outcomes for every call:
  - "keep":   API call on an allowed host
  - "hidden": API call on another host (stored, so the user can include that host later)
  - "drop":   noise (analytics, tracking, static assets, preflight) - never stored
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

API_RESOURCE_TYPES = {"xhr", "fetch"}

NOISE_HOST_SUFFIXES = (
    "google-analytics.com", "analytics.google.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "googleadservices.com", "segment.io", "segment.com", "hotjar.com",
    "hotjar.io", "sentry.io", "mixpanel.com", "amplitude.com", "fullstory.com", "clarity.ms",
    "newrelic.com", "nr-data.net", "datadoghq.com", "datadoghq.eu", "intercom.io", "intercomcdn.com",
    "facebook.com", "facebook.net", "bat.bing.com", "ads.linkedin.com", "px.ads.linkedin.com",
    "heapanalytics.com", "logrocket.io", "lr-ingest.io", "launchdarkly.com", "firebaselogging.googleapis.com",
    "crashlytics.com", "analytics.tiktok.com", "branch.io", "appsflyer.com", "moengage.com",
    "clevertap-prod.com", "webengage.com", "onesignal.com", "cloudflareinsights.com", "posthog.com",
)

NOISE_PATH = re.compile(r"/(collect|beacon|pixel|telemetry|rum|__webpack_hmr|sockjs-node)(/|$)", re.I)

KEEP_CONTENT_TYPES = ("json", "xml", "text/plain", "graphql")

_MULTI_PART_TLD_SECOND = {"co", "com", "org", "net", "gov", "ac", "edu", "gen", "firm", "ind"}


def base_domain(host: str) -> str:
    """Registrable-ish domain: app.example.co.in -> example.co.in, localhost -> localhost."""
    host = (host or "").lower().split(":")[0]
    if not host or host.replace(".", "").isdigit() or "." not in host:
        return host
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _MULTI_PART_TLD_SECOND and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def default_allowed_hosts(target_url: str) -> list[str]:
    host = (urlsplit(target_url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return ["localhost", "127.0.0.1"]
    return [base_domain(host)]


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = (host or "").lower().split(":")[0]
    for a in allowed:
        a = a.lower().strip()
        if not a:
            continue
        if host == a or host.endswith("." + a):
            return True
    return False


def is_noise_host(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    return any(host == s or host.endswith("." + s) for s in NOISE_HOST_SUFFIXES)


def classify(*, method: str, url: str, resource_type: str | None, status: int | None,
             response_content_type: str | None, allowed_hosts: list[str]) -> str:
    """Return "keep", "hidden" or "drop"."""
    if (method or "").upper() == "OPTIONS":
        return "drop"
    if resource_type is not None and resource_type not in API_RESOURCE_TYPES:
        return "drop"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "drop"
    host = parts.hostname or ""
    if is_noise_host(host) or NOISE_PATH.search(parts.path or ""):
        return "drop"
    ctype = (response_content_type or "").lower()
    if ctype and not any(k in ctype for k in KEEP_CONTENT_TYPES):
        return "drop"
    if not ctype and status not in (204, 201, 202, 200, None):
        # error responses with no body are still interesting
        pass
    if not host_allowed(host, allowed_hosts):
        return "hidden"
    return "keep"
