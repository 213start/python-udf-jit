from __future__ import annotations

import ast
import hashlib
import marshal
from dataclasses import dataclass
from types import CodeType, FunctionType
from typing import Callable, Literal

from python_udf_jit.compiler.core_ir import (
    CoreNode,
    CoreUdfModule,
    LogicalType,
    Nullability,
    SemanticCoreModule,
    SemanticOperation,
    SemanticPythonRegion,
)
from python_udf_jit.compiler.region import (
    SemanticRegionGraph,
    VerifiedRegion,
    verify_semantic_region_graph,
)
from python_udf_jit.compiler.verifier import (
    verify_core_module,
    verify_region,
    verify_semantic_module,
)
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.runtime.descriptors import (
    AccessSpec,
    require_access_spec,
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import (
    BOOL_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
)
from python_udf_jit.runtime.continuation import (
    ContinuationContract,
    build_continuation_payload,
)


GuardFunction = Callable[[object], object]
NullFunction = Callable[[object], bool]
LoadFunction = Callable[[object], object]
StoreFunction = Callable[[object, object], object]
StoreNullFunction = Callable[[object], None]
ContinuationPayloadFunction = Callable[..., object]
ArgumentKind = Literal["capability_pair", "backend_pair"]


@dataclass(frozen=True)
class ScalarLoweringHooks:
    """Exact process-local functions admitted into the finite AST template."""

    guard: GuardFunction
    is_null: NullFunction
    load: LoadFunction
    store: StoreFunction
    store_null: StoreNullFunction
    build_continuation_payload: ContinuationPayloadFunction = (
        build_continuation_payload
    )

    def __post_init__(self) -> None:
        if not all(
            callable(value)
            for value in (
                self.guard,
                self.is_null,
                self.load,
                self.store,
                self.store_null,
                self.build_continuation_payload,
            )
        ):
            raise TypeError("scalar lowering hooks must be callable")


@dataclass(frozen=True)
class CompiledScalarFunction:
    """Worker-local code generated solely from verified scalar semantics."""

    semantic_hash: str
    code_hash: str
    execution_mode: str
    code_object: CodeType
    _function: FunctionType
    registry_id: str | None
    argument_kind: ArgumentKind
    input_spec: AccessSpec
    output_spec: AccessSpec

    def __call__(self, input_handle: object, output_handle: object) -> object:
        return self._function(input_handle, output_handle)

    @property
    def jit_function(self) -> FunctionType:
        """The exact verified function submitted to the CinderX JIT."""

        return self._function

    @property
    def code_size(self) -> int:
        """Exact serialized size of the generated Worker-local code object."""

        return len(marshal.dumps(self.code_object))

    def __reduce__(self) -> object:
        raise TypeError("compiled scalar functions are worker-process-local")


def _value_name(value_id: str) -> str:
    if not value_id.startswith("%") or not value_id[1:].isdigit():
        raise ValueError("verified region contains a noncanonical value id")
    return f"value_{value_id[1:]}"


def _guarded_call(helper_name: str, slot_name: str) -> ast.Call:
    return ast.Call(
        func=ast.Name(id=helper_name, ctx=ast.Load()),
        args=[
            ast.Call(
                func=ast.Name(
                    id="_udf_guard_data_handle",
                    ctx=ast.Load(),
                ),
                args=[ast.Name(id=slot_name, ctx=ast.Load())],
                keywords=[],
            )
        ],
        keywords=[],
    )


def _load_expression(slot_name: str = "input_slot") -> ast.expr:
    return _guarded_call("_udf_data_load", slot_name)


def _store_expression(value: ast.expr) -> ast.expr:
    return ast.Call(
        func=ast.Name(id="_udf_data_store", ctx=ast.Load()),
        args=[
            ast.Call(
                func=ast.Name(
                    id="_udf_guard_data_handle",
                    ctx=ast.Load(),
                ),
                args=[ast.Name(id="output_slot", ctx=ast.Load())],
                keywords=[],
            ),
            value,
        ],
        keywords=[],
    )


def _store_null_expression() -> ast.expr:
    return _guarded_call("_udf_data_store_null", "output_slot")


def _return_statements(
    value: ast.expr,
    *,
    output_nullable: bool,
) -> list[ast.stmt]:
    result_name = "_scalar_result"
    statements: list[ast.stmt] = [
        ast.Assign(
            targets=[ast.Name(id=result_name, ctx=ast.Store())],
            value=value,
        )
    ]
    if output_nullable:
        statements.append(
            ast.If(
                test=ast.Compare(
                    left=ast.Name(id=result_name, ctx=ast.Load()),
                    ops=[ast.Is()],
                    comparators=[ast.Constant(value=None)],
                ),
                body=[ast.Expr(value=_store_null_expression())],
                orelse=[
                    ast.Expr(
                        value=_store_expression(
                            ast.Name(id=result_name, ctx=ast.Load())
                        )
                    )
                ],
            )
        )
    else:
        statements.append(
            ast.Expr(
                value=_store_expression(
                    ast.Name(id=result_name, ctx=ast.Load())
                )
            )
        )
    statements.append(
        ast.Return(value=ast.Name(id=result_name, ctx=ast.Load()))
    )
    return statements


def _core_binary_expression(node: CoreNode) -> ast.expr:
    operators: dict[str, type[ast.operator]] = {
        "add.f64": ast.Add,
        "sub.f64": ast.Sub,
        "mul.f64": ast.Mult,
    }
    operator = operators.get(node.op)
    if operator is None:
        raise ValueError(
            f"unsupported verified scalar operation: {node.op}"
        )
    left, right = node.operands
    return ast.BinOp(
        left=ast.Name(id=_value_name(left), ctx=ast.Load()),
        op=operator(),
        right=ast.Name(id=_value_name(right), ctx=ast.Load()),
    )


def _build_core_function_ast(
    module: CoreUdfModule,
    region: VerifiedRegion,
) -> ast.Module:
    body: list[ast.stmt] = []
    for index in region.operation_indexes:
        node = module.nodes[index]
        if node.op == "return":
            body.extend(
                _return_statements(
                    ast.Name(
                        id=_value_name(node.operands[0]),
                        ctx=ast.Load(),
                    ),
                    output_nullable=False,
                )
            )
            continue
        if node.op == "arg.load":
            expression = _load_expression()
        elif node.op == "const.f64":
            expression = ast.Constant(value=node.literal)
        else:
            expression = _core_binary_expression(node)
        body.append(
            ast.Assign(
                targets=[
                    ast.Name(
                        id=_value_name(node.result_id or ""),
                        ctx=ast.Store(),
                    )
                ],
                value=expression,
            )
        )
    return _function_module(body)


def _function_module(body: list[ast.stmt]) -> ast.Module:
    function = ast.FunctionDef(
        name="_verified_scalar_region",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="input_slot"),
                ast.arg(arg="output_slot"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
    )
    return ast.fix_missing_locations(
        ast.Module(body=[function], type_ignores=[])
    )


def _logical_type_for_scalar(scalar_type: str) -> LogicalType:
    if scalar_type == BOOL_SCALAR_TYPE:
        return LogicalType.BOOL
    if scalar_type in {INT32_SCALAR_TYPE, INT64_SCALAR_TYPE}:
        return LogicalType.INT64
    if scalar_type in {FLOAT32_SCALAR_TYPE, FLOAT64_SCALAR_TYPE}:
        return LogicalType.FLOAT64
    raise ValueError("unsupported scalar type")


def _attributes(operation: SemanticOperation) -> dict[str, str]:
    return dict(operation.attributes)


def _semantic_expression(operation: SemanticOperation) -> ast.expr:
    operands = [
        ast.Name(id=_value_name(value), ctx=ast.Load())
        for value in operation.operands
    ]
    binary_operators: dict[str, type[ast.operator]] = {
        "binary.add": ast.Add,
        "binary.sub": ast.Sub,
        "binary.mul": ast.Mult,
        "binary.truediv": ast.Div,
    }
    if operation.op in binary_operators:
        return ast.BinOp(
            left=operands[0],
            op=binary_operators[operation.op](),
            right=operands[1],
        )
    compare_operators: dict[str, type[ast.cmpop]] = {
        "compare.eq": ast.Eq,
        "compare.ne": ast.NotEq,
        "compare.lt": ast.Lt,
        "compare.le": ast.LtE,
        "compare.gt": ast.Gt,
        "compare.ge": ast.GtE,
    }
    if operation.op in compare_operators:
        return ast.Compare(
            left=operands[0],
            ops=[compare_operators[operation.op]()],
            comparators=[operands[1]],
        )
    if operation.op == "null.is_null":
        return ast.Compare(
            left=operands[0],
            ops=[ast.Is()],
            comparators=[ast.Constant(value=None)],
        )
    if operation.op == "select":
        return ast.IfExp(
            test=operands[0],
            body=operands[1],
            orelse=operands[2],
        )
    if operation.op == "cast":
        target = _attributes(operation)["target"]
        helper = {
            LogicalType.BOOL.value: "_scalar_cast_bool",
            LogicalType.INT64.value: "_scalar_cast_int",
            LogicalType.FLOAT64.value: "_scalar_cast_float",
        }.get(target)
        if helper is None:
            raise ValueError("unsupported scalar cast target")
        return ast.Call(
            func=ast.Name(id=helper, ctx=ast.Load()),
            args=[operands[0]],
            keywords=[],
        )
    raise ValueError(
        f"unsupported semantic scalar operation: {operation.op}"
    )


def _operation_statements(
    operation: SemanticOperation,
    *,
    output_nullable: bool,
    continuation_contract: ContinuationContract | None,
) -> list[ast.stmt]:
    if operation.op == "argument":
        if _attributes(operation).get("index") != "0":
            raise ValueError("scalar provider accepts exactly one input")
        expression: ast.expr = ast.Name(
            id="_scalar_input",
            ctx=ast.Load(),
        )
    elif operation.op == "constant":
        if operation.literal is None:
            raise ValueError("semantic scalar constant has no literal")
        expression = ast.Constant(value=operation.literal.value)
    elif operation.op == "branch":
        attributes = _attributes(operation)
        return [
            ast.Assign(
                targets=[ast.Name(id="_scalar_block", ctx=ast.Store())],
                value=ast.IfExp(
                    test=ast.Name(
                        id=_value_name(operation.operands[0]),
                        ctx=ast.Load(),
                    ),
                    body=ast.Constant(value=attributes["true_block"]),
                    orelse=ast.Constant(value=attributes["false_block"]),
                ),
            ),
            ast.Continue(),
        ]
    elif operation.op == "jump":
        return [
            ast.Assign(
                targets=[ast.Name(id="_scalar_block", ctx=ast.Store())],
                value=ast.Constant(
                    value=_attributes(operation)["target_block"]
                ),
            ),
            ast.Continue(),
        ]
    elif operation.op == "return":
        return _return_statements(
            ast.Name(
                id=_value_name(operation.operands[0]),
                ctx=ast.Load(),
            ),
            output_nullable=output_nullable,
        )
    elif operation.op == "python.region":
        if continuation_contract is None:
            raise ValueError("Python region continuation proof is missing")
        if tuple(operation.operands) != continuation_contract.live_names:
            raise ValueError("Python region continuation live-in mismatch")
        source_map = continuation_contract.source_map
        return [
            ast.Return(
                value=ast.Call(
                    func=ast.Name(
                        id="_udf_build_continuation_payload",
                        ctx=ast.Load(),
                    ),
                    args=[
                        ast.Constant(
                            value=continuation_contract.abi_version
                        ),
                        ast.Constant(value="python_region"),
                        ast.Constant(
                            value=continuation_contract.resume_id
                        ),
                        ast.Constant(
                            value=(
                                continuation_contract.source_identity
                                .namespace_sha256
                            )
                        ),
                        ast.Constant(
                            value=(
                                continuation_contract.source_identity
                                .code_sha256
                            )
                        ),
                        ast.Constant(
                            value=(
                                continuation_contract.source_identity
                                .first_line
                            )
                        ),
                        ast.Constant(
                            value=(
                                source_map.schema_version,
                                source_map.bytecode_offset,
                                source_map.line,
                                source_map.column,
                                source_map.end_line,
                                source_map.end_column,
                            )
                        ),
                        ast.Tuple(
                            elts=[
                                ast.Name(
                                    id=_value_name(name),
                                    ctx=ast.Load(),
                                )
                                for name in continuation_contract.live_names
                            ],
                            ctx=ast.Load(),
                        ),
                        ast.Constant(
                            value=tuple(
                                spec.kind.value
                                for spec in continuation_contract.live_values
                            )
                        ),
                        ast.Constant(
                            value=tuple(
                                spec.nullable
                                for spec in continuation_contract.live_values
                            )
                        ),
                        ast.Constant(
                            value=tuple(
                                True
                                for _ in continuation_contract.live_values
                            )
                        ),
                        ast.Constant(value=None),
                        ast.Constant(value=True),
                    ],
                    keywords=[],
                )
            )
        ]
    else:
        expression = _semantic_expression(operation)
    return [
        ast.Assign(
            targets=[
                ast.Name(
                    id=_value_name(operation.result_id or ""),
                    ctx=ast.Store(),
                )
            ],
            value=expression,
        )
    ]


def _verify_semantic_scalar_contract(
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
    input_spec: AccessSpec,
    output_spec: AccessSpec,
    continuation_contract: ContinuationContract | None,
) -> None:
    require_access_spec(input_spec)
    require_access_spec(output_spec)
    if len(module.input_types) != 1:
        raise ValueError("scalar provider accepts exactly one input")
    if module.input_types[0] is not _logical_type_for_scalar(
        input_spec.scalar_type
    ):
        raise ValueError("semantic input type and scalar layout disagree")
    if module.output_type is not _logical_type_for_scalar(
        output_spec.scalar_type
    ):
        raise ValueError("semantic output type and scalar layout disagree")
    if (
        module.input_nullability[0] is not Nullability.NON_NULL
    ) != input_spec.nullable:
        raise ValueError(
            "semantic input nullability and scalar layout disagree"
        )
    if (
        module.output_nullability is not Nullability.NON_NULL
    ) != output_spec.nullable:
        raise ValueError(
            "semantic output nullability and scalar layout disagree"
        )
    admitted = {
        "argument",
        "constant",
        "binary.add",
        "binary.sub",
        "binary.mul",
        "binary.truediv",
        "compare.eq",
        "compare.ne",
        "compare.lt",
        "compare.le",
        "compare.gt",
        "compare.ge",
        "null.is_null",
        "cast",
        "select",
        "branch",
        "jump",
        "return",
    }
    if continuation_contract is None:
        if any(
            operation.op not in admitted
            or operation.effect.value != "pure"
            or operation.python_region_id is not None
            for operation in module.operations
        ):
            raise ValueError("semantic module is not a closed scalar region")
    else:
        _verify_single_python_barrier(
            module,
            continuation_contract,
            input_spec,
        )
    covered = tuple(
        operation_id
        for region in graph.regions
        for operation_id in region.operation_ids
    )
    expected = tuple(
        operation.operation_id
        for operation in module.operations
    )
    if sorted(covered) != sorted(expected) or len(covered) != len(expected):
        raise ValueError("semantic region graph does not cover the module")


def require_single_python_barrier(
    module: SemanticCoreModule,
) -> SemanticPythonRegion:
    """Return one straight-line float64 barrier or fail closed."""

    if (
        len(module.blocks) != 1
        or len(module.python_regions) != 1
        or module.input_types != (LogicalType.FLOAT64,)
        or module.input_nullability != (Nullability.NON_NULL,)
        or module.output_type is not LogicalType.FLOAT64
        or module.output_nullability is not Nullability.NON_NULL
    ):
        raise ValueError("unsupported Python region shape")
    region = module.python_regions[0]
    operation_indexes = {
        operation.operation_id: index
        for index, operation in enumerate(module.operations)
    }
    python_index = operation_indexes.get(region.operation_id)
    if python_index is None:
        raise ValueError("unsupported Python region shape")
    python_operation = module.operations[python_index]
    before = module.operations[:python_index]
    after = module.operations[python_index + 1 :]
    scalar_ops = {
        "argument",
        "constant",
        "binary.add",
        "binary.sub",
        "binary.mul",
    }
    arguments = tuple(
        operation for operation in module.operations
        if operation.op == "argument"
    )
    returns = tuple(
        operation for operation in module.operations
        if operation.op == "return"
    )
    if (
        module.blocks[0].operation_ids
        != tuple(operation.operation_id for operation in module.operations)
        or len(arguments) != 1
        or arguments[0] not in before
        or _attributes(arguments[0]).get("index") != "0"
        or arguments[0].result_id is None
        or not before
        or not after
        or any(
            operation.op not in scalar_ops
            or operation.result_type is not LogicalType.FLOAT64
            or operation.nullability is not Nullability.NON_NULL
            or operation.effect.value != "pure"
            or operation.may_raise
            for operation in before
        )
        or python_operation.op != "python.region"
        or python_operation.operation_id != region.operation_id
        or python_operation.python_region_id != region.region_id
        or python_operation.result_id is None
        or len(python_operation.operands) != 1
        or python_operation.result_type is not LogicalType.UNKNOWN
        or python_operation.nullability is not Nullability.NULLABLE
        or python_operation.effect.value != "python"
        or not python_operation.may_raise
        or region.live_in != python_operation.operands
        or region.live_out != (python_operation.result_id,)
        or any(
            python_operation.result_id in operation.operands
            for operation in after
        )
        or region.handler_blocks
        or len(returns) != 1
        or returns[0] not in after
        or module.return_operation_id != returns[0].operation_id
        or any(
            (
                operation.op not in scalar_ops | {"return"}
                or operation.result_type is not LogicalType.FLOAT64
                or operation.nullability is not Nullability.NON_NULL
                or operation.effect.value != "pure"
                or operation.may_raise
            )
            for operation in after
        )
    ):
        raise ValueError("unsupported Python region shape")
    return region


def _verify_single_python_barrier(
    module: SemanticCoreModule,
    contract: ContinuationContract,
    input_spec: AccessSpec,
) -> SemanticPythonRegion:
    """Bind the Worker proof to the admitted scalar graph-break barrier."""

    region = require_single_python_barrier(module)
    if (
        not contract.is_proven
        or contract.resume_id != f"v1:{region.resume_id}"
        or contract.source_identity.code_sha256 != module.function_id
        or region.source_end is None
        or contract.source_map.bytecode_offset != region.source_end
        or contract.live_names != region.live_in
        or len(contract.live_values) != 1
        or contract.live_values[0].kind.value != input_spec.scalar_type
        or contract.live_values[0].nullable != input_spec.nullable
        or contract.live_values[0].borrowed
        or contract.live_values[0].branch_join
        or contract.preserves_active_exception
        or contract.alias_groups
    ):
        raise ValueError("Python region continuation proof is incomplete")
    return region


def _build_semantic_function_ast(
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
    *,
    input_nullable: bool,
    output_nullable: bool,
    continuation_contract: ContinuationContract | None,
) -> ast.Module:
    operation_by_id = {
        operation.operation_id: operation
        for operation in module.operations
    }
    input_load = _load_expression()
    if input_nullable:
        input_body: list[ast.stmt] = [
            ast.If(
                test=_guarded_call(
                    "_udf_data_is_null",
                    "input_slot",
                ),
                body=[
                    ast.Assign(
                        targets=[
                            ast.Name(
                                id="_scalar_input",
                                ctx=ast.Store(),
                            )
                        ],
                        value=ast.Constant(value=None),
                    )
                ],
                orelse=[
                    ast.Assign(
                        targets=[
                            ast.Name(
                                id="_scalar_input",
                                ctx=ast.Store(),
                            )
                        ],
                        value=input_load,
                    )
                ],
            )
        ]
    else:
        input_body = [
            ast.Assign(
                targets=[
                    ast.Name(id="_scalar_input", ctx=ast.Store())
                ],
                value=input_load,
            )
        ]
    input_body.append(
        ast.Assign(
            targets=[ast.Name(id="_scalar_block", ctx=ast.Store())],
            value=ast.Constant(value=module.entry_block),
        )
    )

    dispatch: ast.If | None = None
    current: ast.If | None = None
    for block in module.blocks:
        block_body: list[ast.stmt] = []
        for operation_id in block.operation_ids:
            block_body.extend(
                _operation_statements(
                    operation_by_id[operation_id],
                    output_nullable=output_nullable,
                    continuation_contract=continuation_contract,
                )
            )
        branch = ast.If(
            test=ast.Compare(
                left=ast.Name(id="_scalar_block", ctx=ast.Load()),
                ops=[ast.Eq()],
                comparators=[ast.Constant(value=block.block_id)],
            ),
            body=block_body,
            orelse=[],
        )
        if dispatch is None:
            dispatch = branch
        else:
            assert current is not None
            current.orelse = [branch]
        current = branch
    if dispatch is None or current is None:
        raise ValueError("semantic scalar module has no blocks")
    current.orelse = [
        ast.Raise(
            exc=ast.Call(
                func=ast.Name(id="_scalar_invalid_block", ctx=ast.Load()),
                args=[],
                keywords=[],
            ),
            cause=None,
        )
    ]
    input_body.append(
        ast.While(
            test=ast.Constant(value=True),
            body=[dispatch],
            orelse=[],
        )
    )
    return _function_module(input_body)


def _resolve_hooks(
    *,
    registry: CapabilityRegistry | None,
    hooks: ScalarLoweringHooks | None,
    argument_kind: ArgumentKind | None,
) -> tuple[ScalarLoweringHooks, str | None, ArgumentKind]:
    if registry is not None:
        if hooks is not None:
            raise ValueError("pass either registry or explicit lowering hooks")
        if argument_kind not in (None, "capability_pair"):
            raise ValueError(
                "registry lowering accepts capability-pair arguments"
            )
        return (
            ScalarLoweringHooks(
                registry.guard_data_handle,
                registry.data_is_null,
                registry.data_load_scalar,
                registry.data_store_scalar,
                registry.data_store_null,
            ),
            registry.registry_id,
            "capability_pair",
        )
    if hooks is None:
        raise ValueError("scalar lowering requires exact runtime hooks")
    if argument_kind not in (None, "backend_pair"):
        raise ValueError(
            "runtime-hook lowering accepts backend-pair arguments"
        )
    return hooks, None, "backend_pair"


def _materialize_compiled(
    *,
    generated: ast.Module,
    semantic_hash: str,
    execution_mode: str,
    hooks: ScalarLoweringHooks,
    registry_id: str | None,
    argument_kind: ArgumentKind,
    input_spec: AccessSpec,
    output_spec: AccessSpec,
) -> CompiledScalarFunction:
    if not isinstance(execution_mode, str) or not execution_mode:
        raise ValueError("execution mode must be a non-empty string")
    module_code = compile(
        generated,
        f"<python-udf-jit-scalar:{semantic_hash}>",
        "exec",
        dont_inherit=True,
        optimize=2,
    )
    namespace = {
        "__builtins__": {},
        "_scalar_cast_bool": bool,
        "_scalar_cast_float": float,
        "_scalar_cast_int": int,
        "_scalar_invalid_block": RuntimeError,
        "_udf_guard_data_handle": hooks.guard,
        "_udf_data_is_null": hooks.is_null,
        "_udf_data_load": hooks.load,
        "_udf_data_store": hooks.store,
        "_udf_data_store_null": hooks.store_null,
        "_udf_build_continuation_payload": (
            hooks.build_continuation_payload
        ),
    }
    exec(module_code, namespace)
    function = namespace["_verified_scalar_region"]
    if not isinstance(function, FunctionType):
        raise RuntimeError("scalar lowering did not create a function")
    code_hash = hashlib.sha256(
        b"python-udf-jit-scalar-code-1.0\0"
        + semantic_hash.encode("ascii")
        + input_spec.layout_kind.encode("ascii")
        + input_spec.scalar_type.encode("ascii")
        + bytes([input_spec.nullable])
        + output_spec.scalar_type.encode("ascii")
        + bytes([output_spec.nullable])
        + marshal.dumps(function.__code__)
    ).hexdigest()
    return CompiledScalarFunction(
        semantic_hash,
        code_hash,
        execution_mode,
        function.__code__,
        function,
        registry_id,
        argument_kind,
        input_spec,
        output_spec,
    )


def compile_scalar_region(
    module: CoreUdfModule,
    region: VerifiedRegion,
    *,
    registry: CapabilityRegistry | None = None,
    hooks: ScalarLoweringHooks | None = None,
    execution_mode: str = "python-interpreter",
    argument_kind: ArgumentKind | None = None,
) -> CompiledScalarFunction:
    """Lower the narrow verified core IR through the formal scalar ABI."""

    verify_core_module(module)
    verify_region(module, region)
    resolved, registry_id, resolved_kind = _resolve_hooks(
        registry=registry,
        hooks=hooks,
        argument_kind=argument_kind,
    )
    input_spec = scalar_input_spec(
        FLOAT64_SCALAR_TYPE,
        nullable=False,
    )
    output_spec = scalar_output_spec(
        FLOAT64_SCALAR_TYPE,
        nullable=False,
    )
    return _materialize_compiled(
        generated=_build_core_function_ast(module, region),
        semantic_hash=module.semantic_hash,
        execution_mode=execution_mode,
        hooks=resolved,
        registry_id=registry_id,
        argument_kind=resolved_kind,
        input_spec=input_spec,
        output_spec=output_spec,
    )


def compile_semantic_scalar_region(
    module: SemanticCoreModule,
    graph: SemanticRegionGraph,
    *,
    input_spec: AccessSpec,
    output_spec: AccessSpec,
    registry: CapabilityRegistry | None = None,
    hooks: ScalarLoweringHooks | None = None,
    execution_mode: str = "python-interpreter",
    argument_kind: ArgumentKind | None = None,
    continuation_contract: ContinuationContract | None = None,
) -> CompiledScalarFunction:
    """Materialize process-local code from the formal scalar region graph."""

    verify_semantic_module(module)
    verify_semantic_region_graph(module, graph)
    _verify_semantic_scalar_contract(
        module,
        graph,
        input_spec,
        output_spec,
        continuation_contract,
    )
    resolved, registry_id, resolved_kind = _resolve_hooks(
        registry=registry,
        hooks=hooks,
        argument_kind=argument_kind,
    )
    return _materialize_compiled(
        generated=_build_semantic_function_ast(
            module,
            graph,
            input_nullable=input_spec.nullable,
            output_nullable=output_spec.nullable,
            continuation_contract=continuation_contract,
        ),
        semantic_hash=module.semantic_hash,
        execution_mode=execution_mode,
        hooks=resolved,
        registry_id=registry_id,
        argument_kind=resolved_kind,
        input_spec=input_spec,
        output_spec=output_spec,
    )
