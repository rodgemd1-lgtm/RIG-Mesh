"""Tests for rig_mesh.boot."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rig_mesh.boot import BootReport, BootRunner, BootStepResult
from rig_mesh.subsystems import Subsystem, SubsystemRegistry


def _heartbeat_reg(tmp_path: Path, fresh: bool = True) -> SubsystemRegistry:
    reg = SubsystemRegistry()
    hb = tmp_path / "hb.json"
    if fresh:
        hb.write_text("{}")
    reg.register(Subsystem(
        name="probe-only",
        description="library probe-only subsystem",
        kind="library",
        heartbeat_path=hb,
    ))
    return reg


def test_boot_one_no_launchd_label(tmp_path: Path):
    reg = _heartbeat_reg(tmp_path, fresh=True)
    br = BootRunner(reg)
    step = br.boot_one(reg.get("probe-only"))
    assert step.action == "skipped_no_label"
    assert step.final_ok


def test_boot_run_returns_report(tmp_path: Path, monkeypatch):
    reg = _heartbeat_reg(tmp_path, fresh=True)
    # Redirect boot artifact dir to tmp.
    from rig_mesh import boot as boot_mod
    monkeypatch.setattr(boot_mod, "_BOOT_DIR", tmp_path)
    br = BootRunner(reg)
    rpt = br.run(max_wait_s=1.0)
    assert isinstance(rpt, BootReport)
    assert rpt.total == 1
    # final_ok=True takes precedence over the skipped_no_label action label
    assert rpt.ok_count == 1
    assert rpt.skipped == 0
    assert rpt.aborted_on is None
    assert (tmp_path / "latest.json").exists()
    payload = json.loads((tmp_path / "latest.json").read_text())
    assert payload["total"] == 1


def test_boot_report_to_dict_shape(tmp_path: Path, monkeypatch):
    reg = _heartbeat_reg(tmp_path, fresh=True)
    from rig_mesh import boot as boot_mod
    monkeypatch.setattr(boot_mod, "_BOOT_DIR", tmp_path)
    rpt = BootRunner(reg).run(max_wait_s=1.0)
    d = rpt.to_dict()
    assert "boot_id" in d and "steps" in d and "total" in d


def test_boot_step_already_live(tmp_path: Path, monkeypatch):
    reg = _heartbeat_reg(tmp_path, fresh=True)
    # Inject a fake launchd_label so the boot path tries the live-check branch
    sub = reg.get("probe-only")
    sub.launchd_label = "com.rig.fake-label"
    # Monkeypatch _is_launchd_loaded to return True so we attempt kickstart not bootstrap.
    br = BootRunner(reg)
    monkeypatch.setattr(br, "_is_launchd_loaded", lambda label: True)
    # Since the heartbeat is fresh, the pre-probe should return ok and short-circuit.
    step = br.boot_one(sub)
    assert step.action == "already_live"
    assert step.final_ok


def test_boot_step_failed_when_kickstart_fails(tmp_path: Path, monkeypatch):
    reg = SubsystemRegistry()
    hb = tmp_path / "never-fresh.json"
    # Don't create the file — probe will report no_data
    reg.register(Subsystem(
        name="dead-daemon",
        description="dead",
        kind="daemon",
        launchd_label="com.rig.nonexistent",
        heartbeat_path=hb,
        heartbeat_max_age_s=60,
    ))
    br = BootRunner(reg)
    # Force the loaded check to return True so we go through the kickstart path
    monkeypatch.setattr(br, "_is_launchd_loaded", lambda label: True)
    # Force kickstart to fail
    monkeypatch.setattr(br, "_kickstart", lambda label: (False, "kickstart simulated fail"))
    step = br.boot_one(reg.get("dead-daemon"), max_wait_s=0.1)
    assert step.action == "failed"
    assert step.final_ok is False


def test_critical_probe_only_subsystem_aborts_boot(tmp_path: Path, monkeypatch):
    """Codex P1 fix: a critical subsystem with no launchd_label whose probe
    fails MUST abort the boot, not be silently skipped."""
    from rig_mesh import boot as boot_mod
    monkeypatch.setattr(boot_mod, "_BOOT_DIR", tmp_path)
    reg = SubsystemRegistry()
    # Critical library subsystem with a missing heartbeat — probe-only path,
    # no launchd_label, probe returns no_data.
    reg.register(Subsystem(
        name="critical-library",
        description="critical probe-only",
        kind="library",
        heartbeat_path=tmp_path / "never-written.json",
        heartbeat_max_age_s=60,
        critical=True,
    ))
    # Non-critical follower that should NEVER be reached.
    hb_follower = tmp_path / "follower.json"
    hb_follower.write_text("{}")
    reg.register(Subsystem(
        name="follower",
        description="must not boot",
        kind="daemon",
        heartbeat_path=hb_follower,
        depends_on=["critical-library"],
    ))
    rpt = BootRunner(reg).run(max_wait_s=0.1, stop_on_critical_fail=True)
    assert rpt.aborted_on == "critical-library", (
        f"expected boot to abort on critical probe-only fail, got aborted_on={rpt.aborted_on!r}"
    )
    assert rpt.failed >= 1
    # follower must not have run
    follower_steps = [s for s in rpt.steps if s.name == "follower"]
    assert follower_steps == [], "follower must not have been booted"


def test_non_critical_probe_only_skipped_when_failing(tmp_path: Path, monkeypatch):
    """Codex P1 fix: a NON-critical probe-only subsystem whose probe fails
    is still skipped (not failed) — only critical ones escalate."""
    from rig_mesh import boot as boot_mod
    monkeypatch.setattr(boot_mod, "_BOOT_DIR", tmp_path)
    reg = SubsystemRegistry()
    reg.register(Subsystem(
        name="optional-library",
        description="non-critical probe-only that's missing",
        kind="library",
        heartbeat_path=tmp_path / "never.json",
        critical=False,
    ))
    rpt = BootRunner(reg).run(max_wait_s=0.1)
    assert rpt.aborted_on is None
    assert rpt.skipped == 1


def test_boot_step_result_dataclass_fields():
    s = BootStepResult(
        name="x", action="already_live", final_status="live_ok",
        final_ok=True, wait_seconds=0.5,
    )
    assert s.name == "x"
    assert s.detail == ""
    assert s.final_ok
