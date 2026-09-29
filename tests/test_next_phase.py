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
            self.assertEqual(json.loads(result.stdout)["error"], "chain_finished")
        summary = self.run_np("advance", "--chain-id", "a3", "--approve")  # harmless: returns the summary
        self.assertTrue(summary["done"])

    def test_help_lists_the_gate_decisions(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "-h"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=self.env)  # console code page on Windows
        for flag in ("--approve", "--reject", "--finish", "final_gate"):
            self.assertIn(flag, result.stdout)

    def test_gate_decisions_outside_a_gate_are_refused(self):
        self.run_np("start", "--chain", "audit-chain", "--query", "x", "--chain-id", "a2")
        for flag in ("--finish", "--reject", "--approve"):
            # With a phase output the decision used to fall through and record the phase as a success.
            result = self.run_np("advance", "--chain-id", "a2", flag, "--phase-output", "work", expect_rc=2)
            self.assertIn("only valid at a pending approval gate", result.stderr)
            error = json.loads(result.stdout)
            self.assertEqual(error["error"], "no_pending_gate")
            self.assertEqual(error["chain_id"], "a2")
        status = self.run_np("status", "--chain-id", "a2")
        self.assertEqual(status["current_phase_index"], 0)
        self.assertEqual(status["phases_executed"], 0)
        self.assertEqual(status["history"], [])


class NextPhaseRefusals(unittest.TestCase):
    """Every refusal exits 2 and prints a JSON error object, so a redirected envelope file is never empty."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        chain_dir = self.home / ".agents" / "skills" / "demo-chain" / "config"
        chain_dir.mkdir(parents=True)
        (chain_dir / "chain_definition.json").write_text(json.dumps({
            "chain_name": "demo-chain",
            "phases": [{"id": "only", "name": "Only", "intent": "work"}],
        }), encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        self.env.pop("SKILL_DISPATCH_CHAIN_ID", None)

    def refused(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                                encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 2, result.stderr)
        error = json.loads(result.stdout)
        self.assertIn(error["message"], result.stderr)
        return error

    def test_error_codes(self):
        cases = [
            (("advance", "--chain-id", "nope", "--phase-output", "x"), "no_chain_state"),
            (("status", "--chain-id", "nope"), "no_chain_state"),
            (("start", "--chain", "missing-chain", "--query", "x"), "no_chain_definition"),
            (("start", "--chain", "demo-chain", "--chain-id", "r1"), "missing_query"),
        ]
        for args, code in cases:
            with self.subTest(code=code, args=args):
                self.assertEqual(self.refused(*args)["error"], code)

    def test_missing_query_echoes_only_a_chosen_chain_id(self):
        self.assertEqual(self.refused("start", "--chain", "demo-chain", "--chain-id", "r1")["chain_id"], "r1")
        self.assertNotIn("chain_id", self.refused("start", "--chain", "demo-chain"))  # generated, never saved

    def test_error_codes_on_a_running_chain(self):
        start = subprocess.run([sys.executable, str(SCRIPT), "start", "--chain", "demo-chain", "--query", "x",
                                "--chain-id", "r2"], capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(start.returncode, 0, start.stderr)
        again = self.refused("start", "--chain", "demo-chain", "--query", "x", "--chain-id", "r2")
        self.assertEqual((again["error"], again["chain_id"]), ("chain_id_exists", "r2"))
        self.assertEqual(self.refused("advance", "--chain-id", "r2")["error"], "missing_phase_output")


if __name__ == "__main__":
    unittest.main()
