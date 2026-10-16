# Author: Wissem Khlifi - August 2026
"""Lab 11 dispatch service — exports root_agent for the ADK runner and agy.

The sys.path line is not boilerplate. This code has to import the same way in
two different contexts:

  In the container, Cloud Run runs `uvicorn main:app` with every module copied
  FLAT into /app, so `from orchestrator import ...` resolves as a top-level
  module and this file is never imported at all.

  Locally, `adk web` loads `agent` as a PACKAGE, and the same flat imports
  inside orchestrator.py, bq_analyst.py and the rest would fail with
  ModuleNotFoundError — the package directory is not on sys.path.

Putting this directory on sys.path first makes the flat imports resolve in both
cases. The alternative — relative imports everywhere — would break the
container, where there is no package.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator import orchestrator as root_agent  # noqa: E402

__all__ = ["root_agent"]
