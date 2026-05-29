"""mcp_server.py — MCP (Model Context Protocol) server surface for RIG Mesh.

Exposes rig-mesh functionality as MCP tools, resources, and prompts that
agent clients (Claude Desktop, Copilot, custom agents) can call.

Install the optional MCP extra to run the server:

    pip install -e ".[mcp]"
    python -m rig_mesh.mcp_server         # stdio transport (default)
    python -m rig_mesh.mcp_server --sse   # SSE transport on :8092

The server degrades gracefully: if the ``mcp`` package is not installed the
module is still importable — only ``serve()`` raises ImportError with a
clear message.

Tools
-----
probe_subsystem(name) -> ProbeResult dict
    Run a live health probe for a single named subsystem.

get_status() -> status snapshot dict
    Probe every registered subsystem and return the aggregate verdict.

list_subsystems() -> list[subsystem dicts]
    Return the full subsystem registry (no network I/O).

run_audit() -> audit report dict
    Run a full audit cycle, write the JSON artifact, and return the report.

smoke() -> smoke result dict
    Run the deterministic local smoke test (version + registry check).

Resources
---------
rig://registry
    The full subsystem registry as JSON. Updated on each read.

rig://audit/latest
    The latest audit JSON artifact, if it exists.

Prompts
-------
health_check_prompt
    Asks an agent to interpret a status snapshot and highlight critical issues.
"""
from __future__ import annotations

import json
from typing import Any

from .router import CommandRouter

# ---------------------------------------------------------------------------
# Tool implementations — pure functions wrapping CommandRouter
# These are callable without the ``mcp`` package installed.
# ---------------------------------------------------------------------------


def tool_probe_subsystem(name: str) -> dict:
    """Probe a single subsystem by name. Returns ProbeResult dict."""
    router = CommandRouter()
    return router.dispatch("probe", {"name": name})


def tool_get_status() -> dict:
    """Probe every subsystem. Returns status snapshot dict."""
    router = CommandRouter()
    return router.dispatch("status")


def tool_list_subsystems() -> dict:
    """Return the subsystem registry (no I/O). Returns list of subsystem dicts."""
    router = CommandRouter()
    return router.dispatch("list")


def tool_run_audit() -> dict:
    """Run a full audit cycle. Returns audit report dict."""
    router = CommandRouter()
    return router.dispatch("audit")


def tool_smoke() -> dict:
    """Deterministic local smoke test. Returns smoke result dict."""
    router = CommandRouter()
    return router.dispatch("smoke")


# ---------------------------------------------------------------------------
# Resource implementations
# ---------------------------------------------------------------------------

def resource_registry() -> str:
    """Return the full subsystem registry as a JSON string."""
    result = tool_list_subsystems()
    return json.dumps(result, indent=2, sort_keys=True)


def resource_audit_latest() -> str:
    """Return the latest audit artifact JSON, or a not-found message."""
    from .subsystems import STATE_DIR
    audit_dir = STATE_DIR / "rig-master"
    latest = audit_dir / "audit-latest.json"
    if latest.exists():
        return latest.read_text(encoding="utf-8")
    return json.dumps({"error": "no audit artifact found", "path": str(latest)})


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

HEALTH_CHECK_PROMPT = """\
You are a RIG Sovereign system operator reviewing a live status snapshot.
The snapshot is:

{snapshot}

Instructions:
1. Identify every subsystem with status != "live_ok".
2. For each unhealthy subsystem, classify as: stale | no_data | down | unknown.
3. Flag any subsystem with critical=true that is not live_ok — this is P0.
4. Recommend the single most impactful recovery action using rig-mesh CLI verbs.
5. Output your assessment as a structured JSON object with keys:
   critical_alerts, warnings, recommended_action, overall_verdict.
Do not speculate beyond what the snapshot data shows.
"""


def prompt_health_check(snapshot: dict | None = None) -> str:
    """Return the health-check prompt, optionally with a live snapshot injected."""
    if snapshot is None:
        snapshot = tool_get_status()
    return HEALTH_CHECK_PROMPT.format(snapshot=json.dumps(snapshot, indent=2))


# ---------------------------------------------------------------------------
# MCP server — requires the ``mcp`` optional extra
# ---------------------------------------------------------------------------

def build_mcp_server():  # type: ignore[return]
    """Build and return the FastMCP server instance.

    Raises ImportError with a helpful message if ``mcp`` is not installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "The 'mcp' package is required to run the MCP server.\n"
            "Install it with:  pip install -e '.[mcp]'"
        ) from exc

    mcp = FastMCP(
        name="rig-mesh",
        version="1.0.0",
        description=(
            "RIG Mesh control-plane MCP server. "
            "Exposes subsystem health, audit, and recovery tools to agent clients."
        ),
    )

    # ---- Tools ----

    @mcp.tool()
    def probe_subsystem(name: str) -> dict:
        """Probe a single RIG subsystem by name and return its health status."""
        return tool_probe_subsystem(name)

    @mcp.tool()
    def get_status() -> dict:
        """Probe every registered subsystem and return the aggregate health snapshot."""
        return tool_get_status()

    @mcp.tool()
    def list_subsystems() -> dict:
        """List all registered subsystems with their declared probes and dependencies."""
        return tool_list_subsystems()

    @mcp.tool()
    def run_audit() -> dict:
        """Run a full audit cycle, write a JSON artifact, and return the audit report."""
        return tool_run_audit()

    @mcp.tool()
    def smoke() -> dict:
        """Run the deterministic local smoke test (no live network calls)."""
        return tool_smoke()

    # ---- Resources ----

    @mcp.resource("rig://registry")
    def registry_resource() -> str:
        """The full RIG Mesh subsystem registry as JSON."""
        return resource_registry()

    @mcp.resource("rig://audit/latest")
    def audit_latest_resource() -> str:
        """The latest RIG Mesh audit artifact as JSON."""
        return resource_audit_latest()

    # ---- Prompts ----

    @mcp.prompt()
    def health_check_prompt() -> str:
        """Prompt an agent to interpret the current status snapshot and advise on recovery."""
        return prompt_health_check()

    return mcp


def serve(use_sse: bool = False, sse_port: int = 8092) -> None:
    """Start the MCP server.

    Args:
        use_sse: If True, serve via HTTP+SSE on ``sse_port`` instead of stdio.
        sse_port: Port for SSE transport (default 8092).
    """
    mcp_server = build_mcp_server()
    if use_sse:
        mcp_server.run(transport="sse", port=sse_port)
    else:
        mcp_server.run(transport="stdio")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="RIG Mesh MCP server")
    p.add_argument(
        "--sse",
        action="store_true",
        help="Use SSE transport instead of stdio",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8092,
        help="Port for SSE transport (default: 8092)",
    )
    ns = p.parse_args()
    serve(use_sse=ns.sse, sse_port=ns.port)


__all__ = [
    "tool_probe_subsystem",
    "tool_get_status",
    "tool_list_subsystems",
    "tool_run_audit",
    "tool_smoke",
    "resource_registry",
    "resource_audit_latest",
    "prompt_health_check",
    "build_mcp_server",
    "serve",
    "HEALTH_CHECK_PROMPT",
]
