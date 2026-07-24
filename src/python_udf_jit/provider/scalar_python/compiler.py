from __future__ import annotations

import ast
import hashlib
import marshal
from dataclasses import dataclass
from types import CodeType, FunctionType
from typing import Callable, Literal

from python_udf_jit.compiler.core_ir import CoreNode, CoreUdfModule
from python_udf_jit.compiler.region import VerifiedRegion
from python_udf_jit.compiler.verifier import verify_core_module, verify_region
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry


GuardFunction = Callable[[object], object]
LoadFunction = Callable[[object], float]


@dataclass(frozen=True)
class CompiledScalarFunction:
    """Worker-local code generated solely from a verified scalar Region."""

    semantic_hash: str
    code_hash: str
    execution_mode: str
    code_object: CodeType
    _function: FunctionType
    registry_id: str | None
    argument_kind: Literal["capability", "backend"]

    def __call__(self, handle: object) -> float:
        return self._function(handle)

    @property
    def jit_function(self) -> FunctionType:
        """The exact Region-derived function submitted to the CinderX JIT."""

        return self._function

    def __reduce__(self) -> object:
        raise TypeError("compiled scalar functions are worker-process-local")


def _value_name(value_id: str) -> str:
    if not value_id.startswith("%") or not value_id[1:].isdigit():
        raise ValueError("verified region contains a noncanonical value id")
    return f"value_{value_id[1:]}"


def _load_expression() -> ast.expr:
    guarded = ast.Call(
        func=ast.Name(id="_udf_guard_data_handle", ctx=ast.Load()),
        args=[ast.Name(id="slot", ctx=ast.Load())],
        keywords=[],
    )
    return ast.Call(
        func=ast.Name(id="_udf_data_load_f64", ctx=ast.Load()),
        args=[guarded],
        keywords=[],
    )


def _binary_expression(node: CoreNode) -> ast.expr:
    operators: dict[str, type[ast.operator]] = {
        "add.f64": ast.Add,
        "sub.f64": ast.Sub,
        "mul.f64": ast.Mult,
    }
    operator = operators.get(node.op)
    if operator is None:
        raise ValueError(f"unsupported verified scalar operation: {node.op}")
    left, right = node.operands
    return ast.BinOp(
        left=ast.Name(id=_value_name(left), ctx=ast.Load()),
        op=operator(),
        right=ast.Name(id=_value_name(right), ctx=ast.Load()),
    )


def _build_function_ast(module: CoreUdfModule, region: VerifiedRegion) -> ast.Module:
    body: list[ast.stmt] = []
    for index in region.operation_indexes:
        node = module.nodes[index]
        if node.op == "return":
            body.append(
                ast.Return(
                    value=ast.Name(id=_value_name(node.operands[0]), ctx=ast.Load())
                )
            )
            continue
        if node.op == "arg.load":
            expression = _load_expression()
        elif node.op == "const.f64":
            expression = ast.Constant(value=node.literal)
        else:
            expression = _binary_expression(node)
        body.append(
            ast.Assign(
                targets=[ast.Name(id=_value_name(node.result_id or ""), ctx=ast.Store())],
                value=expression,
            )
        )
    function = ast.FunctionDef(
        name="_verified_scalar_region",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="slot")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
    )
    return ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))


def compile_scalar_region(
    module: CoreUdfModule,
    region: VerifiedRegion,
    *,
    registry: CapabilityRegistry | None = None,
    guard_function: GuardFunction | None = None,
    load_function: LoadFunction | None = None,
    execution_mode: str = "python-interpreter",
    argument_kind: Literal["capability", "backend"] | None = None,
) -> CompiledScalarFunction:
    """Lower verified IR through a finite AST template, never Artifact source."""

    verify_core_module(module)
    verify_region(module, region)
    if registry is not None:
        if guard_function is not None or load_function is not None:
            raise ValueError("pass either registry or explicit lowering hooks")
        guard_function = registry.guard_data_handle
        load_function = registry.data_load_f64
        registry_id: str | None = registry.registry_id
        if argument_kind not in (None, "capability"):
            raise ValueError("registry lowering accepts capability arguments")
        argument_kind = "capability"
    else:
        registry_id = None
        if argument_kind is None:
            argument_kind = "backend"
    if guard_function is None or load_function is None:
        raise ValueError("scalar lowering requires guard and load functions")
    if not isinstance(execution_mode, str) or not execution_mode:
        raise ValueError("execution mode must be a non-empty string")

    generated = _build_function_ast(module, region)
    filename = f"<python-udf-jit:{module.semantic_hash}>"
    module_code = compile(generated, filename, "exec", dont_inherit=True, optimize=2)
    namespace = {
        "__builtins__": {},
        "_udf_guard_data_handle": guard_function,
        "_udf_data_load_f64": load_function,
    }
    exec(module_code, namespace)
    function = namespace["_verified_scalar_region"]
    if not isinstance(function, FunctionType):
        raise RuntimeError("controlled scalar lowering did not create a function")
    code_hash = hashlib.sha256(
        b"python-udf-jit-scalar-code-v1\0"
        + module.semantic_hash.encode("ascii")
        + marshal.dumps(function.__code__)
    ).hexdigest()
    return CompiledScalarFunction(
        module.semantic_hash,
        code_hash,
        execution_mode,
        function.__code__,
        function,
        registry_id,
        argument_kind,
    )
