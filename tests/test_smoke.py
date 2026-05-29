"""Tests for the rig-mesh smoke CLI verb."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rig_mesh.cli import main as cli_main
from rig_mesh.master import RigMaster
from rig_mesh.router import CommandRouter
from rig_mesh.subsystems import Subsystem, SubsystemRegistry


def _build_router(tmp_path: Path) -> CommandRouter:
    reg = SubsystemRegistry()
    hb = tmp_path / "h.json"
    hb.write_text("{}")
    reg.register(Subsystem(name="x", description="x", kind="daemon", heartbeat_path=hb))
    return CommandRouter(RigMaster(reg))


# ---------------------------------------------------------------------------
# Router-level smoke tests

def test_smoke_is_a_known_verb(tmp_path: Path):
    r = _build_router(tmp_path)
    assert "smoke" in r.known_verbs()


def test_smoke_returns_ok_true(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("smoke")
    assert out["ok"] is True
    assert out["verb"] == "smoke"


def test_smoke_result_is_pass(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("smoke")
    assert out["result"] == "PASS"


def test_smoke_checks_version_ok(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("smoke")
    assert out["checks"]["version_ok"] is True


def test_smoke_checks_registry_populated(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("smoke")
    assert out["checks"]["registry_populated"] is True
    assert out["checks"]["subsystem_count"] == 1


def test_smoke_empty_registry_fails(tmp_path: Path):
    """A router backed by an empty registry should report smoke FAIL."""
    reg = SubsystemRegistry()
    r = CommandRouter(RigMaster(reg))
    out = r.dispatch("smoke")
    assert out["ok"] is False
    assert out["result"] == "FAIL"
    assert out["checks"]["registry_populated"] is False


def test_smoke_includes_version_string(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("smoke")
    assert out["rig_master"]  # non-empty version string


# ---------------------------------------------------------------------------
# CLI-level smoke tests

def test_cli_smoke_exits_0(capsys):
    rc = cli_main(["smoke"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["result"] == "PASS"


def test_cli_smoke_json_output_valid(capsys):
    """Output must be parseable JSON with the expected keys."""
    cli_main(["smoke"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    for key in ("ok", "verb", "rig_master", "checks", "result"):
        assert key in payload, f"missing key: {key}"
