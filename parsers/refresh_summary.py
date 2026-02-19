#!/usr/bin/env python3
"""Thin wrapper to regenerate the TypeOneZen health summary silently."""

import sys
from pathlib import Path

# Ensure the parsers package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.generate_summary import main as generate_main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--quiet"]
    generate_main()
