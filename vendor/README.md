# Candidate wheel staging

The remote validation harness stages exactly one `cinderx-*.whl`, one
`daft-0.7.2-*.whl`, one `pyarrow-22.0.0-*.whl`, one `ray-2.55.0-*.whl`, and one
`python_udf_jit-*.whl` here before building `Dockerfile.candidate`. Wheel files
are generated artifacts and are not source inputs; their SHA-256 values must be
supplied as Docker build arguments and are recorded as image labels. The Daft
Wheel is the upstream `cp310-abi3` build, which is valid for the locked CPython
3.14 runtime.
