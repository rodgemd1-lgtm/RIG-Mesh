"""Tests for rig_mesh.router and rig_mesh.cli."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
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


def test_router_known_verbs(tmp_path: Path):
    r = _build_router(tmp_path)
    verbs = r.known_verbs()
    assert "status" in verbs
    assert "audit" in verbs
    assert "boot" in verbs
    assert "version" in verbs


def test_router_dispatch_unknown_verb(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("does-not-exist")
    assert out["ok"] is False
    assert "unknown verb" in out["error"]


def test_router_status(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("status")
    assert out["ok"] is True
    assert out["verb"] == "status"
    assert "probes" in out and len(out["probes"]) == 1


def test_router_list(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("list")
    assert out["ok"] is True
    assert out["subsystems"][0]["name"] == "x"


def test_router_probe_known(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("probe", {"name": "x"})
    assert out["ok"] is True
    assert out["status"] == "live_ok"


def test_router_probe_unknown(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("probe", {"name": "nope"})
    assert out["ok"] is False
    assert "unknown subsystem" in out["error"]


def test_router_version(tmp_path: Path):
    r = _build_router(tmp_path)
    out = r.dispatch("version")
    assert out["ok"] is True
    assert "rig_master" in out


def test_router_handles_handler_exception(tmp_path: Path, monkeypatch):
    """If a handler raises, dispatch returns ok=False rather than propagating."""
    r = _build_router(tmp_path)
    def boom(args):  # noqa: ANN001
        raise RuntimeError("boom")
    monkeypatch.setattr(r, "_verb_status", boom)
    out = r.dispatch("status")
    assert out["ok"] is False
    assert "boom" in out["error"]


# ---------------------------------------------------------------------------
# CLI smoke

def test_cli_version_exits_0(capsys):
    rc = cli_main(["version"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["rig_master"]


def test_cli_unknown_verb_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as e:
        cli_main(["nope"])
    assert e.value.code != 0


def test_cli_probe_with_missing_name_returns_nonzero(capsys):
    """`probe` without --name should exit nonzero through argparse."""
    with pytest.raises(SystemExit) as e:
        cli_main(["probe"])
    assert e.value.code != 0
