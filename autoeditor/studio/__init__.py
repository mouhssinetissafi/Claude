"""Desktop Studio service boundary for Auto-Editor PRO.

The Studio package intentionally keeps the existing editing engine unchanged.
Electron (or any other desktop shell) talks to this package over a tiny JSON-RPC
protocol, while real generation continues to run through ``run.py`` in a child
process.  That gives the desktop app progress, cancellation and crash isolation
without duplicating the pipeline.
"""

from .project import PROJECT_VERSION, StudioProject, create_project, open_project

__all__ = ["PROJECT_VERSION", "StudioProject", "create_project", "open_project"]
