"""boot.py — cold-boot orchestration for RIG Sovereign.

Runs subsystems in topological order, attempts launchctl bootstrap on those
that aren't already running, waits up to N seconds for each to report
healthy, and binds a boot-proof at the end.

Idempotent: re-running boot on a fully-live system is a no-op.

stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .subsystems import (
    ProbeResult,
    Subsystem,
    SubsystemRegistry,
    default_registry,
    STATE_DIR,
)


_BOOT_DIR = STATE_DIR / "rig-master" / "boot"


# ---------------------------------------------------------------------------

@dataclass
class BootStepResult:
    """Result of booting one subsystem."""
    name: str
    action: str         # "skipped_no_label" | "already_live" | "bootstrapped" | "kickstarted" | "failed"
    final_status: str   # ProbeResult.status after settle
    final_ok: bool
    wait_seconds: float
    detail: str = ""


@dataclass
class BootReport:
    """Aggregate report of the full boot sequence."""
    boot_id: str
    ts: str
    total: int
    steps: list[BootStepResult] = field(default_factory=list)
    ok_count: int = 0
    skipped: int = 0
    failed: int = 0
    aborted_on: Optional[str] = None  # subsystem name that aborted the boot

    def to_dict(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "ts": self.ts,
            "total": self.total,
            "ok_count": self.ok_count,
            "skipped": self.skipped,
            "failed": self.failed,
            "aborted_on": self.aborted_on,
            "steps": [asdict(s) for s in self.steps],
        }


# ---------------------------------------------------------------------------

class BootRunner:
    """Cold-boot orchestrator. Honors topological dependencies."""

    def __init__(
        self,
        registry: SubsystemRegistry | None = None,
        plist_dir: Path | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._plist_dir = plist_dir or (Path.home() / "Library" / "LaunchAgents")
        _BOOT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _uid(self) -> int:
        try:
            return os.getuid()
        except AttributeError:
            return 501

    # ------------------------------------------------------------------
    def _is_launchd_loaded(self, label: str) -> bool:
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    # ------------------------------------------------------------------
    def _bootstrap(self, label: str) -> tuple[bool, str]:
        plist = self._plist_dir / f"{label}.plist"
        if not plist.exists():
            return False, f"plist not on disk at {plist}"
        cmd = ["launchctl", "bootstrap", f"gui/{self._uid()}", str(plist)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError) as e:
            return False, f"bootstrap raised: {e}"
        if r.returncode == 0:
            return True, "bootstrap ok"
        # launchctl returns rc=5 (I/O error) when already loaded — treat as success
        if r.returncode == 5:
            return True, "already loaded (rc=5)"
        return False, f"bootstrap rc={r.returncode}: {r.stderr.strip()[:200]}"

    # ------------------------------------------------------------------
    def _kickstart(self, label: str) -> tuple[bool, str]:
        cmd = ["launchctl", "kickstart", "-k", f"gui/{self._uid()}/{label}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError) as e:
            return False, f"kickstart raised: {e}"
        return r.returncode == 0, f"kickstart rc={r.returncode}"

    # ------------------------------------------------------------------
    def _wait_until_healthy(
        self,
        sub: Subsystem,
        max_wait_s: float = 10.0,
        poll_s: float = 1.0,
    ) -> ProbeResult:
        """Block up to max_wait_s for the subsystem to report healthy."""
        deadline = time.monotonic() + max_wait_s
        last: Optional[ProbeResult] = None
        while time.monotonic() < deadline:
            last = sub.probe()
            if last.ok:
                return last
            time.sleep(poll_s)
        return last if last is not None else sub.probe()

    # ------------------------------------------------------------------
    def boot_one(self, sub: Subsystem, max_wait_s: float = 10.0) -> BootStepResult:
        start = time.monotonic()

        # Library / service subsystems with no launchd_label can only be probed.
        if sub.launchd_label is None:
            result = sub.probe()
            return BootStepResult(
                name=sub.name,
                action="skipped_no_label",
                final_status=result.status,
                final_ok=result.ok,
                wait_seconds=time.monotonic() - start,
                detail=result.detail,
            )

        # Already running?
        pre = sub.probe()
        if pre.ok:
            return BootStepResult(
                name=sub.name,
                action="already_live",
                final_status=pre.status,
                final_ok=True,
                wait_seconds=time.monotonic() - start,
                detail=pre.detail,
            )

        # Try bootstrap. If launchctl says it's loaded, kickstart instead.
        if not self._is_launchd_loaded(sub.launchd_label):
            ok, msg = self._bootstrap(sub.launchd_label)
            action = "bootstrapped" if ok else "failed"
            detail = msg
        else:
            ok, msg = self._kickstart(sub.launchd_label)
            action = "kickstarted" if ok else "failed"
            detail = msg

        post = self._wait_until_healthy(sub, max_wait_s=max_wait_s)
        return BootStepResult(
            name=sub.name,
            action=action,
            final_status=post.status,
            final_ok=post.ok,
            wait_seconds=time.monotonic() - start,
            detail=detail if not post.ok else post.detail,
        )

    # ------------------------------------------------------------------
    def run(
        self,
        max_wait_s: float = 10.0,
        stop_on_critical_fail: bool = True,
    ) -> BootReport:
        boot_id = f"boot-{int(time.time())}"
        report = BootReport(
            boot_id=boot_id,
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            total=len(self._registry.names()),
        )
        order = self._registry.topological_order()
        for sub in order:
            step = self.boot_one(sub, max_wait_s=max_wait_s)
            report.steps.append(step)
            if step.final_ok:
                report.ok_count += 1
            elif step.action == "skipped_no_label":
                # Probe-only subsystem (no launchd_label). If it's critical AND
                # probe failed, treat as a hard boot failure — we cannot start
                # dependents against an unmet critical dependency just because
                # we lack a launchctl handle for the subsystem.
                if sub.critical:
                    report.failed += 1
                    if stop_on_critical_fail:
                        report.aborted_on = sub.name
                        break
                else:
                    report.skipped += 1
            else:
                report.failed += 1
                if stop_on_critical_fail and sub.critical:
                    report.aborted_on = sub.name
                    break

        self._write_artifact(report)
        self._bind_proof_best_effort(report)
        return report

    # ------------------------------------------------------------------
    def _write_artifact(self, report: BootReport) -> Path:
        out = _BOOT_DIR / f"{report.boot_id}.json"
        out.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        (_BOOT_DIR / "latest.json").write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return out

    # ------------------------------------------------------------------
    def _bind_proof_best_effort(self, report: BootReport) -> None:
        try:
            from services.ops.proof_system import generate_proof, store_proof
        except ImportError:
            return
        try:
            proof = generate_proof(
                artifact_path="services/rig_master/boot.py",
                sources=[{"type": "rig-master-boot", "ref": report.boot_id}],
                gates=[{
                    "name": "boot_sequence",
                    "result": "pass" if report.aborted_on is None else "fail",
                    "evidence": "services/rig_master/boot.py",
                }],
                signer="rig-builder",
                approval_state="autonomous",
                metadata={
                    "boot_id": report.boot_id,
                    "total": report.total,
                    "ok": report.ok_count,
                    "failed": report.failed,
                    "skipped": report.skipped,
                    "aborted_on": report.aborted_on,
                },
            )
            store_proof(proof)
        except Exception:
            pass


# ---------------------------------------------------------------------------

def run_boot() -> BootReport:
    return BootRunner().run()


if __name__ == "__main__":  # pragma: no cover
    rpt = run_boot()
    print(json.dumps(rpt.to_dict(), indent=2, sort_keys=True))


__all__ = ["BootReport", "BootStepResult", "BootRunner", "run_boot"]
