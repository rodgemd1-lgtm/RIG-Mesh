"""subsystems.py — registry of every RIG subsystem RigMaster controls.

Each Subsystem declares:
  - name (canonical id, dash-separated)
  - description (human-readable, single line)
  - kind (daemon | service | library | gate)
  - launchd_label (None if not a launchd-managed daemon)
  - http_health (None or (port, path) for HTTP probe)
  - heartbeat_path (None or Path to a JSON heartbeat file)
  - depends_on (list of subsystem names that must be healthy first)
  - critical (True if its failure should abort boot)
  - probe(): runs a fresh health check, returns ProbeResult
  - kickstart(): attempts a single recovery step (launchctl kickstart -k or restart hint)

stdlib only. No live LLM. No FastAPI dep — those callers wrap us.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Path resolution — env-overridable so the CLI works against any RIG install
# ---------------------------------------------------------------------------
#
# REPO_ROOT resolution order:
#   1. $RIG_REPO_ROOT env var
#   2. Walk up from this file looking for a CLAUDE.md marker
#   3. Fall back to ~/Desktop/Startup-Intelligence-OS (canonical install)
#
# STATE_DIR resolution order:
#   1. $RIG_STATE_DIR env var
#   2. <REPO_ROOT>/.omx/state/rig-sovereign

def _repo_root() -> Path:
    env = os.environ.get("RIG_REPO_ROOT", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    candidate = Path(__file__).resolve().parent
    for _ in range(10):
        if (candidate / "CLAUDE.md").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return Path.home() / "Desktop" / "Startup-Intelligence-OS"


def _state_dir(root: Path) -> Path:
    env = os.environ.get("RIG_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return root / ".omx" / "state" / "rig-sovereign"


REPO_ROOT: Path = _repo_root()
STATE_DIR: Path = _state_dir(REPO_ROOT)
HOME_LIBRARY_LOGS = Path.home() / "Library" / "Logs"
HOME_NODE_WORKER_HB = HOME_LIBRARY_LOGS / "rig-node-worker-heartbeat.json"


# ---------------------------------------------------------------------------
# Probe + Subsystem datatypes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """Result of a single subsystem probe."""
    name: str
    ok: bool
    status: str         # one of: "live_ok" | "stale" | "no_data" | "down" | "unknown"
    detail: str         # human-readable diagnostic
    age_seconds: Optional[int] = None
    pid: Optional[int] = None
    probe_kind: str = "none"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Subsystem:
    """Declarative description of one RIG subsystem RigMaster controls."""
    name: str
    description: str
    kind: str                                    # daemon | service | library | gate
    launchd_label: Optional[str] = None
    http_health: Optional[tuple[int, str]] = None  # (port, path) e.g. (8090, "/health")
    heartbeat_path: Optional[Path] = None
    heartbeat_max_age_s: int = 300
    depends_on: list[str] = field(default_factory=list)
    critical: bool = False

    # ------------------------------------------------------------------
    def probe(self, timeout: float = 2.0) -> ProbeResult:
        """Run the most authoritative available probe for this subsystem."""
        # 1. HTTP health probe (most authoritative for services)
        if self.http_health is not None:
            port, path = self.http_health
            return self._probe_http(port, path, timeout)

        # 2. Heartbeat freshness probe (best for background daemons)
        if self.heartbeat_path is not None:
            return self._probe_heartbeat(self.heartbeat_path, self.heartbeat_max_age_s)

        # 3. Launchd state probe (last resort — only knows running/dead)
        if self.launchd_label is not None:
            return self._probe_launchd(self.launchd_label)

        # 4. No probe configured — declare unknown.
        return ProbeResult(
            name=self.name,
            ok=False,
            status="unknown",
            detail="no probe configured (http_health / heartbeat_path / launchd_label all None)",
            probe_kind="none",
        )

    # ------------------------------------------------------------------
    def _probe_http(self, port: int, path: str, timeout: float) -> ProbeResult:
        """Best-effort HTTP probe via stdlib socket — no requests dep."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: localhost:{port}\r\n"
                    f"User-Agent: rig-master/1.0\r\n"
                    f"Connection: close\r\n\r\n"
                )
                sock.sendall(req.encode("utf-8"))
                sock.settimeout(timeout)
                data = sock.recv(8192)
            head = data.split(b"\r\n", 1)[0] if data else b""
            status_code = 0
            try:
                status_code = int(head.split(b" ", 2)[1])
            except (ValueError, IndexError):
                pass
            ok = 200 <= status_code < 300 or status_code == 401  # 401 = auth gate live
            return ProbeResult(
                name=self.name,
                ok=ok,
                status="live_ok" if ok else "down",
                detail=f"HTTP {status_code} on :{port}{path}",
                probe_kind="http",
            )
        except (socket.error, ConnectionRefusedError, OSError) as e:
            return ProbeResult(
                name=self.name,
                ok=False,
                status="down",
                detail=f"connect refused :{port}{path} ({e.__class__.__name__})",
                probe_kind="http",
            )

    # ------------------------------------------------------------------
    def _probe_heartbeat(self, hb_path: Path, max_age_s: int) -> ProbeResult:
        """Heartbeat probe: file exists + mtime ≤ max_age_s."""
        if not hb_path.exists():
            return ProbeResult(
                name=self.name,
                ok=False,
                status="no_data",
                detail=f"heartbeat file missing: {hb_path}",
                probe_kind="heartbeat",
            )
        try:
            age = int(time.time() - hb_path.stat().st_mtime)
        except OSError as e:
            return ProbeResult(
                name=self.name,
                ok=False,
                status="no_data",
                detail=f"stat failed on {hb_path}: {e}",
                probe_kind="heartbeat",
            )
        if age > max_age_s:
            return ProbeResult(
                name=self.name,
                ok=False,
                status="stale",
                detail=f"heartbeat age {age}s exceeds max {max_age_s}s",
                age_seconds=age,
                probe_kind="heartbeat",
            )
        return ProbeResult(
            name=self.name,
            ok=True,
            status="live_ok",
            detail=f"heartbeat fresh ({age}s old)",
            age_seconds=age,
            probe_kind="heartbeat",
        )

    # ------------------------------------------------------------------
    def _probe_launchd(self, label: str) -> ProbeResult:
        """Launchd probe via `launchctl list <label>`."""
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError) as e:
            return ProbeResult(
                name=self.name,
                ok=False,
                status="unknown",
                detail=f"launchctl call failed: {e}",
                probe_kind="launchd",
            )
        if result.returncode != 0:
            return ProbeResult(
                name=self.name,
                ok=False,
                status="down",
                detail=f"launchctl list {label} -> rc={result.returncode}",
                probe_kind="launchd",
            )
        # Output is a plist-like dict. Parse PID + LastExitStatus.
        pid = None
        last_exit = None
        for line in result.stdout.splitlines():
            line = line.strip().rstrip(";")
            if line.startswith('"PID" ='):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith('"LastExitStatus" ='):
                try:
                    last_exit = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
        if pid:
            return ProbeResult(
                name=self.name,
                ok=True,
                status="live_ok",
                detail=f"launchd pid={pid} last_exit={last_exit}",
                pid=pid,
                probe_kind="launchd",
            )
        # No PID + last_exit = 0 is OK (one-shot daemon)
        if last_exit == 0:
            return ProbeResult(
                name=self.name,
                ok=True,
                status="live_ok",
                detail=f"launchd one-shot, last_exit=0",
                probe_kind="launchd",
            )
        return ProbeResult(
            name=self.name,
            ok=False,
            status="down",
            detail=f"no pid, last_exit={last_exit}",
            probe_kind="launchd",
        )

    # ------------------------------------------------------------------
    def kickstart(self) -> dict:
        """Attempt single recovery step. Returns dict with action + result."""
        if self.launchd_label is None:
            return {
                "name": self.name,
                "action": "none",
                "ok": False,
                "detail": "no launchd_label — kickstart not supported",
            }
        try:
            uid = os.getuid()
        except AttributeError:
            uid = 501  # macOS default
        cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{self.launchd_label}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return {
                "name": self.name,
                "action": "launchctl_kickstart",
                "ok": result.returncode == 0,
                "stdout": (result.stdout or "").strip()[:200],
                "stderr": (result.stderr or "").strip()[:200],
            }
        except (subprocess.SubprocessError, OSError) as e:
            return {
                "name": self.name,
                "action": "launchctl_kickstart",
                "ok": False,
                "detail": f"kickstart raised: {e}",
            }

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "launchd_label": self.launchd_label,
            "http_health": list(self.http_health) if self.http_health else None,
            "heartbeat_path": str(self.heartbeat_path) if self.heartbeat_path else None,
            "heartbeat_max_age_s": self.heartbeat_max_age_s,
            "depends_on": list(self.depends_on),
            "critical": self.critical,
        }


