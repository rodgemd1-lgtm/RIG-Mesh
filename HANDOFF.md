# HANDOFF — RIG Mesh

Operational handoff for the standalone `rig-mesh` CLI. Read this on day one
of operating the system, or when picking the project back up after a break.

---

## What it is in one paragraph

`rig-mesh` is a Python CLI installed via `pip install git+https://github.com/rodgemd1-lgtm/RIG-Mesh.git`.
After install, the `rig-mesh` command is on your `$PATH`. It treats every
piece of your AI infrastructure (model routers, queue runners, dashboards,
healers, backup daemons) as a declarative **Subsystem** and gives you six
verbs to see, start, recover, and prove their state. It does not run models
itself — it is the control plane that sits above them.

---

## Where the code lives

- **GitHub**: https://github.com/rodgemd1-lgtm/RIG-Mesh
- **Default branch**: `main`
- **License**: MIT
- **Origin**: extracted from `services/rig_master/` in
  `rodgemd1-lgtm/Startup-Intelligence-OS` (PR #44 merged 2026-05-19). The
  standalone repo drops the `services.rig_master.*` namespace and
  re-namespaces to `rig_mesh.*`.

### Layout

```
RIG-Mesh/
├── src/rig_mesh/
│   ├── __init__.py        # lazy re-exports for the public API
│   ├── subsystems.py      # Subsystem dataclass + 17 default registry
│   ├── audit.py           # MasterAuditor + JSON artifact + proof binding
│   ├── boot.py            # BootRunner with topological cold-boot
│   ├── recovery.py        # 3 recovery flows
│   ├── master.py          # RigMaster facade
│   ├── router.py          # verb dispatcher (shared by CLI + dashboard)
│   ├── cli.py             # argparse entry point — bound to `rig-mesh`
│   └── dashboard.py       # optional FastAPI :8091
├── tests/                 # 63 passing + 2 auto-skipping
├── pyproject.toml         # entry_points: rig-mesh = rig_mesh.cli:main
├── README.md              # user-facing docs
├── LICENSE                # MIT
├── HANDOFF.md             # this file
└── .github/workflows/ci.yml   # pytest py3.10/3.11/3.12 + compile lint
```

---

## What's wired up out of the box

### 17 canonical Subsystems

The default registry knows about the canonical RIG Sovereign stack. Edit
`default_registry()` in `src/rig_mesh/subsystems.py` to add your own.

| Layer | Subsystem | Probe |
|---|---|---|
| Storage | `proof-store` (critical) | heartbeat on proof_store.sqlite |
| Queue | `idle-queue` (critical) | heartbeat .omx/state/rig-sovereign/idle-queue-heartbeat.json |
| Watchdog | `cell-watcher`, `healer-daemon`, `rig-247-supervisor` | launchd + heartbeat |
| Observability | `monitor-runner`, `meta-monitor` | heartbeat |
| Dashboards | `cockpit-v5` :8090, `deviation-dashboard` :8078, `studio-dashboard` :8077, `progress-board` :8083, `avis-nps` :8079 | HTTP /health |
| Model layer | `litellm-mesh-router` :4000 | HTTP /health |
| Hygiene | `backup-runner`, `retention-runner`, `escalation-runner` | launchd |
| Compute | `node-worker` | heartbeat |

### Three probe kinds, picked automatically

1. **HTTP** — `Subsystem(http_health=(port, path))`. Raw socket, no `requests`.
2. **Heartbeat** — `Subsystem(heartbeat_path=Path(...))`. File mtime vs `heartbeat_max_age_s`.
3. **launchd** — `Subsystem(launchd_label="com.x.y")`. Parses `launchctl list` output.

Authority order if more than one is set: **HTTP > heartbeat > launchd.**
A 401 from HTTP counts as live (auth gate working).

### Topological boot order

`SubsystemRegistry.topological_order()` does a depth-first sort over the
`depends_on` graph. The order honors: `proof-store` → `idle-queue` →
`rig-247-supervisor`; `monitor-runner` → `meta-monitor`; `cockpit-v5`
depends on `proof-store`.

`BootRunner.run()` walks this order. If a **critical** Subsystem fails its
probe (HTTP/heartbeat/launchd), boot aborts with `aborted_on=<name>` so
dependents don't start against an unmet dependency. Critical probe-only
subsystems that fail probing also abort boot — they aren't silently skipped.

### Recovery flows

| Flow | What it does |
|---|---|
| `restart_dead_daemons` | For every Subsystem with `launchd_label` whose probe fails, run `launchctl kickstart -k`. Re-probe and report. |
| `replay_blocked_queue` | Reset `running` jobs older than 1h or future-dated (peer clock skew) back to `queued`; close synthetic-backfill jobs for non-local nodes. |
| `restore_proof_store_from_backup` | Replace `services/memory/proof_store.sqlite` with the newest backup from `~/.rig/backups/proof_store/`. Refuses if the live store is ≥95% the backup's size (would lose data). Stages a `.pre-restore.<ts>.sqlite` copy first. |

---

## Day-to-day commands

```bash
# What's alive right now?
rig-mesh status                                 # JSON snapshot, 17 subsystems
rig-mesh status | jq '.overall'                  # one-word verdict
rig-mesh status | jq '.probes[] | select(.status != "live_ok")'

# Verify a specific surface
rig-mesh probe --name litellm-mesh-router        # is the model router live?
rig-mesh probe --name idle-queue                  # is the queue daemon alive?
rig-mesh probe --name proof-store                # can we write proofs?

# Full audit + JSON artifact + proof bind
rig-mesh audit
# → writes .omx/state/rig-sovereign/rig-master/audit-<id>.json
# → writes .omx/state/rig-sovereign/rig-master/audit-latest.json
# → binds an immutable proof if proof_system is installed

# Cold-boot everything in dep order, abort on critical fail
rig-mesh boot --max-wait-s 10

# Preview recovery
rig-mesh recover --dry-run

# Run recovery (default flows: restart_dead + replay_queue)
rig-mesh recover
rig-mesh recover --flows restart_dead_daemons
rig-mesh recover --flows replay_blocked_queue,restore_proof_store_from_backup
```

All commands emit JSON to stdout; exit code reflects health.

---

## Adding an AI model

Edit `src/rig_mesh/subsystems.py`, scroll to `default_registry()`, append:

```python
reg.register(Subsystem(
    name="my-llm-server",
    description="Internal Mistral-7B-Instruct on TGI.",
    kind="service",
    http_health=(7000, "/health"),
    depends_on=[],
    critical=False,
))
```

Then:

```bash
pip install -e .                    # reinstall the package
rig-mesh probe --name my-llm-server   # confirm it's reachable
rig-mesh audit                       # capture in a proof
```

---

## Routing work to a model

`rig-mesh` is the observability + integrity gate around your dispatcher,
not the dispatcher itself. The typical sequence:

```bash
# 1. (your dispatcher, e.g. jake_mesh_runtime in the RIG Sovereign repo)
python3 -m services.ops.jake_mesh_runtime goal "score 50 leads with rig-l-formula"

# 2. (you / a wrapper script) verify the queue + model + proof store are live
rig-mesh probe --name idle-queue            # daemon running?
rig-mesh probe --name litellm-mesh-router    # model up?
rig-mesh probe --name proof-store           # can we write proofs?

# 3. (idle-queue daemon runs the job through the model; writes a proof)
# 4. (rig-mesh — auditable trail)
rig-mesh audit
```

If a probe fails before dispatch, you know which surface is dark and can
recover it (`rig-mesh recover`) without launching work that would fail.

---

## Configuration

| Env var | Effect | Default |
|---|---|---|
| `RIG_REPO_ROOT` | Override repo root | walks up from package install for `CLAUDE.md`, falls back to `~/Desktop/Startup-Intelligence-OS` |
| `RIG_STATE_DIR` | Override state dir | `<repo>/.omx/state/rig-sovereign` |
| `RIG_TEST_BYPASS_AUTH` | Bypass dashboard auth in TestClient only | unset (production never sets this) |

---

## Optional dashboard

```bash
pip install -e ".[dashboard]"
python -m uvicorn rig_mesh.dashboard:app --host 127.0.0.1 --port 8091
open http://127.0.0.1:8091/
```

8 endpoints (GET /health, GET /, GET/POST `/api/*`). Each `/api/*` route
requires a scope via `require_scope()` from `services.security.middleware`
if available — `scorecard:read` for reads, `incident:write` for mutating
verbs. Defense-in-depth: even with workflow permissions set, the actual
auth depends on `services.security` being installed alongside.

For local tests: `RIG_TEST_BYPASS_AUTH=1`. **Do not set this in production.**

---

## Testing

```bash
pip install -e ".[test]"
pytest
# expected: 63 passed + 2 skipped (auth-rejection tests skip when the
# optional services.security module isn't installed — standalone use case)
```

CI on every push (`.github/workflows/ci.yml`):
- `pytest --tb=short -q` on py3.10, py3.11, py3.12
- `python -m py_compile` over all source + tests

---

## Known gotchas

### 1. Heartbeat freshness drift

If `idle-queue-heartbeat.json` is stale by >5 minutes, `status` will report
`stale` (orange in the dashboard). Two causes worth checking:

- Daemon is alive but blocked on a slow job (look at `.omx/state/.../idle-queue-heartbeat.json` `processed_count`)
- Daemon died and launchd hasn't kickstarted it yet (`rig-mesh recover --flows restart_dead_daemons`)

### 2. launchctl returns rc=5 ("already loaded") on bootstrap

Normal behavior on re-boot. `BootRunner._bootstrap()` treats rc=5 as success
— the daemon was already running and didn't need bootstrapping. If you see
rc=5 followed by a `failed` step, the daemon is loaded but its probe is
failing — investigate the daemon's stderr log.

### 3. Future-dated `started_at` from peer clock skew

If a job in the operator queue has a `started_at` timestamp in the future
(common when other RIG nodes are slightly clock-skewed), `replay_blocked_queue`
treats the delta as effectively infinite and resets the job to `queued`. This
prevents the queue from stalling on a "running" job that can never finish.

### 4. Proof binding is best-effort

`audit()` and `boot()` try to import `services.ops.proof_system` to bind an
immutable proof of the run. If that module isn't on `PYTHONPATH` (the
standalone CLI case), they silently skip the proof binding and still write
the JSON artifact. Check `audit-latest.json` for the run record.

