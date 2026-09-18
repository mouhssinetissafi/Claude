#!/usr/bin/env python3
"""Auto-Editor PRO entry point.

python run.py --topic "..."                     # normal mode
python run.py --footage-only --inbox ./inbox    # footage-only mode
python run.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoeditor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
