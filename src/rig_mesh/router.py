"""router.py — command dispatch for RigMaster CLI / dashboard / external callers.

Maps a string verb + args dict to a RigMaster method call. This keeps the CLI
parser thin and lets other surfaces (dashboard, MCP, agents) call the same
verb names.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .master import RigMaster


class CommandRouter:
    """Dispatch verbs to RigMaster methods. Returns JSON-friendly dicts."""

    def __init__(self, master: RigMaster | None = None) -> None:
        self._master = master or RigMaster()

    # ------------------------------------------------------------------
    @property
    def master(self) -> RigMaster:
        return self._master

    # ------------------------------------------------------------------
    def known_verbs(self) -> list[str]:
        return [
            "status",
            "list",
            "audit",
            "boot",
            "recover",
            "probe",
            "smoke",
            "version",
        ]

    # ------------------------------------------------------------------
    def dispatch(self, verb: str, args: dict | None = None) -> dict:
        args = args or {}
        handler = getattr(self, f"_verb_{verb.replace('-', '_')}", None)
        if handler is None or not callable(handler):
            return {
                "ok": False,
                "error": f"unknown verb: {verb!r}",
                "known": self.known_verbs(),
            }
        try:
            return handler(args)
        except Exception as e:
            return {"ok": False, "error": str(e), "verb": verb}

    # ------------------------------------------------------------------
    def _verb_status(self, args: dict) -> dict:
        snap = self._master.status_snapshot(write=bool(args.get("write", False)))
        return {"ok": True, "verb": "status", **snap}

    def _verb_list(self, args: dict) -> dict:
        return {"ok": True, "verb": "list", "subsystems": self._master.list_subsystems()}

    def _verb_audit(self, args: dict) -> dict:
        rpt = self._master.audit()
        return {"ok": rpt.overall != "fail", "verb": "audit", **rpt.to_dict()}

    def _verb_boot(self, args: dict) -> dict:
        max_wait = float(args.get("max_wait_s", 10.0))
        stop_on_fail = bool(args.get("stop_on_critical_fail", True))
        rpt = self._master.boot(max_wait_s=max_wait, stop_on_critical_fail=stop_on_fail)
        return {"ok": rpt.aborted_on is None, "verb": "boot", **rpt.to_dict()}

    def _verb_recover(self, args: dict) -> dict:
        flows = args.get("flows")
        if isinstance(flows, str):
            flows = [f.strip() for f in flows.split(",") if f.strip()]
        dry_run = bool(args.get("dry_run", False))
        rpt = self._master.recover(flows=flows, dry_run=dry_run)
        return {"ok": rpt.ok, "verb": "recover", **rpt.to_dict()}

    def _verb_probe(self, args: dict) -> dict:
        name = args.get("name")
        if not name:
            return {"ok": False, "error": "probe verb requires args.name"}
        try:
            sub = self._master.registry.get(name)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        result = sub.probe()
        return {"ok": result.ok, "verb": "probe", **result.to_dict()}

    def _verb_version(self, args: dict) -> dict:
        from . import __version__
        return {"ok": True, "verb": "version", "rig_master": __version__}

    def _verb_smoke(self, args: dict) -> dict:
        """Deterministic local smoke: version + list — no live network calls."""
        from . import __version__
        subs = self._master.list_subsystems()
        checks = {
            "version_ok": bool(__version__),
            "registry_populated": len(subs) > 0,
            "subsystem_count": len(subs),
        }
        passed = all(v for v in checks.values() if isinstance(v, bool))
        return {
            "ok": passed,
            "verb": "smoke",
            "rig_master": __version__,
            "checks": checks,
            "result": "PASS" if passed else "FAIL",
        }


__all__ = ["CommandRouter"]
