"""rig_mesh — top-level orchestrator for the RIG Sovereign system.

RigMaster sits above every subsystem (proof-store, monitor-runner, idle-queue,
cell-watcher, healer, rig-247-supervisor, cockpit-v5, meta-monitor,
backup-runner, retention-runner, escalation-runner, daily-smoke). It owns:

  - boot sequence (topological cold-start)
  - full system audit (truth-tracker across every subsystem)
  - recovery flows (restart dead daemons, restore from backup)
  - master CLI + dashboard

Lazy imports so the package is importable in any environment.
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = [
    "RigMaster",
    "Subsystem",
    "SubsystemRegistry",
    "MasterAuditReport",
    "BootReport",
    "RecoveryReport",
]


def __getattr__(name: str):
    if name == "RigMaster":
        from .master import RigMaster
        return RigMaster
    if name == "Subsystem":
        from .subsystems import Subsystem
        return Subsystem
    if name == "SubsystemRegistry":
        from .subsystems import SubsystemRegistry
        return SubsystemRegistry
    if name == "MasterAuditReport":
        from .audit import MasterAuditReport
        return MasterAuditReport
    if name == "BootReport":
        from .boot import BootReport
        return BootReport
    if name == "RecoveryReport":
        from .recovery import RecoveryReport
        return RecoveryReport
    raise AttributeError(f"module 'rig_mesh' has no attribute {name!r}")
