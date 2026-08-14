---
title: Framework expression lowering for columnar Python UDFs
date: 2026-08-12
category: architecture-patterns
module: daft-ray-columnar-execution
problem_type: architecture_pattern
component: tooling
severity: high
applies_when:
  - "A row-wise UDF has a structurally provable framework-native equivalent"
  - "A vector kernel is locally faster but end-to-end gains disappear"
  - "Adjacent UDF stages repeatedly cross Python or framework batch boundaries"
tags: [columnar-execution, arrow, daft, udf, vectorization, performance]
---

# Framework expression lowering for columnar Python UDFs

## Context

A faster Arrow kernel is not sufficient when it remains behind one Python
batch-UDF boundary per logical operator. FineWeb experiments established that
an individually faster Arrow predicate or string kernel can lose most or all
of its end-to-end benefit when every logical operator retains a separate
batch-UDF boundary. Those historical timings mixed superseded null/process
policies and are intentionally excluded from current performance evidence.

On the corrected final source, the reduced blue-98 diagnostic captures four
structural patterns and executes 16 Arrow null-guard batches covering 40,000
logical lanes with zero dependency-guard misses. The formal blue-53 200K ABBA
moves from `304.391s` to `279.327s`, or `1.0897x`, with
`200000 -> 199765` row parity.

The decisive optimization was removing the Python execution boundary and
letting the framework optimize the expression graph, not merely replacing the
code that ran behind that boundary.

## Guidance

Treat framework-native expression lowering as a separate provider from guarded
native batch execution:

1. Prove semantics from structure, exact types, bound constants, effects, and
   source identity. Never select by function or pipeline name.
2. Emit framework expressions only for a deliberately small common semantic
   subset. Examples include one-pass literal translation, an exact Unicode
   whitespace normalization, closed string-length intervals, and a restricted
   Python-regex/RE2 common subset.
3. Keep locally fast Arrow kernels behind a batch UDF only when the boundary is
   already amortized by enough work. A per-operator boundary requires an E2E
   cost gate.
4. Attribute every experiment with a same-source feature switch. Compare
   `off` directly with the single provider being tested; do not mix historical
   JIT gain, another experimental provider, or process-policy changes into the
   ratio.
5. Require both exact-value parity and live-dependency parity before production
   enablement. A frozen expression plan is not equivalent to a Python UDF that
   observes a later `__code__`, default, closure, or global dependency change.
6. Complete structural/type capture before constructing any framework
   expression, batch decorator, or null guard. Candidate refusal must be
   observationally identical to feature-off planning.
7. Re-resolve the candidate input expression against the executing DataFrame
   schema before substitution. Function structure does not prove the call-site
   type, and framework coercion can otherwise turn a required Python type error
   into a successful but incorrect native result.

The experimental lowering shape is:

```python
resolved = resolve_callable(original_callable)
plan = capture_supported_shape(resolved)

if plan.kind == "translation":
    result = input_expression
    for source, replacement in plan.replacements:
        result = result.replace(source, replacement)
elif plan.kind == "length_interval":
    length = input_expression.length()
    result = (length >= plan.lower) & (length <= plan.upper)
```

This is intentionally a Driver-side plan rewrite. It should not manufacture a
batch UDF merely to call Arrow compute from Python again.

The proof/construction ordering is equally important:

```python
plan = capture_supported_shape(resolved)
if plan is None:
    return original_expression

# Framework construction starts only after admission succeeds.
nonnull = build_batch_null_guard(input_expression)
return lower_plan(plan, nonnull)
```

An earlier version constructed the temporary Daft batch null guard before
capture. Unsupported audio UDFs never executed a columnar backend, yet that
discarded construction changed planning enough to produce an unstable formal
regression (`0.9045x`). Moving all framework construction after successful
capture restored the same audio task to `1.0009x`, with disabled/enabled pair
drift of only `0.126%`/`0.087%`.

A second counterexample applied the captured punctuation transform to an
`Int64` column. The original UDF raised the indexed `AttributeError`, while an
ungated native expression returned the coerced string `"1"`. The production
gate therefore resolves the original input expression against the DataFrame
schema and admits only exact `String`; missing, unsupported, or non-string
schemas retain the original UDF lineage.

## Why This Matters

The boundary determines whether the framework can fuse, reorder, or otherwise
optimize adjacent kernels. Local stage measurements overstated the value of
punctuation and whitespace kernels by ignoring Daft's batch-UDF setup and
execution cost. The full-pipeline ABBA showed that direct expression lowering
preserved scale benefit while the per-batch provider did not.

Correctness has a second boundary. Structural capture proves what the function
means at plan construction, but a long-lived Python callable can drift before
execution. In a validation probe, the original Daft UDF observed a post-plan
`__code__` replacement. A precomputed native expression could not. Therefore a
winning performance result alone does not authorize production rollout; the
framework plan needs a live dependency guard with a semantics-preserving
fallback, or the admitted contract must explicitly exclude such mutation.

## When to Apply

- Apply it when a row-wise UDF maps exactly to framework-native expressions and
  several adjacent stages would otherwise cross Python boundaries.
- Prefer guarded native batch execution when no equivalent framework expression
  exists but one compiled loop can amortize the boundary over substantial work.
- Reject the rewrite when regex engines, Unicode tables, null behavior,
  exception order, or live dependency semantics cannot be proven equivalent.
- Re-run a same-source E2E ABBA at the formal dataset scale even when a stage
  microbenchmark or reduced dataset exceeds the directional threshold.

## Examples

The useful comparison is architectural:

```text
row UDF -> Python batch wrapper -> Arrow kernel
    locally fast, but the framework still pays one UDF boundary per operator

row UDF -> structural proof -> Daft expression IR -> Arrow execution graph
    no Python batch boundary; adjacent native expressions remain optimizable
```

A conservative rollout keeps direct expression lowering behind an explicit
experimental mode until live dependency guards pass. Failed providers remain
recorded as experiments, not silently included under the production feature
switch.

## Related

- [Scalar mainline vertical slice](scalar-mainline-vertical-slice.md) describes
  the guard, ownership, and evidence discipline that columnar extensions must
  preserve.
