"""API fault: a buggy feature rollout.

Story: a release switches on the new search ranking (feature flag
search_ranking_v2, API 1.2.0). The ranking service it depends on is slow and
fails half the time: searches become slow and many return 500.

What it does: adds search_ranking_v2 to FEATURE_FLAGS in the API's .env and
restarts the API (on the VM with restart-api.ps1, like a deployment), so the
API's startup log lists the flag. The ranking service's delay and error rate
come from the API's settings (RANKING_SERVICE_DELAY_MS, _ERROR_RATE;
shoplite/api/app/ranking.py). Revert removes the flag and restarts again.
Only the FEATURE_FLAGS line is changed; the rest of the file is kept exactly.

Persistent: the flag stays on until reverted (even across restarts);
'status' (on the VM) detects it.
"""

import re
import subprocess

from framework import Context, Incident

FLAG = "search_ranking_v2"
LINE = re.compile(r"^FEATURE_FLAGS=(.*)$", re.MULTILINE)


class ApiFault(Incident):
    name = "api-fault"
    title = "API fault: the new search ranking's dependency is slow and failing"
    runs_on = ("vm",)
    default_minutes = 20
    max_minutes = 45
    root_cause = ("Feature flag search_ranking_v2 was turned on (API restarted with it). Product searches now call "
                  "the ranking service, which takes about 2.5 s and fails about half the time "
                  "(RankingServiceError, 503 from the dependency), so searches are slow and many return 500.")
    correct_fix = ("Turn the flag off (remove search_ranking_v2 from FEATURE_FLAGS) and restart the API with "
                   "restart-api.ps1; investigate the ranking service before re-enabling, ideally behind a timeout "
                   "and a fallback to the normal ordering.")
    expected_signals = [
        "alerts shoplite-api-error-rate and shoplite-api-latency",
        "AppExceptions: RankingServiceError 'Ranking service returned 503' on GET /api/products",
        "AppDependencies: POST /v2/rank to ranking-service, about 2.5 s, half failed",
        "the API restarted shortly before (new process; the startup log lists feature flags on: search_ranking_v2)",
        "SQL Server itself unaffected (no blocking, normal waits)",
    ]

    # ---- the API's .env ----
    def _flags(self, ctx: Context) -> list[str]:
        if not ctx.settings.api_env_file.exists():      # locally the API may run on the root .env alone
            return []
        text = ctx.settings.api_env_file.read_text(encoding="utf-8")
        match = LINE.search(text)
        return [f.strip() for f in match.group(1).split(",") if f.strip()] if match else []

    def _write_flags(self, ctx: Context, flags: list[str]) -> None:
        path = ctx.settings.api_env_file
        # Locally the API's .env may not exist (the root .env is enough); then this
        # creates it as a one-line override file, which the layered settings honour.
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        new_line = "FEATURE_FLAGS=" + ",".join(flags)
        text = LINE.sub(new_line, text) if LINE.search(text) else text.rstrip("\n") + "\n" + new_line + "\n"
        path.write_text(text, encoding="utf-8")

    def _restart(self, ctx: Context) -> None:
        command = ctx.settings.api_restart_command
        if command:
            subprocess.run(command, shell=True, check=True, timeout=180)
        else:
            input("  Restart your local API now (Ctrl+C in its terminal, start uvicorn again), then press Enter: ")

    # ---- the incident ----
    def start(self, ctx: Context) -> None:
        flags = self._flags(ctx)
        ctx.details["flags_before"] = flags
        ctx.do(f"add {FLAG} to FEATURE_FLAGS in {ctx.settings.api_env_file}",
               lambda: self._write_flags(ctx, flags + [FLAG] if FLAG not in flags else flags))
        ctx.do("restart the API (like a deployment)", lambda: self._restart(ctx))

    def revert(self, ctx: Context) -> None:
        flags = self._flags(ctx)
        if FLAG not in flags:
            ctx.note(f"{FLAG} is off: nothing to revert")
            return
        ctx.do(f"remove {FLAG} from FEATURE_FLAGS", lambda: self._write_flags(ctx, [f for f in flags if f != FLAG]))
        ctx.do("restart the API", lambda: self._restart(ctx))

    def detect(self, ctx: Context) -> str | None:
        return f"{FLAG} is ON in {ctx.settings.api_env_file}" if FLAG in self._flags(ctx) else None


INCIDENT = ApiFault()
