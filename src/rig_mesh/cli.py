"""cli.py — `rig-master` CLI.

Usage:
  python3 -m rig_mesh.cli status
  python3 -m rig_mesh.cli list
  python3 -m rig_mesh.cli audit
  python3 -m rig_mesh.cli boot [--max-wait-s 10]
  python3 -m rig_mesh.cli recover [--flows restart_dead_daemons,replay_blocked_queue] [--dry-run]
  python3 -m rig_mesh.cli probe --name <subsystem>
  python3 -m rig_mesh.cli version
"""
from __future__ import annotations

import argparse
import json
import sys

from .router import CommandRouter


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rig-master",
        description="RIG Sovereign master orchestrator CLI",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    s_status = sub.add_parser("status", help="Snapshot every subsystem health")
    s_status.add_argument("--write", action="store_true", help="Write status-latest.json")

    sub.add_parser("list", help="List every registered subsystem")
    sub.add_parser("audit", help="Run full audit + write artifact + bind proof")

    s_boot = sub.add_parser("boot", help="Cold-boot every subsystem in topological order")
    s_boot.add_argument("--max-wait-s", type=float, default=10.0)
    s_boot.add_argument("--no-stop-on-critical-fail", action="store_true")

    s_recover = sub.add_parser("recover", help="Run recovery flows")
    s_recover.add_argument(
        "--flows",
        default="restart_dead_daemons,replay_blocked_queue",
        help="Comma-separated flow names",
    )
    s_recover.add_argument("--dry-run", action="store_true")

    s_probe = sub.add_parser("probe", help="Probe a single subsystem by name")
    s_probe.add_argument("--name", required=True)

    sub.add_parser("version", help="Print rig_master version")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    router = CommandRouter()

    args: dict = {}
    if ns.verb == "status":
        args["write"] = bool(ns.write)
    elif ns.verb == "boot":
        args["max_wait_s"] = ns.max_wait_s
        args["stop_on_critical_fail"] = not ns.no_stop_on_critical_fail
    elif ns.verb == "recover":
        args["flows"] = ns.flows
        args["dry_run"] = bool(ns.dry_run)
    elif ns.verb == "probe":
        args["name"] = ns.name

    response = router.dispatch(ns.verb, args)
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
