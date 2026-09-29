#!/usr/bin/env python3
"""next_phase.py — host-driven chain execution state machine.

Unlike orchestrate.py (which calls the Anthropic API directly for every phase),
this script never calls a model. It composes a per-phase prompt envelope and
hands it back to the calling agent, which produces the phase output using its
own host model (Claude Code, OpenAI Codex, Gemini in VS Code, Antigravity, ...).

Result: chain execution bills to the host's subscription, not to ANTHROPIC_API_KEY.

Commands:
    next_phase.py start    --chain <chain_name> --query <text> [--chain-id <id>]
    next_phase.py advance  --chain-id <id> [--phase-output <text>] [--failed | --skipped]
    next_phase.py advance  --chain-id <id> --approve | --reject [--reason <text>] | --finish [--reason <text>]
    next_phase.py status   --chain-id <id>

Envelope returned by start/advance is JSON on stdout:
    {
      "done": false,
      "chain_id": "...",
      "phase_index": N,
      "phase_total": M,
      "skill": "codebase-context",
      "agent_handled": false,
      "system_prompt": "<SKILL.md text>",
      "user_prompt": "<role-reset preamble + query + context + constraint>",
      "awaiting_approval": false
    }

When the chain is finished, returned envelope has "done": true and a summary.
When a phase carries on_phase_complete=hitl and just completed, the next call
returns "awaiting_approval": true and refuses to advance until a decision:
    --approve   continue with the next phase (or finish, after the last one)
    --reject    halt the chain; the summary reports "halted": "hitl_rejected" and it counts as failed
    --finish    end the chain at the gate as a planned early finish, skipping the remaining phases;
                logged as a success unless an earlier phase failed; the summary reports
                "finished_early": "<reason>" (used by audit-only runs)
A gate after the last phase holds the chain open the same way; its envelope has "final_gate": true.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DISPATCHER_DATA = Path.home() / ".agents" / "dispatcher-data"
CHAIN_RUNS_DIR = DISPATCHER_DATA / "chain_runs"
SKILLS_DIR = Path.home() / ".agents" / "skills"
DISPATCHER_SCRIPTS = SKILLS_DIR / "skill-dispatcher" / "scripts"
CONTEXT_CLIP = 6000  # max chars of accumulated context passed into a phase prompt


# ---------- state ----------

def state_path(chain_id: str) -> Path:
    return CHAIN_RUNS_DIR / f"{chain_id}.json"


def load_state(chain_id: str) -> dict[str, Any]:
    p = state_path(chain_id)
    if not p.exists():
        sys.stderr.write(f"[!] no chain state for chain_id={chain_id} at {p}\n")
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    CHAIN_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(state["chain_id"])
    p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------- chain definition lookup ----------

def load_chain_definition(chain_name: str) -> dict[str, Any]:
    candidates = [
        SKILLS_DIR / chain_name / "config" / "chain_definition.json",
        Path(__file__).resolve().parent.parent.parent / chain_name / "config" / "chain_definition.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    sys.stderr.write(f"[!] no chain_definition.json found for chain '{chain_name}'\n")
    sys.exit(2)


def load_skill_md(skill_name: str) -> str:
    p = SKILLS_DIR / skill_name / "SKILL.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


# ---------- telemetry ----------

# Status to retry with when the installed dispatch_logger rejects the precise one (it may only know
# success/failed). None means: send no --phase-status, the precise status stays in the reason.
LOGGER_FALLBACK_STATUS = {"success": "success", "failed": "failed", "blocked": "failed",
                          "skipped": None, "not_applicable": None}


def log_event(
    decision: str,
    skill: str,
    intent: str,
    reason: str,
    chain_id: str,
    phase_status: str = "",
    skills_csv: str = "",
) -> None:
    """Fire a dispatch_logger event. Quiet on failure — telemetry is best-effort."""
    logger = DISPATCHER_SCRIPTS / "dispatch_logger.py"
    if not logger.exists():
        return
    cmd = [
        sys.executable, str(logger),
        "--skill", skill,
        "--intent", intent,
        "--decision", decision,
        "--reason", reason,
        "--chain-id", chain_id,
    ]
    effective_skills = skills_csv or (skill if decision == "SEQUENCE" else "")
    if effective_skills:
        cmd += ["--skills", effective_skills]
    env = {**os.environ, "SKILL_DISPATCH_DISABLE_WALLBOARD": "1", "SKILL_DISPATCH_CHAIN_ID": chain_id}
    attempts = [cmd + (["--phase-status", phase_status] if phase_status else [])]
    fallback = LOGGER_FALLBACK_STATUS.get(phase_status)
    if phase_status and fallback != phase_status:
        # Older dispatch_logger versions accept fewer statuses; keep the precise one in the reason.
        retry = [a if a != reason else reason + " phase_status=" + phase_status for a in cmd]
        attempts.append(retry + (["--phase-status", fallback] if fallback else []))
    for attempt in attempts:
        try:
            if subprocess.run(attempt, capture_output=True, check=False, env=env).returncode == 0:
                return
        except Exception:
            return


# ---------- envelope composition ----------

ROLE_RESET_PREAMBLE = (
    "=== CHAIN: {chain_name} | PHASE {idx}/{total}: {phase_name} ===\n\n"
    "FORGET all prior outputs and conversation context. For this turn you are "
    "acting exclusively as the '{skill}' sub-skill, fully defined by the SKILL.md "
    "in your system prompt. Do not impersonate other phases. Do not summarise the "
    "chain. Do not add meta-commentary.\n"
)

AGENT_HANDLED_PREAMBLE = (
    "=== CHAIN: {chain_name} | PHASE {idx}/{total}: {phase_name} ===\n\n"
    "This is an AGENT-HANDLED phase — there is no sub-skill SKILL.md for it. You "
    "(the host agent) perform the work yourself using your native tools (Bash, Edit, "
    "Write, Read, Grep, Glob, etc., as appropriate for this phase). The CHAIN "
    "CONSTRAINT block below specifies what to produce — follow it verbatim; the "
    "constraint defines the entire output of this phase. If no CHAIN CONSTRAINT is "
    "present, produce a brief artefact consistent with the phase name and the "
    "accumulated context from prior phases.\n"
)


def build_envelope_for_phase(state: dict[str, Any], chain_def: dict[str, Any]) -> dict[str, Any]:
    """Compose the envelope JSON the host model should consume for the current phase."""
    idx = state["current_phase_index"]
    phases = chain_def["phases"]
    chain_name = chain_def.get("chain_name", state["chain_name"])

    if idx >= len(phases):
        return finalize_chain(state, chain_def)

    phase = phases[idx]
    skill = phase.get("skill")
    phase_name = phase.get("name", f"phase_{idx + 1}")
    intent = phase.get("intent", "execute_chain_phase")
    constraint = phase.get("query_suffix", "")
    agent_handled = skill is None

    ctx = state.get("context_accumulator", "")
    if ctx and len(ctx) > CONTEXT_CLIP:
        ctx_clipped = ctx[-CONTEXT_CLIP:]
        ctx_note = f"\n[Context clipped; showing the last {CONTEXT_CLIP} of {len(ctx)} chars]\n"
        ctx_to_send = ctx_note + ctx_clipped
    else:
        ctx_to_send = ctx

    if agent_handled:
        preamble = AGENT_HANDLED_PREAMBLE.format(
            chain_name=chain_name, idx=idx + 1, total=len(phases), phase_name=phase_name,
        )
        system_prompt = ""
    else:
        preamble = ROLE_RESET_PREAMBLE.format(
            chain_name=chain_name, idx=idx + 1, total=len(phases),
            phase_name=phase_name, skill=skill,
        )
        system_prompt = load_skill_md(skill)

    user_prompt_parts = [
        preamble,
        "USER REQUEST:",
        state.get("query", ""),
        "",
        "CONTEXT FROM PRIOR CHAIN PHASES (most recent last):",
        ctx_to_send or "(none — this is the first context-producing phase)",
    ]
    if constraint:
        user_prompt_parts += ["", "[CHAIN CONSTRAINT]", constraint]
    user_prompt_parts += [
        "",
        "YOUR TASK FOR THIS PHASE:",
        f"Produce the output expected of the '{skill or 'agent-handled implementation'}' "
        f"step for the request above. The output you produce will be appended to the "
        f"chain context and passed to subsequent phases.",
    ]
    user_prompt = "\n".join(user_prompt_parts)

    return {
        "done": False,
        "chain_id": state["chain_id"],
        "chain_name": chain_name,
        "phase_index": idx,
        "phase_one_based": idx + 1,
        "phase_total": len(phases),
        "phase_name": phase_name,
        "skill": skill,
        "intent": intent,
        "agent_handled": agent_handled,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "constraint": constraint,
        "awaiting_approval": False,
    }


def _gate_envelope(state: dict[str, Any], chain_def: dict[str, Any], message: str) -> dict[str, Any]:
    """Envelope for a pending HITL gate. A gate after the last phase must not finalize the chain:
    the chain stays open until --approve or --reject."""
    phases = chain_def["phases"]
    if state["current_phase_index"] >= len(phases):
        envelope = {
            "done": False,
            "chain_id": state["chain_id"],
            "chain_name": chain_def.get("chain_name", state["chain_name"]),
            "phase_index": len(phases),
            "phase_total": len(phases),
            "final_gate": True,
            "skill": None,
            "agent_handled": True,
            "system_prompt": "",
            "user_prompt": "",
        }
    else:
        envelope = build_envelope_for_phase(state, chain_def)
    envelope["awaiting_approval"] = True
    envelope["approval_for_phase_index"] = state["current_phase_index"]
    envelope["approval_message"] = message
    return envelope


def finalize_chain(state: dict[str, Any], _chain_def: dict[str, Any]) -> dict[str, Any]:
    """Mark the chain complete, emit chain_completed log event, regen wallboard, return summary."""
    if state.get("completed_at"):
        # idempotent
        return _summary_envelope(state)

    started = datetime.fromisoformat(state["started_at"])
    duration_s = round((datetime.now() - started).total_seconds(), 2)
    state["completed_at"] = datetime.now().isoformat()
    state["duration_s"] = duration_s
    save_state(state)

    log_event(
        decision="SEQUENCE",
        skill=state["chain_name"],
        intent="chain_completed",
        reason=(
            f"chain_completed phases_defined={state['phases_defined']} "
            f"phases_executed={state['phases_executed']} "
            f"phases_failed={state['phases_failed']} "
            f"total_chars={state.get('total_output_chars', 0)} "
            f"duration_s={duration_s} "
            f"host_driven=true"
            + (f" halted={state['halted']}" if state.get("halted") else "")
            + (f" finished_early={state['finished_early']}" if state.get("finished_early") else "")
        ),
        chain_id=state["chain_id"],
        phase_status="success" if state["phases_failed"] == 0 and not state.get("halted") else "failed",
    )

    # Regenerate the wallboard once at chain end. Per-event regen is suppressed during
    # the chain run (via SKILL_DISPATCH_DISABLE_WALLBOARD=1) to avoid spawning N
    # regenerations for an N-phase chain — but we owe the user one refresh at the end.
    generator = DISPATCHER_SCRIPTS / "generate_wallboard.py"
    if generator.exists():
        try:
            subprocess.Popen(
                [sys.executable, str(generator)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    return _summary_envelope(state)


def _summary_envelope(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "done": True,
        "chain_id": state["chain_id"],
        "chain_name": state["chain_name"],
        "phases_defined": state["phases_defined"],
        "phases_executed": state["phases_executed"],
        "phases_failed": state["phases_failed"],
        "halted": state.get("halted", ""),
        "finished_early": state.get("finished_early", ""),
        "duration_s": state.get("duration_s"),
        "completed_at": state.get("completed_at"),
        "state_path": str(state_path(state["chain_id"])),
        "log_path": str(DISPATCHER_DATA / "logs" / "dispatch_events.jsonl"),
    }


# ---------- commands ----------

def cmd_start(args: argparse.Namespace) -> int:
    chain_def = load_chain_definition(args.chain)
    phases = chain_def["phases"]

    chain_id = args.chain_id or (
        os.environ.get("SKILL_DISPATCH_CHAIN_ID", "").strip()
        or f"{args.chain}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    )

    query = args.query
    if args.query_file:
        query = Path(args.query_file).read_text(encoding="utf-8")
    if not query:
        sys.stderr.write("[!] --query or --query-file is required for start\n")
        return 2

    if state_path(chain_id).exists():
        sys.stderr.write(
            f"[!] chain_id={chain_id} already has a state file at {state_path(chain_id)}. "
            f"Use 'status' to inspect or pass a different --chain-id.\n"
        )
        return 2

    state = {
        "chain_id": chain_id,
        "chain_name": args.chain,
        "started_at": datetime.now().isoformat(),
        "query": query,
        "current_phase_index": 0,
        "phases_defined": len(phases),
        "phases_executed": 0,
        "phases_failed": 0,
        "total_output_chars": 0,
        "history": [],
        "context_accumulator": "",
        "pending_hitl": False,
        "host": args.host or os.environ.get("SKILL_DISPATCH_HOST", "unknown"),
    }
    save_state(state)

    log_event(
        decision="SEQUENCE",
        skill=args.chain,
        intent="chain_initiated",
        reason=f"chain_initiated host_driven=true phases={len(phases)} host={state['host']}",
        chain_id=chain_id,
    )

    envelope = build_envelope_for_phase(state, chain_def)
    print(json.dumps(envelope, indent=2))
    return 0


def cmd_advance(args: argparse.Namespace) -> int:
    state = load_state(args.chain_id)
    chain_def = load_chain_definition(state["chain_name"])
    phases = chain_def["phases"]

    if state.get("completed_at"):
        if args.finish or args.reject:
            decision = "finish" if args.finish else "reject"
            sys.stderr.write(f"[!] The chain has already finished; there is no gate to {decision}.\n")
            return 2
        print(json.dumps(_summary_envelope(state), indent=2))
        return 0

    if args.finish and not state.get("pending_hitl"):
        sys.stderr.write("[!] --finish is only valid at a pending approval gate.\n")
        return 2

    # Resolve HITL approval requests first. These do NOT carry phase output —
    # the gate's whole point is to pause between phases, not to record work.
    if state.get("pending_hitl"):
        if args.finish:
            # A planned early end (e.g. an audit-only run): the chain stops at the gate as a success.
            reason = args.reason or "finished_at_gate"
            state["pending_hitl"] = False
            state["finished_early"] = reason
            state["current_phase_index"] = len(phases)
            save_state(state)
            log_event(
                decision="SEQUENCE", skill=state["chain_name"], intent="chain_finished_early",
                reason=f"finished_at_gate reason={reason}", chain_id=state["chain_id"],
            )
            print(json.dumps(finalize_chain(state, chain_def), indent=2))
            return 0
        if args.reject:
            reason = args.reason or "rejected_by_host"
            state["pending_hitl"] = False
            state["halted"] = "hitl_rejected"
            state["current_phase_index"] = len(phases)  # halt
            save_state(state)
            log_event(
                decision="SEQUENCE", skill=state["chain_name"], intent="chain_halted_hitl",
                reason=f"hitl_rejected reason={reason}", chain_id=state["chain_id"],
            )
            print(json.dumps(finalize_chain(state, chain_def), indent=2))
            return 0
        if not args.approve:
            envelope = _gate_envelope(state, chain_def, (
                "The previous phase requested human-in-the-loop approval before the chain proceeds. "
                "Re-invoke 'next_phase.py advance --chain-id <id> --approve' to continue, "
                "'--reject --reason <text>' to halt as failed, or '--finish --reason <text>' to end the "
                "chain here as a planned early finish."
            ))
            print(json.dumps(envelope, indent=2))
            return 0
        # --approve: clear the gate, log the start of the next phase, return its envelope,
        # and do NOT pass through the output-recording block (no output was supplied).
        state["pending_hitl"] = False
        save_state(state)
        if state["current_phase_index"] >= len(phases):
            print(json.dumps(finalize_chain(state, chain_def), indent=2))
            return 0
        next_phase = phases[state["current_phase_index"]]
        log_event(
            decision="HANDOFF",
            skill=next_phase.get("skill") or state["chain_name"],
            intent=next_phase.get("intent", "chain_phase"),
            reason=(
                f"chain_phase_start chain={state['chain_name']} "
                f"phase={state['current_phase_index'] + 1}/{len(phases)} "
                f"host_driven=true after_hitl_approve=true"
            ),
            chain_id=state["chain_id"],
        )
        print(json.dumps(build_envelope_for_phase(state, chain_def), indent=2))
        return 0

    # Normal advance path: caller must supply --phase-output (or --phase-output-file).
    if not (args.phase_output or args.phase_output_file):
        sys.stderr.write(
            "[!] advance requires --phase-output / --phase-output-file (or --approve / --reject / --finish at an approval gate).\n"
        )
        return 2

    # Record the just-finished phase's output
    just_finished_idx = state["current_phase_index"]
    output = args.phase_output or ""
    if args.phase_output_file:
        output = Path(args.phase_output_file).read_text(encoding="utf-8")

    prev_phase = phases[just_finished_idx]
    prev_skill = prev_phase.get("skill") or "(agent-handled)"
    prev_name = prev_phase.get("name", f"phase_{just_finished_idx + 1}")
    pass_forward = prev_phase.get("pass_context_forward", True)

    # Decide phase outcome. Skipped is for phases the host determined are not
    # applicable to this request (e.g. an E2E phase for a non-UI feature) — the
    # phase still ran (the host produced a justification artefact) but it does
    # NOT count as a failure and the wallboard renders it grey rather than green.
    if args.skipped:
        status = "skipped"
        state["phases_executed"] += 1
    else:
        success = bool(output.strip()) and not args.failed
        status = "success" if success else "failed"
        state["phases_executed"] += 1
        if not success:
            state["phases_failed"] += 1
    state["total_output_chars"] += len(output)
    state["history"].append({
        "phase_index": just_finished_idx,
        "phase_name": prev_name,
        "skill": prev_skill,
        "status": status,
        "output_chars": len(output),
        "completed_at": datetime.now().isoformat(),
    })

    if pass_forward and output.strip():
        snippet = output if len(output) <= 3000 else output[:3000] + f"\n... [{len(output) - 3000} chars truncated]"
        state["context_accumulator"] += (
            f"\n\n=== Phase {just_finished_idx + 1}: {prev_name} ({prev_skill}) ===\n{snippet}"
        )

    log_event(
        decision="HANDOFF",
        skill=prev_skill if prev_skill != "(agent-handled)" else state["chain_name"],
        intent=prev_phase.get("intent", "chain_phase"),
        reason=(
            f"chain_phase_complete chain={state['chain_name']} "
            f"phase={just_finished_idx + 1} output_chars={len(output)} host_driven=true"
        ),
        chain_id=state["chain_id"],
        phase_status=status,
    )

    # Check HITL gate after this phase.
    if prev_phase.get("on_phase_complete") == "hitl":
        state["pending_hitl"] = True
        state["current_phase_index"] = just_finished_idx + 1
        save_state(state)
        envelope = _gate_envelope(state, chain_def, (
            f"Phase {just_finished_idx + 1} ({prev_name}) completed and is marked for "
            f"human-in-the-loop review before the chain continues or finishes. Re-invoke "
            f"'next_phase.py advance --chain-id {state['chain_id']} --approve' to continue, "
            f"'--reject --reason <text>' to halt as failed, or '--finish --reason <text>' to end the "
            f"chain here as a planned early finish."
        ))
        print(json.dumps(envelope, indent=2))
        return 0

    state["current_phase_index"] = just_finished_idx + 1
    save_state(state)

    # Compose envelope for the next phase (or finalize)
    if state["current_phase_index"] >= len(phases):
        print(json.dumps(finalize_chain(state, chain_def), indent=2))
        return 0

    next_phase = phases[state["current_phase_index"]]
    log_event(
        decision="HANDOFF",
        skill=next_phase.get("skill") or state["chain_name"],
        intent=next_phase.get("intent", "chain_phase"),
        reason=(
            f"chain_phase_start chain={state['chain_name']} "
            f"phase={state['current_phase_index'] + 1}/{len(phases)} host_driven=true"
        ),
        chain_id=state["chain_id"],
    )
    envelope = build_envelope_for_phase(state, chain_def)
    print(json.dumps(envelope, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.chain_id)
    print(json.dumps({
        "chain_id": state["chain_id"],
        "chain_name": state["chain_name"],
        "started_at": state["started_at"],
        "completed_at": state.get("completed_at"),
        "current_phase_index": state["current_phase_index"],
        "phases_defined": state["phases_defined"],
        "phases_executed": state["phases_executed"],
        "phases_failed": state["phases_failed"],
        "total_output_chars": state.get("total_output_chars", 0),
        "pending_hitl": state.get("pending_hitl", False),
        "halted": state.get("halted", ""),
        "finished_early": state.get("finished_early", ""),
        "history": state.get("history", []),
        "state_path": str(state_path(state["chain_id"])),
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="Begin a new chain run and emit Phase 1's envelope.")
    s.add_argument("--chain", required=True, help="Chain name (e.g. new-feature-sdlc-skill).")
    s.add_argument("--query", default="", help="Feature description / user request.")
    s.add_argument("--query-file", help="Read the query from a file (use instead of --query).")
    s.add_argument("--chain-id", default="", help="Optional chain id. Auto-generated if omitted.")
    s.add_argument("--host", default="", help="Host identifier for telemetry (claude-code, codex, gemini, antigravity, ...).")
    s.set_defaults(func=cmd_start)

    a = sub.add_parser("advance", help="Record a phase's output and emit the next envelope (or summary).")
    a.add_argument("--chain-id", required=True)
    a.add_argument("--phase-output", default="", help="Text output the host produced for the current phase.")
    a.add_argument("--phase-output-file", help="Read the phase output from a file (use instead of --phase-output).")
    a.add_argument("--failed", action="store_true", help="Mark the just-finished phase as failed.")
    a.add_argument(
        "--skipped",
        action="store_true",
        help=(
            "Mark the just-finished phase as skipped (not applicable to this request). "
            "Still records the host's justification output; does NOT count as a failure; "
            "rendered grey on the wallboard."
        ),
    )
    gate = a.add_mutually_exclusive_group()  # one decision per gate
    gate.add_argument("--approve", action="store_true", help="Approve a pending HITL gate.")
    gate.add_argument("--reject", action="store_true", help="Reject a pending HITL gate and halt the chain (counts as failed).")
    gate.add_argument(
        "--finish",
        action="store_true",
        help=(
            "At a pending HITL gate: end the chain there as a planned early finish, without running the "
            "remaining phases (e.g. an audit-only run). It is logged as a success unless an earlier phase "
            "failed. Only valid at a gate."
        ),
    )
    a.add_argument("--reason", default="", help="Reason text for --reject or --finish.")
    a.set_defaults(func=cmd_advance)

    st = sub.add_parser("status", help="Inspect chain run state.")
    st.add_argument("--chain-id", required=True)
    st.set_defaults(func=cmd_status)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
