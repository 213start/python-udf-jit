from __future__ import annotations

import hashlib
import json
import sys
import types
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


IDENTITY_VERSION = 1
MAX_IDENTITY_BYTES = 1 << 20
MAX_IDENTITY_ITEMS = 65_536
MAX_IDENTITY_DEPTH = 16


class IdentityRejectCode(StrEnum):
    INVALID_FUNCTION = "invalid_identity_function"
    UNSUPPORTED_DEPENDENCY = "unsupported_dependency"
    DEPENDENCY_BUDGET = "dependency_budget_exceeded"
    INVALID_IDENTITY = "invalid_identity"


class IdentityError(ValueError):
    def __init__(self, code: IdentityRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(code: IdentityRejectCode, detail: str = "") -> None:
    raise IdentityError(code, detail)


def _canonical_bytes(document: object) -> bytes:
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > MAX_IDENTITY_BYTES:
        _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "canonical_bytes")
    return encoded


def _sha256(document: object) -> str:
    return hashlib.sha256(_canonical_bytes(document)).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_constant(value: object, *, depth: int = 0) -> object:
    if depth > MAX_IDENTITY_DEPTH:
        _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "constant_depth")
    value_type = type(value)
    if value is None:
        return ["none", None]
    if value is Ellipsis:
        return ["ellipsis", None]
    if value_type is bool:
        return ["bool", value]
    if value_type is int:
        if value.bit_length() > MAX_IDENTITY_BYTES * 8:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "integer")
        return ["int", str(value)]
    if value_type is float:
        return ["float", value.hex()]
    if value_type is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if value_type is str:
        if len(value) > MAX_IDENTITY_BYTES:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "string")
        return ["str", value]
    if value_type is bytes:
        if len(value) > MAX_IDENTITY_BYTES:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "bytes")
        return ["bytes", value.hex()]
    if value_type is tuple:
        if len(value) > MAX_IDENTITY_ITEMS:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "tuple")
        return [
            "tuple",
            [_safe_constant(item, depth=depth + 1) for item in value],
        ]
    if value_type is frozenset:
        if len(value) > MAX_IDENTITY_ITEMS:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "frozenset")
        encoded = [
            _safe_constant(item, depth=depth + 1) for item in value
        ]
        return [
            "frozenset",
            sorted(encoded, key=lambda item: _canonical_bytes(item)),
        ]
    if value_type is types.CodeType:
        return ["code", _code_document(value, depth=depth + 1)]
    _fail(IdentityRejectCode.UNSUPPORTED_DEPENDENCY, "opaque_exact_type")


def _code_document(code: types.CodeType, *, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_IDENTITY_DEPTH:
        _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "code_depth")
    if len(code.co_code) > MAX_IDENTITY_BYTES:
        _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "code_bytes")
    return {
        "argcount": code.co_argcount,
        "bytecode": code.co_code.hex(),
        "cellvars": [_text_hash(value) for value in code.co_cellvars],
        "consts": [
            _safe_constant(value, depth=depth + 1)
            for value in code.co_consts
        ],
        "exceptiontable": code.co_exceptiontable.hex(),
        "flags": code.co_flags,
        "freevars": [_text_hash(value) for value in code.co_freevars],
        "kwonlyargcount": code.co_kwonlyargcount,
        "names": [_text_hash(value) for value in code.co_names],
        "nlocals": code.co_nlocals,
        "posonlyargcount": code.co_posonlyargcount,
        "stacksize": code.co_stacksize,
    }


