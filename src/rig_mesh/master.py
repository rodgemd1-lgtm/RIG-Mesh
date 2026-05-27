"""master.py — RigMaster: the top-level orchestrator.

Composes the SubsystemRegistry, MasterAuditor, BootRunner, and Recovery
modules behind a single object with a small uniform API:

  master.status_snapshot()    -> dict   (cheap; aggregates all subsystem probes)
  master.audit()              -> MasterAuditReport
  master.boot(max_wait_s=10)  -> BootReport
  master.recover(flows=...)   -> RecoveryReport
  master.list_subsystems()    -> list[dict]

stdlib only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import MasterAuditor, MasterAuditReport
from .boot import BootRunner, BootReport
from .recovery import Recovery, RecoveryReport
from .subsystems import (
    Subsystem,
    SubsystemRegistry,
    default_registry,
    STATE_DIR,
)


_MASTER_DIR = STATE_DIR / "rig-master"


# ---------------------------------------------------------------------------

class RigMaster:
    """One object that knows about every subsystem and what to do with it."""

    def __init__(self, registry: SubsystemRegistry | None = None) -> None:
        self._registry: SubsystemRegistry = registry or default_registry()
        _MASTER_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def registry(self) -> SubsystemRegistry:
        return self._registry

    # ------------------------------------------------------------------
    def list_subsystems(self) -> list[dict]:
        """Return a list of every registered subsystem as JSON-friendly dicts."""
        return [s.to_dict() for s in self._registry.all()]

    # ------------------------------------------------------------------
    def status_snapshot(self, write: bool = False) -> dict:
        """Cheap probe-everything snapshot. Returns dict suitable for JSON."""
        probes: list[dict] = []
        counts = {"live_ok": 0, "stale": 0, "no_data": 0, "down": 0, "unknown": 0}
        critical_failed = 0
        for sub in self._registry.all():
            r = sub.probe()
            probes.append({
                "name": sub.name,
                "kind": sub.kind,
                "critical": sub.critical,
                **r.to_dict(),
            })
            counts[r.status] = counts.get(r.status, 0) + 1
            if sub.critical and not r.ok:
                critical_failed += 1

        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total": len(probes),
            **counts,
            "critical_failed": critical_failed,
            "overall": self._overall_verdict(counts, critical_failed, len(probes)),
            "probes": probes,
        }
        if write:
            (_MASTER_DIR / "status-latest.json").write_text(
                json.dumps(snapshot, indent=2, sort_keys=True)
            )
        return snapshot

    # ------------------------------------------------------------------
    @staticmethod
    def _overall_verdict(counts: dict, critical_failed: int, total: int) -> str:
        unhealthy = (counts.get("down", 0) + counts.get("stale", 0)
                     + counts.get("no_data", 0) + counts.get("unknown", 0))
        if critical_failed > 0:
            return "fail"
        if unhealthy >= max(1, total // 3):
            return "warn"
        if counts.get("live_ok", 0) == total:
            return "ok"
        return "warn" if unhealthy > 0 else "ok"

    # ------------------------------------------------------------------
    def audit(self) -> MasterAuditReport:
        """Run a full audit with artifact write + proof binding."""
        return MasterAuditor(self._registry).run()

    # ------------------------------------------------------------------
    def boot(self, max_wait_s: float = 10.0, stop_on_critical_fail: bool = True) -> BootReport:
        """Run cold-boot sequence over the subsystem topological order."""
        return BootRunner(self._registry).run(
            max_wait_s=max_wait_s,
            stop_on_critical_fail=stop_on_critical_fail,
        )

    # ------------------------------------------------------------------
    def recover(
        self,
        flows: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> RecoveryReport:
        """Run the requested recovery flows."""
        return Recovery(self._registry).run(flows=flows, dry_run=dry_run)


__all__ = ["RigMaster"]
