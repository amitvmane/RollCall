"""
Compatibility shim — do not add code here.
All logic lives in bot_state.py and handlers/*.py.
"""
from bot_state import bot  # noqa: F401 — re-exported for runner.py

import handlers  # noqa: F401 — registers all @bot decorators
