# skill-orchestrator

[![version](https://img.shields.io/badge/version-1.2.0-blue)](CHANGELOG.md)
[![status](https://img.shields.io/badge/status-stable--beta-f0ad4e)](SKILL.md)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/jovd83)
[![Validate Skill](https://github.com/jovd83/skill-orchestrator/actions/workflows/validate.yml/badge.svg)](https://github.com/jovd83/skill-orchestrator/actions/workflows/validate.yml)

Execution layer atop [skill-dispatcher](https://github.com/jovd83/skill-dispatcher). Takes a routing decision and runs the planned sequence end-to-end — with a telemetry event per step and a shared `chain_id` across the entire execution.

## Problem it solves

`skill-dispatcher` is a *routing engine*: it decides which skill should handle a task but never invokes anything. `skill-orchestrator` is the *execution layer*:

```
User query
    │
    ▼
skill-dispatcher         ← decides: HANDOFF / SEQUENCE
    │
    ▼
skill-orchestrator       ← executes: chain phases or Phase 0 → Phase 1
    │
    ▼
specialist sub-skills    ← each invoked via Anthropic API with SKILL.md as cached system prompt
```

## Chain-orchestrating skills

When the selected skill has a `config/chain_definition.json`, the orchestrator runs the full multi-phase chain instead of a single Phase 0 + Phase 1 call. All phase events share one `chain_id`.

| Skill | Chain phases |
|:------|:-------------|
| `bug-fix-lifecycle` | `codebase-context` → `test-design-orchestrator` → `stack-aware-unit-testing-skill` \| `api-contract-sentinel` \| `playwright-skill` \| `performance-testing-skill` \| `defensive-appsec-review-skill` [self-gate] → [fix: agent] → `stack-aware-unit-testing-skill` → `automated-test-reviewer` → [report: agent] |
| `new-feature-sdlc-skill` | `codebase-context` → `backlog-story-generator` → `acceptance-criteria-designer` → [implement: agent] → `stack-aware-unit-testing-skill` → `api-contract-sentinel` → `playwright-skill` → `automated-test-reviewer` → `release-manager-skill` |

Any skill can become chain-capable by adding `config/chain_definition.json`. See `propose_chains.py` in `skill-dispatcher` for automated draft generation.

## When to use each

| Goal | Tool |
|:-----|:-----|
| Routing recommendation only | `/skill-dispatcher` |
| Execute a chain-skill end-to-end | `/skill-orchestrator` or `dispatch_cli.py --execute` |
| Execute any single skill via API | `dispatch_cli.py --execute --routing-decision <json>` |

## Quick start

```bash
# Chain-skill dry run — shows all phases that would fire
python scripts/orchestrate.py \
  --routing-decision '{"selected_skill":"bug-fix-lifecycle","decision":"HANDOFF","query":"fix the login bug","risk":"high"}' \
  --dry-run

# Execute via dispatch_cli convenience wrapper
python ../skill-dispatcher/scripts/dispatch_cli.py \
  --execute \
  --routing-decision '{"selected_skill":"new-feature-sdlc-skill","decision":"SEQUENCE","query":"implement SSO","risk":"high"}'
```

## chain_definition.json format

```json
{
  "chain_name": "my-skill",
  "description": "one-line chain description",
  "phases": [
    {
      "id": "repo_discovery",
      "name": "Step 1 - Discover conventions",
      "skill": "codebase-context",
      "intent": "discover_repo_conventions",
      "reason": "why this phase exists",
      "risk": "low",
      "mandatory": true,
      "pass_context_forward": true
    },
    {
      "id": "implement",
      "name": "Step 2 - Implement",
      "skill": null,
      "intent": "implement",
      "reason": "agent writes code directly",
      "risk": "high",
      "_note": "agent-handled"
    },
    {
      "id": "optional_phase",
      "name": "Step 3 - Optional test",
      "skill": "playwright-skill",
      "intent": "run_e2e",
      "risk": "medium",
      "query_suffix": "Only run if the change touches UI. Otherwise state 'not applicable' and stop."
    }
  ]
}
```

`query_suffix` is injected as a `[CHAIN CONSTRAINT]` block into the sub-skill's prompt — use it for self-gating phases that only apply to certain scenarios.

`mandatory` defaults to `true` when omitted. With `orchestrate.py`, mandatory phases stop the chain if they fail, are blocked, or declare themselves not applicable; with `next_phase.py` the agent records the outcome and decides. Optional phases may return `CHAIN_PHASE_STATUS: not_applicable`; the orchestrator logs that terminal status and continues.

## Security: the allowlist gate

Only skills listed in `skill-dispatcher/config/executable_skills.json` can be invoked. Add a skill name explicitly to permit execution. Never auto-populate from the registry.

## Telemetry

Every step emits an event to `dispatch_events.jsonl` under the same `chain_id`:

| Step | Event type | Emitted by |
|:-----|:-----------|:-----------|
| Chain entry | `SEQUENCE` | `run_chain()` — links trigger to all phases |
| Each phase start | `HANDOFF` + `phase_id` | `run_chain()` |
| Each phase complete | `HANDOFF` + `phase_id` + `phase_status` | `run_chain()` |
| Agent-handled phases | `HANDOFF` + `phase_id` + `phase_status=success` | `run_chain()` |
| Bootstrap | `POLICY_CONSULT` | `dispatch_bootstrap.py` |

`chain_id` is auto-generated (UUID) if not passed explicitly. Passing `--chain-id` links events across multiple orchestrator invocations.

Terminal phase statuses are `success`, `failed`, `blocked`, `skipped`, and `not_applicable`. Every mandatory phase must end with `success`, `failed`, or `blocked`; silent omission is a compliance failure.

## Host-driven chain engine

`scripts/next_phase.py` runs a chain without calling any model: it returns one phase at a time as a JSON envelope, the calling agent produces the phase output on its own model, and `advance` records it. The Claude Code chain agents (`bug-fix-lifecycle`, `new-feature-sdlc`, `principal-audit-refactor`, `project-genesis`, `test-lifecycle`) and Codex use it, so chains bill to the host's subscription instead of an API key.

```bash
python scripts/next_phase.py start   --chain bug-fix-lifecycle --query-file request.md --host claude-code
python scripts/next_phase.py advance --chain-id <id> --phase-output-file output.md
python scripts/next_phase.py advance --chain-id <id> --approve        # at an approval gate
python scripts/next_phase.py status  --chain-id <id>
```

Approval gates come from `"on_phase_complete": "hitl"` in `chain_definition.json`. `--failed` and `--skipped` mark the phase that just ran; `--reject --reason` halts the chain. Run state is kept in `~/.agents/dispatcher-data/chain_runs/`.

## Repository structure

```
skill-orchestrator/
├── SKILL.md                  # Dispatcher contract
├── README.md                 # This file
├── scripts/
│   ├── next_phase.py         # Host-driven chain engine used by the chain agents (no model calls)
│   ├── orchestrate.py        # API-key runner — chain detection, run_chain(), phase execution
│   └── validate_repo.py      # Repository validation (runs the tests)
└── tests/
    └── test_orchestrate.py
```

## Running tests

```bash
pip install pytest
pytest tests/ -v
```
