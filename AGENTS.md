# AGENTS — RIG Mesh V10 Agent Design

This document defines agent roles, model-routing expectations, quality gates,
and the weekly self-improvement loop for RIG Mesh V10.

---

## V10 Product Promise

**RIG Mesh** is the deterministic control plane that sits above every AI model
provider, work-queue daemon, dashboard, and backup service in the RIG Sovereign
stack. It exposes a uniform CLI (`rig-mesh`), an MCP server (`rig-mesh-mcp`),
and a Python API so human operators, CLI scripts, CI pipelines, and AI agents
can all see, start, recover, and prove the state of the AI infrastructure —
without ever running a model themselves.

V10 adds:
- **Agent readiness**: structured agent roles with model routing
- **MCP surface**: tools/resources/prompts for agent clients
- **Weekly improvement loop**: automated quality gate with human-approval gate

---

## Agent Roles

Four roles form the V10 agent lattice. Each maps to a specific model tier
and has a clear responsibility boundary.

### 1. Planner (A1)

| Attribute | Value |
|---|---|
| Role | Decomposes work into a list of issues or sub-tasks |
| Model | `claude-opus-4-5` (long context, high reasoning) |
| Input | V10 issue text + current KPI scorecard |
| Output | Ordered task list, acceptance criteria, blockers |
| Quality gate | Must produce a DoneContract before product work begins |
| What it must NOT do | Merge PRs, push to main, create schedules |

### 2. Reviewer (A2)

| Attribute | Value |
|---|---|
| Role | Reviews code changes against the DoneContract checklist |
| Model | `claude-sonnet-4-5` (balanced speed/quality) |
| Input | PR diff + DoneContract + test output |
| Output | Approval or explicit blocking comments |
| Quality gate | All checklist items must be ticked; no open blocking comments |
| What it must NOT do | Approve its own changes, override a QA FAIL |

### 3. Fixer (A3)

| Attribute | Value |
|---|---|
| Role | Addresses Reviewer comments and CI failures |
| Model | `claude-sonnet-4-5` (fast iteration) |
| Input | Review comments + failing test output |
| Output | Corrective commits on the PR branch |
| Quality gate | CI must pass before re-requesting review |
| What it must NOT do | Bypass CodeQL checks, weaken tests |

### 4. QA (A4)

| Attribute | Value |
|---|---|
| Role | Validates that tests pass and proof paths are documented |
| Model | `claude-haiku-4-5` (deterministic checks) |
| Input | Test output + audit artifact + smoke result |
| Output | `QA: PASS` or `QA: FAIL <reason>` |
| Quality gate | `pytest` must report 0 failures; `rig-mesh smoke` must exit 0 |
| What it must NOT do | Claim PASS without running tests, silence flaky tests |

---

## Model Routing Expectations

```
Issue → Planner (Opus)
              ↓
        DoneContract
              ↓
Fixer (Sonnet) ←→ Reviewer (Sonnet)
              ↓
         CI passes
              ↓
         QA (Haiku)
              ↓
       Human approves merge
```

- **Opus** is reserved for planning and high-stakes reasoning (cost gate).
- **Sonnet** handles iterative development and review.
- **Haiku** handles deterministic quality checks (fast, cheap, repeatable).
- No model role may approve its own output.
- All model calls in CI must be read-only; no agent writes to `main` directly.

---

## Quality Gates

| Gate | Tool | Passing condition |
|---|---|---|
| Unit tests | `pytest --tb=short -q` | 0 failures (skips OK) |
| Compile lint | `python -m py_compile src/**/*.py tests/**/*.py` | No syntax errors |
| Smoke | `rig-mesh smoke` | `result: PASS`, exit 0 |
| CodeQL | GitHub code scanning | 0 new high/critical alerts |
| Human review | PR approval | At least 1 human approval on `main` |

No work may be merged to `main` without all five gates passing.

---

## MCP Surface

