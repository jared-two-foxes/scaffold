#!/usr/bin/env python3
"""Build a standalone scaffold executable with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "scaffold",
        "src/scaffold_cli/cli.py",
    ]
    print("Building standalone binary...")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode

    print(f"Build complete. Output is in {DIST_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
