"""Strategy implementations.

Importing this package registers every strategy with the `aar.core.rules`
registry, so `get_strategy("break-fade")` resolves. Add new strategies here.
"""

from . import break_fade  # noqa: F401

__all__ = ["break_fade"]
