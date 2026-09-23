"""Diagnostic only: send the build script's ping to the packaged engine with exact bytes (no PowerShell)."""

import subprocess
import sys

exe = sys.argv[1]
for label, payload in (
    ("LF", b'{"id":1,"method":"ping","params":{}}\n'),
    ("CRLF", b'{"id":1,"method":"ping","params":{}}\r\n'),
):
    result = subprocess.run([exe], input=payload, capture_output=True, timeout=180)
    print(f"--- {label}: exit={result.returncode}")
    print("stdout:", repr(result.stdout[:600]))
    print("stderr:", repr(result.stderr[-1500:]))
