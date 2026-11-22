"""The incident injector's command line.

    python incidents/injector.py list
    python incidents/injector.py start <incident> [--minutes N] [--dry-run] [--skip-traffic-check]
    python incidents/injector.py status
    python incidents/injector.py revert [<incident>]

  list      every incident: name, where it runs, default and maximum minutes
  start     cause it (foreground: it reverts at the deadline or on Ctrl+C);
            --dry-run shows exactly what it would do and the ground truth it
            would record, and changes nothing
  status    the active incident (from the state file) and any persistent change
            still in place (a dropped index, a renamed column, a flag left on),
            found on the live system
  revert    put it back: the active incident by default, or a named one (works
            even after a crash, from the live system)

Exit codes: 0 done, 1 the incident failed (and was reverted), 2 refused by a
guard or a configuration problem (nothing was changed), 3 'status' couldn't
check every incident (result unknown).

Each incident is one file in incidents/scenarios/, defining INCIDENT. Settings:
incidents/.env (template .env.example). See incidents/README.md.
"""

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework import Refused, Runner, Settings  # noqa: E402

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


def load_incidents() -> dict:
    """Every incidents/scenarios/*.py that defines INCIDENT, by name."""
    incidents = {}
    for path in sorted(SCENARIOS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = importlib.import_module(f"scenarios.{path.stem}")
        incident = getattr(module, "INCIDENT", None)
        if incident is not None:
            incidents[incident.name] = incident
    return incidents


def main() -> int:
    incidents = load_incidents()
    parser = argparse.ArgumentParser(description="ShopLite incident injector (Phase 5)")
    parser.add_argument("--env-file", type=Path,
                        help="settings file instead of incidents/.env (e.g. incidents/.env.prod for the "
                             "Mac's production vendor copy)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the incidents")
    start = commands.add_parser("start", help="start an incident")
    start.add_argument("incident", choices=sorted(incidents))
    start.add_argument("--minutes", type=int, help="how long it lasts (default: the incident's own)")
    start.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    start.add_argument("--skip-traffic-check", action="store_true",
                       help="don't require recent shopper traffic (local testing)")
    commands.add_parser("status", help="the active incident and any change still in place")
    revert = commands.add_parser("revert", help="revert the active (or a named) incident")
    revert.add_argument("incident", nargs="?", choices=sorted(incidents))
    args = parser.parse_args()

    if args.command == "list":
        print(f"{'incident':24} {'runs on':10} {'minutes':10} title")
        for incident in incidents.values():
            minutes = f"{incident.default_minutes} (max {incident.max_minutes})" if incident.foreground else "until revert"
            print(f"{incident.name:24} {'/'.join(incident.runs_on):10} {minutes:10} {incident.title}")
        return 0

    try:
        runner = Runner(Settings.load(args.env_file), incidents)
        if args.command == "start":
            return runner.start(args.incident, args.minutes, args.dry_run, args.skip_traffic_check)
        if args.command == "status":
            return runner.status()
        return runner.revert(args.incident)
    except Refused as refusal:
        print(f"REFUSED: {refusal} Nothing was changed.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
