"""audit.py — full RIG Sovereign system audit.

Probes every registered subsystem, aggregates results, computes overall
health verdict, and writes a JSON report to disk + binds a proof via the
proof_system (best-effort; degrades gracefully if proof_system not present).

stdlib only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .subsystems import (
    ProbeResult,
    Subsystem,
    SubsystemRegistry,
    default_registry,
    REPO_ROOT,
    STATE_DIR,
)


_REPORT_DIR = STATE_DIR / "rig-master"


# ---------------------------------------------------------------------------

@dataclass
class MasterAuditReport:
    """Aggregate audit across every subsystem."""
    audit_id: str
    ts: str
    total: int
    live_ok: int
    stale: int
    no_data: int
    down: int
    unknown: int
    critical_failed: int
    overall: str           # "ok" | "warn" | "fail"
    probes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------

class MasterAuditor:
    """Runs the system-wide audit."""

    def __init__(self, registry: SubsystemRegistry | None = None) -> None:
        self._registry = registry or default_registry()
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def run(self, write_artifact: bool = True) -> MasterAuditReport:
        """Probe every subsystem; return aggregate report."""
        probes: list[ProbeResult] = []
        critical_failed = 0
        for sub in self._registry.all():
            result = sub.probe()
            probes.append(result)
            if sub.critical and not result.ok:
                critical_failed += 1

        counts = {"live_ok": 0, "stale": 0, "no_data": 0, "down": 0, "unknown": 0}
        for r in probes:
            counts[r.status] = counts.get(r.status, 0) + 1

        unhealthy = counts["down"] + counts["stale"] + counts["no_data"] + counts["unknown"]
        if critical_failed > 0:
            overall = "fail"
        elif unhealthy >= max(1, len(probes) // 3):
            overall = "warn"
        elif counts["live_ok"] == len(probes):
            overall = "ok"
        else:
            # Some non-critical unhealthy but under threshold; still warn.
            overall = "warn" if unhealthy > 0 else "ok"

        audit_id = f"audit-{int(time.time())}-{counts['live_ok']}"
        report = MasterAuditReport(
            audit_id=audit_id,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            total=len(probes),
            live_ok=counts["live_ok"],
            stale=counts["stale"],
            no_data=counts["no_data"],
            down=counts["down"],
            unknown=counts["unknown"],
            critical_failed=critical_failed,
            overall=overall,
            probes=[r.to_dict() for r in probes],
        )

        if write_artifact:
            self._write_artifact(report)
            self._bind_proof_best_effort(report)

        return report

    # ------------------------------------------------------------------
    def _write_artifact(self, report: MasterAuditReport) -> Path:
        out = _REPORT_DIR / f"audit-{report.audit_id}.json"
        out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        # Also overwrite a "latest.json" pointer
        latest = _REPORT_DIR / "audit-latest.json"
        latest.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return out

    # ------------------------------------------------------------------
    def _bind_proof_best_effort(self, report: MasterAuditReport) -> None:
        """Try to bind an audit proof. Never raises — degrade silently."""
        try:
            from services.ops.proof_system import generate_proof, store_proof
        except ImportError:
            return
        try:
            proof = generate_proof(
                artifact_path="services/rig_master/audit.py",
                sources=[{"type": "rig-master-audit", "ref": report.audit_id}],
                gates=[{
                    "name": "system_audit",
                    "result": "pass" if report.overall == "ok" else "warn",
                    "evidence": "services/rig_master/audit.py",
                }],
                signer="rig-auditor",
                approval_state="autonomous",
                metadata={
                    "audit_id": report.audit_id,
                    "overall": report.overall,
                    "live_ok": report.live_ok,
                    "down": report.down,
                    "stale": report.stale,
                    "total": report.total,
                },
            )
            store_proof(proof)
        except Exception:
            # Proof binding is best-effort; never raise from audit.
            pass


# ---------------------------------------------------------------------------

def run_audit() -> MasterAuditReport:
    """Module-level entry point for `python3 -m rig_mesh.audit`."""
    return MasterAuditor().run()


if __name__ == "__main__":  # pragma: no cover - CLI smoke
    rpt = run_audit()
    print(json.dumps(rpt.to_dict(), indent=2, sort_keys=True))


__all__ = ["MasterAuditReport", "MasterAuditor", "run_audit"]
