# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-09-28

### Added
- `scripts/next_phase.py`, the host-driven chain engine the chain agents use: `start`, `advance` and `status`, one JSON envelope per phase, approval gates from `on_phase_complete: "hitl"`, `--failed`, `--skipped`, `--approve` and `--reject`, and `--query-file` / `--phase-output-file`. It never calls a model, so chains run on the host's subscription.
- SKILL.md and README sections on the engine.

### Changed
- `orchestrate.py` chain execution enforces mandatory phases: every phase logs a terminal status with its `phase_id`; a mandatory phase that fails, is blocked by the allowlist, or declares `CHAIN_PHASE_STATUS: not_applicable` stops the chain, while optional phases log `skipped` or `not_applicable` and the chain continues. A declined mid-chain approval gate also stops it, and the summary event names the phase that stopped the chain.
- README rewritten around chain definitions, the allowlist and telemetry. SKILL.md version marker realigned (it said 1.0.0).

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