The MCP server (`src/rig_mesh/mcp_server.py`) exposes:

### Tools (agent-callable actions)

| Tool | Description | Read/Write |
|---|---|---|
| `probe_subsystem(name)` | Live health probe for one subsystem | Read |
| `get_status()` | Aggregate probe of all subsystems | Read |
| `list_subsystems()` | Registry listing (no I/O) | Read |
| `run_audit()` | Full audit + JSON artifact | Write (artifact) |
| `smoke()` | Deterministic local smoke test | Read |

### Resources (agent-readable state)

| Resource URI | Description |
|---|---|
| `rig://registry` | Full subsystem registry as JSON |
| `rig://audit/latest` | Latest audit artifact |

### Prompts (agent prompt templates)

| Prompt | Description |
|---|---|
| `health_check_prompt` | Asks an agent to interpret a status snapshot |

### MCP deferral rationale (for future reference)

The MCP server **write tools** (`run_audit`, and any future `boot`/`recover`)
must only be called in sandboxed CI contexts or with an explicit human-in-the-loop
confirmation step. They are not exposed to autonomous agent loops without a
sealed DoneContract authorizing that use.

---

## Weekly Improvement Loop

The `.github/workflows/weekly.yml` workflow runs every Monday at 08:00 UTC:

1. **Tests** — `pytest --tb=short -q` on py3.10/3.11/3.12
2. **Smoke** — `rig-mesh smoke` exits 0
3. **Report** — Writes a JSON report to the workflow summary
4. **Human gate** — The workflow never merges, deploys, or creates schedules.
   Any remediation it identifies is filed as an issue for a human to triage.

### What the weekly loop must NEVER do automatically

- Merge PRs to `main`
- Push commits that weren't reviewed
- Create or modify launchd/systemd schedules
- Publish packages
- Send messages, webhooks, or notifications to external services
- Run the Planner, Reviewer, or Fixer agents autonomously

---

## Proof Paths

```bash
# 1. Verify install
rig-mesh version
#    → { "ok": true, "rig_master": "1.0.0" }

# 2. Deterministic smoke (first proof gate)
rig-mesh smoke
#    → { "ok": true, "result": "PASS", "checks": { ... } }

# 3. Registry proof
rig-mesh list | jq '.subsystems | length'
#    → 17

# 4. Full test suite
pytest --tb=short -q
#    → 63+ passed, 2 skipped

# 5. MCP surface import (without MCP package — must not error)
python -c "from rig_mesh.mcp_server import tool_smoke; print(tool_smoke())"
#    → { "ok": true, "result": "PASS", ... }
```

---

## Blockers

| Blocker | Impact | Resolution path |
|---|---|---|
| `services.security` not on PYTHONPATH | 2 auth tests auto-skip; dashboard auth is defence-in-depth only | Acceptable for standalone use; document clearly |
| `launchctl` macOS-only | launchd probes return `unknown` on Linux CI | Heartbeat + HTTP probes work everywhere; systemd support is a roadmap item |
| No live model in CI | `litellm-mesh-router` probe returns `down` in CI | Expected; CI is infrastructure-only, not model-layer |
| MCP package optional | `rig-mesh-mcp` script fails if `[mcp]` extra not installed | Documented; graceful ImportError with install hint |

---

## V10 KPI Targets (from this change)

| KPI | Before | Target | Signal |
|---|---|---|---|
| `agent_readiness` | 2/10 | 6/10 | AGENTS.md + role definitions |
| `cli_readiness` | 3/10 | 6/10 | `smoke` verb added |
| `mcp_readiness` | 0/10 | 5/10 | `mcp_server.py` + tools/resources/prompts |
| `quality_readiness` | 6/10 | 7/10 | Smoke gate in weekly workflow |
| `weekly_automation_readiness` | 7/10 | 8/10 | `weekly.yml` added |
| `proof_readiness` | 2/10 | 5/10 | Proof paths documented |
