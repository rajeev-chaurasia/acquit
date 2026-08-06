"""GitHub delivery: the sticky PR comment and the action's runner-file outputs.

Everything in this package is best-effort by design. A delivery failure is a
warning on stderr and a zero exit, because reporting must never fail CI; the
fail-closed guarantees all live upstream in selection and replay.
"""
