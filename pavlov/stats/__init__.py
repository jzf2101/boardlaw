from .registry import to_run
from .monitoring import from_run, from_run_sync
from .plotting import review, view
from .registry import KINDS

for name, func in KINDS.items():
    globals()[name] = func
