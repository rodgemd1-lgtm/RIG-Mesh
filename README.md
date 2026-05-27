# RIG Mesh

> **A control-plane CLI for AI models and work routing.**
> One command (`rig-mesh`) sits above your model providers, daemons, dashboards, and queues, declares them as Subsystems, probes them for live health, boots them in dependency order, and routes work to the right model on the right node — with proof.

```
        ┌─────────────────────── rig-mesh CLI ────────────────────────┐
        │ status · list · probe · audit · boot · recover · version    │
        └─────────┬───────────────────────┬───────────────────────────┘
                  │                       │
          ┌───────▼────────┐      ┌───────▼────────┐
          │  SubsystemReg  │      │  CommandRouter │
          │  (17 default)  │      │  (verbs → ops) │
          └───────┬────────┘      └───────┬────────┘
                  │                       │
   ┌──────────────┼──────────────────┐    │
   │              │                  │    │
 ┌─▼──────────┐ ┌─▼──────────┐ ┌─────▼────▼─────┐
 │ AI MODELS  │ │ WORK QUEUE │ │  OBSERVABILITY │
 │  litellm   │ │ idle-queue │ │  monitor-runner│
 │  :4000     │ │  + goals   │ │  meta-monitor  │
 │  → ollama  │ │            │ │  cockpit-v5    │
 │  → openai  │ │            │ │  deviation-dash│
 │  → anthropic│ │           │ │                │
 └────────────┘ └────────────┘ └────────────────┘
```

The CLI does not run models or schedule jobs itself. It treats each piece
of your AI stack (model providers, queue runners, dashboards, healers,
backup daemons) as a declarative **Subsystem** and gives you a uniform way
to see, start, recover, and prove their state.

---

## Why this exists

If you run AI workloads locally — Ollama, LiteLLM, a private mesh, a queue
of overnight jobs — you usually end up with:

- A `launchctl list` of 10+ daemons you mostly trust
- Three dashboards on three ports you forget about
- A `proof_store.sqlite` that may or may not be writable
- A backlog of jobs in a queue you can't see
- An idea of which model handled which job — but no audit trail

**rig-mesh** is the small CLI that makes all of that observable, restartable,
and provable in one place. It's the "did the right model serve the right job
on the right node with the right outcome?" gate that doesn't exist in any of
the underlying tools.

---

## Install

```bash
pip install git+https://github.com/rodgemd1-lgtm/RIG-Mesh.git
```

Or from source:
```bash
git clone https://github.com/rodgemd1-lgtm/RIG-Mesh.git
cd RIG-Mesh
pip install -e .                    # CLI only (stdlib)
pip install -e ".[dashboard]"       # CLI + :8091 FastAPI dashboard
pip install -e ".[dev]"             # everything + tests
```

The `rig-mesh` console script is on your `$PATH` after install.

---

## 30-second quickstart

```bash
rig-mesh version                       # confirms install
rig-mesh status                        # snapshot of every subsystem (17 default)
rig-mesh probe --name litellm-mesh-router    # check your model router
rig-mesh audit                         # full audit + JSON artifact + proof
rig-mesh boot --max-wait-s 10          # cold-boot in dependency order
rig-mesh recover --dry-run             # preview recovery flows
```

All output is JSON. Exit code reflects health: `0` ok, `1` warn or fail.

---

## How AI model setup works in rig-mesh

### The model layer

rig-mesh ships with **`litellm-mesh-router`** registered as a Subsystem on
`:4000`. LiteLLM is the canonical multi-provider router — point it at any
combination of Ollama, OpenAI, Anthropic, Together, Groq, etc., and rig-mesh
will probe its `/health` endpoint and report green/red.

### Add your own model provider

Each Subsystem is a small declaration. To register a new model endpoint —
say, a local TGI server on `:7000` — append to the registry:

```python
# in src/rig_mesh/subsystems.py default_registry():
reg.register(Subsystem(
    name="tgi-mistral-7b",
    description="Text-Generation-Inference server, Mistral-7B-Instruct.",
    kind="service",
    http_health=(7000, "/health"),
    depends_on=[],         # add e.g. ["gpu-driver"] if you split GPU as a subsystem
    critical=False,        # not critical means audit warns, doesn't fail
))
```

After that:

```bash
rig-mesh probe --name tgi-mistral-7b
rig-mesh status     # tgi-mistral-7b now shows in the matrix
rig-mesh boot       # rig-mesh will probe + report; launchctl bootstrap if you
                    # gave it a launchd_label
```

### Three probe kinds, picked automatically

| Probe | When it's used | Backed by |
|---|---|---|
| **HTTP** | Subsystem has `http_health=(port, path)` | Raw `socket` — no `requests` dep |
| **Heartbeat** | Subsystem has `heartbeat_path=Path(...)` | File `mtime` vs `heartbeat_max_age_s` |
| **launchd** | Subsystem has only `launchd_label="com.x.y"` | `launchctl list` parser |

