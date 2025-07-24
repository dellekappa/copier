#!/usr/bin/env python3
"""Copier CLI entry point for PyInstaller."""

import sys
from pathlib import Path

# Add the current directory to the Python path
sys.path.append(str(Path.cwd()))

from copier._cli import CopierApp

if __name__ == "__main__":
    CopierApp.run()
