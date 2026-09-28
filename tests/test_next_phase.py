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


if __name__ == "__main__":
    unittest.main()
