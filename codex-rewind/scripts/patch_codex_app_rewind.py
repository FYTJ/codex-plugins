#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
from pathlib import Path


TARGET = Path(__file__).resolve().with_name("codex_rewind_patch_app.py")


def main() -> None:
    os.execv(sys.executable or "/usr/bin/python3", [sys.executable or "/usr/bin/python3", str(TARGET), *sys.argv[1:]])


if __name__ == "__main__":
    main()
