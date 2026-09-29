# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-09-29

### Added
- `next_phase.py advance --finish --reason <text>`: at an approval gate, end the chain there as a success and skip the remaining phases. The summary and `status` report `finished_early`. It is the exit for planned early ends such as an audit-only `principal-audit-refactor` run, which used `--reject` and would now count as a failed chain. `--finish` outside a gate is refused. It is logged as a success unless an earlier phase failed.
- `--approve`, `--reject` and `--finish` are mutually exclusive, and the gate's approval message lists all three.
- `status` reports `halted` and `finished_early`; the module help describes the gate decisions and the final gate.

## [1.2.0] - 2026-09-28

### Added
- `scripts/next_phase.py`, the host-driven chain engine the chain agents use: `start`, `advance` and `status`, one JSON envelope per phase, approval gates from `on_phase_complete: "hitl"`, `--failed`, `--skipped`, `--approve` and `--reject`, and `--query-file` / `--phase-output-file`. It never calls a model, so chains run on the host's subscription.
- SKILL.md and README sections on the engine.
- `tests/test_next_phase.py` (final approval gate: approve and reject) and log fallback tests for `orchestrate.py`.

### Changed
- `orchestrate.py` chain execution enforces mandatory phases: every phase logs a terminal status with its `phase_id`; a mandatory phase that fails, is blocked by the allowlist, or declares `CHAIN_PHASE_STATUS: not_applicable` stops the chain, while optional phases log `skipped` or `not_applicable` and the chain continues. A declined mid-chain approval gate also stops it. The `chain_completed` summary names the phase that stopped the chain, and the exit code is 1 whenever a mandatory phase stopped it. Mandatory enforcement is an `orchestrate.py` feature; `next_phase.py` records outcomes and leaves the decision to the agent.
- README rewritten around chain definitions, the allowlist and telemetry. SKILL.md version marker realigned (it said 1.0.0), and `metadata` carries author and version.

### Fixed
- A status the installed `dispatch_logger.py` does not accept (`blocked`, `not_applicable`, `skipped` on older versions) no longer drops the event: both scripts retry with a status the logger accepts and keep the precise one in the reason; `orchestrate.py` warns when an event is still rejected.
- `next_phase.py`: an approval gate after the last phase finished the chain at once, so `--reject` did nothing and the saved state kept `pending_hitl`. The gate now holds the chain open (this affects `test-lifecycle-skill`, whose final report is gated).
- `next_phase.py`: a rejected gate was summarised as a success; the summary now reports `halted: hitl_rejected` and logs the chain as failed.

## [1.1.0] - 2026-05-11

### Added
- Standardized badges to README.md.
- MIT License file.
- GitHub Actions validation workflow.
- Basic CHANGELOG.md.

### Fixed
- Improved telemetry event generation in `orchestrate.py`.

## [1.0.0] - 2026-04-20

### Added
- Initial release of `skill-orchestrator`.
- Core orchestration logic for HANDOFF and SEQUENCE decisions.
- Telemetry logging integration.
