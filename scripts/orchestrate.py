#!/usr/bin/env python3
"""orchestrate.py -- Execute a skill-dispatcher routing decision end-to-end.

Usage:
    python scripts/orchestrate.py --routing-decision '{"decision":"SEQUENCE",...}' [--dry-run]
    python scripts/orchestrate.py --query "review my UI" [--dry-run]
    python scripts/orchestrate.py --routing-decision '...' --model claude-haiku-4-5-20251001
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_API_TIMEOUT_SEC = 60
DEFAULT_MAX_TOKENS_PER_CHAIN = 200_000  # safety ceiling per chain


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent

# Locate the skill-dispatcher sibling
DISPATCHER_CANDIDATES = [
    SKILL_ROOT.parent / "skill-dispatcher",
    Path.home() / ".agents" / "skills" / "skill-dispatcher",
    Path.home() / ".claude" / "skills" / "skill-dispatcher",
]
DISPATCHER_ROOT = next((p for p in DISPATCHER_CANDIDATES if p.exists()), None)


def find_dispatcher_script(name: str) -> Path | None:
    if DISPATCHER_ROOT is None:
        return None
    p = DISPATCHER_ROOT / "scripts" / name
    return p if p.exists() else None


def load_allowlist() -> list[str]:
    if DISPATCHER_ROOT is None:
        return []
    path = DISPATCHER_ROOT / "config" / "executable_skills.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [s.strip() for s in data.get("allowed_skills", []) if s.strip()]
    except Exception:
        return []




def load_chain_definition(skill_name: str) -> "dict | None":
    """Load chain_definition.json for a chain-orchestrating skill, if it exists."""
    candidates = [
        Path.home() / ".agents" / "skills" / skill_name / "config" / "chain_definition.json",
        SKILL_ROOT.parent / skill_name / "config" / "chain_definition.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"  [!] Failed to load chain_definition for {skill_name}: {exc}", file=sys.stderr)
                return None
    return None


NOT_APPLICABLE_MARKER = "CHAIN_PHASE_STATUS: not_applicable"

# Status to retry with when the installed dispatch_logger rejects the precise one (it may only know
# success/failed). None means: send no --phase-status, the precise status stays in the reason.
LOGGER_FALLBACK_STATUS = {"success": "success", "failed": "failed", "blocked": "failed",
                          "skipped": None, "not_applicable": None}


def classify_phase_status(response_text: str, rc: int, mandatory: bool) -> str:
    """Terminal status of an invoked phase: success, failed or not_applicable.

    A phase may declare itself not applicable with NOT_APPLICABLE_MARKER in its output. That is a
    valid terminal status for an optional phase; a mandatory phase that does so has failed.
    """
    if rc != 0:
        return "failed"
    if NOT_APPLICABLE_MARKER.lower() in (response_text or "").lower():
        return "failed" if mandatory else "not_applicable"
    return "success"


def run_chain(
    chain_def: dict,
    routing_decision: dict,
    allowlist: list,
    chain_id: str,
    dry_run: bool,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_API_TIMEOUT_SEC,
    max_tokens_per_chain: int = DEFAULT_MAX_TOKENS_PER_CHAIN,
    auto_approve: bool = False,
) -> int:
    """Execute a multi-phase chain from chain_definition.json.

    Each phase invokes a sub-skill via the Anthropic API. Accumulated context
    from phases with pass_context_forward=True is prepended to the next phase query.
    All events share chain_id for traceability.
    """
    phases = chain_def.get("phases", [])
    chain_name = chain_def.get("chain_name", routing_decision.get("selected_skill", "unknown"))
    query = routing_decision.get("query", "")
    intent = routing_decision.get("intent", "execute_chain")
    context_accumulator = ""
    total_tokens = 0
    failed_phases = []

    print("\n[*] chain=" + chain_name + " phases=" + str(len(phases)) + " chain_id=" + chain_id)

    # Pin chain_id into process env so any indirect log-dispatch invocations
    # (e.g. sub-skills that shell out to log-dispatch.cmd themselves) inherit it.
    os.environ["SKILL_DISPATCH_CHAIN_ID"] = chain_id

    chain_started_at = datetime.now()
    phases_executed = 0

    # Log chain entry point so the trigger event and all phase events share chain_id in the wallboard.
    log_event(
        decision="SEQUENCE",
        skill=chain_name,
        intent=intent,
        reason="chain_initiated phases=" + str(len(phases)),
        chain_id=chain_id,
        dry_run=dry_run,
    )

    stop = None  # (phase_id, status) of the mandatory phase that stopped the chain
    for i, phase in enumerate(phases, 1):
        phase_skill = phase.get("skill")
        phase_id = phase.get("id") or ("phase_" + str(i))
        mandatory = phase.get("mandatory", True)
        phase_name = phase.get("name", "phase_" + str(i))
        phase_intent = phase.get("intent", intent)
        phase_reason = phase.get("reason", "chain_phase_" + str(i))
        pass_forward = phase.get("pass_context_forward", True)

        print("\n  [" + str(i) + "/" + str(len(phases)) + "] " + phase_name)

        phase_status = "skipped"

        if not phase_skill:
            print("    -> agent-handled (no sub-skill defined)")
            log_event(
                decision="HANDOFF",
                skill=chain_name,
                intent=phase_intent,
                reason="chain=" + chain_name + " phase=" + str(i) + " agent_handled",
                chain_id=chain_id,
                phase_status="success",
                dry_run=dry_run,
                phase_id=phase_id,
            )
            phases_executed += 1
            phase_status = "success"
        elif phase_skill not in allowlist:
            print("    -> BLOCKED: '" + phase_skill + "' not in allowlist — add to executable_skills.json")
            phase_status = "blocked" if mandatory else "skipped"
            log_event(
                decision="HANDOFF",
                skill=phase_skill,
                intent=phase_intent,
                reason="chain=" + chain_name + " phase=" + str(i) + " not_in_allowlist",
                chain_id=chain_id,
                phase_status=phase_status,
                dry_run=dry_run,
                phase_id=phase_id,
            )
            if mandatory:
                failed_phases.append(phase_skill)
                stop = (phase_id, "blocked")
                print("    [!] Mandatory phase blocked — stopping the chain.")
                break
            continue
        else:
            print("    -> " + phase_skill)

            # Build phase query: original query + context accumulated from prior phases
            if context_accumulator:
                phase_query = query + "\n\n---\nContext from previous chain phases:\n" + context_accumulator
            else:
                phase_query = query
            # Append phase-level constraints so the sub-skill receives them in its prompt.
            query_suffix = phase.get("query_suffix", "")
            if query_suffix:
                phase_query = phase_query + "\n\n[CHAIN CONSTRAINT]\n" + query_suffix

            log_event(
                decision="HANDOFF",
                skill=phase_skill,
                intent=phase_intent,
                reason="chain=" + chain_name + " phase=" + str(i) + "/" + str(len(phases)) + " " + phase_reason,
                chain_id=chain_id,
                dry_run=dry_run,
                phase_id=phase_id,
            )

            response_text, rc, tokens = _invoke_skill_via_api(
                skill_name=phase_skill,
                query=phase_query,
                context="",
                model=model,
                dry_run=dry_run,
                timeout_sec=timeout_sec,
                tokens_used_so_far=total_tokens,
                max_tokens_per_chain=max_tokens_per_chain,
            )
            total_tokens += tokens
            phase_status = classify_phase_status(response_text, rc, mandatory)

            log_event(
                decision="HANDOFF",
                skill=phase_skill,
                intent=phase_intent,
                reason="chain_phase_complete chain=" + chain_name + " phase=" + str(i) + " tokens=" + str(tokens) + " rc=" + str(rc),
                chain_id=chain_id,
                phase_status=phase_status,
                dry_run=dry_run,
                phase_id=phase_id,
            )
            phases_executed += 1

            if phase_status == "failed":
                failed_phases.append(phase_skill)
                if mandatory:
                    print("    [!] Mandatory phase " + str(i) + " (" + phase_skill + ") failed (rc=" + str(rc) + ") — stopping the chain")
                    stop = (phase_id, "failed")
                    break
                print("    [!] Optional phase " + str(i) + " (" + phase_skill + ") failed (rc=" + str(rc) + ") — continuing")
            elif phase_status == "not_applicable":
                print("    -> not applicable (optional) — continuing")
            elif pass_forward and response_text:
                snippet = response_text[:3000]
                context_accumulator += "\n\n=== Phase " + str(i) + ": " + phase_name + " (" + phase_skill + ") ===\n" + snippet
                if len(response_text) > 3000:
                    context_accumulator += "\n... [" + str(len(response_text) - 3000) + " chars truncated]"

        # Mid-chain HITL gate: pause if this phase declared on_phase_complete: "hitl"
        # and it succeeded. Skipped on dry-run or when auto-approve is on.
        if (
            phase.get("on_phase_complete") == "hitl"
            and phase_status == "success"
            and not dry_run
            and not auto_approve
        ):
            approved = _mid_chain_hitl_gate(
                chain_name=chain_name,
                phase_num=i,
                total_phases=len(phases),
                phase_name=phase_name,
                hitl_note=phase.get("_hitl_note", ""),
                chain_id=chain_id,
            )
            log_event(
                decision="SEQUENCE",
                skill=chain_name,
                intent="hitl_gate",
                reason=(
                    "hitl_gate chain=" + chain_name
                    + " phase=" + str(i) + "/" + str(len(phases))
                    + " decision=" + ("approved" if approved else "declined")
                ),
                chain_id=chain_id,
                phase_status="success" if approved else "failed",
                dry_run=dry_run,
            )
            if not approved:
                print("  [!] HITL gate at phase " + str(i) + " declined — aborting chain.")
                failed_phases.append("hitl_gate@" + str(i))
                stop = (phase_id, "failed")
                break

    duration_s = round((datetime.now() - chain_started_at).total_seconds(), 2)
    phases_defined = len(phases)
    phases_failed = len(failed_phases)

    print("\n[*] Chain complete: " + str(total_tokens) + " tokens across "
          + str(phases_executed) + "/" + str(phases_defined) + " phases ("
          + str(duration_s) + "s)")
    if failed_phases:
        print("    [!] Failed phases: " + str(failed_phases))

    # Emit a single chain_completed summary event so partial runs are visible from the log alone.
    # Without this, the only way to detect "chain ran 7 of 9 phases" is to manually count chain_id rows.
    log_event(
        decision="SEQUENCE",
        skill=chain_name,
        intent="chain_completed",
        reason=(
            "chain_completed phases_defined=" + str(phases_defined)
            + " phases_executed=" + str(phases_executed)
            + " phases_failed=" + str(phases_failed)
            + " total_tokens=" + str(total_tokens)
            + " duration_s=" + str(duration_s)
            + (" stopped_by=" + stop[0] if stop else "")
        ),
        chain_id=chain_id,
        # When a mandatory phase stopped the chain, the summary carries that phase's id and status.
        phase_status=stop[1] if stop else ("success" if phases_failed == 0 else "failed"),
        dry_run=dry_run,
        phase_id=stop[0] if stop else "",
    )

    return 1 if stop or phases_failed == phases_defined else 0



def _find_skill_md(skill_name: str) -> Path | None:
    """Locate SKILL.md for a given skill name across common install locations."""
    candidates = []
    # ~/.agents/skills/<name>/SKILL.md  (runtime -- canonical live location)
    candidates.append(Path.home() / ".agents" / "skills" / skill_name / "SKILL.md")
    # sibling of skill-orchestrator in source tree
    candidates.append(SKILL_ROOT.parent / skill_name / "SKILL.md")
    # ~/.claude/skills/<name>/SKILL.md
    candidates.append(Path.home() / ".claude" / "skills" / skill_name / "SKILL.md")
    return next((p for p in candidates if p.exists()), None)


def _lookup_preferred_model(skill_name: str) -> str:
    """Read dispatcher-preferred-model for a skill from the live registry.

    Returns "" if the registry can't be located or the skill has no preference.
    """
    candidates = [
        Path.home() / ".agents" / "dispatcher-data" / "registry" / "SKILL_REGISTRY.json",
        SKILL_ROOT.parent / "skill-dispatcher" / "registry" / "SKILL_REGISTRY.json",
    ]
    env_override = os.environ.get("SKILL_DISPATCH_REGISTRY")
    if env_override:
        candidates.insert(0, Path(env_override))

    registry_path = next((p for p in candidates if p.exists()), None)
    if not registry_path:
        return ""

    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    skills_raw = payload.get("skills", {})
    if isinstance(skills_raw, list):
        skill_data = next((s for s in skills_raw if isinstance(s, dict) and s.get("name") == skill_name), None)
    elif isinstance(skills_raw, dict):
        skill_data = skills_raw.get(skill_name)
    else:
        skill_data = None

    if not isinstance(skill_data, dict):
        return ""
    pm = skill_data.get("preferred_model", "")
    return pm.strip() if isinstance(pm, str) else ""


def _invoke_skill_via_api(
    skill_name: str,
    query: str,
    context: str,
    model: str,
    dry_run: bool,
    timeout_sec: int = DEFAULT_API_TIMEOUT_SEC,
    tokens_used_so_far: int = 0,
    max_tokens_per_chain: int = DEFAULT_MAX_TOKENS_PER_CHAIN,
) -> tuple[str, int, int]:
    """Call the Anthropic API with the skill's SKILL.md as the system prompt.

    Uses prompt caching on the system prompt to reduce cost when the same
    skill is invoked multiple times in a session.

    Returns (response_text, exit_code, tokens_used).
    """
    skill_md_path = _find_skill_md(skill_name)
    if skill_md_path is None:
        msg = f"  [!] Cannot invoke '{skill_name}': SKILL.md not found in any search path."
        print(msg, file=sys.stderr)
        return msg, 1, 0

    system_prompt = skill_md_path.read_text(encoding="utf-8")
    user_message = query
    if context:
        user_message = f"Context from Phase 0:\n{context}\n\n---\n\n{query}"

    # Cost budget circuit breaker (rough estimate from input chars)
    estimated_input_tokens = (len(system_prompt) + len(user_message)) // 4
    if tokens_used_so_far + estimated_input_tokens > max_tokens_per_chain:
        msg = (
            f"  [!] Chain budget exceeded: {tokens_used_so_far} tokens used, "
            f"~{estimated_input_tokens} estimated for this call, limit {max_tokens_per_chain}."
        )
        print(msg, file=sys.stderr)
        return msg, 1, 0

    if dry_run:
        print(f"  [dry-run] Would call Anthropic API:")
        print(f"    model     : {model}")
        print(f"    system    : {skill_md_path} ({len(system_prompt)} chars, cached)")
        print(f"    user      : {user_message[:120]}{'...' if len(user_message) > 120 else ''}")
        print(f"    timeout   : {timeout_sec}s")
        print(f"    budget    : {tokens_used_so_far}/{max_tokens_per_chain} tokens used so far")
        return "[dry-run: no response]", 0, 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        msg = "  [!] ANTHROPIC_API_KEY not set -- cannot invoke skill via API."
        print(msg, file=sys.stderr)
        return msg, 1, 0

    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        msg = "  [!] anthropic SDK not installed. Run: pip install anthropic"
        print(msg, file=sys.stderr)
        return msg, 1, 0

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout_sec)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text if response.content else ""
        cache_status = getattr(response.usage, "cache_creation_input_tokens", 0)
        cache_hits = getattr(response.usage, "cache_read_input_tokens", 0)
        in_tokens = response.usage.input_tokens
        out_tokens = response.usage.output_tokens
        total_tokens = in_tokens + out_tokens
        print(f"  [Phase 1] Response received ({len(text)} chars). "
              f"Tokens in/out: {in_tokens}/{out_tokens}. "
              f"Cache: created={cache_status} hits={cache_hits}")
        return text, 0, total_tokens
    except anthropic.APITimeoutError:
        msg = f"  [!] API call timed out after {timeout_sec}s."
        print(msg, file=sys.stderr)
        return msg, 1, 0
    except anthropic.APIStatusError as exc:
        msg = f"  [!] API error {exc.status_code}: {exc.message}"
        print(msg, file=sys.stderr)
        return msg, 1, 0
    except Exception as exc:
        msg = f"  [!] Unexpected error calling API: {exc}"
        print(msg, file=sys.stderr)
        return msg, 1, 0


def log_event(
    decision: str,
    skill: str,
    intent: str,
    reason: str,
    chain_id: str,
    target: str = "",
    phase_status: str = "",
    skills: str = "",
    dry_run: bool = False,
    phase_id: str = "",
) -> None:
    if phase_id:  # dispatch_logger has no phase-id field; keep it in the reason
        reason = reason + " phase_id=" + phase_id
    logger = find_dispatcher_script("dispatch_logger.py")
    if logger is None:
        print(f"  [~] No logger found -- skipping event: {decision} {skill}")
        return

    cmd = [
        sys.executable, str(logger),
        "--skill", skill,
        "--intent", intent,
        "--decision", decision,
        "--reason", reason,
        "--model", os.environ.get("SKILL_DISPATCH_MODEL", "unknown"),
        "--chain-id", chain_id,
    ]
    if target:
        cmd += ["--target", target]
    # dispatch_logger requires --skills for SEQUENCE decisions. Default to the
    # primary skill so chain_initiated / chain_completed events can be written.
    effective_skills = skills or (skill if decision == "SEQUENCE" else "")
    if effective_skills:
        cmd += ["--skills", effective_skills]

    attempts = [cmd + (["--phase-status", phase_status] if phase_status else [])]
    fallback = LOGGER_FALLBACK_STATUS.get(phase_status)
    if phase_status and fallback != phase_status:
        # Older dispatch_logger versions accept fewer statuses; keep the precise one in the reason.
        retry = [a if a != reason else reason + " phase_status=" + phase_status for a in cmd]
        attempts.append(retry + (["--phase-status", fallback] if fallback else []))

    if dry_run:
        print(f"  [dry-run] log: {' '.join(attempts[0])}")
        return

    env = {**os.environ, "SKILL_DISPATCH_DISABLE_WALLBOARD": "1", "SKILL_DISPATCH_CHAIN_ID": chain_id}
    try:
        for attempt in attempts:
            result = subprocess.run(attempt, capture_output=True, check=False, env=env)
            if result.returncode == 0:
                return
        print(f"  [!] log_event: dispatch_logger rejected the event (rc={result.returncode}): {decision} {skill}",
              file=sys.stderr)
    except Exception as exc:
        print(f"  [!] log_event failed: {exc}", file=sys.stderr)


def run_bootstrap(dry_run: bool) -> dict:
    bootstrap = find_dispatcher_script("dispatch_bootstrap.py")
    if bootstrap is None:
        print("[~] Bootstrap not found -- skipping policy lookup.")
        return {}

    if dry_run:
        print(f"[dry-run] bootstrap: python {bootstrap} --topic RoutingPolicies --format json")
        return {"policy_lookup": {"status": "skipped", "source": "none", "hit_count": 0}}

    result = subprocess.run(
        [sys.executable, str(bootstrap), "--topic", "RoutingPolicies", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("[~] Bootstrap returned no data -- continuing without policy context.")
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _hitl_approval_gate(skill_name: str, query: str, chain_id: str) -> bool:
    """Prompt for human approval before invoking a high-risk skill.

    Honors the SKILL_ORCHESTRATOR_AUTO_APPROVE env var for non-interactive runs.
    Returns True if approved, False otherwise.
    """
    if os.environ.get("SKILL_ORCHESTRATOR_AUTO_APPROVE", "").lower() in {"1", "true", "yes"}:
        print(f"  [HITL] auto-approve env var set -- proceeding with '{skill_name}'.")
        return True

    if not sys.stdin.isatty():
        print(
            f"  [HITL] non-interactive shell and no auto-approve env var set -- "
            f"declining '{skill_name}' (risk: high, chain={chain_id}).",
            file=sys.stderr,
        )
        return False

    print()
    print(f"  +-- HITL approval required --+")
    print(f"    Skill   : {skill_name}")
    print(f"    Risk    : high")
    print(f"    Chain   : {chain_id}")
    print(f"    Query   : {query[:100]}{'...' if len(query) > 100 else ''}")
    print(f"  +----------------------------+")
    try:
        answer = input("  Proceed with execution? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _mid_chain_hitl_gate(
    chain_name: str,
    phase_num: int,
    total_phases: int,
    phase_name: str,
    hitl_note: str,
    chain_id: str,
) -> bool:
    """Prompt for human approval after a phase declared on_phase_complete: 'hitl'.

    Honors SKILL_ORCHESTRATOR_AUTO_APPROVE for non-interactive runs.
    Returns True to continue the chain, False to abort.
    """
    if os.environ.get("SKILL_ORCHESTRATOR_AUTO_APPROVE", "").lower() in {"1", "true", "yes"}:
        print(f"  [HITL] auto-approve env set -- continuing past phase {phase_num} gate.")
        return True

    if not sys.stdin.isatty():
        print(
            f"  [HITL] non-interactive shell, no auto-approve env var set -- "
            f"declining mid-chain gate at phase {phase_num}/{total_phases} "
            f"({phase_name}). Chain will abort.",
            file=sys.stderr,
        )
        return False

    print()
    print(f"  +-- HITL gate: phase {phase_num}/{total_phases} complete --+")
    print(f"    Chain   : {chain_name}")
    print(f"    Phase   : {phase_name}")
    if hitl_note:
        print(f"    Note    : {hitl_note}")
    print(f"    ChainId : {chain_id}")
    print(f"  +------------------------------------------------+")
    try:
        answer = input("  Approve and continue to next phase? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _read_portfolio_file(skill_name: str, target: str) -> str:
    """Read a portfolio file from a context skill's directory.

    Search order: ~/.agents/skills/<skill>/my-portfolio/<target>,
    then <skill>/<target>. Returns empty string if not found.
    """
    candidates = [
        Path.home() / ".agents" / "skills" / skill_name / "my-portfolio" / target,
        Path.home() / ".agents" / "skills" / skill_name / target,
        SKILL_ROOT.parent / skill_name / "my-portfolio" / target,
        SKILL_ROOT.parent / skill_name / target,
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except Exception as exc:
                print(f"  [!] Phase 0: failed to read {path}: {exc}", file=sys.stderr)
                return ""
    return ""


def phase_zero(routing_decision: dict, chain_id: str, dry_run: bool) -> dict:
    """Invoke a Phase 0 context skill: read the portfolio file and emit telemetry."""
    decision = routing_decision.get("decision", "HANDOFF")
    risk = routing_decision.get("risk", "low")

    # Phase 0 is required for SEQUENCE decisions and high-risk HANDOFFs (per §12)
    needs_phase0 = decision == "SEQUENCE" or risk == "high"
    if not needs_phase0:
        return {}

    # Default Phase 0 skill is personal-context-portfolio
    context_skill = routing_decision.get("phase0_skill", "personal-context-portfolio")
    target = routing_decision.get("phase0_target", "identity.md")

    print(f"  [Phase 0] Context load: {context_skill} -> {target}")
    log_event(
        decision="CONTEXT_LOAD",
        skill=context_skill,
        intent="load_personal_context",
        reason="phase=0 context_load",
        chain_id=chain_id,
        target=target,
        dry_run=dry_run,
    )

    if dry_run:
        return {"phase0_skill": context_skill, "target": target, "artifact": f"[dry-run: would read {target}]"}

    content = _read_portfolio_file(context_skill, target)
    if not content:
        print(f"  [~] Phase 0: '{target}' not found in {context_skill}; continuing without context.")
        return {"phase0_skill": context_skill, "target": target, "artifact": ""}

    print(f"  [Phase 0] Loaded {len(content)} chars from {target}")
    return {
        "phase0_skill": context_skill,
        "target": target,
        "artifact": content,
        "artifact_chars": len(content),
    }


def phase_one(
    routing_decision: dict,
    phase0_artifact: dict,
    allowlist: list[str],
    chain_id: str,
    dry_run: bool,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_API_TIMEOUT_SEC,
    tokens_used_so_far: int = 0,
    max_tokens_per_chain: int = DEFAULT_MAX_TOKENS_PER_CHAIN,
    auto_approve: bool = False,
) -> tuple[str, int, int]:
    """Invoke the selected specialist skill via the Anthropic API.

    Returns (response_text, exit_code, tokens_used).
    """
    selected = routing_decision.get("selected_skill", "none")
    decision = routing_decision.get("decision", "HANDOFF")
    intent = routing_decision.get("intent", "execute_routing_decision")
    query = routing_decision.get("query", "")
    risk = routing_decision.get("risk", "low")

    if selected == "none":
        print("  [!] No skill selected in routing decision -- nothing to execute.")
        return "", 1, 0

    if selected not in allowlist:
        print(
            f"  [!] Execution blocked: '{selected}' is not in the allowlist.\n"
            f"      Add it to skill-dispatcher/config/executable_skills.json.",
            file=sys.stderr,
        )
        return "", 1, 0

    # HITL checkpoint for high-risk skills
    if risk == "high" and not dry_run and not auto_approve:
        if not _hitl_approval_gate(selected, query, chain_id):
            print(f"  [!] HITL: human declined approval for '{selected}'. Aborting Phase 1.")
            return "", 1, 0

    # Resolve effective model: explicit override wins, then registry preferred-model, then default
    effective_model = model
    user_explicit_override = (model != DEFAULT_MODEL)
    if not user_explicit_override:
        preferred = _lookup_preferred_model(selected)
        if preferred:
            effective_model = preferred
            print(f"  [Phase 1] Using preferred model from registry: {preferred} (skill={selected})")

    reason = f"phase=1 specialist={selected} risk={risk}"
    context = ""
    if phase0_artifact:
        context = phase0_artifact.get("artifact", "")
        reason += f" context_from={phase0_artifact.get('phase0_skill', '')}"

    print(f"  [Phase 1] Invoking specialist: {selected} via Anthropic API (model={effective_model})")
    log_event(
        decision=decision,
        skill=selected,
        intent=intent,
        reason=reason,
        chain_id=chain_id,
        dry_run=dry_run,
    )

    response_text, rc, tokens = _invoke_skill_via_api(
        skill_name=selected,
        query=query,
        context=context,
        model=effective_model,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
        tokens_used_so_far=tokens_used_so_far,
        max_tokens_per_chain=max_tokens_per_chain,
    )

    # Emit a phase-completion event so the wallboard can compute per-skill failure rate
    log_event(
        decision=decision,
        skill=selected,
        intent=intent,
        reason=f"phase=1 complete tokens={tokens} rc={rc}",
        chain_id=chain_id,
        phase_status="success" if rc == 0 else "failed",
        dry_run=dry_run,
    )
    return response_text, rc, tokens


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a routing decision end-to-end.")
    parser.add_argument(
        "--routing-decision",
        help="JSON string with the routing decision (from dispatch_cli --decide).",
    )
    parser.add_argument(
        "--query",
        help="Natural-language query; runs bootstrap + trivial decision if no --routing-decision provided.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("SKILL_DISPATCH_MODEL", DEFAULT_MODEL),
        help=f"Anthropic model to use for skill invocation (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=DEFAULT_API_TIMEOUT_SEC,
        help=f"Anthropic API call timeout in seconds (default: {DEFAULT_API_TIMEOUT_SEC}).",
    )
    parser.add_argument(
        "--max-tokens-per-chain",
        type=int,
        default=DEFAULT_MAX_TOKENS_PER_CHAIN,
        help=f"Cost circuit breaker: abort chain if estimated tokens exceed this (default: {DEFAULT_MAX_TOKENS_PER_CHAIN}).",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the HITL prompt for risk:high skills (use in CI / non-interactive contexts).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without executing anything.",
    )
    args = parser.parse_args()

    if not args.routing_decision and not args.query:
        parser.error("Provide --routing-decision JSON or --query.")

    # Inherit chain_id from SKILL_DISPATCH_CHAIN_ID env when present so a caller
    # (subagent, wrapper script, scheduled job) can tag a known id onto the run.
    chain_id = os.environ.get("SKILL_DISPATCH_CHAIN_ID", "").strip() or str(uuid.uuid4())[:8]
    print(f"[*] skill-orchestrator | chain_id={chain_id}")

    # Step 1: Bootstrap
    print("[1] Bootstrap policy context")
    bootstrap_payload = run_bootstrap(args.dry_run)

    # Step 2: Parse or build routing decision
    print("[2] Routing decision")
    if args.routing_decision:
        try:
            routing_decision = json.loads(args.routing_decision)
        except json.JSONDecodeError as exc:
            print(f"[!] Invalid --routing-decision JSON: {exc}", file=sys.stderr)
            return 1
    else:
        # Minimal stub decision from query alone
        policy_status = bootstrap_payload.get("policy_lookup", {}).get("status", "miss")
        routing_decision = {
            "decision": "HANDOFF",
            "selected_skill": "none",
            "query": args.query,
            "policy_context": policy_status,
        }
    print(f"    decision={routing_decision.get('decision')} skill={routing_decision.get('selected_skill')}")

    # Load allowlist
    allowlist = load_allowlist()

    # Step 3: Detect chain-skill.
    selected_skill = routing_decision.get("selected_skill", "none")
    print(f"[3] Chain detection for '{selected_skill}'")
    chain_def = load_chain_definition(selected_skill)
    if chain_def:
        n_phases = len(chain_def.get("phases", []))
        print(f"    chain found: {chain_def.get('chain_name')} — {n_phases} phases")
        rc = run_chain(
            chain_def=chain_def,
            routing_decision=routing_decision,
            allowlist=allowlist,
            chain_id=chain_id,
            dry_run=args.dry_run,
            model=args.model,
            timeout_sec=args.api_timeout,
            max_tokens_per_chain=args.max_tokens_per_chain,
            auto_approve=args.auto_approve,
        )
        print(json.dumps({"chain_id": chain_id, "chain_name": chain_def.get("chain_name"), "exit_code": rc}, indent=2))
        return rc
    print("    no chain definition — running Phase 0 + Phase 1")

    # Step 4: Phase 0 (context load if required)
    print("[4] Phase 0 -- context load")
    phase0_artifact = phase_zero(routing_decision, chain_id, args.dry_run)
    if not phase0_artifact:
        print("    (skipped -- HANDOFF with low risk)")

    # Step 4: Phase 1 -- specialist execution
    print("[5] Phase 1 -- specialist")
    response_text, rc, tokens_used = phase_one(
        routing_decision,
        phase0_artifact,
        allowlist,
        chain_id,
        args.dry_run,
        model=args.model,
        timeout_sec=args.api_timeout,
        tokens_used_so_far=0,
        max_tokens_per_chain=args.max_tokens_per_chain,
        auto_approve=args.auto_approve,
    )

    # Step 5: Summary
    chain_log = {
        "chain_id": chain_id,
        "decision": routing_decision.get("decision"),
        "selected_skill": routing_decision.get("selected_skill"),
        "model": args.model,
        "phase0": {k: v for k, v in (phase0_artifact or {}).items() if k != "artifact"} or None,
        "phase1_response_chars": len(response_text) if response_text else 0,
        "phase1_exit_code": rc,
        "tokens_used": tokens_used,
        "max_tokens_per_chain": args.max_tokens_per_chain,
        "dry_run": args.dry_run,
    }
    print("\n[5] Chain summary:")
    print(json.dumps(chain_log, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
