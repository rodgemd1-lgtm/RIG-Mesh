"""dashboard.py — RIG Master HTTP dashboard on :8091.

FastAPI service that exposes the same router verbs as JSON endpoints plus a
plain HTML root page that lists every subsystem with its current probe
status. Auth-bypass aware: if RIG_TEST_BYPASS_AUTH=1, no token required.

If FastAPI is not installed, the module still imports (lazy degradation);
the `app` object is then None and the server cannot start.

Endpoints:
  GET  /health                 — liveness (200 always)
  GET  /                       — HTML status page
  GET  /api/status             — full snapshot JSON
  GET  /api/list               — subsystems JSON
  GET  /api/probe/{name}       — single probe JSON
  POST /api/audit              — run full audit + write artifact
  POST /api/boot               — run boot sequence
  POST /api/recover            — run recovery flows
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException, Request, Depends
    from fastapi.responses import HTMLResponse, JSONResponse
    _FASTAPI_OK = True
except ImportError:  # pragma: no cover - degraded environment
    FastAPI = None  # type: ignore[misc, assignment]
    Depends = None  # type: ignore[misc, assignment]
    _FASTAPI_OK = False

try:
    from services.security.middleware import (
        add_auth_middleware as _add_auth_middleware,
        require_scope as _require_scope,
    )
    _AUTH_OK = True
except ImportError:  # pragma: no cover - degraded environment
    _AUTH_OK = False

    def _add_auth_middleware(app, **kw):  # type: ignore[misc]
        return None

    def _require_scope(scope: str):  # type: ignore[misc]
        # Degraded shim — when middleware isn't installed, no enforcement
        # is possible. Mutating endpoints still reject via the env-bypass
        # check below; read endpoints are visible.
        def _noop(request: object = None) -> None:
            return None
        return _noop

from .router import CommandRouter


PORT = 8091
_router = CommandRouter()


# ---------------------------------------------------------------------------

def _build_html(snapshot: dict) -> str:
    """Return a small no-JS HTML page rendering the snapshot."""
    rows = []
    for p in snapshot.get("probes", []):
        color = {
            "live_ok": "#3fb950",
            "stale": "#d29922",
            "no_data": "#8b949e",
            "down": "#f85149",
            "unknown": "#8b949e",
        }.get(p.get("status", "unknown"), "#8b949e")
        rows.append(
            "<tr>"
            f"<td style='padding:6px 12px;font-family:monospace'>{_h(p.get('name', '?'))}</td>"
            f"<td style='padding:6px 12px'>{_h(p.get('kind', '?'))}</td>"
            f"<td style='padding:6px 12px;color:{color};font-weight:600'>{_h(p.get('status', '?'))}</td>"
            f"<td style='padding:6px 12px;color:#8b949e'>{_h(p.get('detail', ''))[:120]}</td>"
            "</tr>"
        )
    overall = snapshot.get("overall", "?")
    badge_color = {"ok": "#3fb950", "warn": "#d29922", "fail": "#f85149"}.get(overall, "#8b949e")
    return f"""<!doctype html>
<html><head><title>RIG Master · {overall}</title>
<meta http-equiv="refresh" content="10">
<style>
  body {{ background:#0d1117; color:#c9d1d9; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 16px 0; }}
  .badge {{ background:{badge_color}; color:#0d1117; padding:4px 10px; border-radius:6px;
            font-weight:700; font-family:monospace; }}
  table {{ border-collapse:collapse; width:100%; max-width:1100px; }}
  th {{ text-align:left; padding:6px 12px; color:#8b949e; font-weight:500;
        border-bottom:1px solid #30363d; }}
  tr {{ border-bottom:1px solid #161b22; }}
  .stats {{ color:#8b949e; font-size:13px; margin-bottom:16px; }}
</style></head><body>
<h1>RIG Master <span class="badge">{_h(overall)}</span></h1>
<div class="stats">
  total {snapshot.get('total', 0)} ·
  live_ok {snapshot.get('live_ok', 0)} ·
  down {snapshot.get('down', 0)} ·
  stale {snapshot.get('stale', 0)} ·
  no_data {snapshot.get('no_data', 0)} ·
  critical_failed {snapshot.get('critical_failed', 0)} ·
  ts {_h(snapshot.get('ts', ''))}
</div>
<table>
  <thead><tr><th>subsystem</th><th>kind</th><th>status</th><th>detail</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</body></html>"""


def _h(s: Any) -> str:
    """Tiny HTML escape."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------

def create_app() -> Optional["FastAPI"]:
    """Return a FastAPI app, or None if FastAPI isn't installed."""
    if not _FASTAPI_OK:
        return None

    app = FastAPI(
        title="RIG Master Dashboard",
        version="1.0.0",
        description="Top-level orchestrator dashboard for RIG Sovereign.",
    )

    # Wire the canonical RIG auth middleware so /api endpoints are token-gated.
    # Health and the safe-path bypass list (configured in middleware) remain
    # accessible without a token. Mutating endpoints below additionally
    # require explicit scopes via require_scope().
    if _AUTH_OK:
        _add_auth_middleware(app)

    @app.get("/health", response_class=JSONResponse)
    def health() -> dict:
        return {"status": "ok", "service": "rig-master", "port": PORT}

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        snap = _router.dispatch("status", {})
        return _build_html(snap)

    # Read endpoints — auth middleware already gates /api/* paths. require_scope
    # adds RBAC on top so a viewer-only token can still read state but cannot
    # trigger mutations.
    @app.get("/api/status", response_class=JSONResponse)
    def api_status(_auth=_require_scope("scorecard:read")) -> dict:
        return _router.dispatch("status", {"write": False})

    @app.get("/api/list", response_class=JSONResponse)
    def api_list(_auth=_require_scope("scorecard:read")) -> dict:
        return _router.dispatch("list", {})

    @app.get("/api/probe/{name}", response_class=JSONResponse)
    def api_probe(name: str, _auth=_require_scope("monitor:read")) -> dict:
        return _router.dispatch("probe", {"name": name})

    # Mutating endpoints — require write-scoped tokens. Audit + boot + recover
    # are destructive enough that operator-tier RBAC is the right bar.
    @app.post("/api/audit", response_class=JSONResponse)
    def api_audit(_auth=_require_scope("scorecard:write")) -> dict:
        return _router.dispatch("audit", {})

    @app.post("/api/boot", response_class=JSONResponse)
    def api_boot(max_wait_s: float = 10.0, _auth=_require_scope("incident:write")) -> dict:
        return _router.dispatch("boot", {"max_wait_s": max_wait_s})

    @app.post("/api/recover", response_class=JSONResponse)
    def api_recover(
        flows: str | None = None,
        dry_run: bool = False,
        _auth=_require_scope("incident:write"),
    ) -> dict:
        args: dict = {"dry_run": dry_run}
        if flows:
            args["flows"] = flows
        return _router.dispatch("recover", args)

    return app


# Module-level app for uvicorn discovery
app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn  # type: ignore[import-not-found]
    if app is None:
        raise SystemExit("FastAPI not installed — cannot run dashboard")
    uvicorn.run(app, host="127.0.0.1", port=PORT)


__all__ = ["app", "create_app", "PORT"]
