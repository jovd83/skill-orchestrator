#!/usr/bin/env python3
"""Validation for the skill-orchestrator repository."""

import subprocess
import sys
from pathlib import Path

def run_command(command: list[str], root: Path) -> int:
    print(f"--- Running: {' '.join(command)} ---")
    try:
        result = subprocess.run(command, cwd=root, check=False)
        return result.returncode
    except FileNotFoundError as exc:
        print(f"Error: Command not found: {exc}")
        return 1

def main() -> int:
    root = Path(__file__).resolve().parent.parent
    
    # 1. Run unit tests
    print("--- Running pytest ---")
    rc = run_command([sys.executable, "-m", "pytest", "tests/", "-v"], root)
    if rc != 0:
        print("Tests failed.")
        return rc
        
    print("\nValidation complete!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
