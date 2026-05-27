"""Tests for rig_mesh.audit."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rig_mesh.audit import MasterAuditReport, MasterAuditor
from rig_mesh.subsystems import (
    Subsystem,
    SubsystemRegistry,
)


def _make_registry(tmp_path: Path, all_healthy: bool = True) -> SubsystemRegistry:
    reg = SubsystemRegistry()
    hb_a = tmp_path / "a.json"
    hb_a.write_text("{}")
    reg.register(Subsystem(
        name="a", description="a", kind="daemon",
        heartbeat_path=hb_a, heartbeat_max_age_s=300, critical=True,
    ))
    hb_b = tmp_path / "b.json"
    if all_healthy:
        hb_b.write_text("{}")
    else:
        # Don't create the file -> no_data
        pass
    reg.register(Subsystem(
        name="b", description="b", kind="daemon",
        heartbeat_path=hb_b, heartbeat_max_age_s=300,
    ))
    return reg


def test_audit_all_healthy(tmp_path: Path):
    reg = _make_registry(tmp_path, all_healthy=True)
    auditor = MasterAuditor(reg)
    rpt = auditor.run(write_artifact=False)
    assert rpt.total == 2
    assert rpt.live_ok == 2
    assert rpt.down == 0
    assert rpt.critical_failed == 0
    assert rpt.overall == "ok"


def test_audit_one_missing(tmp_path: Path):
    reg = _make_registry(tmp_path, all_healthy=False)
    rpt = MasterAuditor(reg).run(write_artifact=False)
    assert rpt.total == 2
    assert rpt.no_data == 1
    assert rpt.overall in ("warn", "fail")


def test_audit_critical_failed_is_fail(tmp_path: Path):
    reg = SubsystemRegistry()
    reg.register(Subsystem(
        name="critical-x",
        description="x",
        kind="daemon",
        heartbeat_path=tmp_path / "never.json",  # missing
        critical=True,
    ))
    rpt = MasterAuditor(reg).run(write_artifact=False)
    assert rpt.critical_failed == 1
    assert rpt.overall == "fail"


def test_audit_writes_artifact(tmp_path: Path, monkeypatch):
    # Redirect _REPORT_DIR to tmp by monkeypatching the module-level constant.
    from rig_mesh import audit as audit_mod
    monkeypatch.setattr(audit_mod, "_REPORT_DIR", tmp_path)
    reg = _make_registry(tmp_path, all_healthy=True)
    rpt = MasterAuditor(reg).run(write_artifact=True)
    assert (tmp_path / f"audit-{rpt.audit_id}.json").exists()
    assert (tmp_path / "audit-latest.json").exists()
    payload = json.loads((tmp_path / "audit-latest.json").read_text())
    assert payload["total"] == rpt.total


def test_audit_to_dict_shape(tmp_path: Path):
    reg = _make_registry(tmp_path, all_healthy=True)
    rpt = MasterAuditor(reg).run(write_artifact=False)
    d = rpt.to_dict()
    assert "audit_id" in d and "overall" in d and "probes" in d
    assert len(d["probes"]) == 2


def test_audit_id_is_unique_per_run(tmp_path: Path):
    reg = _make_registry(tmp_path, all_healthy=True)
    r1 = MasterAuditor(reg).run(write_artifact=False)
    time.sleep(1.1)  # audit_id is second-precision
    r2 = MasterAuditor(reg).run(write_artifact=False)
    assert r1.audit_id != r2.audit_id
