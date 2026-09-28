---
name: skill-orchestrator
description: "Execution layer atop skill-dispatcher. Runs HANDOFF/SEQUENCE decisions end-to-end: Phase 0 context load, Phase 1 specialist, chain telemetry."
disable-model-invocation: true
metadata:
  dispatcher-category: orchestration
  dispatcher-layer: execution
  dispatcher-lifecycle: active
  dispatcher-risk: medium
  dispatcher-writes-files: true
  dispatcher-capabilities: skill-execution, sequence-runner, phase-orchestration, chain-telemetry
  dispatcher-accepted-intents: execute_routing_decision, run_skill_sequence, orchestrate_handoff, fix_bug, implement_feature, audit_codebase, prepare_release, orchestrate_workflow
  dispatcher-stack-tags: orchestration, execution, sequence
  dispatcher-downstream-skills: skill-dispatcher, personal-context-portfolio, bug-fix-lifecycle, new-feature-sdlc-skill, principal-audit-refactor, test-design-orchestrator, stitch-loop
  dispatcher-preferred-model: claude-sonnet-4-6
---

> **Author:** jovd83 | **Version:** 1.2.0 | **License:** MIT

# Skill Orchestrator

Sits between `skill-dispatcher` (decisions) and specialist skills (work). Takes a routing decision and executes it end-to-end with telemetry at each step.

## Architecture

```
User query
    │
    ▼
skill-dispatcher         ← decides: HANDOFF / SEQUENCE / NO_MATCH
    │
    ▼
skill-orchestrator       ← executes: Phase 0 → Phase 1 (this skill)
    │         │
    ▼         ▼
Phase 0       Phase 1
(context)     (specialist)
```

## Host-driven chains (`scripts/next_phase.py`)

`next_phase.py` is the chain engine the chain agents use: `bug-fix-lifecycle`, `new-feature-sdlc`, `principal-audit-refactor`, `project-genesis` and `test-lifecycle` in Claude Code, and the same chains in Codex. It never calls a model. It reads a chain's `config/chain_definition.json`, hands the calling agent one phase at a time as a JSON envelope (the phase skill's SKILL.md as `system_prompt`, plus the query, accumulated context and phase constraint), and records what the agent produced. The work therefore runs on the host's own model and subscription.

```bash
python scripts/next_phase.py start   --chain <chain-name> --query-file request.md --host claude-code
python scripts/next_phase.py advance --chain-id <id> --phase-output-file output.md [--failed | --skipped]
python scripts/next_phase.py advance --chain-id <id> --approve | --reject --reason "<why>"
python scripts/next_phase.py status  --chain-id <id>
```

- A phase with `"on_phase_complete": "hitl"` returns `awaiting_approval: true`; the chain waits for `--approve` or `--reject`.
- A phase without a `skill` is agent-handled: the envelope has an empty `system_prompt` and `agent_handled: true`.
- `--skipped` records a not-applicable phase without counting it as a failure.
- Run state lives in `~/.agents/dispatcher-data/chain_runs/<chain_id>.json`; phase skills are read from `~/.agents/skills/<skill>/SKILL.md`.

`scripts/orchestrate.py` is the older runner that calls the Anthropic API for every phase and needs `ANTHROPIC_API_KEY`; keep it for scripted runs outside an agent.

## When to Use

- Use `/skill-dispatcher` when you only want a **routing recommendation**.
- Use `/skill-orchestrator` (or `dispatch_cli.py --execute`) when you want the sequence **run end-to-end** with telemetry per step.
- **Always** use `/skill-orchestrator` when the dispatcher routes to a chain-orchestrating skill: `bug-fix-lifecycle`, `new-feature-sdlc-skill`, `principal-audit-refactor`, `test-design-orchestrator`, `stitch-loop`, `release-manager-skill`. These skills define multi-step sequences; the orchestrator provides the execution frame, context loading, and per-step telemetry.

## Chain-Orchestrating Skills

These skills are sequence definitions, not single invocations. Route them through the orchestrator:

| Skill | Chain phases |
|:------|:-------------|
| `bug-fix-lifecycle` | `codebase-context` → `test-design-orchestrator` → `stack-aware-unit-testing-skill` → [fix: agent] → `stack-aware-unit-testing-skill` → `playwright-skill` → `automated-test-reviewer` → [report: agent] |
| `new-feature-sdlc-skill` | `codebase-context` → `backlog-story-generator` → `acceptance-criteria-designer` → [implement: agent] → `stack-aware-unit-testing-skill` → `api-contract-sentinel` → `playwright-skill` → `automated-test-reviewer` → `release-manager-skill` |
| `principal-audit-refactor` | Audit → severity-rank → approval-gated refactor |
| `test-design-orchestrator` | Requirements → test technique selection → artifact generation |
| `stitch-loop` | Iterative design → generation → validation loop |
| `release-manager-skill` | Changelog → version bump → publish cycle |

## Workflow

1. **Bootstrap**: Run `dispatch_bootstrap.py` — gather policy context and emit `POLICY_CONSULT` event.
2. **Decide**: Read the routing decision (from `--routing-decision` JSON or by calling dispatcher logic).
3. **Phase 0**: If decision is `SEQUENCE` or if risk is `high`, invoke the Phase 0 context skill and emit a `CONTEXT_LOAD` event.
4. **Phase 1 / Chain execution**: Invoke the specialist skill or every phase in `config/chain_definition.json`. Log `HANDOFF` or `SEQUENCE` with the shared `chain_id`.
5. **Mandatory phase enforcement**: For chain definitions, each mandatory phase must emit a terminal event containing `phase_id` and `phase_status` (`success`, `failed`, or `blocked`). Optional phases may emit `skipped` or `not_applicable`, but they must still be logged. Stop the chain when a mandatory phase fails, is blocked, or declares itself not applicable.
6. **Summary**: Print the chain log JSON (chain_id, skill, model, tokens, phase status).

## Allowlist

Only skills listed in `skill-dispatcher/config/executable_skills.json` can be invoked. Add a skill name there to permit execution.

## Scope

- Enforced path only: no retry or rollback, but mandatory phase failures stop the chain instead of being silently skipped.
- `--dry-run` mode: prints planned commands without executing.
- `chain_id` (auto-generated UUID) links all phase events in the log for correlation.
