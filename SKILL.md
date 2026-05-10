---
name: skill-orchestrator
description: Execution layer atop skill-dispatcher. Runs HANDOFF/SEQUENCE decisions end-to-end: Phase 0 context load, Phase 1 specialist, chain telemetry.
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

> **Author:** jovd83 | **Version:** 1.0.0 | **License:** MIT

## Telemetry & Logging
> [!IMPORTANT]
> Each orchestration chain step MUST be logged with a shared `chain_id`:
> `python scripts/orchestrate.py --dry-run` to preview, then run without `--dry-run`.
> Each phase emits its own event via `dispatch_logger.py` so the wallboard shows the full chain.

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
4. **Phase 1**: Invoke the specialist skill via Anthropic API with its SKILL.md as cached system prompt. Log `HANDOFF` or `SEQUENCE` event with the shared `chain_id`.
5. **Summary**: Print the chain log JSON (chain_id, skill, model, tokens, phase status).

## Allowlist

Only skills listed in `skill-dispatcher/config/executable_skills.json` can be invoked. Add a skill name there to permit execution.

## Scope

- Happy path only: no retry, no rollback.
- `--dry-run` mode: prints planned commands without executing.
- `chain_id` (auto-generated UUID) links all phase events in the log for correlation.
