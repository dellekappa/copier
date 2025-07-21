#!/usr/bin/env python3
"""Copier CLI entry point for PyInstaller."""

import sys
from pathlib import Path

# Add the current directory to the Python path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from copier._cli import CopierApp

if __name__ == "__main__":
    CopierApp.run()