If a Subsystem declares more than one, the most authoritative wins: HTTP
> heartbeat > launchd. A 401 from HTTP counts as live (auth gate working);
a 200 with health JSON is preferred.

---

## How work routing works

### The queue layer

rig-mesh ships **`idle-queue`** as a registered Subsystem — the 24/7 runner
that processes jobs from your operator queue. Its heartbeat says how many
jobs ran and how many errored. The default registry treats it as **critical**:
if its heartbeat goes stale or it dies, `rig-mesh audit` reports `fail` and
`rig-mesh boot` aborts dependent daemons.

### Route work via the goal flow

Work routing happens via your own `jake_mesh_runtime` or equivalent — rig-mesh
doesn't dispatch jobs itself. It provides the **observability + integrity**
layer around the dispatcher:

```bash
# 1. (your dispatcher) enqueue a goal
python3 -m services.ops.jake_mesh_runtime goal "score these 50 leads with rig-l-formula"

# 2. (rig-mesh) verify the queue daemon is actually running
rig-mesh probe --name idle-queue
#   → "live_ok" + age_seconds + processed_count from the heartbeat

# 3. (rig-mesh) verify the model the queue will route to is up
rig-mesh probe --name litellm-mesh-router
#   → "live_ok" if /health 200

# 4. (idle-queue) processes job, calls model, writes proof to proof_store
# 5. (rig-mesh) audit binds a proof of the audit run itself
rig-mesh audit
#   → writes audit-<id>.json + binds a proof if proof_system reachable
```

### Recovery flows for stuck routing

When the queue stalls (live in production — clock skew, stuck-running jobs,
synthetic backfills for offline nodes), `rig-mesh recover` cleans it up:

```bash
rig-mesh recover --dry-run                       # preview
rig-mesh recover                                 # default: restart_dead_daemons + replay_blocked_queue
rig-mesh recover --flows restore_proof_store_from_backup
```

`replay_blocked_queue` does three things:
1. Resets `running` jobs older than 1h (or future-dated from peer clock skew) back to `queued`
2. Closes synthetic-backfill jobs for non-local nodes that will never run
3. Refuses to overwrite a live proof store with a smaller backup (would lose data)

---

## CLI reference

| Verb | What it does | Exit code |
|---|---|---|
| `status [--write]` | Probe every subsystem, return JSON snapshot with aggregate verdict | `0` if `overall: ok` |
| `list` | Print every registered subsystem with declared probes + deps | `0` |
| `probe --name <subsystem>` | Run one subsystem's probe | `0` if ok |
| `audit` | Full audit + write artifact to `<state>/rig-master/audit-*.json` + best-effort proof bind | `0` if `overall != fail` |
| `boot [--max-wait-s N] [--no-stop-on-critical-fail]` | Cold-boot all in topological dependency order | `0` if no critical aborts |
| `recover [--flows ...] [--dry-run]` | Recovery flows: `restart_dead_daemons`, `replay_blocked_queue`, `restore_proof_store_from_backup` | `0` if all steps ok |
| `version` | Print rig-mesh version | `0` |

All commands emit JSON to stdout. Pipe to `jq` for pretty-printing or to other
tooling for routing decisions.

---

## The 17 canonical subsystems

| Name | Kind | Critical | Probe |
|---|---|---|---|
| `proof-store` | library | ✓ | heartbeat (proof_store.sqlite mtime) |
| `idle-queue` | daemon | ✓ | heartbeat (.omx/state/.../idle-queue-heartbeat.json) |
| `cell-watcher` | daemon | | launchd (com.rig.cell-watcher) |
| `healer-daemon` | daemon | | heartbeat (healer-heartbeat.json) |
| `rig-247-supervisor` | daemon | | heartbeat (rig-247-supervisor-heartbeat.json) |
| `monitor-runner` | daemon | | heartbeat (monitor-runner-heartbeat.json) |
| `meta-monitor` | daemon | | heartbeat (meta-monitor-heartbeat.json) |
| `cockpit-v5` | service | | HTTP :8090 /health |
| `deviation-dashboard` | service | | HTTP :8078 /health |
| `studio-dashboard` | service | | HTTP :8077 /health |
| `progress-board` | service | | HTTP :8083 /health |
| `avis-nps` | service | | HTTP :8079 /health |
| `litellm-mesh-router` | service | | HTTP :4000 /health |
| `backup-runner` | daemon | | launchd (com.rig.backup-runner) |
| `retention-runner` | daemon | | launchd (com.rig.retention-runner) |
| `escalation-runner` | daemon | | launchd (com.rig.escalation-runner) |
| `node-worker` | daemon | | heartbeat (~/Library/Logs/rig-node-worker-heartbeat.json) |

Topological order (dependencies resolve before dependents):

```
proof-store → idle-queue → rig-247-supervisor
              monitor-runner → meta-monitor
              cockpit-v5
```

---

## Configuration

### Environment variables

