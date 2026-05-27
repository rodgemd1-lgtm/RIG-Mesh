"""Tests for rig_mesh.subsystems."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rig_mesh.subsystems import (
    ProbeResult,
    Subsystem,
    SubsystemRegistry,
    default_registry,
)


# ---------------------------------------------------------------------------
# ProbeResult dataclass

def test_probe_result_serializes_to_dict():
    r = ProbeResult(name="x", ok=True, status="live_ok", detail="hi")
    d = r.to_dict()
    assert d["name"] == "x"
    assert d["ok"] is True
    assert d["status"] == "live_ok"
    assert "age_seconds" in d


# ---------------------------------------------------------------------------
# Subsystem heartbeat probe

def test_heartbeat_probe_fresh(tmp_path: Path):
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": "now"}))
    sub = Subsystem(
        name="t1",
        description="t",
        kind="daemon",
        heartbeat_path=hb,
        heartbeat_max_age_s=300,
    )
    r = sub.probe()
    assert r.ok
    assert r.status == "live_ok"
    assert r.probe_kind == "heartbeat"


def test_heartbeat_probe_stale(tmp_path: Path):
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": "old"}))
    # Force mtime to 1 hour ago
    old_ts = time.time() - 3600
    import os
    os.utime(hb, (old_ts, old_ts))
    sub = Subsystem(
        name="t2",
        description="t",
        kind="daemon",
        heartbeat_path=hb,
        heartbeat_max_age_s=300,
    )
    r = sub.probe()
    assert not r.ok
    assert r.status == "stale"
    assert r.age_seconds is not None and r.age_seconds >= 3500


def test_heartbeat_probe_no_data(tmp_path: Path):
    sub = Subsystem(
        name="t3",
        description="t",
        kind="daemon",
        heartbeat_path=tmp_path / "missing.json",
        heartbeat_max_age_s=300,
    )
    r = sub.probe()
    assert not r.ok
    assert r.status == "no_data"


# ---------------------------------------------------------------------------
# HTTP probe (connection refused on a dead port)

def test_http_probe_refused():
    # Use a port that is extremely unlikely to be open.
    sub = Subsystem(
        name="t-http",
        description="t",
        kind="service",
        http_health=(59321, "/health"),
    )
    r = sub.probe(timeout=0.5)
    assert not r.ok
    assert r.status == "down"
    assert r.probe_kind == "http"


# ---------------------------------------------------------------------------
# No probe configured

def test_no_probe_configured():
    sub = Subsystem(name="t-none", description="t", kind="library")
    r = sub.probe()
    assert not r.ok
    assert r.status == "unknown"
    assert r.probe_kind == "none"


# ---------------------------------------------------------------------------
# Registry behavior

def test_registry_register_and_get():
    reg = SubsystemRegistry()
    sub = Subsystem(name="a", description="d", kind="daemon")
    reg.register(sub)
    assert reg.get("a") is sub
    assert reg.names() == ["a"]
    assert reg.all() == [sub]


def test_registry_duplicate_raises():
    reg = SubsystemRegistry()
    reg.register(Subsystem(name="a", description="d", kind="daemon"))
    with pytest.raises(ValueError):
        reg.register(Subsystem(name="a", description="d2", kind="daemon"))


def test_registry_unknown_raises():
    reg = SubsystemRegistry()
    with pytest.raises(KeyError):
        reg.get("missing")


# ---------------------------------------------------------------------------
# Topological sort

def test_topological_order_resolves_deps():
    reg = SubsystemRegistry()
    reg.register(Subsystem(name="c", description="c", kind="daemon", depends_on=["b"]))
    reg.register(Subsystem(name="b", description="b", kind="daemon", depends_on=["a"]))
    reg.register(Subsystem(name="a", description="a", kind="daemon"))
    order = reg.topological_order()
    names = [s.name for s in order]
    assert names.index("a") < names.index("b") < names.index("c")


def test_topological_order_cycle_raises():
    reg = SubsystemRegistry()
    reg.register(Subsystem(name="a", description="a", kind="daemon", depends_on=["b"]))
    reg.register(Subsystem(name="b", description="b", kind="daemon", depends_on=["a"]))
    with pytest.raises(ValueError):
        reg.topological_order()


# ---------------------------------------------------------------------------
# Default registry

def test_default_registry_has_critical_subsystems():
    reg = default_registry()
    names = reg.names()
    assert "proof-store" in names
    assert "idle-queue" in names
    assert "cockpit-v5" in names
    # Topological order works on the live registry.
    order = reg.topological_order()
    name_order = [s.name for s in order]
    # idle-queue depends on proof-store
    assert name_order.index("proof-store") < name_order.index("idle-queue")


def test_default_registry_at_least_15_subsystems():
    reg = default_registry()
    assert len(reg.names()) >= 15