# ---------------------------------------------------------------------------
# SubsystemRegistry
# ---------------------------------------------------------------------------

class SubsystemRegistry:
    """In-memory registry of all subsystems RigMaster knows about."""

    def __init__(self) -> None:
        self._by_name: dict[str, Subsystem] = {}

    def register(self, sub: Subsystem) -> None:
        if sub.name in self._by_name:
            raise ValueError(f"subsystem already registered: {sub.name}")
        self._by_name[sub.name] = sub

    def get(self, name: str) -> Subsystem:
        if name not in self._by_name:
            raise KeyError(f"unknown subsystem: {name}")
        return self._by_name[name]

    def all(self) -> list[Subsystem]:
        return list(self._by_name.values())

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    # ------------------------------------------------------------------
    def topological_order(self) -> list[Subsystem]:
        """Return subsystems sorted so dependencies come first.
        Raises ValueError on cycle."""
        visited: set[str] = set()
        order: list[Subsystem] = []
        temp: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in temp:
                raise ValueError(f"subsystem dependency cycle through: {name}")
            sub = self._by_name.get(name)
            if sub is None:
                return  # tolerate unknown deps (they were filtered out elsewhere)
            temp.add(name)
            for dep in sub.depends_on:
                visit(dep)
            temp.discard(name)
            visited.add(name)
            order.append(sub)

        for n in self._by_name.keys():
            visit(n)
        return order


