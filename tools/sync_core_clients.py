#!/usr/bin/env python3
"""Synchronize the maintained standard-library client into each bundle skill."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).absolute().parents[1]
SOURCE = ROOT / "skills" / "file-processing" / "scripts" / "anti_entropy_core_adapter.py"
SKILLS = ("markdown-conversion", "pdf-conversion", "file-conversion")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only byte parity check")
    args = parser.parse_args()
    expected = SOURCE.read_bytes()
    mismatches = []
    for skill in SKILLS:
        target = ROOT / "skills" / skill / "scripts" / SOURCE.name
        if not target.is_file() or target.read_bytes() != expected:
            if args.check:
                mismatches.append(str(target.relative_to(ROOT)))
            else:
                target.write_bytes(expected)
    if mismatches:
        print("Core client copies are stale: " + ", ".join(mismatches), file=sys.stderr)
        print("Run python tools/sync_core_clients.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
