"""Safe, value-free errors for the external-closure verifier primitives."""

from __future__ import annotations


class ExternalClosurePrimitiveError(ValueError):
    """A deterministic primitive failure that never echoes attacker-controlled input."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("external_closure_error_code_invalid")
        self.code = code
        super().__init__(code)


__all__ = ["ExternalClosurePrimitiveError"]
