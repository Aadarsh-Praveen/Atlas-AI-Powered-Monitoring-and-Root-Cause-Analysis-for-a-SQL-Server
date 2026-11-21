"""The load shape: how many simulated users Locust runs, minute by minute.

Real stores aren't busy at a constant rate. This shape follows ShopLite's
business day in BOSTON time (it handles daylight saving itself):

    users
     peak |              ___                 ___
          |            _/   \\_             _/   \\_
          |        ___/       \\___     ___/       \\__
     low  |_______/               \\___/              \\______
          7am     10    12   14    15  17  18   19    21
                      (midday peak)     (evening peak)

plus a FRIDAY FLASH SALE: 12:00-12:20 Boston time, users multiplied by
LOAD_FLASH_MULTIPLIER (a short, sharp spike).

A GitHub Actions session runs for LOAD_DURATION_SECONDS (default 10 min) at the
level for the current time of day, so the day's sessions together trace the
curve. The shape re-reads the clock every tick, so a session that crosses
12:00 on a Friday sees the spike start.

Settings (environment variables):
  LOAD_PEAK_USERS        default 5    users at the busiest normal moment (light).
                                      Keep it modest: the VM is a burstable
                                      B2as_v2, and sustained load drains its CPU
                                      credits (then it throttles to ~40%).
  LOAD_FLASH_MULTIPLIER  default 2.0  how much the Friday flash sale multiplies
  LOAD_DURATION_SECONDS  default 600  stop the session after this long
  LOAD_CLOCK             (unset)      pretend it's this Boston time, e.g.
                                      "fri 12:05" or "tue 18:30" (local testing)
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from locust import LoadTestShape

BOSTON = ZoneInfo("America/New_York")

# Share of the peak at each Boston hour (interpolated in between). Hours not
# listed use the overnight floor.
HOURLY_SHARE = {
    7: 0.15, 8: 0.25, 9: 0.35, 10: 0.5, 11: 0.75, 12: 1.0, 13: 0.9, 14: 0.65,
    15: 0.5, 16: 0.55, 17: 0.75, 18: 0.95, 19: 0.85, 20: 0.55, 21: 0.3,
}
OVERNIGHT_SHARE = 0.1
FLASH_SALE = (4, 12, 0, 20)          # weekday (Mon=0 .. Fri=4), hour, start minute, end minute
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    value = float(raw) if raw else default
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return value


def parse_clock(text: str) -> tuple[int, int, int]:
    """"fri 12:05" -> (4, 12, 5)."""
    day, _, hhmm = text.strip().lower().partition(" ")
    hour, _, minute = hhmm.partition(":")
    if day[:3] not in DAY_NAMES:
        raise ValueError(f"LOAD_CLOCK: day must be one of {', '.join(DAY_NAMES)}, got {text!r}")
    return DAY_NAMES.index(day[:3]), int(hour), int(minute or 0)


def share_of_peak(weekday: int, hour: int, minute: int) -> float:
    """The curve: share of the peak (0.1 to 1.0) at this Boston time, before the flash sale."""
    here = HOURLY_SHARE.get(hour, OVERNIGHT_SHARE)
    following = HOURLY_SHARE.get((hour + 1) % 24, OVERNIGHT_SHARE)
    return here + (following - here) * (minute / 60)


def in_flash_sale(weekday: int, hour: int, minute: int) -> bool:
    day, sale_hour, start, end = FLASH_SALE
    return weekday == day and hour == sale_hour and start <= minute < end


class DailyCurveShape(LoadTestShape):
    """Locust calls tick() about once a second; it returns (users, spawn_rate)
    or None to stop the test."""

    def __init__(self):
        super().__init__()
        self.peak = _env_float("LOAD_PEAK_USERS", 5, 1, 50)
        self.flash = _env_float("LOAD_FLASH_MULTIPLIER", 2.0, 1, 5)
        self.duration = _env_float("LOAD_DURATION_SECONDS", 600, 30, 3600)
        clock = os.environ.get("LOAD_CLOCK", "").strip()
        self.fake_start = parse_clock(clock) if clock else None

    def _boston_now(self) -> tuple[int, int, int]:
        if self.fake_start:
            weekday, hour, minute = self.fake_start
            # Advance the fake clock with real run time, so a test at
            # "fri 11:58" walks into the flash sale.
            total = hour * 60 + minute + int(self.get_run_time() // 60)
            return weekday, (total // 60) % 24, total % 60
        now = datetime.now(BOSTON)
        return now.weekday(), now.hour, now.minute

    def tick(self):
        if self.get_run_time() >= self.duration:
            return None                                   # session over
        weekday, hour, minute = self._boston_now()
        share = share_of_peak(weekday, hour, minute)
        if in_flash_sale(weekday, hour, minute):
            share *= self.flash
        users = max(1, round(self.peak * share))
        # Spawn quickly enough to reach the level within a few seconds.
        return users, max(1, users)
