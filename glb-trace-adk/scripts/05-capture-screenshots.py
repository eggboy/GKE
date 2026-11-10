# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "playwright>=1.49,<2",
#     "python-dotenv>=1.0.1,<2",
# ]
# ///
"""Capture Cloud Trace Explorer waterfalls and Logs Explorer views in a visible local Chrome window.

Run from the repository root:  uv run scripts/05-capture-screenshots.py
Sign in once in the Chrome window that opens; the script continues automatically.
Edit TRACES and LOG_QUERIES with the trace IDs printed by scripts/04-run-demo-checks.sh.
"""

import os
import sys
import time
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)  # same precedence as the bash scripts: .env wins over the shell
PROJECT_ID = os.environ["PROJECT_ID"]  # no personal default: screenshots must come from the project under test
PROFILE_DIR = ROOT_DIR / ".chrome-profile"
OUT_DIR = ROOT_DIR / "docs" / "screenshots"
SIGN_IN_TIMEOUT_S = 600
EXPLORER_URL = f"https://console.cloud.google.com/traces/explorer?project={PROJECT_ID}"

TRACES: list[tuple[str, str, bool]] = [
    # (file stem, trace id, also capture Graph view)
    ("01-scenario-g2-latency-1500ms", "6f43190c5099dfc79177225b3e99f935", False),
    ("02-scenario-a-full-waterfall", "bf53601177d05a9bcd24eab73fd964be", True),
    ("03-scenario-g1-outage-503", "3e2c8c31d576182c2ec8d99746632c08", False),
    ("04-scenario-d-sampled", "ec73d8a96a0442a83aa5293891471bdc", False),
    ("05-scenario-d-unsampled", "96ee5a51c1eb5bc999a736cbd5790220", False),
    ("06-scenario-f-waf-403", "4e5cb4cfcf86e7c3568ba52a62b39f72", False),
    ("07-scenario-c-edge-404", "d4f984ff5054bc1b7a277ce35836bf56", False),
    ("08-scenario-b-continuity", "63a46f8cd749d1cbfcbf4885b04ba8bf", False),
]

LOG_QUERIES: list[tuple[str, str, str | None]] = [
    (
        "09-scenario-f-cloud-armor-deny-log",
        f'trace="projects/{PROJECT_ID}/traces/4e5cb4cfcf86e7c3568ba52a62b39f72"',
        "jsonPayload/statusDetails,jsonPayload/enforcedSecurityPolicy/name,jsonPayload/enforcedSecurityPolicy/outcome,spanId",
    ),
    (
        "10-scenario-e-log-correlation",
        f'trace="projects/{PROJECT_ID}/traces/bf53601177d05a9bcd24eab73fd964be"',
        "resource/type,httpRequest/status,jsonPayload/message",
    ),
]


def logs_url(query: str, summary_fields: str | None) -> str:
    """Return the Logs Explorer deep link for a query with optional summary fields."""
    encoded = urllib.parse.quote(query, safe="")
    parts = [f"query={encoded}", "duration=PT3H"]
    if summary_fields:
        parts.append(f"summaryFields={urllib.parse.quote(summary_fields, safe=',')}:false:32:beginning")
    return f"https://console.cloud.google.com/logs/query;{';'.join(parts)}?project={PROJECT_ID}"


def wait_for_sign_in(page: Page) -> Page:
    """Block until any page in the context reaches the console; return that page."""
    deadline = time.time() + SIGN_IN_TIMEOUT_S
    announced = False
    last_report = 0.0
    while time.time() < deadline:
        for candidate in page.context.pages:
            if candidate.url.startswith("https://console.cloud.google.com/"):
                return candidate
        if not announced:
            print(
                "ACTION REQUIRED: sign in to Google Cloud Console in the Chrome window.",
                flush=True,
            )
            announced = True
        if time.time() - last_report > 10:
            urls = [p.url[:120] for p in page.context.pages]
            print(f"waiting... current page URLs: {urls}", flush=True)
            last_report = time.time()
        time.sleep(2)
    raise TimeoutError("Sign-in not completed within timeout")


def settle(page: Page, seconds: float) -> None:
    """Wait for network idle (best effort) plus a fixed render delay."""
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        print(
            "note: network did not go idle (console long-polls); using fixed delay",
            flush=True,
        )
    time.sleep(seconds)


def goto(page: Page, url: str, settle_s: float) -> Page:
    """Navigate, handle sign-in, settle, and return the active page."""
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page = wait_for_sign_in(page)
    if not page.url.startswith(url.split("?", maxsplit=1)[0].split(";", maxsplit=1)[0]):
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    settle(page, settle_s)
    return page


def capture_trace(page: Page, stem: str, tid: str, graph: bool) -> Page:
    """Open Trace Explorer, look up tid via the Trace ID button, screenshot the waterfall."""
    page = goto(page, EXPLORER_URL, settle_s=6)
    page.get_by_role("button", name="Trace ID").first.click()
    box = page.locator("input[aria-label='Trace ID']").last
    box.wait_for(state="visible", timeout=15_000)
    box.fill(tid)
    box.press("Enter")
    try:
        page.get_by_text(f"Trace ID {tid[:16]}", exact=False).first.wait_for(timeout=25_000)
    except Exception:  # noqa: BLE001 - unsampled trace shows no panel; capture whatever is shown
        print(f"note: no detail panel for {tid} (expected for unsampled)", flush=True)
    time.sleep(5)
    out = OUT_DIR / f"{stem}.png"
    page.screenshot(path=str(out), full_page=False)
    print(f"saved {out}", flush=True)
    if graph:
        try:
            page.get_by_role("button", name="Graph").first.click()
            time.sleep(6)
            out_g = OUT_DIR / f"{stem}-graph.png"
            page.screenshot(path=str(out_g), full_page=False)
            print(f"saved {out_g}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"graph view skipped: {exc}", flush=True)
    return page


def capture_logs(page: Page, stem: str, query: str, summary_fields: str | None) -> Page:
    """Open Logs Explorer with the query, expand the first entry, screenshot."""
    page = goto(page, logs_url(query, summary_fields), settle_s=10)
    try:
        page.get_by_text("denied_by_security_policy", exact=False).first.click(timeout=5_000)
        time.sleep(4)
    except PlaywrightTimeoutError:
        print("note: no Cloud Armor entry to expand for this query", flush=True)
    out = OUT_DIR / f"{stem}.png"
    page.screenshot(path=str(out), full_page=False)
    print(f"saved {out}", flush=True)
    return page


def parse_selection(
    argv: list[str],
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, str | None]]]:
    """Return (traces, log_queries) to capture.

    With no arguments, capture everything in TRACES and LOG_QUERIES. Each argument of the form
    ``stem=trace_id`` captures only that trace (useful for a freshly generated Scenario H trace).
    """
    if not argv:
        return TRACES, LOG_QUERIES
    traces: list[tuple[str, str, bool]] = []
    for arg in argv:
        stem, _, tid = arg.partition("=")
        if not tid:
            raise SystemExit(f"expected stem=trace_id, got {arg!r}")
        traces.append((stem, tid, False))
    return traces, []


def main() -> None:
    """Open a persistent headed Chrome profile and capture all screenshots."""
    traces, log_queries = parse_selection(sys.argv[1:])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1680, "height": 1050},
            device_scale_factor=2,
            args=["--lang=en-US"],
            locale="en-US",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for stem, tid, graph in traces:
            page = capture_trace(page, stem, tid, graph)
        for stem, query, summary in log_queries:
            page = capture_logs(page, stem, query, summary)
        ctx.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
