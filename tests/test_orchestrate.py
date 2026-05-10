"""
test_orchestrate.py — Tests for skill-orchestrator orchestrate.py.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import orchestrate


class TestLoadAllowlist(unittest.TestCase):
    def test_empty_when_no_dispatcher(self):
        with patch.object(orchestrate, "DISPATCHER_ROOT", None):
            assert orchestrate.load_allowlist() == []

    def test_returns_allowed_skills(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            allowlist_path = config_dir / "executable_skills.json"
            allowlist_path.write_text(
                json.dumps({"allowed_skills": ["my-skill", "other-skill"]}),
                encoding="utf-8",
            )
            with patch.object(orchestrate, "DISPATCHER_ROOT", Path(tmp)):
                result = orchestrate.load_allowlist()
            assert result == ["my-skill", "other-skill"]


class TestPhaseZero(unittest.TestCase):
    def test_skipped_for_low_risk_handoff(self):
        routing_decision = {"decision": "HANDOFF", "risk": "low"}
        with patch.object(orchestrate, "log_event") as mock_log:
            artifact = orchestrate.phase_zero(routing_decision, chain_id="abc", dry_run=True)
        assert artifact == {}
        mock_log.assert_not_called()

    def test_runs_for_sequence(self):
        routing_decision = {"decision": "SEQUENCE", "risk": "low"}
        with patch.object(orchestrate, "log_event") as mock_log:
            artifact = orchestrate.phase_zero(routing_decision, chain_id="abc", dry_run=True)
        assert artifact != {}
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        assert call_kwargs.kwargs["decision"] == "CONTEXT_LOAD"

    def test_runs_for_high_risk_handoff(self):
        routing_decision = {"decision": "HANDOFF", "risk": "high"}
        with patch.object(orchestrate, "log_event") as mock_log:
            artifact = orchestrate.phase_zero(routing_decision, chain_id="abc", dry_run=True)
        assert artifact != {}
        mock_log.assert_called_once()


class TestPhaseOne(unittest.TestCase):
    def test_blocked_when_skill_not_in_allowlist(self):
        routing_decision = {"decision": "HANDOFF", "selected_skill": "some-skill", "intent": "test"}
        _, rc, tokens = orchestrate.phase_one(routing_decision, {}, allowlist=[], chain_id="abc", dry_run=False)
        assert rc == 1
        assert tokens == 0

    def test_allowed_skill_dry_run_succeeds(self):
        routing_decision = {"decision": "HANDOFF", "selected_skill": "my-skill", "intent": "test", "query": "test query"}
        with patch.object(orchestrate, "log_event"):
            with patch.object(orchestrate, "_invoke_skill_via_api", return_value=("[dry-run]", 0, 0)):
                _, rc, _tokens = orchestrate.phase_one(routing_decision, {}, allowlist=["my-skill"], chain_id="abc", dry_run=True)
        assert rc == 0

    def test_no_skill_selected_returns_error(self):
        routing_decision = {"decision": "HANDOFF", "selected_skill": "none"}
        _, rc, _tokens = orchestrate.phase_one(routing_decision, {}, allowlist=["my-skill"], chain_id="abc", dry_run=False)
        assert rc == 1

    def test_high_risk_skill_blocked_when_hitl_declines(self):
        """High-risk skill should not invoke API when HITL gate returns False."""
        routing_decision = {"decision": "HANDOFF", "selected_skill": "my-skill", "intent": "test", "risk": "high", "query": "x"}
        with patch.object(orchestrate, "log_event"):
            with patch.object(orchestrate, "_hitl_approval_gate", return_value=False):
                with patch.object(orchestrate, "_invoke_skill_via_api") as mock_api:
                    _, rc, _tokens = orchestrate.phase_one(
                        routing_decision, {}, allowlist=["my-skill"], chain_id="abc",
                        dry_run=False, auto_approve=False,
                    )
        assert rc == 1
        mock_api.assert_not_called()

    def test_preferred_model_used_when_user_did_not_override(self):
        """Registry preferred_model wins when --model defaults to DEFAULT_MODEL."""
        routing_decision = {"decision": "HANDOFF", "selected_skill": "my-skill", "intent": "test", "query": "x"}
        captured_model = []
        def fake_api(skill_name, query, context, model, dry_run, **kwargs):
            captured_model.append(model)
            return ("ok", 0, 50)
        with patch.object(orchestrate, "log_event"):
            with patch.object(orchestrate, "_lookup_preferred_model", return_value="claude-haiku-4-5-20251001"):
                with patch.object(orchestrate, "_invoke_skill_via_api", side_effect=fake_api):
                    orchestrate.phase_one(
                        routing_decision, {}, allowlist=["my-skill"], chain_id="abc",
                        dry_run=False, model=orchestrate.DEFAULT_MODEL,
                    )
        assert captured_model == ["claude-haiku-4-5-20251001"]

    def test_user_explicit_model_overrides_registry_preferred(self):
        """When user passes a non-default --model, registry preferred_model is ignored."""
        routing_decision = {"decision": "HANDOFF", "selected_skill": "my-skill", "intent": "test", "query": "x"}
        captured_model = []
        def fake_api(skill_name, query, context, model, dry_run, **kwargs):
            captured_model.append(model)
            return ("ok", 0, 50)
        with patch.object(orchestrate, "log_event"):
            with patch.object(orchestrate, "_lookup_preferred_model", return_value="claude-haiku-4-5-20251001"):
                with patch.object(orchestrate, "_invoke_skill_via_api", side_effect=fake_api):
                    orchestrate.phase_one(
                        routing_decision, {}, allowlist=["my-skill"], chain_id="abc",
                        dry_run=False, model="claude-opus-4-7",
                    )
        assert captured_model == ["claude-opus-4-7"]

    def test_high_risk_skill_proceeds_with_auto_approve(self):
        """High-risk skill should bypass HITL when auto_approve=True."""
        routing_decision = {"decision": "HANDOFF", "selected_skill": "my-skill", "intent": "test", "risk": "high", "query": "x"}
        with patch.object(orchestrate, "log_event"):
            with patch.object(orchestrate, "_hitl_approval_gate") as mock_hitl:
                with patch.object(orchestrate, "_invoke_skill_via_api", return_value=("ok", 0, 100)):
                    _, rc, tokens = orchestrate.phase_one(
                        routing_decision, {}, allowlist=["my-skill"], chain_id="abc",
                        dry_run=False, auto_approve=True,
                    )
        assert rc == 0
        assert tokens == 100
        mock_hitl.assert_not_called()


class TestDryRunEndToEnd(unittest.TestCase):
    def test_dry_run_with_query(self):
        """Dry run with query should produce a chain_log and exit 1 (no skill selected)."""
        with patch.object(orchestrate, "run_bootstrap", return_value={}):
            with patch.object(orchestrate, "log_event"):
                with patch.object(orchestrate, "load_allowlist", return_value=["allowed"]):
                    # Patch sys.argv
                    old_argv = sys.argv
                    sys.argv = ["orchestrate.py", "--query", "review my UI", "--dry-run"]
                    try:
                        rc = orchestrate.main()
                    finally:
                        sys.argv = old_argv
        # No skill selected in stub decision, so phase_one returns 1
        assert rc == 1
