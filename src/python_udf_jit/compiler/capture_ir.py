from __future__ import annotations

import json
import types
from dataclasses import dataclass
from typing import Any

from python_udf_jit.compiler.bytecode_decoder import (
    DecodedBytecode,
    decode_code,
    verify_decoded_bytecode,
)
from python_udf_jit.compiler.cfg import (
    ControlFlowGraph,
    build_control_flow_graph,
    verify_control_flow_graph,
)


CAPTURE_FRONTEND_VERSION = 1


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class CaptureFrontend:
    format_version: int
    decoded_bytecode: DecodedBytecode
    control_flow_graph: ControlFlowGraph
    required_capabilities: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "control_flow_graph": self.control_flow_graph.to_document(),
            "decoded_bytecode": self.decoded_bytecode.to_document(),
            "format_version": self.format_version,
            "required_capabilities": list(self.required_capabilities),
        }

    def canonical_bytes(self) -> bytes:
        verify_capture_frontend(self)
        return _canonical_bytes(self.to_document())

    @classmethod
    def from_document(cls, document: object) -> "CaptureFrontend":
        expected = {
            "control_flow_graph",
            "decoded_bytecode",
            "format_version",
            "required_capabilities",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid capture frontend fields")
        if type(document["format_version"]) is not int:
            raise ValueError("invalid capture frontend version")
        capabilities = document["required_capabilities"]
        if not isinstance(capabilities, list) or not all(
            isinstance(value, str) for value in capabilities
        ):
            raise ValueError("invalid capture frontend capabilities")
        result = cls(
            document["format_version"],
            DecodedBytecode.from_document(document["decoded_bytecode"]),
            ControlFlowGraph.from_document(document["control_flow_graph"]),
            tuple(capabilities),
        )
        verify_capture_frontend(result)
        return result


def _required_capabilities(decoded: DecodedBytecode) -> tuple[str, ...]:
    capabilities = {"static_capture"}
    for instruction in decoded.instructions:
        if instruction.capability == "python_region":
            capabilities.add("python_region")
        if instruction.operation == "aggregate.tuple":
            capabilities.add("readonly_tuple")
        elif instruction.operation == "aggregate.list":
            capabilities.add("readonly_list")
        elif instruction.constant_kind == "str":
            capabilities.add("controlled_str")
        elif instruction.operation == "field.load":
            capabilities.add("fixed_field")
        elif instruction.operation == "index.load":
            capabilities.add("fixed_index")
        elif instruction.operation.startswith("exception."):
            capabilities.add("exception_flow")
    return tuple(sorted(capabilities))


def build_capture_frontend(code: types.CodeType) -> CaptureFrontend:
    decoded = decode_code(code)
    graph = build_control_flow_graph(decoded)
    result = CaptureFrontend(
        CAPTURE_FRONTEND_VERSION,
        decoded,
        graph,
        _required_capabilities(decoded),
    )
    verify_capture_frontend(result)
    return result


def verify_capture_frontend(frontend: CaptureFrontend) -> None:
    if frontend.format_version != CAPTURE_FRONTEND_VERSION:
        raise ValueError("unsupported capture frontend version")
    if (
        not frontend.required_capabilities
        or frontend.required_capabilities
        != tuple(sorted(set(frontend.required_capabilities)))
        or "static_capture" not in frontend.required_capabilities
    ):
        raise ValueError("invalid capture frontend capabilities")
    verify_decoded_bytecode(frontend.decoded_bytecode)
    verify_control_flow_graph(
        frontend.control_flow_graph,
        frontend.decoded_bytecode,
    )
    if frontend.required_capabilities != _required_capabilities(
        frontend.decoded_bytecode
    ):
        raise ValueError("capture frontend capability mismatch")