def _dependency_value(
    value: object,
    *,
    depth: int = 0,
    seen_functions: frozenset[int] = frozenset(),
) -> object:
    if depth > MAX_IDENTITY_DEPTH:
        _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "dependency_depth")
    value_type = type(value)
    try:
        return ["constant", _safe_constant(value, depth=depth + 1)]
    except IdentityError as error:
        if error.code is not IdentityRejectCode.UNSUPPORTED_DEPENDENCY:
            raise

    if value_type is dict:
        if len(value) > MAX_IDENTITY_ITEMS:
            _fail(IdentityRejectCode.DEPENDENCY_BUDGET, "dict")
        entries = []
        for key, item in value.items():
            key_document = _safe_constant(key, depth=depth + 1)
            entries.append(
                [
                    key_document,
                    _dependency_value(
                        item,
                        depth=depth + 1,
                        seen_functions=seen_functions,
                    ),
                ]
            )
        return [
            "dict",
            sorted(entries, key=lambda item: _canonical_bytes(item[0])),
        ]
    if value_type is types.FunctionType:
        code_sha256 = _sha256(
            _code_document(value.__code__, depth=depth + 1)
        )
        marker = id(value)
        if marker in seen_functions:
            return ["function_cycle", code_sha256]
        nested_seen = seen_functions | {marker}
        defaults = [
            _dependency_value(
                item,
                depth=depth + 1,
                seen_functions=nested_seen,
            )
            for item in (value.__defaults__ or ())
        ]
        closure_values = []
        for cell in value.__closure__ or ():
            try:
                item = cell.cell_contents
            except ValueError as error:
                raise IdentityError(
                    IdentityRejectCode.UNSUPPORTED_DEPENDENCY,
                    "empty_closure_cell",
                ) from error
            closure_values.append(
                _dependency_value(
                    item,
                    depth=depth + 1,
                    seen_functions=nested_seen,
                )
            )
        globals_document = []
        namespace = value.__globals__
        for name in sorted(set(value.__code__.co_names)):
            if name in namespace:
                globals_document.append(
                    [
                        _text_hash(name),
                        _dependency_value(
                            namespace[name],
                            depth=depth + 1,
                            seen_functions=nested_seen,
                        ),
                    ]
                )
        return [
            "function",
            code_sha256,
            defaults,
            closure_values,
            globals_document,
        ]
    if value_type in {
        types.BuiltinFunctionType,
        types.BuiltinMethodType,
    }:
        module = value.__module__
        name = value.__name__
        if type(module) is not str or type(name) is not str:
            _fail(
                IdentityRejectCode.UNSUPPORTED_DEPENDENCY,
                "builtin_metadata",
            )
        return ["builtin", _text_hash(module), _text_hash(name)]
    if value_type is types.ModuleType:
        namespace = vars(value)
        module_name = namespace.get("__name__")
        module_version = namespace.get("__version__")
        if type(module_name) is not str:
            _fail(
                IdentityRejectCode.UNSUPPORTED_DEPENDENCY,
                "module_name",
            )
        version_document = (
            _safe_constant(module_version, depth=depth + 1)
            if type(module_version) in {str, int, float, tuple}
            else ["absent", None]
        )
        return [
            "module",
            _text_hash(module_name),
            version_document,
        ]
    if value is bool or value is float or value is int or value is str:
        return ["builtin_type", value.__name__]
    _fail(IdentityRejectCode.UNSUPPORTED_DEPENDENCY, "opaque_exact_type")


