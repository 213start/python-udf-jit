from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from python_udf_jit.compiler import bytecode_decoder
from python_udf_jit.compiler.bytecode_decoder import (
    BytecodeDecodeError,
    DecodeRejectCode,
    DecodedBytecode,
    decode_code,
)
from python_udf_jit.compiler.cfg import build_control_flow_graph


def arithmetic(value):
    return value * 2.0 + 3.0


def comparison(value):
    return value > 0.0


def comparison_less_equal(value):
    return value <= 0.0


def local_branch(value):
    result = 1.0
    if value > 0.0:
        result = value
    return result


def exception_path(value):
    try:
        return value + 1.0
    except TypeError:
        return 0.0


def fixed_field(value):
    return value.price


def fixed_index(value):
    return value[0]


def controlled_str(value):
    return value + "!"


def controlled_str_method(value):
    return value.upper()


def readonly_tuple(value):
    return (value, 1.0)


def readonly_list(value):
    return [value, 1.0]


class BytecodeDecoderTest(unittest.TestCase):
    def test_selects_the_locked_cpython_314_wordcode_contract(self):
        decoded = decode_code(arithmetic.__code__)

        self.assertEqual(decoded.bytecode_format.implementation, "cpython")
        self.assertEqual(
            (decoded.bytecode_format.major, decoded.bytecode_format.minor),
            (3, 14),
        )
        self.assertEqual(decoded.bytecode_format.cache_tag, "cpython-314")
        self.assertEqual(decoded.bytecode_format.soabi_family, "cpython-314")
        self.assertEqual(
            decoded.bytecode_format.decoder_id,
            "cpython-3.14-wordcode-v1",
        )

    def test_rejects_an_unsupported_runtime_format_before_decoding(self):
        with mock.patch.object(
            bytecode_decoder.platform,
            "python_implementation",
            return_value="PyPy",
        ):
            with self.assertRaises(BytecodeDecodeError) as raised:
                decode_code(arithmetic.__code__)

        self.assertEqual(
            raised.exception.code,
            DecodeRejectCode.UNSUPPORTED_FORMAT,
        )

    def test_normalizes_required_opcode_families_without_source_values(self):
        cases = {
            arithmetic: {"binary.multiply", "binary.add"},
            comparison: {"compare.greater"},
            comparison_less_equal: {"compare.less_equal"},
            local_branch: {"local.store", "branch.if_false"},
            exception_path: {"exception.push", "exception.match", "exception.reraise"},
            fixed_field: {"field.load"},
            fixed_index: {"index.load"},
            controlled_str: {"binary.add"},
            controlled_str_method: {"method.load", "call.opaque"},
            readonly_tuple: {"aggregate.tuple"},
            readonly_list: {"aggregate.list"},
        }

        for function, required in cases.items():
            with self.subTest(function=function.__name__):
                decoded = decode_code(function.__code__)
                graph = build_control_flow_graph(decoded)
                operations = {
                    instruction.operation for instruction in decoded.instructions
                }
                self.assertTrue(required <= operations)
                self.assertEqual(
                    len(graph.instruction_states),
                    len(decoded.instructions),
                )
                document = decoded.to_document()
                encoded = json.dumps(document, sort_keys=True)
                self.assertNotIn(function.__code__.co_filename, encoded)
                self.assertNotIn('"!"', encoded)

    def test_decodes_exception_and_location_tables_with_complete_coverage(self):
        decoded = decode_code(exception_path.__code__)

        self.assertGreaterEqual(len(decoded.exception_handlers), 1)
        offsets = tuple(
            instruction.offset for instruction in decoded.instructions
        )
        self.assertEqual(
            tuple(entry.bytecode_offset for entry in decoded.source_map.entries),
            offsets,
        )
        for handler in decoded.exception_handlers:
            self.assertIn(handler.start_offset, offsets)
            self.assertIn(handler.target_offset, offsets)
            self.assertGreater(handler.end_offset, handler.start_offset)

    def test_canonical_encoding_round_trips_and_is_cross_process_stable(self):
        decoded = decode_code(local_branch.__code__)
        encoded = decoded.canonical_bytes()

        self.assertEqual(
            DecodedBytecode.from_document(json.loads(encoded)),
            decoded,
        )
        expected_hash = hashlib.sha256(encoded).hexdigest()
        command = (
            "import hashlib;"
            "from tests.unit.compiler.test_bytecode_decoder import local_branch;"
            "from python_udf_jit.compiler.bytecode_decoder import decode_code;"
            "print(hashlib.sha256("
            "decode_code(local_branch.__code__).canonical_bytes()"
            ").hexdigest())"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "src"
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), expected_hash)

    def test_rejects_corrupt_location_and_exception_tables(self):
        corruptions = (
            (
                arithmetic.__code__.replace(co_linetable=b""),
                DecodeRejectCode.INVALID_LOCATION_TABLE,
            ),
            (
                exception_path.__code__.replace(co_exceptiontable=b"\x80"),
                DecodeRejectCode.INVALID_EXCEPTION_TABLE,
            ),
        )

        for code, expected in corruptions:
            with self.subTest(expected=expected):
                with self.assertRaises(BytecodeDecodeError) as raised:
                    decode_code(code)
                self.assertEqual(raised.exception.code, expected)

    def test_rejects_unknown_opcode_and_invalid_jump_shapes(self):
        with self.assertRaises(BytecodeDecodeError) as raised:
            bytecode_decoder._read_logical_instructions(bytes((121, 0)))
        self.assertEqual(raised.exception.code, DecodeRejectCode.UNKNOWN_OPCODE)

        code = arithmetic.__code__.replace(
            co_code=bytes(
                (
                    128,
                    0,  # RESUME
                    77,
                    255,  # JUMP_FORWARD beyond the code object
                    35,
                    0,  # RETURN_VALUE
                )
            )
        )
        with self.assertRaises(BytecodeDecodeError) as raised:
            decode_code(code)
        self.assertEqual(raised.exception.code, DecodeRejectCode.INVALID_JUMP)

    def test_decoded_verifier_rejects_structural_mutation(self):
        decoded = decode_code(arithmetic.__code__)
        first = decoded.instructions[0]
        corrupted = dataclasses.replace(
            decoded,
            instructions=(
                dataclasses.replace(first, offset=2),
                *decoded.instructions[1:],
            ),
        )

        with self.assertRaisesRegex(ValueError, "offset"):
            corrupted.canonical_bytes()


if __name__ == "__main__":
    unittest.main()
