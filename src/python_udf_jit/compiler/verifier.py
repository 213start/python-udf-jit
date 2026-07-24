from __future__ import annotations

from enum import StrEnum

from python_udf_jit.compiler.core_ir import CORE_IR_VERSION, CoreUdfModule


class VerificationRejectCode(StrEnum):
    INVALID_MODULE = "invalid_module"
    NODE_LIMIT = "node_limit"
    DUPLICATE_VALUE = "duplicate_value"
    INVALID_OPERAND = "invalid_operand"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_RETURN = "invalid_return"
    SEMANTIC_HASH_MISMATCH = "semantic_hash_mismatch"
    REGION_MISMATCH = "region_mismatch"


class VerificationError(ValueError):
    def __init__(self, code: VerificationRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(code: VerificationRejectCode, detail: str = "") -> None:
    raise VerificationError(code, detail)


def verify_core_module(module: CoreUdfModule, *, max_nodes: int = 256, max_constants: int = 256) -> None:
    if (
        module.format_version != CORE_IR_VERSION
        or module.input_type != "float64"
        or module.output_type != "float64"
        or module.effect != "pure"
    ):
        _fail(VerificationRejectCode.INVALID_MODULE)
    if not module.nodes or len(module.nodes) > max_nodes:
        _fail(VerificationRejectCode.NODE_LIMIT)
    supported_operations = {"arg.load", "const.f64", "add.f64", "sub.f64", "mul.f64", "return"}
    unsupported = next((node.op for node in module.nodes if node.op not in supported_operations), None)
    if unsupported is not None:
        _fail(VerificationRejectCode.UNSUPPORTED_OPERATION, unsupported)

    values: set[str] = set()
    expected_value_index = 0
    return_count = 0
    constant_count = 0
    for index, node in enumerate(module.nodes):
        if node.result_type != "float64":
            _fail(VerificationRejectCode.TYPE_MISMATCH)
        if node.op == "return":
            return_count += 1
            if index != len(module.nodes) - 1 or node.result_id is not None or node.literal is not None:
                _fail(VerificationRejectCode.INVALID_RETURN)
            if len(node.operands) != 1 or node.operands[0] not in values:
                _fail(VerificationRejectCode.INVALID_RETURN)
            continue

        expected_id = f"%{expected_value_index}"
        expected_value_index += 1
        if node.result_id in values:
            _fail(VerificationRejectCode.DUPLICATE_VALUE, str(node.result_id))
        if node.result_id != expected_id:
            _fail(VerificationRejectCode.INVALID_MODULE, "noncanonical_value_id")
        if node.op == "arg.load":
            if values or node.operands or node.literal is not None:
                _fail(VerificationRejectCode.INVALID_MODULE, "arg_load")
        elif node.op == "const.f64":
            constant_count += 1
            if node.operands or type(node.literal) is not float:
                _fail(VerificationRejectCode.TYPE_MISMATCH, "const")
        else:
            if node.literal is not None or len(node.operands) != 2:
                _fail(VerificationRejectCode.INVALID_OPERAND, node.op)
            if any(operand not in values for operand in node.operands):
                _fail(VerificationRejectCode.INVALID_OPERAND, node.op)
        values.add(node.result_id)
    if constant_count > max_constants:
        _fail(VerificationRejectCode.NODE_LIMIT, "constants")
    if return_count != 1 or module.return_value not in values:
        _fail(VerificationRejectCode.INVALID_RETURN)
    if module.nodes[-1].operands != (module.return_value,):
        _fail(VerificationRejectCode.INVALID_RETURN)
    if module.recompute_semantic_hash() != module.semantic_hash:
        _fail(VerificationRejectCode.SEMANTIC_HASH_MISMATCH)


def verify_region(module: CoreUdfModule, region: object) -> None:
    verify_core_module(module)
    required = (
        getattr(region, "entry_values", None) == ("%0",),
        getattr(region, "exit_values", None) == (module.return_value,),
        getattr(region, "operation_indexes", None) == tuple(range(len(module.nodes))),
        getattr(region, "pure", None) is True,
        getattr(region, "single_entry", None) is True,
        getattr(region, "single_exit", None) is True,
        getattr(region, "semantic_hash", None) == module.semantic_hash,
    )
    if not all(required):
        _fail(VerificationRejectCode.REGION_MISMATCH)
