"""Settings for the Playwright shoppers, read from environment variables.

Every value has a safe default for a LOCAL run (storefront on localhost:3000,
API on localhost:8000, light traffic). GitHub Actions sets the production
values from repository variables and secrets (see .github/workflows/shoppers.yml).

| Variable                     | Default                               | Meaning |
|------------------------------|---------------------------------------|---------|
| STOREFRONT_URL               | http://localhost:3000                 | Where the browsers go |
| API_URL                      | http://localhost:8000                 | The API the storefront calls (health check, response counting) |
| SHOPLITE_ADMIN_TOKEN         | (empty)                               | For the admin persona; empty = admin persona skipped |
| SHOPPERS_CONCURRENCY         | 2                                     | Shoppers browsing at the same time |
| SHOPPERS_DURATION_SECONDS    | 300                                   | Keep starting new sessions until this much time has passed |
| SHOPPERS_THINK_MIN_SECONDS   | 1                                     | Shortest pause between actions |
| SHOPPERS_THINK_MAX_SECONDS   | 6                                     | Longest pause between actions |
| SHOPPERS_WEIGHTS             | browser=40,searcher=25,buyer=20,loyal=8,admin=2 | Persona mix (relative weights) |
| SHOPPERS_MOBILE_SHARE        | 0.05                                  | Share of sessions (any persona) on a phone-sized screen |
| SHOPPERS_HEADLESS            | true                                  | false = show the browser windows |
| SHOPPERS_BROWSER_CHANNEL     | (empty = Playwright's bundled Chromium) | "chrome" = your installed Google Chrome (e.g. for local headed runs) |
| SHOPPERS_SLOW_MO_MS          | 0                                     | Slow every action down (watching a visible run) |
| SHOPPERS_STEP_TIMEOUT_MS     | 15000                                 | How long to wait for a page element before calling the page broken |
| SHOPPERS_ARTIFACTS_DIR       | shoppers-artifacts                    | Parent folder; each run writes to its own run-<UTC time>/ inside it |
| SHOPPERS_SEED                | (random)                              | Fix the random choices to repeat a run |

The run.py command line can override concurrency, duration, headless, and
force a single persona for testing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

PERSONAS = ("browser", "searcher", "buyer", "loyal", "admin")

# Playwright browser channels the shoppers accept. Empty = the Chromium that
# `python -m playwright install chromium` downloads (what GitHub Actions uses).
# The others are browsers already installed on the machine.
BROWSER_CHANNELS = ("chrome", "chrome-beta", "msedge")
DEFAULT_WEIGHTS = "browser=40,searcher=25,buyer=20,loyal=8,admin=2"


class SettingsError(ValueError):
    """A setting is missing or invalid; the message says which and why."""


def _text(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SettingsError(f"{name} must be true or false, got {raw!r}")


def _url(name: str, default: str) -> str:
    value = _text(name, default).rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise SettingsError(f"{name} must start with http:// or https://, got {value!r}")
    return value


def parse_weights(raw: str) -> dict[str, int]:
    """"browser=40,buyer=20" -> {"browser": 40, "buyer": 20}. Unknown names are
    an error (a typo would otherwise silently drop a persona)."""
    weights: dict[str, int] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        name, _, number = part.partition("=")
        name = name.strip().lower()
        if name not in PERSONAS:
            raise SettingsError(f"SHOPPERS_WEIGHTS: unknown persona {name!r}; use {', '.join(PERSONAS)}")
        try:
            weight = int(number)
        except ValueError:
            raise SettingsError(f"SHOPPERS_WEIGHTS: weight for {name!r} must be a whole number") from None
        if weight < 0:
            raise SettingsError(f"SHOPPERS_WEIGHTS: weight for {name!r} can't be negative")
        weights[name] = weight
    if not any(weights.values()):
        raise SettingsError("SHOPPERS_WEIGHTS: at least one persona needs a weight above 0")
    return weights


@dataclass
class Settings:
    storefront_url: str
    api_url: str
    admin_token: str = field(repr=False)          # never printed (repr=False)
    concurrency: int
    duration_seconds: int
    think_min: float
    think_max: float
    weights: dict[str, int]
    mobile_share: float
    headless: bool
    browser_channel: str | None       # None = Playwright's bundled Chromium
    slow_mo_ms: int
    step_timeout_ms: int
    artifacts_dir: Path
    seed: int | None

    @property
    def is_local(self) -> bool:
        return "localhost" in self.storefront_url or "127.0.0.1" in self.storefront_url


def load_settings() -> Settings:
    think_min = _float("SHOPPERS_THINK_MIN_SECONDS", 1.0, 0.0, 60.0)
    think_max = _float("SHOPPERS_THINK_MAX_SECONDS", 6.0, 0.0, 120.0)
    if think_max < think_min:
        raise SettingsError("SHOPPERS_THINK_MAX_SECONDS must be at least SHOPPERS_THINK_MIN_SECONDS")

    weights = parse_weights(_text("SHOPPERS_WEIGHTS", DEFAULT_WEIGHTS))
    admin_token = os.environ.get("SHOPLITE_ADMIN_TOKEN", "").strip()
    if not admin_token:
        weights["admin"] = 0       # no token, no admin sessions (they'd only fail)
        if not any(weights.values()):
            raise SettingsError("Only the admin persona has weight, but SHOPLITE_ADMIN_TOKEN is empty")

    channel = os.environ.get("SHOPPERS_BROWSER_CHANNEL", "").strip().lower() or None
    if channel and channel not in BROWSER_CHANNELS:
        raise SettingsError(f"SHOPPERS_BROWSER_CHANNEL must be empty (bundled Chromium) or one of "
                            f"{', '.join(BROWSER_CHANNELS)}, got {channel!r}")

    seed_raw = os.environ.get("SHOPPERS_SEED", "").strip()
    try:
        seed = int(seed_raw) if seed_raw else None
    except ValueError:
        raise SettingsError(f"SHOPPERS_SEED must be a whole number, got {seed_raw!r}") from None

    return Settings(
        storefront_url=_url("STOREFRONT_URL", "http://localhost:3000"),
        api_url=_url("API_URL", "http://localhost:8000"),
        admin_token=admin_token,
        # Upper limits keep a typo from launching a small army of browsers
        # against a burstable VM.
        concurrency=_int("SHOPPERS_CONCURRENCY", 2, 1, 8),
        duration_seconds=_int("SHOPPERS_DURATION_SECONDS", 300, 10, 3600),
        think_min=think_min,
        think_max=think_max,
        weights=weights,
        mobile_share=_float("SHOPPERS_MOBILE_SHARE", 0.05, 0.0, 1.0),
        headless=_bool("SHOPPERS_HEADLESS", True),
        browser_channel=channel,
        slow_mo_ms=_int("SHOPPERS_SLOW_MO_MS", 0, 0, 5000),
        step_timeout_ms=_int("SHOPPERS_STEP_TIMEOUT_MS", 15000, 1000, 120000),
        artifacts_dir=Path(_text("SHOPPERS_ARTIFACTS_DIR", "shoppers-artifacts")),
        seed=seed,
    )
