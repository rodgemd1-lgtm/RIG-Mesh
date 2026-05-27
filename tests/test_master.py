"""Tests for rig_mesh.master."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rig_mesh.master import RigMaster
from rig_mesh.subsystems import Subsystem, SubsystemRegistry


def _build_test_master(tmp_path: Path) -> RigMaster:
    reg = SubsystemRegistry()
    hb = tmp_path / "ok.json"
    hb.write_text("{}")
    reg.register(Subsystem(
        name="ok-cap",
        description="ok",
        kind="daemon",
        heartbeat_path=hb,
        heartbeat_max_age_s=300,
    ))
    reg.register(Subsystem(
        name="missing-cap",
        description="missing",
        kind="daemon",
        heartbeat_path=tmp_path / "missing.json",
        heartbeat_max_age_s=300,
    ))
    return RigMaster(reg)


def test_master_list_subsystems(tmp_path: Path):
    m = _build_test_master(tmp_path)
    rows = m.list_subsystems()
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"ok-cap", "missing-cap"}


def test_master_status_snapshot(tmp_path: Path):
    m = _build_test_master(tmp_path)
    snap = m.status_snapshot(write=False)
    assert snap["total"] == 2
    assert snap["live_ok"] == 1
    assert snap["no_data"] == 1
    assert snap["overall"] in ("warn", "fail")
    assert len(snap["probes"]) == 2


def test_master_overall_verdict_all_ok(tmp_path: Path):
    reg = SubsystemRegistry()
    hb = tmp_path / "h.json"
    hb.write_text("{}")
    reg.register(Subsystem(name="only", description="x", kind="daemon", heartbeat_path=hb))
    m = RigMaster(reg)
    snap = m.status_snapshot(write=False)
    assert snap["overall"] == "ok"


def test_master_overall_verdict_critical_fail(tmp_path: Path):
    reg = SubsystemRegistry()
    reg.register(Subsystem(
        name="critical-dead", description="x", kind="daemon",
        heartbeat_path=tmp_path / "missing.json",
        critical=True,
    ))
    m = RigMaster(reg)
    snap = m.status_snapshot(write=False)
    assert snap["overall"] == "fail"
    assert snap["critical_failed"] == 1


def test_master_audit_returns_report(tmp_path: Path, monkeypatch):
    from rig_mesh import audit as audit_mod
    monkeypatch.setattr(audit_mod, "_REPORT_DIR", tmp_path)
    m = _build_test_master(tmp_path)
    rpt = m.audit()
    assert rpt.total == 2
    assert rpt.audit_id.startswith("audit-")


def test_master_boot_returns_report(tmp_path: Path, monkeypatch):
    from rig_mesh import boot as boot_mod
    monkeypatch.setattr(boot_mod, "_BOOT_DIR", tmp_path)
    m = _build_test_master(tmp_path)
    rpt = m.boot(max_wait_s=0.5)
    assert rpt.total == 2
    assert rpt.boot_id.startswith("boot-")


def test_master_recover_dry_run(tmp_path: Path, monkeypatch):
    from rig_mesh import recovery as rec_mod
    monkeypatch.setattr(rec_mod, "_RECOVERY_DIR", tmp_path)
    m = _build_test_master(tmp_path)
    rpt = m.recover(dry_run=True)
    assert rpt.ok is True
    assert rpt.recovery_id.startswith("recover-")
