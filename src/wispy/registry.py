"""Handler registration for WSP methods.

The :class:`HandlerRegistry` is the single source of truth mapping WSP
method names to the Python callables that service them. Both
programmatic registration and Config_File
registration funnel through :meth:`HandlerRegistry.register`.

Attempting to bind a method that is already
registered raises :class:`~wispy.errors.DuplicateRegistrationError`
without modifying any prior state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from wispy.errors import DuplicateRegistrationError

__all__ = ["Handler", "HandlerRegistry"]


# A Handler accepts the request's ``params`` value and returns either the
# result directly (sync handler) or an awaitable that resolves to the
# result (async handler). The dispatcher takes care of awaiting or
# off-loading sync handlers to the default executor.
Handler = Callable[[Any], Awaitable[Any] | Any]


class HandlerRegistry:
    """In-memory mapping from WSP method names to :data:`Handler` callables.

    The registry is intentionally minimal: it stores bindings, refuses
    duplicates, and exposes the bound method names. It does not validate
    method names against the WSP method registry — that is the caller's
    responsibility (the Config_File loader does so before calling
    :meth:`register`; programmatic callers are trusted).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        """Bind ``handler`` to ``method``.

        Raises :class:`~wispy.errors.DuplicateRegistrationError` if
        ``method`` is already bound; in that case the registry's state is
        left unchanged.
        """
        if method in self._handlers:
            msg = f"method {method!r} is already registered"
            raise DuplicateRegistrationError(msg)
        self._handlers[method] = handler

    def methods(self) -> list[str]:
        """Return the bound method names in sorted order."""
        return sorted(self._handlers)

    def lookup(self, method: str) -> Handler | None:
        """Return the handler bound to ``method``, or ``None`` if unbound."""
        return self._handlers.get(method)
