#!/usr/bin/env python3
"""PyInstaller entry point for the Auto-Editor PRO Studio sidecar.

The same executable has two modes:
* default / ``--studio-service``: newline-delimited local JSON-RPC service
* ``--engine-cli ...``: run the existing Auto-Editor CLI in an isolated child

Keeping both modes in one binary lets Studio cancel a generation by terminating
only the CLI child while leaving the service/UI alive.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--engine-cli":
        from autoeditor.cli import main as cli_main

        return int(cli_main(sys.argv[2:]))
    from autoeditor.studio.service import main as service_main

    return int(service_main())


if __name__ == "__main__":
    raise SystemExit(main())
