"""Provider completion telemetry contracts and pure runtime controls.

This package is deliberately independent of the ``researchops`` package. It holds
the pure rule interpreter, surface registry, capture boundary, and write-time
sanitizer/validators without importing a Provider Adapter or the analysis stack.
Nothing here may import ``researchops``.
"""