---

## Roadmap (operator-driven, not code-driven)

These aren't promises — they're the obvious next moves when the operator
needs them:

1. **Linux/systemd support.** Currently macOS-first (launchd). Adding
   `Subsystem.systemd_unit="x.service"` is a small addition to
   `subsystems.py` and a parallel branch in `BootRunner` / `Recovery`.
2. **Registry from YAML.** Instead of editing `default_registry()`, load
   `~/.config/rig-mesh/subsystems.yaml` so non-Python users can extend
   without modifying the package.
3. **Prometheus exporter.** `/metrics` endpoint on the dashboard that
   emits the snapshot as Prometheus text format. ~30 lines.
4. **rig-mesh shell completion.** `argcomplete` for the CLI.
5. **Brier-tracked outcomes.** Treat each Subsystem's probe history as a
   Brier-calibrated forecast — promote `M4 → M5` (production with
   monitoring) for any Subsystem with ≥7-day rolling pass rate ≥0.95.

---

## How to keep it healthy

Daily:
```bash
rig-mesh audit && rig-mesh status      # gate on `overall: ok`
```

Weekly:
```bash
rig-mesh recover --dry-run             # see what would be cleaned up
```

Monthly:
```bash
pytest                                  # full test suite (60s)
rig-mesh boot                           # cold-boot drill — proves recovery works
```

Whenever you add a new model or service:
1. Append to `default_registry()` with the right probe
2. Re-install: `pip install -e .`
3. `rig-mesh probe --name <new>` to confirm
4. Commit and push the new Subsystem entry

---

## Contact + provenance

- **Maintainer**: Mike Rodgers (rodgemd1@gmail.com)
- **Origin commit**: PR #44 in rodgemd1-lgtm/Startup-Intelligence-OS, merged 2026-05-19
- **Co-author of the initial extraction**: Claude Opus 4.7 (1M context)
- **License**: MIT
