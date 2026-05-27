"""Tests for rig_mesh.recovery."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from rig_mesh.recovery import (
    Recovery,
    RecoveryReport,
    RecoveryStep,
)
from rig_mesh.subsystems import Subsystem, SubsystemRegistry


def test_recovery_step_dataclass():
    s = RecoveryStep(name="x", action="restart", ok=True, detail="d")
    assert s.name == "x"
    assert s.ok
    assert s.data == {}


def test_recovery_report_to_dict():
    r = RecoveryReport(recovery_id="r1", ts="now", flows_run=["a"], ok=True)
    d = r.to_dict()
    assert d["recovery_id"] == "r1"
    assert d["ok"] is True
    assert d["flows_run"] == ["a"]


def test_recovery_dry_run_no_steps(tmp_path: Path, monkeypatch):
    from rig_mesh import recovery as rec_mod
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    reg = SubsystemRegistry()
    rpt = Recovery(reg).run(dry_run=True)
    assert rpt.ok is True
    assert rpt.steps == []
    assert rpt.recovery_id.startswith("recover-")


def test_restore_proof_store_no_backup_dir(tmp_path: Path, monkeypatch):
    """When backup dir is missing, restore returns ok=False with detail."""
    from rig_mesh import recovery as rec_mod
    monkeypatch.setattr(rec_mod, "_BACKUP_DIR", tmp_path / "nope")
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().restore_proof_store_from_backup()
    assert step.ok is False
    assert "backup dir missing" in step.detail


def test_restore_proof_store_no_backups(tmp_path: Path, monkeypatch):
    """Empty backup dir returns ok=False."""
    from rig_mesh import recovery as rec_mod
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(rec_mod, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().restore_proof_store_from_backup()
    assert step.ok is False
    assert "no .sqlite backups" in step.detail


def test_restore_proof_store_refuses_when_live_larger(tmp_path: Path, monkeypatch):
    """If the live store is much larger than the backup, refuse to restore."""
    from rig_mesh import recovery as rec_mod
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "old.sqlite"
    backup.write_bytes(b"x" * 100)
    live_store = tmp_path / "proof_store.sqlite"
    live_store.write_bytes(b"y" * 1000)
    monkeypatch.setattr(rec_mod, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(rec_mod, "_PROOF_STORE", live_store)
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().restore_proof_store_from_backup(max_size_ratio=0.95)
    assert step.ok is False
    assert "refused" in step.detail
    # Live store must be untouched
    assert live_store.read_bytes() == b"y" * 1000


def test_restore_proof_store_succeeds_when_live_missing(tmp_path: Path, monkeypatch):
    """If no live store, restore happily copies the newest backup."""
    from rig_mesh import recovery as rec_mod
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "newest.sqlite"
    backup.write_bytes(b"backup-bytes")
    live_store = tmp_path / "missing-live.sqlite"
    monkeypatch.setattr(rec_mod, "_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(rec_mod, "_PROOF_STORE", live_store)
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().restore_proof_store_from_backup()
    assert step.ok is True
    assert live_store.read_bytes() == b"backup-bytes"


def test_replay_blocked_queue_missing_file(tmp_path: Path, monkeypatch):
    from rig_mesh import recovery as rec_mod
    monkeypatch.setattr(rec_mod, "_QUEUE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().replay_blocked_queue()
    assert step.ok is False
    assert "missing" in step.detail


def test_replay_blocked_queue_resets_stuck(tmp_path: Path, monkeypatch):
    """A 'running' job started >1h ago should reset to 'queued'."""
    from rig_mesh import recovery as rec_mod
    q = tmp_path / "jobs.json"
    stale_started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh_started = datetime.now(timezone.utc).isoformat()
    payload = {
        "updated_at": "before",
        "jobs": [
            {"job_id": "j1", "status": "running", "started_at": stale_started, "node": "rig-48gb"},
            {"job_id": "j2", "status": "running", "started_at": fresh_started, "node": "rig-48gb"},
            {"job_id": "j3", "status": "queued",
             "goal": "Autonomous backfill for blackwell: deep work",
             "node": "blackwell"},
        ],
    }
    q.write_text(json.dumps(payload))
    monkeypatch.setattr(rec_mod, "_QUEUE_PATH", q)
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    step = Recovery().replay_blocked_queue()
    assert step.ok is True
    assert step.data["reset_stuck"] >= 1
    assert step.data["closed_synthetic"] >= 1
    out = json.loads(q.read_text())
    statuses = {j["job_id"]: j["status"] for j in out["jobs"]}
    assert statuses["j1"] == "queued"          # stuck → reset
    assert statuses["j2"] == "running"          # fresh stays
    assert statuses["j3"] == "completed"        # synthetic backfill closed


def test_recovery_run_dispatches_flows(tmp_path: Path, monkeypatch):
    """Recovery.run honors the flows list."""
    from rig_mesh import recovery as rec_mod
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    monkeypatch.setattr(rec_mod, "_QUEUE_PATH", tmp_path / "missing.json")
    reg = SubsystemRegistry()
    # Inject one daemon with a missing heartbeat so restart_dead_daemons has work to skip
    reg.register(Subsystem(
        name="d1",
        description="d",
        kind="daemon",
        launchd_label="com.rig.nonexistent-fake",
        heartbeat_path=tmp_path / "nope.json",
    ))
    # Force kickstart to fail without actually spawning launchctl
    rec = Recovery(reg)
    monkeypatch.setattr(rec._registry.get("d1"), "kickstart", lambda: {"ok": False, "action": "kickstart"})
    rpt = rec.run(flows=["restart_dead_daemons", "replay_blocked_queue"])
    assert "restart_dead_daemons" in rpt.flows_run
    assert "replay_blocked_queue" in rpt.flows_run
    assert (tmp_path / "latest.json").exists()
