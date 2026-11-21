"""Feature flags: named behaviors switched on or off by configuration.

A flag is turned on by listing its name in FEATURE_FLAGS in shoplite/api/.env
(comma-separated). Everything not listed is off. Changing flags means
restarting the API; no code changes.

Code checks a flag where behavior differs:
    from app import flags
    if flags.is_enabled("example_flag"):
        ...  # alternative behavior

Phase 1: the registry below is EMPTY and every flag is off. Phase 5 registers
flags that switch on deliberate "buggy release" behavior for incident
scenarios. Keeping the mechanism separate from the scenarios means the normal
code path is always the default.

Two kinds of typo are caught:
  - in configuration: validate_configured_flags() refuses to start the API if
    FEATURE_FLAGS names a flag that isn't registered.
  - in code: is_enabled() raises if asked about an unregistered flag, instead
    of quietly returning False forever.
"""

from app.config import get_settings

# Registry of every flag the code knows about: name -> what turning it on does.
# Names are lowercase with underscores. Empty in Phase 1.
KNOWN_FLAGS: dict[str, str] = {}


def validate_configured_flags() -> None:
    """Fail at startup if FEATURE_FLAGS contains names that aren't registered."""
    unknown = sorted(get_settings().enabled_flag_names - KNOWN_FLAGS.keys())
    if unknown:
        known = ", ".join(sorted(KNOWN_FLAGS)) or "(none registered yet)"
        raise ValueError(f"Unknown feature flag(s) in FEATURE_FLAGS: {', '.join(unknown)}. Known flags: {known}")


def is_enabled(name: str) -> bool:
    """True if the flag is registered and turned on in configuration."""
    key = name.lower()
    if key not in KNOWN_FLAGS:
        raise KeyError(f"Feature flag {name!r} is not registered in app/flags.py")
    return key in get_settings().enabled_flag_names


def enabled_flags() -> list[str]:
    """Names of the flags currently on, sorted (for startup logging)."""
    return sorted(get_settings().enabled_flag_names & KNOWN_FLAGS.keys())