# ---------------------------------------------------------------------------
# Canonical default registry
# ---------------------------------------------------------------------------

def default_registry() -> SubsystemRegistry:
    """Build the canonical RIG Sovereign subsystem registry."""
    reg = SubsystemRegistry()

    # Library layer — proof_store + sqlite stores are "always available" if the
    # files exist. They have no daemon to probe; the gate is file presence.
    reg.register(Subsystem(
        name="proof-store",
        description="Immutable proof store sqlite — the system's source of truth.",
        kind="library",
        heartbeat_path=REPO_ROOT / "services" / "memory" / "proof_store.sqlite",
        heartbeat_max_age_s=10 * 365 * 24 * 3600,  # essentially forever
        critical=True,
    ))

    # Daemons — managed by launchd, identified by heartbeat where available.
    reg.register(Subsystem(
        name="idle-queue",
        description="24/7 operator-queue runner; processes goals every 90s.",
        kind="daemon",
        launchd_label="com.rig.idle-queue",
        heartbeat_path=STATE_DIR / "idle-queue-heartbeat.json",
        heartbeat_max_age_s=300,
        depends_on=["proof-store"],
        critical=True,
    ))

    reg.register(Subsystem(
        name="cell-watcher",
        description="Lattice cell-watcher daemon.",
        kind="daemon",
        launchd_label="com.rig.cell-watcher",
        heartbeat_max_age_s=600,
    ))

    reg.register(Subsystem(
        name="healer-daemon",
        description="Auto-healer for stuck/failed services.",
        kind="daemon",
        launchd_label="com.rig.healer",
        heartbeat_path=STATE_DIR / "healer-heartbeat.json",
        heartbeat_max_age_s=300,
    ))

    reg.register(Subsystem(
        name="rig-247-supervisor",
        description="Top-level 6-check supervisor; loops every 120s.",
        kind="daemon",
        launchd_label="com.rig.rig-247-supervisor",
        heartbeat_path=STATE_DIR / "rig-247-supervisor-heartbeat.json",
        heartbeat_max_age_s=300,
        depends_on=["proof-store", "idle-queue"],
    ))

    reg.register(Subsystem(
        name="monitor-runner",
        description="Continuous brier + capability monitor; ticks every 60s.",
        kind="daemon",
        launchd_label="com.rig.monitor-runner",
        heartbeat_path=STATE_DIR / "monitor-runner-heartbeat.json",
        heartbeat_max_age_s=180,
        depends_on=["proof-store"],
    ))

    reg.register(Subsystem(
        name="meta-monitor",
        description="Watches monitor-runner + other daemons; opens incidents.",
        kind="daemon",
        launchd_label="com.rig.meta-monitor",
        heartbeat_path=STATE_DIR / "meta-monitor-heartbeat.json",
        heartbeat_max_age_s=300,
        depends_on=["monitor-runner"],
    ))

    reg.register(Subsystem(
        name="cockpit-v5",
        description="Unified operator dashboard on :8090.",
        kind="service",
        launchd_label="com.rig.cockpit-v5",
        http_health=(8090, "/health"),
        depends_on=["proof-store"],
    ))

    reg.register(Subsystem(
        name="deviation-dashboard",
        description="Deviation ±30σ dashboard on :8078.",
        kind="service",
        http_health=(8078, "/health"),
    ))

    reg.register(Subsystem(
        name="studio-dashboard",
        description="Studio index dashboard on :8077.",
        kind="service",
        http_health=(8077, "/health"),
    ))

    reg.register(Subsystem(
        name="progress-board",
        description="Progress board dashboard on :8083.",
        kind="service",
        http_health=(8083, "/health"),
    ))

    reg.register(Subsystem(
        name="avis-nps",
        description="AVIS NPS MVP on :8079.",
        kind="service",
        http_health=(8079, "/health"),
    ))

    reg.register(Subsystem(
        name="litellm-mesh-router",
        description="LiteLLM mesh router on :4000.",
        kind="service",
        http_health=(4000, "/health"),
    ))

    reg.register(Subsystem(
        name="backup-runner",
        description="Sqlite backup runner; 6h schedule.",
        kind="daemon",
        launchd_label="com.rig.backup-runner",
    ))

    reg.register(Subsystem(
        name="retention-runner",
        description="Retention policy enforcer; daily archive.",
        kind="daemon",
        launchd_label="com.rig.retention-runner",
    ))

    reg.register(Subsystem(
        name="escalation-runner",
        description="Notification + escalation dispatcher.",
        kind="daemon",
        launchd_label="com.rig.escalation-runner",
    ))

    reg.register(Subsystem(
        name="node-worker",
        description="Local node-worker heartbeat (rig-48gb).",
        kind="daemon",
        launchd_label="com.rig.node-worker",
        heartbeat_path=HOME_NODE_WORKER_HB,
        heartbeat_max_age_s=300,
    ))

    return reg


__all__ = [
    "Subsystem",
    "SubsystemRegistry",
    "ProbeResult",
    "default_registry",
    "REPO_ROOT",
    "STATE_DIR",
]
