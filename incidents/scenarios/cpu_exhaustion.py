"""CPU credit exhaustion: an aggressive crawler.

Story: a price-comparison bot discovers the store and crawls the catalog as
fast as it can: searches with many terms and every product page, from many
connections at once. The API and SQL Server work flat out; the burstable VM
spends its CPU credits; if they run out, it's throttled to its baseline and
everything slows down.

What it does: N threads (default 8) request searches and product pages from
the API, continuously, with a crawler's User-Agent and no synthetic-source
tag (a bot doesn't announce itself). Read-only: it only GETs.

Run it from the Mac (recommended: the traffic then arrives from outside, like
a real crawler's, through Caddy's public HTTPS address; set API_BASE_URL in the
Mac copy's .env to https://shoplite-aadarsh.northcentralus.cloudapp.azure.com)
or from the VM (API_BASE_URL http://127.0.0.1:8000).

How long: the B2as_v2 banks up to several hundred credits and the crawler
burns about one a minute at full load, so draining a full balance takes
hours. Latency and CPU signals show within minutes; the CPU-credits alert
(below 30) fires only once the balance gets there. Long runs are allowed (up
to 4 hours) inside the 13:30-22:30 window.

Session-only: stopping the injector stops the crawler.
"""

import random
import threading
import time

from framework import Context, Incident

THREADS = 8
USER_AGENT = "Mozilla/5.0 (compatible; PriceSpyBot/2.1; +https://pricespy.example/bot)"
TERMS = ["hammer", "drill", "saw", "paint", "pipe", "wrench", "screw", "bolt", "ladder", "glove",
         "tape", "level", "brush", "hose", "valve", "light", "cable", "nail", "sander", "clamp"]


class CpuExhaustion(Incident):
    name = "cpu-exhaustion"
    title = "CPU credit exhaustion: an aggressive crawler"
    runs_on = ("mac", "vm")
    default_minutes = 90
    max_minutes = 240
    needs_traffic = False            # it IS the traffic
    root_cause = ("An aggressive crawler (User-Agent PriceSpyBot/2.1, many parallel connections) requests catalog "
                  "searches and product pages continuously. The extra load keeps the API and SQL Server busy, "
                  "draining the burstable VM's CPU credits; once they run out, the VM is throttled to its baseline.")
    correct_fix = ("Identify and limit the crawler: rate-limit or block its User-Agent/IP at Caddy, or return 429 "
                   "above a request rate per client. Short term, credits recover once the load stops. Longer term: "
                   "rate limiting at the edge, caching catalog responses, or a non-burstable VM size.")
    expected_signals = [
        "API: request rate far above the daily curve, mostly GET /api/products and /api/products/{id} with no "
        "synthetic-source tag; p95 latency rising",
        "VM: Percentage CPU high for a long time; CPU Credits Remaining falling steadily (alert below 30)",
        "SqlPerfCounters_CL: Batch Requests/sec far above normal; SqlWaitStats_CL: SOS_SCHEDULER_YIELD",
        "Perf: sqlservr and python CPU high",
    ]

    def __init__(self):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.requests_made = 0
        self.errors = 0
        self.lock = threading.Lock()

    def start(self, ctx: Context) -> None:
        ctx.do(f"start {THREADS} crawler threads ({USER_AGENT}) requesting searches and product pages from "
               f"{ctx.settings.api_base_url}", lambda: self._launch(ctx))

    def _launch(self, ctx: Context) -> None:
        for _ in range(THREADS):
            thread = threading.Thread(target=self._crawl, args=(ctx.settings.api_base_url,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _crawl(self, base_url: str) -> None:
        import requests
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        while not self.stop.is_set():
            try:
                term = random.choice(TERMS)
                page = session.get(f"{base_url}/api/products", params={"q": term, "page_size": 60}, timeout=30)
                items = page.json().get("items", []) if page.ok else []
                for item in items[:10]:                     # then visit some of the products found
                    if self.stop.is_set():
                        break
                    session.get(f"{base_url}/api/products/{item['product_id']}", timeout=30)
                    with self.lock:
                        self.requests_made += 1
                with self.lock:
                    self.requests_made += 1
            except Exception:
                with self.lock:
                    self.errors += 1
                time.sleep(1)

    def hold(self, ctx: Context) -> None:
        while not ctx.should_stop():
            ctx.wait(60)
            ctx.note(f"{self.requests_made} requests so far, {self.errors} errors")

    def revert(self, ctx: Context) -> None:
        if not self.threads:
            ctx.note("no crawler running from this run")
            return

        def stop():
            self.stop.set()
            for thread in self.threads:
                thread.join(timeout=40)
        ctx.do(f"stop the crawler ({self.requests_made} requests)", stop)


INCIDENT = CpuExhaustion()
