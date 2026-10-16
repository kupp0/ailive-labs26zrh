# Author: Wissem Khlifi - August 2026
"""Lab 11 transactional service — the two write specialists.

This package runs in its own Cloud Run service, as lab11-transact-sa. It holds
no BigQuery data role, so it cannot read the warehouse, and only the dispatch
service holds run.invoker on it.

The sys.path line does the same job as the one in agent/__init__.py: the modules
import flat (which is how they run in the container) and this makes that work
when `adk web` loads the directory as a package instead.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alloydb_specialist import alloydb_specialist  # noqa: E402
from spanner_specialist import spanner_specialist  # noqa: E402

__all__ = ["alloydb_specialist", "spanner_specialist"]
