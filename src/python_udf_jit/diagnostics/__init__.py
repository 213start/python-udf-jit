"""Process-local structured diagnostics.

Deep diagnostics stay in explicit submodules so existing runtime imports such
as ``from python_udf_jit.diagnostics import events`` do not load the bundle,
policy, or profiling stack when diagnostics are disabled.
"""
