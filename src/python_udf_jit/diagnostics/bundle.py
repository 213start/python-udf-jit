"""Safe, bounded, non-executable diagnostic bundle storage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from python_udf_jit.diagnostics.config import DiagnosticPolicySnapshot


_BUNDLE_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ARTIFACTS = 4096
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 65536
_MAX_JSON_STRING = 4096


class BundleStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"


class BundleRejectCode(StrEnum):
    POLICY_OFF = "bundle_policy_off"
    PATH_INVALID = "bundle_path_invalid"
    PATH_SYMLINK = "bundle_path_symlink"
    PATH_HARDLINK = "bundle_path_hardlink"
    FILE_EXISTS = "bundle_file_exists"
    PAYLOAD_INVALID = "bundle_payload_invalid"
    MEDIA_TYPE_INVALID = "bundle_media_type_invalid"
    METADATA_INVALID = "bundle_metadata_invalid"
    BUDGET_EXCEEDED = "bundle_budget_exceeded"
    STATE_INVALID = "bundle_state_invalid"
    MANIFEST_INVALID = "bundle_manifest_invalid"
    UNSUPPORTED_VERSION = "bundle_unsupported_version"
    HASH_MISMATCH = "bundle_hash_mismatch"
    SIZE_MISMATCH = "bundle_size_mismatch"
    PERMISSION_INVALID = "bundle_permission_invalid"
    COMPLETE_MARKER_INVALID = "bundle_complete_marker_invalid"
    TOTAL_SIZE_LIMIT = "bundle_total_size_limit"
    IO_ERROR = "bundle_io_error"


class DiagnosticBundleError(ValueError):
    def __init__(self, code: BundleRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(
            code.value if not detail else f"{code.value}:{detail}"
        )


def _fail(code: BundleRejectCode, detail: str = "") -> None:
    raise DiagnosticBundleError(code, detail)


@dataclass(frozen=True)
class BundleRunContext:
    run_id: str
    runtime_mode: str
    process_key: str

    def __post_init__(self) -> None:
        for field in ("run_id", "runtime_mode", "process_key"):
            value = getattr(self, field)
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                _fail(BundleRejectCode.METADATA_INVALID, field)


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    media_type: str
    layer: str
    sha256: str
    byte_size: int
    optional: bool = False
    unavailable_reason: str | None = None

    @property
    def document(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "layer": self.layer,
            "media_type": self.media_type,
            "optional": self.optional,
            "path": self.path,
            "sha256": self.sha256,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class BundleRef:
    path: Path
    status: BundleStatus
    manifest_sha256: str


@dataclass(frozen=True)
class DiagnosticBundle:
    path: Path
    status: BundleStatus
    manifest: Mapping[str, object]
    artifacts: tuple[ArtifactRef, ...]


def _canonical_json(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        _fail(BundleRejectCode.METADATA_INVALID, type(error).__name__)


def _relative_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        _fail(BundleRejectCode.PATH_INVALID)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or raw != path.as_posix()
        or any(part in ("", ".", "..") for part in path.parts)
        or path.name in ("manifest.json", "COMPLETE")
    ):
        _fail(BundleRejectCode.PATH_INVALID)
    return path


def _private_directory(path: Path) -> None:
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    if mode != 0o700:
        _fail(BundleRejectCode.PERMISSION_INVALID, os.fspath(path))


def _private_file(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        _fail(BundleRejectCode.IO_ERROR, type(error).__name__)
    if stat.S_ISLNK(info.st_mode):
        _fail(BundleRejectCode.PATH_SYMLINK, os.fspath(path))
    if not stat.S_ISREG(info.st_mode):
        _fail(BundleRejectCode.PATH_INVALID, os.fspath(path))
    if info.st_nlink != 1:
        _fail(BundleRejectCode.PATH_HARDLINK, os.fspath(path))
    if stat.S_IMODE(info.st_mode) != 0o600:
        _fail(BundleRejectCode.PERMISSION_INVALID, os.fspath(path))
    return info


def _read_regular(path: Path, *, limit: int) -> bytes:
    before = _private_file(path)
    if before.st_size > limit:
        _fail(BundleRejectCode.TOTAL_SIZE_LIMIT)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                _fail(BundleRejectCode.PATH_HARDLINK)
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > limit:
                _fail(BundleRejectCode.TOTAL_SIZE_LIMIT)
            return payload
        finally:
            os.close(descriptor)
    except DiagnosticBundleError:
        raise
    except OSError as error:
        _fail(BundleRejectCode.IO_ERROR, type(error).__name__)


class BundleWriter:
    """Single-use writer that publishes a verified manifest last."""

    def __init__(
        self,
        policy: DiagnosticPolicySnapshot,
        run_context: BundleRunContext,
    ) -> None:
        if not policy.enabled or policy.output_root is None:
            _fail(BundleRejectCode.POLICY_OFF)
        self._policy = policy
        self._context = run_context
        self._output_root = policy.output_root
        self._ensure_output_root()
        try:
            temporary = tempfile.mkdtemp(
                prefix=f".udfjit-{run_context.run_id}-",
                dir=self._output_root,
            )
            self._temporary_path = Path(temporary)
            os.chmod(self._temporary_path, 0o700)
        except OSError as error:
            _fail(BundleRejectCode.IO_ERROR, type(error).__name__)
        self._artifacts: list[ArtifactRef] = []
        self._artifact_bytes = 0
        self._budget_exhausted = 0
        self._writer_failures = 0
        self._created_ns = time.time_ns()
        self._finalized = False

    @property
    def temporary_path(self) -> Path:
        return self._temporary_path

    def _ensure_output_root(self) -> None:
        current = Path(self._output_root.anchor)
        try:
            for part in self._output_root.parts[1:]:
                current /= part
                if current.is_symlink():
                    _fail(
                        BundleRejectCode.PATH_SYMLINK,
                        os.fspath(current),
                    )
                if current.exists():
                    if not current.is_dir():
                        _fail(
                            BundleRejectCode.PATH_INVALID,
                            os.fspath(current),
                        )
                    continue
                current.mkdir(mode=0o700)
            if self._output_root.is_symlink():
                _fail(BundleRejectCode.PATH_SYMLINK)
        except DiagnosticBundleError:
            raise
        except OSError as error:
            _fail(BundleRejectCode.IO_ERROR, type(error).__name__)

    def _ensure_open(self) -> None:
        if self._finalized:
            _fail(BundleRejectCode.STATE_INVALID)

    def _parent(self, path: PurePosixPath) -> Path:
        current = self._temporary_path
        for part in path.parts[:-1]:
            current /= part
            try:
                if current.is_symlink():
                    _fail(
                        BundleRejectCode.PATH_SYMLINK,
                        path.as_posix(),
                    )
                if current.exists():
                    if not current.is_dir():
                        _fail(
                            BundleRejectCode.PATH_INVALID,
                            path.as_posix(),
                        )
                    continue
                current.mkdir(mode=0o700)
                os.chmod(current, 0o700)
            except DiagnosticBundleError:
                raise
            except OSError as error:
                _fail(BundleRejectCode.IO_ERROR, type(error).__name__)
        return current

    def _write_new(self, path: Path, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fsync(descriptor)
                info = os.fstat(descriptor)
                if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                    _fail(BundleRejectCode.PATH_HARDLINK)
            finally:
                os.close(descriptor)
            os.chmod(path, 0o600, follow_symlinks=False)
        except FileExistsError:
            _fail(BundleRejectCode.FILE_EXISTS)
        except DiagnosticBundleError:
            raise
        except OSError as error:
            _fail(BundleRejectCode.IO_ERROR, type(error).__name__)

    def _manifest_document(
        self,
        *,
        status: BundleStatus,
        finalized_ns: int,
        unavailable_reason: str | None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> dict[str, object]:
        selected = self._artifacts if artifacts is None else artifacts
        return {
            "artifacts": [artifact.document for artifact in selected],
            "created_ns": self._created_ns,
            "diagnostic_policy_hash": self._policy.sha256,
            "diagnostic_profile": self._policy.profile.value,
            "dropped_counts": {
                "budget_exhausted": self._budget_exhausted,
                "writer_failures": self._writer_failures,
            },
            "finalized_ns": finalized_ns,
            "limits": {"max_bytes": self._policy.max_bytes},
            "process_key": self._context.process_key,
            "run_id": self._context.run_id,
            "run_kind": "diagnostic",
            "runtime_mode": self._context.runtime_mode,
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "status": status.value,
            "timing_scope": "diagnostic_only",
            "unavailable_reason": unavailable_reason,
        }

    def _fits_with(self, candidate: ArtifactRef) -> bool:
        artifacts = [*self._artifacts, candidate]
        manifest = _canonical_json(
            self._manifest_document(
                status=BundleStatus.INCOMPLETE,
                finalized_ns=99_999_999_999_999_999_999,
                unavailable_reason="budget_exhausted",
                artifacts=artifacts,
            )
        )
        return (
            self._artifact_bytes + candidate.byte_size + len(manifest)
            <= self._policy.max_bytes
        )

    def add(
        self,
        path: str,
        media_type: str,
        payload: bytes | str,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef | None:
        self._ensure_open()
        relative = _relative_path(path)
        if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
            _fail(BundleRejectCode.MEDIA_TYPE_INVALID)
        if isinstance(payload, str):
            encoded = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            encoded = payload
        else:
            _fail(BundleRejectCode.PAYLOAD_INVALID)
        details = {} if metadata is None else dict(metadata)
        layer = details.pop("layer", "")
        optional = details.pop("optional", False)
        unavailable_reason = details.pop("unavailable_reason", None)
        if details:
            _fail(BundleRejectCode.METADATA_INVALID)
        if (
            not isinstance(layer, str)
            or (layer and _SAFE_ID.fullmatch(layer) is None)
            or type(optional) is not bool
            or (
                unavailable_reason is not None
                and (
                    not isinstance(unavailable_reason, str)
                    or _SAFE_REASON.fullmatch(unavailable_reason) is None
                )
            )
        ):
            _fail(BundleRejectCode.METADATA_INVALID)
        candidate = ArtifactRef(
            path=relative.as_posix(),
            media_type=media_type,
            layer=layer,
            sha256=hashlib.sha256(encoded).hexdigest(),
            byte_size=len(encoded),
            optional=optional,
            unavailable_reason=unavailable_reason,
        )
        if len(self._artifacts) >= _MAX_ARTIFACTS or not self._fits_with(candidate):
            self._budget_exhausted += 1
            return None
        parent = self._parent(relative)
        target = parent / relative.name
        self._write_new(target, encoded)
        self._artifacts.append(candidate)
        self._artifact_bytes += len(encoded)
        return candidate

    def _fit_final_manifest(
        self,
        status: BundleStatus,
        unavailable_reason: str | None,
    ) -> tuple[BundleStatus, bytes]:
        while True:
            document = self._manifest_document(
                status=status,
                finalized_ns=time.time_ns(),
                unavailable_reason=unavailable_reason,
            )
            payload = _canonical_json(document)
            if self._artifact_bytes + len(payload) <= self._policy.max_bytes:
                return status, payload
            if not self._artifacts:
                _fail(BundleRejectCode.BUDGET_EXCEEDED)
            removed = self._artifacts.pop()
            target = self._temporary_path.joinpath(
                *PurePosixPath(removed.path).parts
            )
            try:
                target.unlink()
            except OSError as error:
                _fail(BundleRejectCode.IO_ERROR, type(error).__name__)
            self._artifact_bytes -= removed.byte_size
            self._budget_exhausted += 1
            if status is not BundleStatus.INCOMPLETE:
                status = BundleStatus.PARTIAL
                unavailable_reason = "budget_exhausted"

    def _verify_staging(self) -> None:
        expected = {artifact.path for artifact in self._artifacts}
        observed: set[str] = set()
        for directory, directory_names, file_names in os.walk(
            self._temporary_path,
            topdown=True,
            followlinks=False,
        ):
            directory_path = Path(directory)
            _private_directory(directory_path)
            for name in directory_names:
                child = directory_path / name
                if child.is_symlink():
                    _fail(BundleRejectCode.PATH_SYMLINK)
            for name in file_names:
                child = directory_path / name
                _private_file(child)
                relative = child.relative_to(self._temporary_path).as_posix()
                if relative not in expected:
                    _fail(
                        BundleRejectCode.MANIFEST_INVALID,
                        "unlisted_file",
                    )
                observed.add(relative)
        if observed != expected:
            _fail(BundleRejectCode.MANIFEST_INVALID, "artifact_missing")

    def _publish(
        self,
        *,
        status: BundleStatus,
        unavailable_reason: str | None,
        complete_marker: bool,
    ) -> BundleRef:
        self._ensure_open()
        if unavailable_reason is not None and _SAFE_REASON.fullmatch(
            unavailable_reason
        ) is None:
            _fail(BundleRejectCode.METADATA_INVALID)
        self._verify_staging()
        status, manifest = self._fit_final_manifest(status, unavailable_reason)
        self._write_new(self._temporary_path / "manifest.json", manifest)
        digest = hashlib.sha256(manifest).hexdigest()
        final_path = self._output_root / (
            f"diagnostic-{self._context.run_id}-{digest[:16]}"
        )
        if final_path.exists() or final_path.is_symlink():
            _fail(BundleRejectCode.FILE_EXISTS)
        try:
            os.replace(self._temporary_path, final_path)
            os.chmod(final_path, 0o700)
            if complete_marker:
                marker_tmp = final_path / f".COMPLETE-{digest[:16]}"
                self._write_new(marker_tmp, b"")
                os.replace(marker_tmp, final_path / "COMPLETE")
            self._finalized = True
        except DiagnosticBundleError:
            raise
        except OSError as error:
            _fail(BundleRejectCode.IO_ERROR, type(error).__name__)
        return BundleRef(
            path=final_path,
            status=status,
            manifest_sha256=digest,
        )

    def complete(
        self,
        status: BundleStatus = BundleStatus.COMPLETE,
    ) -> BundleRef:
        if status is BundleStatus.INCOMPLETE:
            return self.abort("diagnostic_incomplete")
        selected = (
            BundleStatus.PARTIAL
            if self._budget_exhausted or self._writer_failures
            else status
        )
        reason = None
        if selected is BundleStatus.PARTIAL:
            reason = (
                "budget_exhausted"
                if self._budget_exhausted
                else "diagnostic_partial"
            )
        return self._publish(
            status=selected,
            unavailable_reason=reason,
            complete_marker=True,
        )

    def abort(self, reason: str) -> BundleRef:
        return self._publish(
            status=BundleStatus.INCOMPLETE,
            unavailable_reason=reason,
            complete_marker=False,
        )


def open_bundle(
    policy: DiagnosticPolicySnapshot,
    run_context: BundleRunContext,
) -> BundleWriter:
    return BundleWriter(policy, run_context)


def _validate_json_shape(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            _fail(BundleRejectCode.MANIFEST_INVALID)
        if isinstance(current, str):
            if len(current) > _MAX_JSON_STRING:
                _fail(BundleRejectCode.MANIFEST_INVALID)
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > _MAX_JSON_STRING:
                    _fail(BundleRejectCode.MANIFEST_INVALID)
                stack.append((item, depth + 1))
        elif current is not None and type(current) not in (bool, int, float):
            _fail(BundleRejectCode.MANIFEST_INVALID)


def _parse_artifact(document: object) -> ArtifactRef:
    if not isinstance(document, dict) or set(document) != {
        "byte_size",
        "layer",
        "media_type",
        "optional",
        "path",
        "sha256",
        "unavailable_reason",
    }:
        _fail(BundleRejectCode.MANIFEST_INVALID)
    path = _relative_path(document["path"]).as_posix()
    media_type = document["media_type"]
    layer = document["layer"]
    digest = document["sha256"]
    byte_size = document["byte_size"]
    optional = document["optional"]
    unavailable_reason = document["unavailable_reason"]
    if (
        not isinstance(media_type, str)
        or _MEDIA_TYPE.fullmatch(media_type) is None
        or not isinstance(layer, str)
        or (layer and _SAFE_ID.fullmatch(layer) is None)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or type(byte_size) is not int
        or byte_size < 0
        or type(optional) is not bool
        or (
            unavailable_reason is not None
            and (
                not isinstance(unavailable_reason, str)
                or _SAFE_REASON.fullmatch(unavailable_reason) is None
            )
        )
    ):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    return ArtifactRef(
        path=path,
        media_type=media_type,
        layer=layer,
        sha256=digest,
        byte_size=byte_size,
        optional=optional,
        unavailable_reason=unavailable_reason,
    )


def read_bundle(path: str | os.PathLike[str]) -> DiagnosticBundle:
    """Read and validate data only; no payload is imported or executed."""

    root = Path(path)
    if root.is_symlink():
        _fail(BundleRejectCode.PATH_SYMLINK)
    if not root.is_dir():
        _fail(BundleRejectCode.PATH_INVALID)
    _private_directory(root)
    manifest_payload = _read_regular(
        root / "manifest.json",
        limit=_MAX_MANIFEST_BYTES,
    )
    try:
        document = json.loads(
            manifest_payload.decode("ascii"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    _validate_json_shape(document)
    if not isinstance(document, dict):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    required = {
        "artifacts",
        "created_ns",
        "diagnostic_policy_hash",
        "diagnostic_profile",
        "dropped_counts",
        "finalized_ns",
        "limits",
        "process_key",
        "run_id",
        "run_kind",
        "runtime_mode",
        "schema_version",
        "status",
        "timing_scope",
        "unavailable_reason",
    }
    if set(document) != required:
        _fail(BundleRejectCode.MANIFEST_INVALID)
    if document["schema_version"] != _BUNDLE_SCHEMA_VERSION:
        _fail(BundleRejectCode.UNSUPPORTED_VERSION)
    try:
        status = BundleStatus(document["status"])
    except (TypeError, ValueError):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    artifacts_document = document["artifacts"]
    reason = document["unavailable_reason"]
    dropped_counts = document["dropped_counts"]
    if (
        document["run_kind"] != "diagnostic"
        or document["timing_scope"] != "diagnostic_only"
        or document["diagnostic_profile"] not in ("summary", "full")
        or document["runtime_mode"] not in ("off", "observe", "auto")
        or not isinstance(document["diagnostic_policy_hash"], str)
        or _SHA256.fullmatch(document["diagnostic_policy_hash"]) is None
        or not isinstance(document["run_id"], str)
        or _SAFE_ID.fullmatch(document["run_id"]) is None
        or not isinstance(document["process_key"], str)
        or _SAFE_ID.fullmatch(document["process_key"]) is None
        or type(document["created_ns"]) is not int
        or document["created_ns"] < 0
        or type(document["finalized_ns"]) is not int
        or document["finalized_ns"] < document["created_ns"]
        or not isinstance(dropped_counts, dict)
        or set(dropped_counts) != {"budget_exhausted", "writer_failures"}
        or any(
            type(dropped_counts[field]) is not int
            or dropped_counts[field] < 0
            for field in dropped_counts
        )
        or (
            reason is not None
            and (
                not isinstance(reason, str)
                or _SAFE_REASON.fullmatch(reason) is None
            )
        )
        or (
            status is BundleStatus.COMPLETE
            and reason is not None
        )
        or (
            status is BundleStatus.INCOMPLETE
            and reason is None
        )
    ):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    if (
        not isinstance(artifacts_document, list)
        or len(artifacts_document) > _MAX_ARTIFACTS
        or not isinstance(document["limits"], dict)
        or set(document["limits"]) != {"max_bytes"}
        or type(document["limits"]["max_bytes"]) is not int
        or document["limits"]["max_bytes"] <= 0
    ):
        _fail(BundleRejectCode.MANIFEST_INVALID)
    max_bytes = document["limits"]["max_bytes"]
    artifacts = tuple(_parse_artifact(item) for item in artifacts_document)
    if len({artifact.path for artifact in artifacts}) != len(artifacts):
        _fail(BundleRejectCode.MANIFEST_INVALID)

    expected_files = {"manifest.json"}
    total_size = len(manifest_payload)
    for artifact in artifacts:
        relative = PurePosixPath(artifact.path)
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                _fail(BundleRejectCode.PATH_SYMLINK, artifact.path)
            if not current.is_dir():
                _fail(BundleRejectCode.PATH_INVALID, artifact.path)
            _private_directory(current)
        artifact_path = root.joinpath(*relative.parts)
        payload = _read_regular(artifact_path, limit=max_bytes)
        if len(payload) != artifact.byte_size:
            _fail(BundleRejectCode.SIZE_MISMATCH, artifact.path)
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            _fail(BundleRejectCode.HASH_MISMATCH, artifact.path)
        expected_files.add(artifact.path)
        total_size += len(payload)

    marker = root / "COMPLETE"
    if status in (BundleStatus.COMPLETE, BundleStatus.PARTIAL):
        marker_payload = _read_regular(marker, limit=0)
        if marker_payload:
            _fail(BundleRejectCode.COMPLETE_MARKER_INVALID)
        expected_files.add("COMPLETE")
    elif marker.exists() or marker.is_symlink():
        _fail(BundleRejectCode.COMPLETE_MARKER_INVALID)

    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        _private_directory(directory_path)
        for name in directory_names:
            child = directory_path / name
            if child.is_symlink():
                _fail(BundleRejectCode.PATH_SYMLINK)
        for name in file_names:
            file_path = directory_path / name
            relative = file_path.relative_to(root).as_posix()
            if relative not in expected_files:
                _fail(BundleRejectCode.MANIFEST_INVALID, "unlisted_file")
    if total_size > max_bytes:
        _fail(BundleRejectCode.TOTAL_SIZE_LIMIT)
    return DiagnosticBundle(
        path=root,
        status=status,
        manifest=MappingProxyType(document),
        artifacts=artifacts,
    )


def validate_bundle(path: str | os.PathLike[str]) -> DiagnosticBundle:
    """Validate a bundle using the same non-executing bounded reader."""

    return read_bundle(path)
