"""Tests for scripts/next_phase.py, run as a subprocess against a throwaway home folder."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "next_phase.py"


class NextPhaseFinalGate(unittest.TestCase):
    """A HITL gate on the last phase must hold the chain open until --approve or --reject."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        chain_dir = self.home / ".agents" / "skills" / "demo-chain" / "config"
        chain_dir.mkdir(parents=True)
        (chain_dir / "chain_definition.json").write_text(json.dumps({
            "chain_name": "demo-chain",
            "phases": [
                {"id": "first", "name": "First", "intent": "work"},
                {"id": "last", "name": "Last", "intent": "report", "on_phase_complete": "hitl"},
            ],
        }), encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        self.env.pop("SKILL_DISPATCH_CHAIN_ID", None)

    def run_np(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                                encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_to_final_gate(self):
        env = self.run_np("start", "--chain", "demo-chain", "--query", "x", "--chain-id", "t1")
        self.assertEqual(env["phase_index"], 0)
        env = self.run_np("advance", "--chain-id", "t1", "--phase-output", "one")
        self.assertEqual(env["phase_index"], 1)
        env = self.run_np("advance", "--chain-id", "t1", "--phase-output", "two")
        self.assertTrue(env["awaiting_approval"])
        self.assertFalse(env["done"])
        self.assertTrue(env.get("final_gate"))
        # Asking again without a decision keeps the gate open.
        again = self.run_np("advance", "--chain-id", "t1")
        self.assertTrue(again["awaiting_approval"])
        self.assertFalse(again["done"])

    def test_reject_on_final_gate_halts_the_chain(self):
        self.run_to_final_gate()
        summary = self.run_np("advance", "--chain-id", "t1", "--reject", "--reason", "not good enough")
        self.assertTrue(summary["done"])
        self.assertEqual(summary["halted"], "hitl_rejected")
        status = self.run_np("status", "--chain-id", "t1")
        self.assertFalse(status["pending_hitl"])

    def test_approve_on_final_gate_completes_the_chain(self):
        self.run_to_final_gate()
        summary = self.run_np("advance", "--chain-id", "t1", "--approve")
        self.assertTrue(summary["done"])
        self.assertEqual(summary["halted"], "")
        self.assertEqual(summary["phases_failed"], 0)


class NextPhaseFinishAtGate(unittest.TestCase):
    """--finish ends a chain at an approval gate as a success (the audit-only exit)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        chain_dir = self.home / ".agents" / "skills" / "audit-chain" / "config"
        chain_dir.mkdir(parents=True)
        (chain_dir / "chain_definition.json").write_text(json.dumps({
            "chain_name": "audit-chain",
            "phases": [
                {"id": "audit", "name": "Audit", "intent": "audit", "on_phase_complete": "hitl"},
                {"id": "refactor", "name": "Refactor", "intent": "refactor"},
            ],
        }), encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        self.env.pop("SKILL_DISPATCH_CHAIN_ID", None)

    def run_np(self, *args, expect_rc=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                                encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, expect_rc, result.stderr)
        return json.loads(result.stdout) if expect_rc == 0 else result

    def test_finish_at_gate_ends_the_chain_as_success(self):
        self.run_np("start", "--chain", "audit-chain", "--query", "x", "--chain-id", "a1")
        gate = self.run_np("advance", "--chain-id", "a1", "--phase-output", "audit report")
        self.assertTrue(gate["awaiting_approval"])
        summary = self.run_np("advance", "--chain-id", "a1", "--finish", "--reason", "audit-only run")
        self.assertTrue(summary["done"])
        self.assertEqual(summary["finished_early"], "audit-only run")
        self.assertEqual(summary["halted"], "")
        self.assertEqual(summary["phases_executed"], 1)
        self.assertEqual(summary["phases_failed"], 0)
        status = self.run_np("status", "--chain-id", "a1")
        self.assertFalse(status["pending_hitl"])
        self.assertEqual(status["finished_early"], "audit-only run")

    def test_gate_decisions_on_a_finished_chain(self):
        self.run_np("start", "--chain", "audit-chain", "--query", "x", "--chain-id", "a3")
        self.run_np("advance", "--chain-id", "a3", "--phase-output", "audit report")
        self.run_np("advance", "--chain-id", "a3", "--finish", "--reason", "audit-only run")
        for flag in ("--finish", "--reject"):
            result = self.run_np("advance", "--chain-id", "a3", flag, expect_rc=2)
            self.assertIn("already finished", result.stderr)
        summary = self.run_np("advance", "--chain-id", "a3", "--approve")  # harmless: returns the summary
        self.assertTrue(summary["done"])

    def test_help_lists_the_gate_decisions(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "-h"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=self.env)  # console code page on Windows
        for flag in ("--approve", "--reject", "--finish", "final_gate"):
            self.assertIn(flag, result.stdout)

    def test_finish_outside_a_gate_is_refused(self):
        self.run_np("start", "--chain", "audit-chain", "--query", "x", "--chain-id", "a2")
        result = self.run_np("advance", "--chain-id", "a2", "--finish", expect_rc=2)
        self.assertIn("only valid at a pending approval gate", result.stderr)


if __name__ == "__main__":
    unittest.main()