| Variable | Effect | Default |
|---|---|---|
| `RIG_REPO_ROOT` | Override repo root detection | Walks up looking for `CLAUDE.md`, falls back to `~/Desktop/Startup-Intelligence-OS` |
| `RIG_STATE_DIR` | Override state directory | `<repo>/.omx/state/rig-sovereign` |
| `RIG_TEST_BYPASS_AUTH` | Bypass dashboard auth in TestClient probes | unset (production never sets this) |

### Pointing at a different RIG install

```bash
RIG_REPO_ROOT=/opt/my-rig rig-mesh status
RIG_STATE_DIR=/var/lib/rig-state rig-mesh probe --name idle-queue
```

---

## Dashboard (optional)

```bash
pip install -e ".[dashboard]"
python -m uvicorn rig_mesh.dashboard:app --host 127.0.0.1 --port 8091
open http://127.0.0.1:8091/
```

Endpoints:
- `GET /health` — liveness, always 200
- `GET /` — HTML status page, auto-refreshes every 10s
- `GET /api/status` — JSON snapshot (auth: `scorecard:read`)
- `GET /api/list` — subsystem registry (auth: `scorecard:read`)
- `GET /api/probe/{name}` — single probe (auth: `monitor:read`)
- `POST /api/audit` — run audit (auth: `scorecard:write`)
- `POST /api/boot` — boot sequence (auth: `incident:write`)
- `POST /api/recover` — recovery flows (auth: `incident:write`)

Auth uses the standard RIG Sovereign HMAC token middleware if `services.security`
is on PYTHONPATH. Without it, mutating endpoints are still gated by FastAPI's
require-scope dependency. Set `RIG_TEST_BYPASS_AUTH=1` for local TestClient
work; **production never sets this**.

---

## Extending the registry

For a permanent add, edit `default_registry()` in `src/rig_mesh/subsystems.py`.
For a one-off, build your own registry programmatically:

```python
from rig_mesh import RigMaster, Subsystem, SubsystemRegistry, default_registry

# Start from the canonical 17, add your own
reg = default_registry()
reg.register(Subsystem(
    name="my-private-llm",
    description="Internal fine-tuned 70B server",
    kind="service",
    http_health=(9100, "/v1/health"),
    depends_on=["proof-store"],
    critical=True,
))

master = RigMaster(reg)
print(master.status_snapshot())
print(master.audit().to_dict())
```

---

## Use cases

### Set up a new AI model

1. Start the model server (Ollama, LiteLLM, TGI, vLLM, whatever)
2. Add a `Subsystem(http_health=(<port>, "/health"))` entry — 5 lines
3. `rig-mesh probe --name <new-model>` to confirm it's reachable
4. `rig-mesh audit` to capture the new state in a proof

### Verify the routing path before sending traffic

```bash
# Probe the chain end-to-end
rig-mesh probe --name idle-queue            # is the queue daemon alive?
rig-mesh probe --name litellm-mesh-router    # is the model router live?
rig-mesh probe --name proof-store           # can we write proofs?
rig-mesh probe --name cockpit-v5             # is the observability layer live?
# All four ok → safe to dispatch work.
```

### Watch a fleet of models

```bash
# Every 30s: status snapshot piped to jq for a TUI-style summary
watch -n 30 "rig-mesh status | jq '.probes[] | select(.kind == \"service\") | {name, status}'"
```

### Auto-restart dead daemons

```bash
# Cron / launchd / systemd:
*/5 * * * * /usr/local/bin/rig-mesh recover --flows restart_dead_daemons
```

### Treat as a one-line health check in CI

```bash
- name: RIG health gate
  run: rig-mesh audit && rig-mesh status
```

---

## Testing

```bash
pip install -e ".[test]"
pytest
```

63 tests covering registry, probes (HTTP refused, heartbeat fresh / stale /
missing, launchd parser, no-probe path), topological sort with cycle
detection, audit aggregation, boot ordering with critical-fail abort, recovery
flows, router dispatch, CLI argparse, and the FastAPI dashboard (auth-bypass
aware). 2 tests skip when the optional `services.security` auth module isn't
installed.

CI runs on every push: pytest on py3.10 / 3.11 / 3.12 + compile lint.

---

## Design notes

- **stdlib only for the core.** HTTP probes use raw `socket`. FastAPI / uvicorn
  are opt-in via the `[dashboard]` extra.
- **No live LLM.** Every check is deterministic and re-runnable. The optional
  proof binding reaches into a `proof_system` module if installed; otherwise
  degrades silently.
- **Truth-tracker doctrine.** Probes always report what's reachable right now,
  not what a YAML claims. The CLI's exit code reflects live state, never the
  scorecard.
- **macOS-first.** `launchctl bootstrap` / `kickstart` for daemon control.
  Heartbeat + HTTP probes are platform-agnostic, so Linux/systemd users can
  ignore the launchd-only Subsystems and add their own.

---

## License

MIT — see [`LICENSE`](LICENSE).
