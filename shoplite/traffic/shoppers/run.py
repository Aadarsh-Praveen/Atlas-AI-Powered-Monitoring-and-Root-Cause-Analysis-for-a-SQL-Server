"""Run simulated shoppers against the ShopLite storefront.

    cd shoplite/traffic/shoppers
    python run.py                         # local defaults: 2 shoppers, 5 minutes, headless
    python run.py --headed --persona buyer --duration 60   # watch one persona shop

What it does:
  1. Checks the API's /health. If production is down or the VM is off, it
     prints why and exits 0 ("skipped"), so a scheduled run doesn't fail.
  2. Runs `concurrency` shoppers at once. Each keeps starting new sessions until
     the duration is up. A session = a fresh browser context (its own storage,
     like a new visitor), a persona picked by weight, ~5% on a phone-sized screen.
  3. Every request carries X-Synthetic-Source: playwright/<persona>.
  4. A broken page (PageBroken, or any crash) is the only failure. Then it
     saves a screenshot, and a trace for personas without secrets. Declines,
     409s, 503s, and empty searches are normal outcomes.
  5. Writes the summary to the console, to the GitHub job summary (when
     GITHUB_STEP_SUMMARY is set), and to <artifacts>/results.json.

Exit code: 0 when no page was broken (or the run was skipped), 1 otherwise.
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

from playwright.async_api import Browser, Playwright, async_playwright

from helpers import ApiWatcher, Outcome, Recorder, Session, describe_failure, synthetic_headers
from personas import NO_TRACE, PERSONAS
from settings import Settings, SettingsError, load_settings

MOBILE_DEVICE = "iPhone 13"          # a phone-sized screen, emulated in Chromium
GRACE_SECONDS = 180                  # let running sessions finish after the duration


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def api_is_healthy(api_url: str) -> tuple[bool, str]:
    """GET /health: (True, version) when status is ok, else (False, reason)."""
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=10) as response:
            body = json.loads(response.read().decode())
            if body.get("status") == "ok":
                return True, body.get("version", "?")
            return False, f"status {body.get('status')!r}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"unreachable ({getattr(error, 'reason', error)})"


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------

async def run_session(browser: Browser, playwright: Playwright, settings: Settings,
                      persona: str, mobile: bool, rng: random.Random, number: int) -> Outcome:
    options: dict = {"extra_http_headers": synthetic_headers(persona)}
    if mobile:
        device = dict(playwright.devices[MOBILE_DEVICE])
        device.pop("default_browser_type", None)        # run it in Chromium
        options.update(device)
    else:
        options["viewport"] = {"width": 1366, "height": 860}
    context = await browser.new_context(**options)
    context.set_default_timeout(settings.step_timeout_ms)

    tracing = persona not in NO_TRACE
    if tracing:
        await context.tracing.start(screenshots=True, snapshots=True)

    page = await context.new_page()
    recorder = Recorder(persona, mobile)
    api = ApiWatcher(page, settings.api_url)
    session = Session(page=page, settings=settings, rng=rng, recorder=recorder, api=api)

    broken = False
    try:
        outcome = await PERSONAS[persona](session)
    except Exception as error:                    # PageBroken, Playwright timeouts, crashes
        # A broken page while the API was answering 503 is "unavailable": the
        # storefront showed its error notice instead of the expected content.
        if api.seen(503):
            outcome = recorder.finish("unavailable", f"503 from the API ({error.__class__.__name__})")
        else:
            broken = True
            last_step = recorder.result.steps[-1] if recorder.result.steps else ""
            outcome = recorder.finish("error", describe_failure(error, last_step))
    outcome.api_status_counts = api.counts()

    stem = f"{number:03d}-{persona}{'-mobile' if mobile else ''}"
    try:
        if broken:
            settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
            if persona in NO_TRACE:
                # Blank every password box first, so no secret is in the picture.
                await page.evaluate(
                    "document.querySelectorAll('input[type=password]').forEach(e => { e.value = ''; })")
            await page.screenshot(path=settings.artifacts_dir / f"{stem}.png", full_page=True)
        if tracing:
            if broken:
                await context.tracing.stop(path=settings.artifacts_dir / f"{stem}-trace.zip")
            else:
                await context.tracing.stop()          # discard: only failures keep traces
    except Exception as error:                        # saving evidence must not crash the run
        print(f"  (couldn't save artifacts for session {number}: {error})", file=sys.stderr)
    finally:
        await context.close()
    return outcome


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def pick_persona(settings: Settings, rng: random.Random, forced: str | None) -> str:
    if forced:
        return forced
    names = [name for name, weight in settings.weights.items() if weight > 0]
    return rng.choices(names, weights=[settings.weights[name] for name in names])[0]


async def shopper(slot: int, browser: Browser, playwright: Playwright, settings: Settings,
                  master: random.Random, forced: str | None, deadline: float,
                  results: list[Outcome], counter: list[int]) -> None:
    """One concurrent shopper: start sessions back to back until the deadline."""
    await asyncio.sleep(master.uniform(0, 5))         # don't all arrive at the same second
    while time.monotonic() < deadline:
        counter[0] += 1
        number = counter[0]
        persona = pick_persona(settings, master, forced)
        mobile = master.random() < settings.mobile_share
        rng = random.Random(master.random())
        outcome = await run_session(browser, playwright, settings, persona, mobile, rng, number)
        results.append(outcome)
        device = " (mobile)" if mobile else ""
        print(f"[{number:03d}] {persona}{device}: {outcome.outcome} in {outcome.seconds}s"
              f"{' - ' + outcome.detail if outcome.detail else ''}", flush=True)
        await asyncio.sleep(master.uniform(1, 5))     # a gap before the next visitor


async def run(settings: Settings, forced: str | None) -> list[Outcome]:
    master = random.Random(settings.seed)
    results: list[Outcome] = []
    counter = [0]
    deadline = time.monotonic() + settings.duration_seconds
    async with async_playwright() as playwright:
        # channel=None launches Playwright's bundled Chromium; "chrome" launches
        # the Google Chrome installed on this machine (SHOPPERS_BROWSER_CHANNEL).
        browser = await playwright.chromium.launch(headless=settings.headless, slow_mo=settings.slow_mo_ms,
                                                   channel=settings.browser_channel)
        try:
            tasks = [asyncio.create_task(shopper(slot, browser, playwright, settings, master,
                                                 forced, deadline, results, counter))
                     for slot in range(settings.concurrency)]
            done, pending = await asyncio.wait(tasks, timeout=settings.duration_seconds + GRACE_SECONDS)
            for task in pending:                      # a session stuck past the grace period
                task.cancel()
            for task in done:
                if task.exception():
                    print(f"A shopper stopped unexpectedly: {task.exception()!r}", file=sys.stderr)
        finally:
            await browser.close()
    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarize(settings: Settings, results: list[Outcome], version: str) -> str:
    outcomes = Counter(r.outcome for r in results)
    personas = Counter(r.persona for r in results)
    mobile = sum(r.mobile for r in results)
    lines = [
        "## ShopLite shoppers",
        "",
        f"Target: {settings.storefront_url} (API {settings.api_url}, version {version})  ",
        f"{len(results)} sessions by {settings.concurrency} concurrent shoppers over "
        f"{settings.duration_seconds}s; {mobile} on a phone-sized screen.",
        "",
        "| Outcome | Sessions |",
        "|---|---|",
        *[f"| {name} | {count} |" for name, count in outcomes.most_common()],
        "",
        "| Persona | Sessions | Avg seconds |",
        "|---|---|---|",
    ]
    for name, count in personas.most_common():
        seconds = [r.seconds for r in results if r.persona == name]
        lines.append(f"| {name} | {count} | {sum(seconds) / len(seconds):.0f} |")
    statuses = Counter()
    for r in results:
        statuses.update(r.api_status_counts)
    if statuses:
        lines += ["", "API responses seen by the browsers: " +
                  ", ".join(f"{code} x{n}" for code, n in sorted(statuses.items()))]
    errors = [r for r in results if r.outcome == "error"]
    if errors:
        lines += ["", f"### {len(errors)} broken page(s)", ""]
        lines += [f"- {r.persona}{' (mobile)' if r.mobile else ''}: {r.detail} "
                  f"(last step: {r.steps[-1] if r.steps else 'none'})" for r in errors]
        lines += ["", "Screenshots (and traces, except admin) are in the run's artifacts."]
    return "\n".join(lines)


def write_summary(text: str) -> None:
    print("\n" + text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Simulated ShopLite shoppers (Playwright).")
    parser.add_argument("--persona", choices=sorted(PERSONAS), help="run only this persona (testing)")
    parser.add_argument("--concurrency", type=int, help="override SHOPPERS_CONCURRENCY")
    parser.add_argument("--duration", type=int, help="override SHOPPERS_DURATION_SECONDS")
    parser.add_argument("--headed", action="store_true", help="show the browser windows")
    parser.add_argument("--slow-mo", type=int, help="slow every action by this many ms (with --headed)")
    parser.add_argument("--skip-health-check", action="store_true", help="don't check /health first")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except SettingsError as error:
        print(f"Settings error: {error}", file=sys.stderr)
        return 2
    overrides = {}
    if args.concurrency:
        overrides["concurrency"] = max(1, min(args.concurrency, 8))
    if args.duration:
        overrides["duration_seconds"] = max(10, args.duration)
    if args.headed:
        overrides["headless"] = False
    if args.slow_mo is not None:
        overrides["slow_mo_ms"] = args.slow_mo
    # Each run writes to its own folder, so evidence from different runs never
    # mixes: shoppers-artifacts/run-20261121-174405Z/
    stamp = dt.datetime.now(dt.timezone.utc).strftime("run-%Y%m%d-%H%M%SZ")
    overrides["artifacts_dir"] = settings.artifacts_dir / stamp
    settings = replace(settings, **overrides)
    if args.persona == "admin" and not settings.admin_token:
        print("The admin persona needs SHOPLITE_ADMIN_TOKEN.", file=sys.stderr)
        return 2

    version = "?"
    if not args.skip_health_check:
        healthy, version = api_is_healthy(settings.api_url)
        if not healthy:
            write_summary(f"## ShopLite shoppers\n\nSkipped: the API at {settings.api_url} is not "
                          f"healthy ({version}). The VM may be off. Nothing was run.")
            return 0

    where = "LOCAL" if settings.is_local else "PRODUCTION"
    browser_name = f"Chrome channel {settings.browser_channel!r}" if settings.browser_channel else "bundled Chromium"
    print(f"Shopping at {settings.storefront_url} ({where}, API version {version}) with "
          f"{settings.concurrency} shopper(s) for {settings.duration_seconds}s in {browser_name}...", flush=True)
    results = asyncio.run(run(settings, args.persona))

    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (settings.artifacts_dir / "results.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    write_summary(summarize(settings, results, version))
    print(f"\nThis run's files: {settings.artifacts_dir}")
    return 1 if any(r.outcome == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
