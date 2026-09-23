"""Diagnostic only: print exactly which bytes arrived on stdin."""

import sys

data = sys.stdin.buffer.read()
print("stdin bytes:", repr(data))
print("stdin hex  :", data[:16].hex(" "))