@dataclass(frozen=True)
class CodeIdentity:
    format_version: int
    python_tag: str
    sha256: str

    def to_document(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "python_tag": self.python_tag,
            "sha256": self.sha256,
        }

    @classmethod
    def from_document(cls, document: object) -> "CodeIdentity":
        expected = {"format_version", "python_tag", "sha256"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid code identity fields")
        result = cls(
            document["format_version"],
            document["python_tag"],
            document["sha256"],
        )
        verify_code_identity(result)
        return result


@dataclass(frozen=True)
class DependencyEntry:
    kind: str
    name_sha256: str
    value_sha256: str

    def to_document(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name_sha256": self.name_sha256,
            "value_sha256": self.value_sha256,
        }

    @classmethod
    def from_document(cls, document: object) -> "DependencyEntry":
        expected = {"kind", "name_sha256", "value_sha256"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid dependency entry fields")
        if not all(isinstance(document[name], str) for name in expected):
            raise ValueError("invalid dependency entry string")
        result = cls(
            document["kind"],
            document["name_sha256"],
            document["value_sha256"],
        )
        if (
            result.kind not in {"closure", "default", "global", "kwdefault"}
            or not _valid_digest(result.name_sha256)
            or not _valid_digest(result.value_sha256)
        ):
            raise ValueError("invalid dependency entry")
        return result


@dataclass(frozen=True)
class DependencyFingerprint:
    format_version: int
    entries: tuple[DependencyEntry, ...]
    sha256: str

    def semantic_document(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_document() for entry in self.entries],
            "format_version": self.format_version,
        }

    def recompute_sha256(self) -> str:
        return _sha256(self.semantic_document())

    def to_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document(),
            "sha256": self.sha256,
        }

    @classmethod
    def from_document(cls, document: object) -> "DependencyFingerprint":
        expected = {"entries", "format_version", "sha256"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid dependency fingerprint fields")
        if not isinstance(document["entries"], list):
            raise ValueError("invalid dependency entries")
        result = cls(
            document["format_version"],
            tuple(
                DependencyEntry.from_document(entry)
                for entry in document["entries"]
            ),
            document["sha256"],
        )
        verify_dependency_fingerprint(result)
        return result


@dataclass(frozen=True)
class SourceIdentity:
    format_version: int
    namespace_sha256: str
    code_sha256: str
    first_line: int

    def to_document(self) -> dict[str, Any]:
        return {
            "code_sha256": self.code_sha256,
            "first_line": self.first_line,
            "format_version": self.format_version,
            "namespace_sha256": self.namespace_sha256,
        }

    @classmethod
    def from_document(cls, document: object) -> "SourceIdentity":
        expected = {
            "code_sha256",
            "first_line",
            "format_version",
            "namespace_sha256",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid source identity fields")
        result = cls(
            document["format_version"],
            document["namespace_sha256"],
            document["code_sha256"],
            document["first_line"],
        )
        verify_source_identity(result)
        return result


@dataclass(frozen=True)
class CaptureIdentities:
    code: CodeIdentity
    dependency: DependencyFingerprint
    source: SourceIdentity

    def to_document(self) -> dict[str, Any]:
        return {
            "code": self.code.to_document(),
            "dependency": self.dependency.to_document(),
            "source": self.source.to_document(),
        }

    @classmethod
    def from_document(cls, document: object) -> "CaptureIdentities":
        expected = {"code", "dependency", "source"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid capture identities fields")
        return cls(
            CodeIdentity.from_document(document["code"]),
            DependencyFingerprint.from_document(document["dependency"]),
            SourceIdentity.from_document(document["source"]),
        )


def code_identity(function: types.FunctionType) -> CodeIdentity:
    if type(function) is not types.FunctionType:
        _fail(IdentityRejectCode.INVALID_FUNCTION)
    document = _code_document(function.__code__)
    return CodeIdentity(
        IDENTITY_VERSION,
        sys.implementation.cache_tag,
        _sha256(document),
    )


def _entry(kind: str, name: str, value: object) -> DependencyEntry:
    return DependencyEntry(
        kind,
        _text_hash(name),
        _sha256(_dependency_value(value)),
    )


def dependency_fingerprint(
    function: types.FunctionType,
) -> DependencyFingerprint:
    if type(function) is not types.FunctionType:
        _fail(IdentityRejectCode.INVALID_FUNCTION)
    entries: list[DependencyEntry] = []
    defaults = function.__defaults__ or ()
    for index, value in enumerate(defaults):
        entries.append(_entry("default", str(index), value))
    kwdefaults = function.__kwdefaults__ or {}
    if type(kwdefaults) is not dict:
        _fail(IdentityRejectCode.UNSUPPORTED_DEPENDENCY, "kwdefaults")
    kwdefault_names = tuple(kwdefaults)
    if any(type(name) is not str for name in kwdefault_names):
        _fail(IdentityRejectCode.UNSUPPORTED_DEPENDENCY, "kwdefault_name")
    for name in sorted(kwdefault_names):
        entries.append(_entry("kwdefault", name, kwdefaults[name]))
    closure = function.__closure__ or ()
    if len(closure) != len(function.__code__.co_freevars):
        _fail(IdentityRejectCode.INVALID_FUNCTION, "closure_shape")
    for name, cell in zip(
        function.__code__.co_freevars,
        closure,
        strict=True,
    ):
        try:
            value = cell.cell_contents
        except ValueError as error:
            raise IdentityError(
                IdentityRejectCode.UNSUPPORTED_DEPENDENCY,
                "empty_closure_cell",
            ) from error
        entries.append(_entry("closure", name, value))

    namespace = function.__globals__
    if type(namespace) is not dict:
        _fail(IdentityRejectCode.INVALID_FUNCTION, "globals")
    for name in sorted(set(function.__code__.co_names)):
        if name in namespace:
            entries.append(_entry("global", name, namespace[name]))
    entries.sort(
        key=lambda entry: (
            entry.kind,
            entry.name_sha256,
            entry.value_sha256,
        )
    )
    provisional = DependencyFingerprint(
        IDENTITY_VERSION,
        tuple(entries),
        "",
    )
    return DependencyFingerprint(
        provisional.format_version,
        provisional.entries,
        provisional.recompute_sha256(),
    )


def capture_identities(
    function: types.FunctionType,
    *,
    namespace_salt: str = "python-udf-jit",
) -> CaptureIdentities:
    if type(namespace_salt) is not str or not namespace_salt:
        _fail(IdentityRejectCode.INVALID_IDENTITY, "namespace_salt")
    code = code_identity(function)
    module = function.__module__
    qualname = function.__qualname__
    if type(module) is not str or type(qualname) is not str:
        _fail(IdentityRejectCode.INVALID_FUNCTION, "source_metadata")
    source = SourceIdentity(
        IDENTITY_VERSION,
        _text_hash(f"{namespace_salt}\0{module}\0{qualname}"),
        code.sha256,
        function.__code__.co_firstlineno,
    )
    result = CaptureIdentities(
        code,
        dependency_fingerprint(function),
        source,
    )
    verify_capture_identities(result)
    return result


def verify_code_identity(identity: CodeIdentity) -> None:
    if (
        identity.format_version != IDENTITY_VERSION
        or identity.python_tag != "cpython-314"
        or not _valid_digest(identity.sha256)
    ):
        raise ValueError("invalid code identity")


def verify_dependency_fingerprint(
    fingerprint: DependencyFingerprint,
) -> None:
    if (
        fingerprint.format_version != IDENTITY_VERSION
        or tuple(fingerprint.entries)
        != tuple(
            sorted(
                fingerprint.entries,
                key=lambda entry: (
                    entry.kind,
                    entry.name_sha256,
                    entry.value_sha256,
                ),
            )
        )
        or not _valid_digest(fingerprint.sha256)
        or fingerprint.recompute_sha256() != fingerprint.sha256
    ):
        raise ValueError("invalid dependency fingerprint")


def verify_source_identity(identity: SourceIdentity) -> None:
    if (
        identity.format_version != IDENTITY_VERSION
        or not _valid_digest(identity.namespace_sha256)
        or not _valid_digest(identity.code_sha256)
        or type(identity.first_line) is not int
        or identity.first_line < 0
    ):
        raise ValueError("invalid source identity")


def verify_capture_identities(identities: CaptureIdentities) -> None:
    verify_code_identity(identities.code)
    verify_dependency_fingerprint(identities.dependency)
    verify_source_identity(identities.source)
    if identities.source.code_sha256 != identities.code.sha256:
        raise ValueError("source identity code mismatch")
