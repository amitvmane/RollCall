"""
Handlers package — importing each module registers its @bot decorators with the bot instance.
Order matters: lifecycle must load before modules that call its functions.
"""
from handlers import lifecycle    # noqa: F401
from handlers import core         # noqa: F401
from handlers import settings     # noqa: F401
from handlers import voting       # noqa: F401
from handlers import proxy        # noqa: F401
from handlers import lists        # noqa: F401
from handlers import ghost        # noqa: F401
from handlers import admin        # noqa: F401
from handlers import templates    # noqa: F401
from handlers import stats        # noqa: F401
from handlers import web          # noqa: F401
