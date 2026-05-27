"""Tests for rig_mesh.dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _has_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    """Dashboard endpoints honor RIG_TEST_BYPASS_AUTH; set it for HTTP tests."""
    monkeypatch.setenv("RIG_TEST_BYPASS_AUTH", "1")


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_app_exists():
    from rig_mesh.dashboard import app, PORT
    assert app is not None
    assert PORT == 8091


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_health_returns_200():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "rig-master"


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_root_returns_html():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "<table" in text.lower()
    assert "RIG Master" in text


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_api_status_returns_json():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "probes" in body
    assert "overall" in body


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_api_list_returns_subsystems():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    resp = client.get("/api/list")
    assert resp.status_code == 200
    body = resp.json()
    assert "subsystems" in body
    assert len(body["subsystems"]) > 0


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_api_probe_known_subsystem():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    # Use a known subsystem from the default registry.
    resp = client.get("/api/probe/proof-store")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verb"] == "probe"
    assert "status" in body


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_api_probe_unknown_returns_error():
    from fastapi.testclient import TestClient
    from rig_mesh.dashboard import app
    client = TestClient(app)
    resp = client.get("/api/probe/does-not-exist")
    assert resp.status_code == 200  # dispatch returns 200 with ok=False
    body = resp.json()
    assert body["ok"] is False


def _has_auth_module() -> bool:
    """Auth-rejection tests need the optional services.security module that the
    standalone rig-mesh CLI doesn't ship. Skip when unavailable."""
    try:
        from services.security import middleware  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
@pytest.mark.skipif(not _has_auth_module(),
                    reason="optional services.security auth module not installed")
def test_dashboard_boot_endpoint_rejects_without_auth(monkeypatch):
    """Codex P2 fix: /api/boot must NOT be reachable without auth or bypass."""
    # Force-disable the bypass for THIS test only
    monkeypatch.delenv("RIG_TEST_BYPASS_AUTH", raising=False)
    from fastapi.testclient import TestClient
    # Re-import so the route registration picks up the bypass-off state
    import importlib
    import rig_mesh.dashboard as dash_mod
    importlib.reload(dash_mod)
    client = TestClient(dash_mod.app)
    resp = client.post("/api/boot")
    # Without a token, the middleware should return 401
    assert resp.status_code == 401, (
        f"expected 401 unauthenticated, got {resp.status_code}; body={resp.text[:200]}"
    )
    # Re-enable bypass for the rest of the suite
    monkeypatch.setenv("RIG_TEST_BYPASS_AUTH", "1")
    importlib.reload(dash_mod)


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
@pytest.mark.skipif(not _has_auth_module(),
                    reason="optional services.security auth module not installed")
def test_dashboard_recover_endpoint_rejects_without_auth(monkeypatch):
    """Companion: /api/recover must also reject unauthenticated POSTs."""
    monkeypatch.delenv("RIG_TEST_BYPASS_AUTH", raising=False)
    from fastapi.testclient import TestClient
    import importlib
    import rig_mesh.dashboard as dash_mod
    importlib.reload(dash_mod)
    client = TestClient(dash_mod.app)
    resp = client.post("/api/recover", params={"dry_run": "true"})
    assert resp.status_code == 401
    monkeypatch.setenv("RIG_TEST_BYPASS_AUTH", "1")
    importlib.reload(dash_mod)


@pytest.mark.skipif(not _has_fastapi(), reason="fastapi not installed")
def test_dashboard_build_html_no_xss():
    """The HTML builder must escape probe data."""
    from rig_mesh.dashboard import _build_html
    snap = {
        "ts": "2026-01-01",
        "total": 1,
        "live_ok": 1,
        "down": 0,
        "stale": 0,
        "no_data": 0,
        "unknown": 0,
        "critical_failed": 0,
        "overall": "ok",
        "probes": [
            {"name": "<script>alert(1)</script>", "kind": "daemon",
             "status": "live_ok", "detail": "<b>x</b>"}
        ],
    }
    html = _build_html(snap)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
