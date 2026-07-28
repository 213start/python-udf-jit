from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from python_udf_jit.diagnostics.acceptance import (
    CompletionStatus,
    TestSuiteContract,
    load_acceptance_contract,
)
from python_udf_jit.diagnostics.cinderx_evidence import (
    build_cinderx_evidence,
    validate_cinderx_evidence,
)
from python_udf_jit.diagnostics.environment_evidence import (
    validate_auth_evidence,
    validate_cleanup_evidence,
    validate_hygiene_evidence,
)
from python_udf_jit.diagnostics.invalidation_evidence import (
    build_invalidation_evidence,
    validate_invalidation_evidence,
)
from python_udf_jit.diagnostics.test_evidence import (
    build_unittest_evidence,
    validate_unittest_evidence,
)
from python_udf_jit.integration.daft_ray.environment import (
    DockerPreflightStatus,
    preflight_docker,
)
from python_udf_jit.integration.daft_ray.network_preflight import (
    DEFAULT_DASHBOARD_SUBNET,
    DEFAULT_DATA_PLANE_SUBNET,
    preflight_compose_networks,
)
from tests.e2e.assemble_report import assemble as assemble_e2e_report
from tests.e2e.build_phase_evidence import build as build_phase_evidence
from tests.e2e.capture_candidate_manifest import capture as capture_manifest
from tests.e2e.capture_phase_snapshot import capture as capture_phase_snapshot
from tests.e2e.submit_ray_job import submit_and_wait
from tests.system.assemble_formal_acceptance import (
    assemble as assemble_formal_acceptance,
)
from tests.system.assemble_infrastructure_evidence import (
    assemble as assemble_infrastructure_evidence,
)
from tests.system.build_hygiene_evidence import build_hygiene_proof
from tests.system.capture_host_state import capture as capture_host_state
from tests.system.capture_source_identity import capture as capture_source_identity
from tests.system.firewalld_bridge import (
    DockerBridge,
    RuntimeZoneBinding,
    bind_runtime_trusted,
    resolve_project_bridge,
    unbind_runtime_trusted,
)
from tests.system.probe_environment import probe as probe_environment
from tests.system.private_output import (
    open_private_output,
    write_private_bytes,
    write_private_json,
)
from tests.system.verify_cleanup import build_cleanup_proof


_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROLES = ("ray-head-driver", "ray-worker-1", "ray-worker-2")
_TRUSTED_TOOL_DIRECTORIES = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
_REQUIRED_ROOT_TOOLS = (
    "docker",
    "docker-compose",
    "firewall-cmd",
    "git",
    "ip",
    "nft",
    "tar",
)
class AcceptanceRunError(RuntimeError):
    """The live run cannot produce a valid formal acceptance report."""


@dataclass(frozen=True)
class CinderXInputs:
    fingerprint: Path
    runtime_log: Path
    release_log: Path
    adaptive_summary: Path
    adaptive_log: Path
    official_summary: Path
    official_log: Path
    targeted_log: Path


@dataclass(frozen=True)
class RunLayout:
    root: Path
    evidence: Path
    logs: Path
    private: Path
    work: Path

    @classmethod
    def create(cls, root: Path) -> RunLayout:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(root, 0o700)
        paths = cls(
            root=root,
            evidence=root / "evidence",
            logs=root / "logs",
            private=root / "private",
            work=root / "work",
        )
        for path in (paths.evidence, paths.logs, paths.private, paths.work):
            path.mkdir(mode=0o700)
        return paths


class Commands:
    def run(
        self,
        arguments: Iterable[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 60,
    ) -> str:
        argv = [str(value) for value in arguments]
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise AcceptanceRunError(
                f"command_failed:{argv[0]}:{completed.returncode}:"
                f"{detail[-2000:]}"
            )
        return completed.stdout

    def log(
        self,
        arguments: Iterable[str],
        path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 900,
    ) -> None:
        argv = [str(value) for value in arguments]
        descriptor = open_private_output(path)
        with os.fdopen(descriptor, "wb") as stream:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                check=False,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        if completed.returncode != 0:
            raise AcceptanceRunError(
                f"logged_command_failed:{argv[0]}:{completed.returncode}:{path}"
            )


def _announce(message: str) -> None:
    print(f"[acceptance] {message}", flush=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, dict):
        raise AcceptanceRunError(f"json_object_required:{path}")
    return document


def _write_private_bytes(path: Path, payload: bytes) -> None:
    write_private_bytes(path, payload)


def _write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    write_private_json(path, document)


def _require_private(path: Path) -> None:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AcceptanceRunError(f"private_file_required:{path}")


def _validate_id(value: str, field: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise AcceptanceRunError(f"{field}_invalid:{value!r}")
    return value


def _trusted_root_executable(name: str, search_path: str) -> str:
    candidate = shutil.which(name, path=search_path)
    if candidate is None:
        raise AcceptanceRunError(f"required_executable_missing:{name}")
    resolved = Path(candidate).resolve(strict=True)
    metadata = resolved.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise AcceptanceRunError(f"untrusted_root_executable:{name}")
    return str(resolved)


def _install_trusted_root_environment(
    resolver: Callable[[str, str], str] | None = None,
) -> dict[str, str]:
    directories = []
    for raw_directory in _TRUSTED_TOOL_DIRECTORIES:
        directory = Path(raw_directory)
        if not directory.is_dir():
            continue
        metadata = directory.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise AcceptanceRunError(
                f"untrusted_root_tool_directory:{raw_directory}"
            )
        directories.append(raw_directory)
    search_path = os.pathsep.join(directories)
    resolve = resolver or _trusted_root_executable
    tools = {
        name: resolve(name, search_path)
        for name in _REQUIRED_ROOT_TOOLS
    }
    os.environ.clear()
    os.environ.update(
        {
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": search_path,
        }
    )
    return tools


def _compose_project(cluster_epoch: str) -> str:
    suffix = cluster_epoch.removeprefix("epoch-")
    return _validate_id(f"u13-{suffix}", "compose_project")


def _git_identity(commands: Commands, repository: Path) -> str:
    commit = commands.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
    ).strip()
    if _GIT_COMMIT.fullmatch(commit) is None:
        raise AcceptanceRunError("git_commit_invalid")
    status = commands.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=repository,
    )
    if status:
        raise AcceptanceRunError("repository_must_be_clean_and_committed")
    return commit


def _wheel(path: Path, prefix: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.name.startswith(prefix):
        raise AcceptanceRunError(f"wheel_invalid:{prefix}:{resolved}")
    if re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", resolved.name) is None:
        raise AcceptanceRunError(f"wheel_extension_invalid:{resolved}")
    return resolved


def _container_exec(
    container: str,
    command: Iterable[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    arguments = ["docker", "exec"]
    for name, value in sorted((environment or {}).items()):
        arguments.extend(("-e", f"{name}={value}"))
    arguments.append(container)
    arguments.extend(str(value) for value in command)
    return arguments


def _port_open(host: str, port: int) -> bool:
    try:
        connection = socket.create_connection((host, port), timeout=2)
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False
    connection.close()
    return True


def _container_tcp_probe(
    commands: Commands,
    *,
    container: str,
    host: str,
    port: int,
) -> tuple[bool, str]:
    source = (
        "import socket,sys;"
        "connection=socket.create_connection((sys.argv[1],int(sys.argv[2])),3);"
        "connection.close();"
        "print('connected')"
    )
    try:
        output = commands.run(
            _container_exec(
                container,
                ["python", "-c", source, host, str(port)],
            ),
            timeout=10,
        )
    except (AcceptanceRunError, subprocess.TimeoutExpired) as error:
        return False, str(error)[-1000:]
    return output.strip().endswith("connected"), output[-1000:]


def _await_container_tcp(
    commands: Commands,
    *,
    container: str,
    host: str,
    port: int,
    timeout_seconds: int = 60,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_detail = ""
    while time.monotonic() < deadline:
        connected, last_detail = _container_tcp_probe(
            commands,
            container=container,
            host=host,
            port=port,
        )
        if connected:
            return
        time.sleep(1)
    raise AcceptanceRunError(
        f"container_tcp_not_ready:{container}:{host}:{port}:{last_detail}"
    )


def _interface_exists(interface: str) -> bool:
    completed = subprocess.run(
        ["ip", "link", "show", "dev", interface],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise AcceptanceRunError(
        f"interface_probe_failed:{interface}:{completed.returncode}"
    )


def _project_ids(
    commands: Commands,
    *,
    kind: str,
    project: str,
    run_id: str | None = None,
) -> list[str]:
    if kind == "container":
        arguments = [
            "docker",
            "ps",
            "--no-trunc",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    elif kind == "network":
        arguments = [
            "docker",
            "network",
            "ls",
            "--no-trunc",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ]
    else:
        raise ValueError(f"unknown project resource kind: {kind}")
    if run_id is not None:
        arguments.extend(
            (
                "--filter",
                f"label=org.python-udf-jit.run-id={run_id}",
            )
        )
    return sorted(
        line
        for line in commands.run(arguments).splitlines()
        if line
    )


def _compose_containers(
    commands: Commands,
    compose: list[str],
    environment: Mapping[str, str],
) -> dict[str, str]:
    containers = {
        role: commands.run(
            [*compose, "ps", "-q", role],
            env=environment,
        ).strip()
        for role in _ROLES
    }
    if (
        any(not identifier for identifier in containers.values())
        or len(set(containers.values())) != 3
    ):
        raise AcceptanceRunError("compose_did_not_create_exactly_three_containers")
    return containers


def _await_cluster(
    commands: Commands,
    *,
    head_container: str,
    timeout_seconds: int = 240,
) -> dict[str, Any]:
    source = (
        "import json,ray;"
        "ray.init(address='auto',logging_level=40);"
        "nodes=[n for n in ray.nodes() if n.get('Alive')];"
        "print(json.dumps(nodes,sort_keys=True));"
        "ray.shutdown()"
    )
    deadline = time.monotonic() + timeout_seconds
    last_detail = ""
    while time.monotonic() < deadline:
        try:
            output = commands.run(
                _container_exec(
                    head_container,
                    ["python", "-c", source],
                ),
                timeout=45,
            )
            lines = [
                line for line in output.splitlines() if line.startswith("[")
            ]
            nodes = json.loads(lines[-1]) if lines else []
            alive = {
                str(node.get("NodeName")): node
                for node in nodes
                if isinstance(node, dict)
            }
            if set(alive) != set(_ROLES):
                last_detail = f"roles={sorted(alive)}"
            elif alive["ray-head-driver"].get("Resources", {}).get("CPU", 0) != 0:
                last_detail = "head_cpu_nonzero"
            elif any(
                float(alive[role].get("Resources", {}).get("CPU", 0)) <= 0
                for role in ("ray-worker-1", "ray-worker-2")
            ):
                last_detail = "worker_cpu_missing"
            else:
                return {"nodes": nodes}
        except (
            AcceptanceRunError,
            json.JSONDecodeError,
            subprocess.TimeoutExpired,
            TypeError,
            ValueError,
        ) as error:
            last_detail = str(error)
        time.sleep(2)
    raise AcceptanceRunError(f"ray_cluster_not_ready:{last_detail}")


def _copy_from_container(
    commands: Commands,
    *,
    container: str,
    source: str,
    destination: Path,
) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    commands.run(
        ["docker", "cp", f"{container}:{source}", str(destination)],
        timeout=60,
    )
    os.chmod(destination, 0o600)
    _require_private(destination)


def _copy_to_container(
    commands: Commands,
    *,
    source: Path,
    container: str,
    destination: str,
) -> None:
    commands.run(
        ["docker", "cp", str(source), f"{container}:{destination}"],
        timeout=60,
    )
    commands.run(
        _container_exec(container, ["chmod", "0600", destination]),
    )


def _private_scan_files(layout: RunLayout) -> list[Path]:
    roots = (layout.evidence, layout.logs)
    paths = sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
    )
    for path in paths:
        _require_private(path)
    if not paths:
        raise AcceptanceRunError("secret_scan_has_no_artifacts")
    return paths


def _delete_raw_files(paths: Iterable[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _scrub_failure_payloads(layout: RunLayout) -> None:
    """Remove value-bearing failure artifacts while preserving the run root."""

    for root in (layout.evidence, layout.logs, layout.private, layout.work):
        paths = sorted(
            root.rglob("*"),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for path in paths:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, AcceptanceRunError):
        reason = str(error).split(":", 1)[0]
        if re.fullmatch(r"[a-z0-9_]+", reason):
            return reason
    return "unexpected_failure"


def _assert_absent(paths: Iterable[Path]) -> None:
    remaining = [path for path in paths if path.exists()]
    if remaining:
        raise AcceptanceRunError(
            "raw_files_remain:" + ",".join(str(path) for path in remaining)
        )


def _build_context(
    commands: Commands,
    *,
    repository: Path,
    layout: RunLayout,
    base_image: str,
    build_backend_wheel: Path,
    third_party_wheels: tuple[Path, ...],
) -> tuple[Path, Path]:
    context = layout.work / "build-context"
    context.mkdir(mode=0o700)
    archive = layout.work / "source.tar"
    commands.run(
        [
            "git",
            "archive",
            "--format=tar",
            f"--output={archive}",
            "HEAD",
        ],
        cwd=repository,
    )
    commands.run(["tar", "-xf", str(archive), "-C", str(context)])
    archive.unlink()
    vendor = context / "vendor"
    vendor.mkdir(mode=0o755, exist_ok=True)
    for source in third_party_wheels:
        destination = vendor / source.name
        if destination.exists():
            raise AcceptanceRunError(f"wheel_name_collision:{source.name}")
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o644)

    wheel_build_log = layout.logs / "udf-wheel-build.log"
    commands.log(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{context}:/workspace",
            "--volume",
            (
                f"{build_backend_wheel}:/tmp/"
                f"{build_backend_wheel.name}:ro"
            ),
            "--workdir",
            "/workspace",
            "--entrypoint",
            "/bin/sh",
            base_image,
            "-c",
            (
                "python -m pip install --disable-pip-version-check "
                f"--no-index --no-deps /tmp/{build_backend_wheel.name}"
                " && python -m pip wheel --disable-pip-version-check "
                "--no-deps --no-build-isolation "
                "--wheel-dir /workspace/vendor ."
            ),
        ],
        wheel_build_log,
        timeout=300,
    )
    wheels = tuple(vendor.glob("python_udf_jit-*.whl"))
    if len(wheels) != 1:
        raise AcceptanceRunError(
            f"udf_wheel_count_invalid:{len(wheels)}"
        )
    return context, wheels[0]


def _capture_snapshot(
    *,
    phase: str,
    run_id: str,
    cluster_epoch: str,
    manifest_sha256: str,
    containers: Mapping[str, str],
    output: Path,
) -> dict[str, object]:
    document = capture_phase_snapshot(
        phase=phase,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        manifest_sha256=manifest_sha256,
        containers=dict(containers),
    )
    _write_private_json(output, document)
    return document


def _restart_worker_and_capture(
    commands: Commands,
    *,
    worker_container: str,
    head_container: str,
    run_id: str,
    cluster_epoch: str,
    manifest_sha256: str,
    containers: Mapping[str, str],
    output: Path,
    log_path: Path,
) -> dict[str, object]:
    commands.log(
        [
            "docker",
            "restart",
            "--time",
            "10",
            worker_container,
        ],
        log_path,
        timeout=180,
    )
    _await_cluster(
        commands,
        head_container=head_container,
    )
    return _capture_snapshot(
        phase="e2e",
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        manifest_sha256=manifest_sha256,
        containers=containers,
        output=output,
    )


def _test_receipt(
    *,
    suite: TestSuiteContract,
    run_id: str,
    cluster_epoch: str,
    git_commit: str,
    argv: list[str],
    log_path: Path,
    output: Path,
) -> dict[str, object]:
    proof = build_unittest_evidence(
        gate_id=suite.gate_id,
        tier=suite.tier,
        run_id=run_id,
        cluster_epoch=cluster_epoch,
        source_git_commit=git_commit,
        argv=argv,
        required_tests=suite.required_tests,
        minimum_test_count=suite.expected_test_count,
        expected_test_count=suite.expected_test_count,
        allow_skips=suite.allow_skips,
        log_path=log_path,
    )
    _write_private_json(output, proof)
    return proof


def _job(
    commands: Commands,
    *,
    run_id: str,
    address: str,
    token_file: Path,
    head_container: str,
    label: str,
    mode: str,
    entrypoint: str,
    internal_output: str,
    output: Path,
    log: Path,
    timeout_seconds: int = 900,
) -> None:
    submission_id = f"{run_id}-{label}"
    result = submit_and_wait(
        address=address,
        token_file=token_file,
        submission_id=submission_id,
        entrypoint=entrypoint,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )
    _write_private_json(log, result)
    _copy_from_container(
        commands,
        container=head_container,
        source=internal_output,
        destination=output,
    )


def _down(
    commands: Commands,
    *,
    compose: list[str],
    environment: Mapping[str, str],
    project: str,
    run_id: str,
    log_path: Path | None,
) -> None:
    arguments = [*compose, "down", "--remove-orphans", "--volumes"]
    cleanup_errors: list[str] = []
    try:
        if log_path is None:
            commands.run(arguments, env=environment, timeout=180)
        else:
            commands.log(
                arguments,
                log_path,
                env=environment,
                timeout=180,
            )
    except Exception:
        for kind in ("container", "network"):
            try:
                identifiers = _project_ids(
                    commands,
                    kind=kind,
                    project=project,
                    run_id=run_id,
                )
            except Exception as error:
                cleanup_errors.append(
                    f"{kind}_enumeration:{type(error).__name__}"
                )
                continue
            if not identifiers:
                continue
            operation = (
                ["docker", "rm", "-f", *identifiers]
                if kind == "container"
                else ["docker", "network", "rm", *identifiers]
            )
            try:
                commands.run(operation, timeout=120)
            except Exception as error:
                cleanup_errors.append(
                    f"{kind}_removal:{type(error).__name__}"
                )

    for kind in ("container", "network"):
        try:
            remaining = _project_ids(
                commands,
                kind=kind,
                project=project,
                run_id=run_id,
            )
        except Exception as error:
            cleanup_errors.append(
                f"{kind}_verification:{type(error).__name__}"
            )
            continue
        if remaining:
            cleanup_errors.append(f"{kind}_resources_remain")
    if cleanup_errors:
        raise AcceptanceRunError(
            "compose_down_cleanup_failed:" + ",".join(cleanup_errors)
        )


def _resolve_acceptance_profile(
    repository: Path,
    selection: str | os.PathLike[str] | None,
) -> Path:
    value = "scalar-piercing" if selection is None else os.fspath(selection)
    named_profiles = {
        "scalar-piercing": "scalar-piercing-acceptance.json",
        "mainline-production": "mainline-production-acceptance.json",
    }
    if value in named_profiles:
        return repository / "config" / named_profiles[value]
    candidate = Path(value)
    if candidate.suffix.lower() != ".json":
        raise AcceptanceRunError(f"acceptance_profile_unknown:{value}")
    resolved = (
        candidate
        if candidate.is_absolute()
        else repository / candidate
    ).resolve()
    if not resolved.is_file():
        raise AcceptanceRunError(
            f"acceptance_profile_path_unreadable:{resolved}"
        )
    return resolved


def _acceptance_report_passes(
    report: Mapping[str, object],
    *,
    require_release_ready: bool,
) -> bool:
    if require_release_ready:
        return (
            report.get("verdict") == "pass"
            and report.get("release_ready") is True
        )
    return report.get(
        "executed_gate_verdict",
        report.get("verdict"),
    ) == "pass"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the complete U13 formal acceptance on blue-98."
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--run-root", type=Path)
    parser.add_argument(
        "--acceptance-profile",
        default="scalar-piercing",
        help=(
            "scalar-piercing or mainline-production; an explicit JSON "
            "path is accepted for compatibility"
        ),
    )
    parser.add_argument(
        "--acceptance-schema",
        type=Path,
        help="optional JSON schema whose $id must match the selected profile",
    )
    parser.add_argument(
        "--unit-completion-status",
        choices=tuple(status.value for status in CompletionStatus),
        default=CompletionStatus.INCOMPLETE.value,
        help=(
            "implementation lifecycle, reported separately from gate outcomes"
        ),
    )
    parser.add_argument(
        "--require-release-ready",
        action="store_true",
        help=(
            "fail unless the selected production profile has complete "
            "lifecycle evidence; milestone runs leave this unset"
        ),
    )
    parser.add_argument(
        "--cinderx-base-image",
        default="cinderx-pyperf-realenv:arm64",
    )
    parser.add_argument(
        "--cinderx-wheel",
        type=Path,
        default=Path(
            "/root/python-udf-jit/runs/"
            "u13-formal-20260723-cinderx-local-static-2/source/build/"
            "testgate/cinderx_local-20260724-122045-736688-1/wheelhouse/"
            "cinderx-2026.7.24.0-cp314-cp314-linux_aarch64.whl"
        ),
    )
    parser.add_argument(
        "--daft-wheel",
        type=Path,
        default=Path(
            "/root/python-udf-jit/artifacts/repacked/u4/wheelhouse/"
            "daft-0.7.2-cp310-abi3-manylinux_2_24_aarch64.whl"
        ),
    )
    parser.add_argument(
        "--pyarrow-wheel",
        type=Path,
        default=Path(
            "/root/python-udf-jit/artifacts/wheels/u4/third-party/"
            "pyarrow-22.0.0-cp314-cp314-manylinux_2_28_aarch64.whl"
        ),
    )
    parser.add_argument(
        "--ray-wheel",
        type=Path,
        default=Path(
            "/root/python-udf-jit/artifacts/wheels/u4/third-party/"
            "ray-2.55.0-cp314-cp314-manylinux2014_aarch64.whl"
        ),
    )
    parser.add_argument(
        "--setuptools-wheel",
        type=Path,
        default=Path(
            "/root/python-udf-jit/runs/"
            "u13-formal-20260723-cinderx-local-static-1/"
            "tool-wheelhouse/setuptools-83.0.0-py3-none-any.whl"
        ),
    )
    cinderx_root = Path(
        "/root/python-udf-jit/runs/"
        "u13-formal-20260723-cinderx-local-static-2"
    )
    parser.add_argument(
        "--cinderx-fingerprint",
        type=Path,
        default=cinderx_root / "static-python-fingerprint.json",
    )
    parser.add_argument(
        "--cinderx-runtime-log",
        type=Path,
        default=Path(
            "/root/python-udf-jit/runs/"
            "u13-formal-20260723-runtime-static-2/cinderx-runtime-l4.log"
        ),
    )
    parser.add_argument(
        "--cinderx-release-log",
        type=Path,
        default=cinderx_root / "cinderx-local-l4.log",
    )
    parser.add_argument(
        "--cinderx-adaptive-summary",
        type=Path,
        default=(
            cinderx_root
            / "ptrace-full-libtest-readable"
            / "lib-test-adaptive-aware-24.json"
        ),
    )
    parser.add_argument(
        "--cinderx-adaptive-log",
        type=Path,
        default=(
            cinderx_root
            / "ptrace-full-libtest-readable"
            / "lib-test-adaptive-aware-24.log"
        ),
    )
    parser.add_argument(
        "--cinderx-official-summary",
        type=Path,
        default=(
            cinderx_root
            / "ptrace-official-skip-libtest"
            / "lib-test-official-skip-ok-26.json"
        ),
    )
    parser.add_argument(
        "--cinderx-official-log",
        type=Path,
        default=(
            cinderx_root
            / "ptrace-official-skip-libtest"
            / "lib-test-official-skip-ok-26.log"
        ),
    )
    parser.add_argument(
        "--cinderx-targeted-log",
        type=Path,
        default=(
            cinderx_root
            / "udf-python-targeted"
            / "test-udf-data-intrinsic.log"
        ),
    )
    parser.add_argument(
        "--data-plane-subnet",
        default=DEFAULT_DATA_PLANE_SUBNET,
    )
    parser.add_argument(
        "--dashboard-subnet",
        default=DEFAULT_DASHBOARD_SUBNET,
    )
    parser.add_argument(
        "--allow-runtime-firewalld-trusted",
        action="store_true",
        help=(
            "allow a blocked per-run data-plane bridge to be bound to the "
            "firewalld trusted zone at runtime; the binding is removed and "
            "host-state restoration is mandatory"
        ),
    )
    parser.add_argument("--non-loopback-host", default="192.168.41.98")
    parser.add_argument("--jobs-address", default="http://127.0.0.1:8265")
    return parser


def run(arguments: argparse.Namespace) -> Path:
    if os.geteuid() != 0:
        raise AcceptanceRunError("blue98_acceptance_requires_root")
    trusted_tools = _install_trusted_root_environment()
    repository = arguments.repository.resolve()
    contract_path = _resolve_acceptance_profile(
        repository,
        getattr(arguments, "acceptance_profile", None),
    )
    schema_path = getattr(arguments, "acceptance_schema", None)
    contract = load_acceptance_contract(
        contract_path,
        schema_path=schema_path.resolve() if schema_path is not None else None,
    )
    unit_completion_status = CompletionStatus(
        getattr(
            arguments,
            "unit_completion_status",
            CompletionStatus.INCOMPLETE.value,
        )
    )
    unit_suite = contract.test_suites["unit"]
    integration_suite = contract.test_suites["integration"]
    live_suite = contract.test_suites["live"]
    commands = Commands()
    commit = _git_identity(commands, repository)
    docker = preflight_docker()
    if docker.status != DockerPreflightStatus.READY:
        raise AcceptanceRunError(docker.reason)
    compose_executable = trusted_tools["docker-compose"]
    if _port_open("127.0.0.1", 8265):
        raise AcceptanceRunError("dashboard_port_8265_already_in_use")

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = _validate_id(
        f"u13-{timestamp}-{commit[:8]}",
        "run_id",
    )
    cluster_epoch = _validate_id(
        f"epoch-{secrets.token_hex(12)}",
        "cluster_epoch",
    )
    project = _compose_project(cluster_epoch)
    run_root = (
        arguments.run_root.resolve()
        if arguments.run_root is not None
        else repository / "runs" / run_id
    )
    layout = RunLayout.create(run_root)
    token_file = layout.private / "ray-auth-token"
    compose_active = False
    data_plane_bridge: DockerBridge | None = None
    runtime_zone_binding: RuntimeZoneBinding | None = None
    runtime_zone_binding_active = False
    bridge_accommodation: dict[str, Any] | None = None

    compose_file = repository / "docker/scalar-piercing/compose.yaml"
    patch_manifest_path = (
        repository / "vendor/cinderx/patches/manifest.json"
    )
    patch_manifest = json.loads(
        patch_manifest_path.read_text(encoding="utf-8")
    )
    patch_entries = patch_manifest.get("patches")
    if not isinstance(patch_entries, list) or not patch_entries:
        raise AcceptanceRunError("committed_cinderx_manifest_invalid")
    patch_paths = []
    patch_hasher = hashlib.sha256()
    seen_patch_paths = set()
    for entry in patch_entries:
        if not isinstance(entry, dict):
            raise AcceptanceRunError("committed_cinderx_manifest_invalid")
        relative_path = Path(str(entry.get("path", "")))
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.as_posix() in seen_patch_paths
        ):
            raise AcceptanceRunError("committed_cinderx_manifest_invalid")
        patch_path = (
            repository / "vendor/cinderx/patches" / relative_path
        )
        if (
            not patch_path.is_file()
            or entry.get("sha256") != _sha256(patch_path)
        ):
            raise AcceptanceRunError("committed_cinderx_manifest_invalid")
        seen_patch_paths.add(relative_path.as_posix())
        patch_paths.append(patch_path)
        patch_hasher.update(patch_path.read_bytes())
    cinderx_commit = str(patch_manifest["upstream_commit"])
    cinderx_tree = str(patch_manifest["candidate_runtime_tree_sha256"])
    patch_sha256 = patch_hasher.hexdigest()
    if (
        _GIT_COMMIT.fullmatch(cinderx_commit) is None
        or _SHA256.fullmatch(cinderx_tree) is None
        or patch_manifest.get("patch_series_sha256") != patch_sha256
    ):
        raise AcceptanceRunError("committed_cinderx_manifest_invalid")

    cinderx_wheel = _wheel(arguments.cinderx_wheel, "cinderx-")
    cinderx_wheel_sha256 = _sha256(cinderx_wheel)
    daft_wheel = _wheel(arguments.daft_wheel, "daft-0.7.2")
    pyarrow_wheel = _wheel(arguments.pyarrow_wheel, "pyarrow-22.0.0")
    ray_wheel = _wheel(arguments.ray_wheel, "ray-2.55.0")
    setuptools_wheel = _wheel(
        arguments.setuptools_wheel,
        "setuptools-83.0.0",
    )
    cinderx_inputs = CinderXInputs(
        fingerprint=arguments.cinderx_fingerprint.resolve(),
        runtime_log=arguments.cinderx_runtime_log.resolve(),
        release_log=arguments.cinderx_release_log.resolve(),
        adaptive_summary=arguments.cinderx_adaptive_summary.resolve(),
        adaptive_log=arguments.cinderx_adaptive_log.resolve(),
        official_summary=arguments.cinderx_official_summary.resolve(),
        official_log=arguments.cinderx_official_log.resolve(),
        targeted_log=arguments.cinderx_targeted_log.resolve(),
    )
    compose = [
        compose_executable,
        "-f",
        str(compose_file),
        "-p",
        project,
    ]
    compose_environment: dict[str, str] = {}
    containers: dict[str, str] = {}
    before_state: dict[str, object] | None = None
    token = ""

    try:
        if _project_ids(
            commands,
            kind="container",
            project=project,
        ) or _project_ids(
            commands,
            kind="network",
            project=project,
        ):
            raise AcceptanceRunError("compose_project_already_exists")
        _announce(f"run={run_id} commit={commit}")
        before_state = capture_host_state()
        _write_private_json(
            layout.evidence / "host-state-before.json",
            before_state,
        )
        network = preflight_compose_networks(
            data_plane_subnet=arguments.data_plane_subnet,
            dashboard_subnet=arguments.dashboard_subnet,
        )
        _write_private_json(
            layout.evidence / "network-preflight.json",
            {"status": "ready", **asdict(network)},
        )
        _announce("host/network preflight passed without mutation")

        cinderx_proof = build_cinderx_evidence(
            cinderx_commit=cinderx_commit,
            source_tree_sha256=cinderx_tree,
            patch_sha256=patch_sha256,
            cinderx_wheel_sha256=cinderx_wheel_sha256,
            fingerprint_path=cinderx_inputs.fingerprint,
            runtime_log_path=cinderx_inputs.runtime_log,
            release_log_path=cinderx_inputs.release_log,
            adaptive_summary_path=cinderx_inputs.adaptive_summary,
            adaptive_log_path=cinderx_inputs.adaptive_log,
            official_summary_path=cinderx_inputs.official_summary,
            official_log_path=cinderx_inputs.official_log,
            targeted_log_path=cinderx_inputs.targeted_log,
        )
        if validate_cinderx_evidence(cinderx_proof) != "pass":
            raise AcceptanceRunError("cinderx_proof_validation_failed")
        base_image_id = commands.run(
            [
                "docker",
                "image",
                "inspect",
                arguments.cinderx_base_image,
                "--format",
                "{{.Id}}",
            ]
        ).strip()
        if cinderx_proof["identity"]["image_digest"] != base_image_id:
            raise AcceptanceRunError("cinderx_base_image_identity_drift")
        cinderx_proof_path = layout.evidence / "cinderx-proof.json"
        _write_private_json(cinderx_proof_path, cinderx_proof)
        _announce("exact CinderX RuntimeTests/Python proof accepted")

        context, udf_wheel = _build_context(
            commands,
            repository=repository,
            layout=layout,
            base_image=arguments.cinderx_base_image,
            build_backend_wheel=setuptools_wheel,
            third_party_wheels=(
                cinderx_wheel,
                daft_wheel,
                pyarrow_wheel,
                ray_wheel,
            ),
        )
        wheel_hashes = {
            "CINDERX_WHEEL_SHA256": cinderx_wheel_sha256,
            "DAFT_WHEEL_SHA256": _sha256(daft_wheel),
            "PYARROW_WHEEL_SHA256": _sha256(pyarrow_wheel),
            "RAY_WHEEL_SHA256": _sha256(ray_wheel),
            "UDFJIT_WHEEL_SHA256": _sha256(udf_wheel),
        }
        image = f"python-udf-jit:u13-{timestamp}-{commit[:8]}"
        build_arguments = [
            "docker",
            "build",
            "--file",
            str(context / "docker/scalar-piercing/Dockerfile.candidate"),
            "--tag",
            image,
            "--build-arg",
            f"CINDERX_BASE_IMAGE={arguments.cinderx_base_image}",
            "--build-arg",
            f"CINDERX_BASE_IMAGE_DIGEST={base_image_id}",
            "--build-arg",
            f"SOURCE_GIT_COMMIT={commit}",
            "--build-arg",
            f"CINDERX_COMMIT={cinderx_commit}",
            "--build-arg",
            f"CINDERX_SOURCE_TREE_SHA256={cinderx_tree}",
            "--build-arg",
            f"CINDERX_PATCH_SHA256={patch_sha256}",
        ]
        for name, value in wheel_hashes.items():
            build_arguments.extend(("--build-arg", f"{name}={value}"))
        build_arguments.append(str(context))
        commands.log(
            build_arguments,
            layout.logs / "candidate-image-build.log",
            timeout=1800,
        )
        source_document = capture_source_identity(
            repository=repository,
            image=image,
            udf_jit_wheel=udf_wheel,
            cinderx_wheel=cinderx_wheel,
            cinderx_base_image_digest=base_image_id,
            cinderx_proof_path=cinderx_proof_path,
            patch_paths=patch_paths,
        )
        source_path = layout.evidence / "source-identity.json"
        _write_private_json(source_path, source_document)
        _announce(
            "candidate image built and bound to Git/CinderX/patch/Wheel"
        )

        token = secrets.token_hex(32)
        _write_private_bytes(token_file, f"{token}\n".encode("ascii"))
        compose_environment = dict(os.environ)
        compose_environment.update(
            {
                "CINDERX_BASE_IMAGE": arguments.cinderx_base_image,
                "SCALAR_PIERCING_IMAGE": image,
                "SCALAR_PIERCING_DATA_PLANE_SUBNET":
                    arguments.data_plane_subnet,
                "SCALAR_PIERCING_DASHBOARD_SUBNET":
                    arguments.dashboard_subnet,
                "RAY_AUTH_TOKEN_FILE": str(token_file),
                "UDFJIT_CLUSTER_EPOCH": cluster_epoch,
                "UDFJIT_MODE": "auto",
                "UDFJIT_RUN_ID": run_id,
            }
        )
        resolved_compose = commands.run(
            [*compose, "config"],
            env=compose_environment,
        )
        _write_private_bytes(
            layout.evidence / "compose-resolved.yaml",
            resolved_compose.encode("utf-8"),
        )
        compose_active = True
        commands.log(
            [*compose, "up", "-d", "--no-build"],
            layout.logs / "compose-up.log",
            env=compose_environment,
            timeout=300,
        )
        containers = _compose_containers(
            commands,
            compose,
            compose_environment,
        )
        data_plane_bridge = resolve_project_bridge(
            commands,
            project=project,
            logical_network="scalar-piercing",
        )
        _await_container_tcp(
            commands,
            container=containers["ray-head-driver"],
            host="ray-head-data-plane",
            port=6379,
            timeout_seconds=90,
        )
        connectivity_before = {
            role: _container_tcp_probe(
                commands,
                container=containers[role],
                host="ray-head-data-plane",
                port=6379,
            )[0]
            for role in ("ray-worker-1", "ray-worker-2")
        }
        if not all(connectivity_before.values()):
            if not arguments.allow_runtime_firewalld_trusted:
                raise AcceptanceRunError(
                    "data_plane_bridge_blocked:"
                    "rerun_with_--allow-runtime-firewalld-trusted"
                )
            runtime_zone_binding = bind_runtime_trusted(
                commands,
                data_plane_bridge,
            )
            runtime_zone_binding_active = True
            action = "runtime-trusted"
            zone: str | None = runtime_zone_binding.zone
            scope: str | None = "runtime"
        else:
            action = "not-required"
            zone = None
            scope = None
        for role in ("ray-worker-1", "ray-worker-2"):
            _await_container_tcp(
                commands,
                container=containers[role],
                host="ray-head-data-plane",
                port=6379,
                timeout_seconds=90,
            )
        bridge_accommodation = {
            "action": action,
            "network_id": data_plane_bridge.network_id,
            "bridge_interface": data_plane_bridge.interface,
            "zone": zone,
            "scope": scope,
            "connectivity_before": connectivity_before,
            "connectivity_after": {
                role: True for role in ("ray-worker-1", "ray-worker-2")
            },
            "binding_added": runtime_zone_binding is not None,
            "binding_removed": False,
            "bridge_interface_exists_after_cleanup": True,
        }
        _announce(
            "data-plane bridge connectivity verified"
            + (
                " with a run-scoped firewalld runtime binding"
                if runtime_zone_binding is not None
                else " without host accommodation"
            )
        )
        _await_cluster(
            commands,
            head_container=containers["ray-head-driver"],
        )
        _announce("one Head/Driver and two Workers are alive")

        manifest_document = capture_manifest(
            containers=dict(containers),
            source_git_commit=commit,
            cinderx_commit=cinderx_commit,
            cinderx_source_tree_sha256=cinderx_tree,
            cinderx_patch_sha256=patch_sha256,
            cinderx_wheel_sha256=cinderx_wheel_sha256,
            cinderx_base_image_digest=base_image_id,
            udf_jit_wheel_sha256=wheel_hashes["UDFJIT_WHEEL_SHA256"],
        )
        manifest_path = layout.evidence / "candidate-manifest.json"
        _write_private_json(manifest_path, manifest_document)
        manifest_sha256 = str(
            manifest_document["candidate_manifest_sha256"]
        )

        readiness_snapshot_path = (
            layout.evidence / "readiness-snapshot.json"
        )
        _capture_snapshot(
            phase="readiness",
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            manifest_sha256=manifest_sha256,
            containers=containers,
            output=readiness_snapshot_path,
        )
        readiness_internal = f"/tmp/{run_id}-readiness.json"
        live_environment = {
            "UDFJIT_CLUSTER_EPOCH": cluster_epoch,
            "UDFJIT_LIVE_RAY": "1",
            "UDFJIT_MANIFEST_PATH":
                "/opt/python-udf-jit/config/scalar-piercing-manifest.json",
            "UDFJIT_MODE": "auto",
            "UDFJIT_RUN_ID": run_id,
        }
        readiness_argv = _container_exec(
            containers["ray-head-driver"],
            [
                "python",
                "-m",
                "unittest",
                "tests.integration.test_ray_cinderx_scalar_slot_smoke",
                "-v",
            ],
            environment={
                **live_environment,
                "UDFJIT_READINESS_REPORT_PATH": readiness_internal,
            },
        )
        commands.log(
            readiness_argv,
            layout.logs / "readiness-test.log",
            timeout=300,
        )
        readiness_report_path = (
            layout.evidence / "readiness-report.json"
        )
        _copy_from_container(
            commands,
            container=containers["ray-head-driver"],
            source=readiness_internal,
            destination=readiness_report_path,
        )

        qualification_snapshot_path = (
            layout.evidence / "qualification-snapshot.json"
        )
        _capture_snapshot(
            phase="qualification",
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            manifest_sha256=manifest_sha256,
            containers=containers,
            output=qualification_snapshot_path,
        )
        qualification_internal = f"/tmp/{run_id}-qualification.json"
        qualification_argv = _container_exec(
            containers["ray-head-driver"],
            [
                "python",
                "-m",
                "unittest",
                "tests.integration.test_per_worker_artifact_qualification",
                "-v",
            ],
            environment={
                **live_environment,
                "UDFJIT_QUALIFICATION_REPORT_PATH":
                    qualification_internal,
            },
        )
        commands.log(
            qualification_argv,
            layout.logs / "qualification-test.log",
            timeout=300,
        )
        qualification_report_path = (
            layout.evidence / "qualification-report.json"
        )
        _copy_from_container(
            commands,
            container=containers["ray-head-driver"],
            source=qualification_internal,
            destination=qualification_report_path,
        )

        pre_live_snapshot_path = (
            layout.evidence / "e2e-snapshot-pre-live.json"
        )
        _capture_snapshot(
            phase="e2e",
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            manifest_sha256=manifest_sha256,
            containers=containers,
            output=pre_live_snapshot_path,
        )
        _announce("both Workers passed readiness and production qualification")

        unit_argv = _container_exec(
            containers["ray-head-driver"],
            [
                "python",
                "-m",
                "unittest",
                *unit_suite.arguments,
            ],
            environment=live_environment,
        )
        unit_log = layout.logs / "python-unit.log"
        commands.log(unit_argv, unit_log, timeout=300)
        unit_path = layout.evidence / "python-unit-proof.json"
        unit_proof = _test_receipt(
            suite=unit_suite,
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            git_commit=commit,
            argv=unit_argv,
            log_path=unit_log,
            output=unit_path,
        )

        integration_argv = _container_exec(
            containers["ray-head-driver"],
            [
                "python",
                "-m",
                "unittest",
                *integration_suite.arguments,
            ],
            environment=live_environment,
        )
        integration_log = layout.logs / "python-integration.log"
        commands.log(
            integration_argv,
            integration_log,
            timeout=900,
        )
        integration_path = (
            layout.evidence / "python-integration-proof.json"
        )
        integration_proof = _test_receipt(
            suite=integration_suite,
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            git_commit=commit,
            argv=integration_argv,
            log_path=integration_log,
            output=integration_path,
        )
        _announce("candidate-image UT and live IT passed with zero skips")

        off_observation_path = layout.evidence / "e2e-off-observation.json"
        auto_observation_path = (
            layout.evidence / "e2e-auto-observation.json"
        )
        for mode, output in (
            ("off", off_observation_path),
            ("auto", auto_observation_path),
        ):
            internal = f"/tmp/{run_id}-e2e-{mode}.json"
            _job(
                commands,
                run_id=run_id,
                address=arguments.jobs_address,
                token_file=token_file,
                head_container=containers["ray-head-driver"],
                label=f"e2e-{mode}",
                mode=mode,
                entrypoint=(
                    "python -m tests.e2e.live_job "
                    f"--output {internal}"
                ),
                internal_output=internal,
                output=output,
                log=layout.logs / f"e2e-{mode}-submission.json",
            )

        pre_live_phase_path = (
            layout.evidence / "phase-evidence-pre-live.json"
        )
        pre_live_phase = build_phase_evidence(
            snapshots=[
                _load_json(readiness_snapshot_path),
                _load_json(qualification_snapshot_path),
                _load_json(pre_live_snapshot_path),
            ],
            readiness=_load_json(readiness_report_path),
            qualification=_load_json(qualification_report_path),
            manifest=manifest_document,
        )
        _write_private_json(pre_live_phase_path, pre_live_phase)
        pre_live_report_path = layout.evidence / "base-report-pre-live.json"
        pre_live_report = assemble_e2e_report(
            _load_json(off_observation_path),
            _load_json(auto_observation_path),
            pre_live_phase,
            raw_root=layout.work / "raw-base-pre-live",
            output=pre_live_report_path,
        )
        if pre_live_report.get("verdict") != "pass":
            raise AcceptanceRunError(
                "pre_live_e2e_failed:"
                f"{pre_live_report.get('reason_codes')}"
            )
        _announce(
            "pre-live E2E report passed with compile/hit/execute evidence"
        )

        black_box_paths: dict[str, Path] = {}
        for mode in ("off", "auto"):
            output = layout.evidence / f"black-box-{mode}.json"
            internal = f"/tmp/{run_id}-black-box-{mode}.json"
            _job(
                commands,
                run_id=run_id,
                address=arguments.jobs_address,
                token_file=token_file,
                head_container=containers["ray-head-driver"],
                label=f"black-box-{mode}",
                mode=mode,
                entrypoint=(
                    "python -m tests.system.transparent_user_job "
                    f"--output {internal}"
                ),
                internal_output=internal,
                output=output,
                log=layout.logs / f"black-box-{mode}-submission.json",
            )
            black_box_paths[mode] = output

        measurement_path = layout.evidence / "measurement.json"
        measurement_internal = f"/tmp/{run_id}-measurement.json"
        _job(
            commands,
            run_id=run_id,
            address=arguments.jobs_address,
            token_file=token_file,
            head_container=containers["ray-head-driver"],
            label="measurement",
            mode="auto",
            entrypoint=(
                "python -m benchmarks.scalar_piercing.run "
                f"--samples 3 --output {measurement_internal}"
            ),
            internal_output=measurement_internal,
            output=measurement_path,
            log=layout.logs / "measurement-submission.json",
        )
        _announce("no-touch off/auto black-box and non-gating measurement passed")

        internal_base_report = f"/tmp/{run_id}-base-report.json"
        _copy_to_container(
            commands,
            source=pre_live_report_path,
            container=containers["ray-head-driver"],
            destination=internal_base_report,
        )
        live_argv = _container_exec(
            containers["ray-head-driver"],
            [
                "python",
                "-m",
                "unittest",
                *live_suite.arguments,
            ],
            environment={
                **live_environment,
                "UDFJIT_E2E_REPORT_PATH": internal_base_report,
            },
        )
        live_log = layout.logs / "python-live-e2e.log"
        commands.log(live_argv, live_log, timeout=600)
        live_path = layout.evidence / "python-live-proof.json"
        live_proof = _test_receipt(
            suite=live_suite,
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            git_commit=commit,
            argv=live_argv,
            log_path=live_log,
            output=live_path,
        )
        _announce(
            f"{live_proof['test_count']} profile-selected live ST tests passed"
        )

        e2e_snapshot_path = layout.evidence / "e2e-snapshot-before.json"
        _capture_snapshot(
            phase="e2e",
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            manifest_sha256=manifest_sha256,
            containers=containers,
            output=e2e_snapshot_path,
        )
        phase_path = layout.evidence / "phase-evidence-base.json"
        phase_document = build_phase_evidence(
            snapshots=[
                _load_json(readiness_snapshot_path),
                _load_json(qualification_snapshot_path),
                _load_json(e2e_snapshot_path),
            ],
            readiness=_load_json(readiness_report_path),
            qualification=_load_json(qualification_report_path),
            manifest=manifest_document,
        )
        _write_private_json(phase_path, phase_document)
        base_report_path = layout.evidence / "base-report.json"
        base_report = assemble_e2e_report(
            _load_json(off_observation_path),
            _load_json(auto_observation_path),
            phase_document,
            raw_root=layout.work / "raw-base",
            output=base_report_path,
        )
        if base_report.get("verdict") != "pass":
            raise AcceptanceRunError(
                f"base_e2e_failed:{base_report.get('reason_codes')}"
            )
        _announce(
            "post-workload E2E report passed; restart baseline is fresh"
        )

        e2e_after_path = layout.evidence / "e2e-snapshot-after.json"
        _restart_worker_and_capture(
            commands,
            worker_container=containers["ray-worker-2"],
            head_container=containers["ray-head-driver"],
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            manifest_sha256=manifest_sha256,
            containers=containers,
            output=e2e_after_path,
            log_path=layout.logs / "worker-2-restart.log",
        )
        invalid_phase_path = (
            layout.evidence / "phase-evidence-invalidated.json"
        )
        invalid_phase = build_phase_evidence(
            snapshots=[
                _load_json(readiness_snapshot_path),
                _load_json(qualification_snapshot_path),
                _load_json(e2e_after_path),
            ],
            readiness=_load_json(readiness_report_path),
            qualification=_load_json(qualification_report_path),
            manifest=manifest_document,
        )
        _write_private_json(invalid_phase_path, invalid_phase)
        invalid_report_path = (
            layout.evidence / "invalidated-report.json"
        )
        invalid_report = assemble_e2e_report(
            _load_json(off_observation_path),
            _load_json(auto_observation_path),
            invalid_phase,
            raw_root=layout.work / "raw-invalidated",
            output=invalid_report_path,
        )
        if (
            invalid_report.get("verdict") != "inconclusive"
            or "phase_identity_drift"
            not in invalid_report.get("reason_codes", [])
        ):
            raise AcceptanceRunError(
                "real_worker_restart_did_not_invalidate_old_evidence"
            )
        invalidation = build_invalidation_evidence(
            source_git_commit=commit,
            before_snapshot_path=e2e_snapshot_path,
            after_snapshot_path=e2e_after_path,
            invalidated_report_path=invalid_report_path,
        )
        if (
            validate_invalidation_evidence(
                invalidation,
                run_id=run_id,
                cluster_epoch=cluster_epoch,
                source_git_commit=commit,
            )
            != "pass"
        ):
            raise AcceptanceRunError("invalidation_proof_failed")
        invalidation_path = (
            layout.evidence / "invalidation-proof.json"
        )
        _write_private_json(invalidation_path, invalidation)
        _announce("real Worker-2 restart invalidated stale phase evidence")

        for role, container in containers.items():
            commands.log(
                ["docker", "logs", container],
                layout.logs / f"{role}.log",
                timeout=60,
            )
        auth_proof = probe_environment(
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            address=arguments.jobs_address,
            non_loopback_host=arguments.non_loopback_host,
            token_file=token_file,
            containers=[containers[role] for role in _ROLES],
            image=image,
            scan_artifacts=_private_scan_files(layout),
        )
        if (
            validate_auth_evidence(
                auth_proof,
                run_id=run_id,
                cluster_epoch=cluster_epoch,
            )
            != "pass"
        ):
            raise AcceptanceRunError("dashboard_auth_probe_failed")
        auth_path = layout.evidence / "environment-auth-proof.json"
        _write_private_json(auth_path, auth_proof)
        _announce("loopback/auth/secret negative probes passed")

        removed_container_ids = _project_ids(
            commands,
            kind="container",
            project=project,
            run_id=run_id,
        )
        removed_network_ids = _project_ids(
            commands,
            kind="network",
            project=project,
            run_id=run_id,
        )
        if len(removed_container_ids) != 3 or len(removed_network_ids) != 2:
            raise AcceptanceRunError(
                "project_resource_count_drift_before_cleanup"
            )
        if runtime_zone_binding is not None:
            unbind_runtime_trusted(commands, runtime_zone_binding)
            runtime_zone_binding_active = False
            if bridge_accommodation is None:
                raise AcceptanceRunError(
                    "bridge_accommodation_missing_before_cleanup"
                )
            bridge_accommodation["binding_removed"] = True
        _down(
            commands,
            compose=compose,
            environment=compose_environment,
            project=project,
            run_id=run_id,
            log_path=layout.logs / "compose-down.log",
        )
        compose_active = False
        token_file.unlink()
        if data_plane_bridge is None or bridge_accommodation is None:
            raise AcceptanceRunError("bridge_cleanup_evidence_missing")
        bridge_accommodation[
            "bridge_interface_exists_after_cleanup"
        ] = _interface_exists(data_plane_bridge.interface)
        after_state = capture_host_state()
        _write_private_json(
            layout.evidence / "host-state-after.json",
            after_state,
        )
        cleanup = build_cleanup_proof(
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            before=before_state,
            after=after_state,
            removed_container_ids=removed_container_ids,
            removed_network_ids=removed_network_ids,
            remaining_project_containers=_project_ids(
                commands,
                kind="container",
                project=project,
                run_id=run_id,
            ),
            remaining_project_networks=_project_ids(
                commands,
                kind="network",
                project=project,
                run_id=run_id,
            ),
            dashboard_port_open=_port_open("127.0.0.1", 8265),
            token_exists=token_file.exists(),
            bridge_accommodation=bridge_accommodation,
        )
        cleanup_path = layout.evidence / "cleanup-proof.json"
        _write_private_json(cleanup_path, cleanup)
        if (
            validate_cleanup_evidence(
                cleanup,
                run_id=run_id,
                cluster_epoch=cluster_epoch,
            )
            != "pass"
        ):
            raise AcceptanceRunError("cleanup_proof_failed")
        _announce("containers/networks/token removed; routes/firewall restored")

        raw_files = [
            off_observation_path,
            auto_observation_path,
            readiness_snapshot_path,
            qualification_snapshot_path,
            pre_live_snapshot_path,
            e2e_snapshot_path,
            e2e_after_path,
            readiness_report_path,
            qualification_report_path,
            pre_live_phase_path,
            pre_live_report_path,
            phase_path,
            invalid_phase_path,
        ]
        _delete_raw_files(raw_files)
        _assert_absent(raw_files)
        retained_reports = [
            base_report_path,
            invalid_report_path,
            black_box_paths["off"],
            black_box_paths["auto"],
            measurement_path,
            source_path,
            cinderx_proof_path,
            unit_path,
            integration_path,
            live_path,
            auth_path,
            invalidation_path,
            cleanup_path,
        ]
        hygiene = build_hygiene_proof(
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            retained_reports=retained_reports,
            raw_event_files=raw_files,
        )
        if (
            validate_hygiene_evidence(
                hygiene,
                run_id=run_id,
                cluster_epoch=cluster_epoch,
            )
            != "pass"
        ):
            raise AcceptanceRunError("evidence_hygiene_failed")
        hygiene_path = layout.evidence / "hygiene-proof.json"
        _write_private_json(hygiene_path, hygiene)

        infrastructure = assemble_infrastructure_evidence(
            run_id=run_id,
            cluster_epoch=cluster_epoch,
            cinderx=cinderx_proof,
            unit=unit_proof,
            integration=integration_proof,
            live=live_proof,
            auth=auth_proof,
            invalidation=invalidation,
            cleanup=cleanup,
            hygiene=hygiene,
        )
        infrastructure_path = (
            layout.evidence / "infrastructure-proof.json"
        )
        _write_private_json(infrastructure_path, infrastructure)
        formal_report = assemble_formal_acceptance(
            contract_path=contract_path,
            base_report_path=base_report_path,
            black_box_off_path=black_box_paths["off"],
            black_box_auto_path=black_box_paths["auto"],
            source_path=source_path,
            infrastructure_path=infrastructure_path,
            measurement_path=measurement_path,
            unit_completion_status=unit_completion_status,
        )
        formal_path = layout.evidence / "formal-acceptance-report.json"
        _write_private_json(formal_path, formal_report)
        if not _acceptance_report_passes(
            formal_report,
            require_release_ready=bool(
                getattr(arguments, "require_release_ready", False)
            ),
        ):
            raise AcceptanceRunError(
                "formal_acceptance_failed:"
                f"{formal_report.get('reason_codes')}:"
                f"{formal_report.get('lifecycle_reason_codes', [])}"
            )

        formal_container_path = "/evidence/evidence/formal-acceptance-report.json"
        formal_test_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{layout.root}:/evidence:ro",
            "--env",
            "UDFJIT_FORMAL_ACCEPTANCE=1",
            "--env",
            f"UDFJIT_FORMAL_ACCEPTANCE_REPORT_PATH={formal_container_path}",
            "--env",
            f"UDFJIT_RUN_ID={run_id}",
            "--env",
            f"UDFJIT_CLUSTER_EPOCH={cluster_epoch}",
            "--env",
            f"UDFJIT_GIT_COMMIT={commit}",
            "--entrypoint",
            "python",
            image,
            "-m",
            "unittest",
            "tests.system.test_formal_acceptance",
            "-v",
        ]
        commands.log(
            formal_test_argv,
            layout.logs / "formal-report-test.log",
            timeout=180,
        )
        final_state = capture_host_state()
        if (
            final_state != before_state
            or _project_ids(
                commands,
                kind="container",
                project=project,
                run_id=run_id,
            )
            or _project_ids(
                commands,
                kind="network",
                project=project,
                run_id=run_id,
            )
            or _port_open("127.0.0.1", 8265)
        ):
            raise AcceptanceRunError("post_report_host_state_drift")

        token_bytes = token.encode("ascii")
        final_private_files = _private_scan_files(layout)
        if any(token_bytes in path.read_bytes() for path in final_private_files):
            raise AcceptanceRunError("token_leaked_after_final_assembly")
        summary = {
            "schema_version": 1,
            "status": "pass",
            "profile": contract.profile,
            "contract_schema": contract.schema_id,
            "unit_completion_status": unit_completion_status.value,
            "release_ready": formal_report.get("release_ready"),
            "run_id": run_id,
            "cluster_epoch": cluster_epoch,
            "git_commit": commit,
            "image": image,
            "image_digest": source_document["image_digest"],
            "formal_report": str(formal_path),
            "formal_report_sha256": _sha256(formal_path),
            "base_report_sha256": _sha256(base_report_path),
            "cinderx_proof_sha256": cinderx_proof["proof_sha256"],
            "unit_tests": unit_proof["test_count"],
            "integration_tests": integration_proof["test_count"],
            "live_tests": live_proof["test_count"],
            "gate_count": len(formal_report["gates"]),
            "requirement_count": len(formal_report["requirements"]),
            "acceptance_example_count": len(
                formal_report["acceptance_examples"]
            ),
        }
        _write_private_json(layout.root / "RUN_SUMMARY.json", summary)
        _announce(
            "formal acceptance PASS; all UT/IT/ST gates and cleanup verified"
        )
        return formal_path
    except BaseException as error:
        cleanup_errors: list[str] = []
        if (
            runtime_zone_binding is not None
            and runtime_zone_binding_active
        ):
            try:
                unbind_runtime_trusted(commands, runtime_zone_binding)
                runtime_zone_binding_active = False
                if bridge_accommodation is not None:
                    bridge_accommodation["binding_removed"] = True
            except Exception as cleanup_error:
                cleanup_errors.append(
                    "firewalld_unbind_before_down:"
                    f"{type(cleanup_error).__name__}"
                )
        if compose_active:
            try:
                _down(
                    commands,
                    compose=compose,
                    environment=compose_environment,
                    project=project,
                    run_id=run_id,
                    log_path=None,
                )
                compose_active = False
            except Exception as cleanup_error:
                cleanup_errors.append(
                    "compose_down:"
                    f"{type(cleanup_error).__name__}"
                )
        if (
            runtime_zone_binding is not None
            and runtime_zone_binding_active
        ):
            try:
                unbind_runtime_trusted(commands, runtime_zone_binding)
                runtime_zone_binding_active = False
                if bridge_accommodation is not None:
                    bridge_accommodation["binding_removed"] = True
            except Exception as cleanup_error:
                cleanup_errors.append(
                    "firewalld_unbind_after_down:"
                    f"{type(cleanup_error).__name__}"
                )
        try:
            token_file.unlink(missing_ok=True)
        except Exception as cleanup_error:
            cleanup_errors.append(
                "token_removal:"
                f"{type(cleanup_error).__name__}"
            )
        failure_host_state_restored: bool | None = None
        if before_state is not None:
            try:
                failure_after_state = capture_host_state()
                failure_host_state_restored = (
                    failure_after_state == before_state
                )
            except Exception as cleanup_error:
                cleanup_errors.append(
                    "failure_host_state_capture:"
                    f"{type(cleanup_error).__name__}"
                )
        try:
            _scrub_failure_payloads(layout)
        except Exception as cleanup_error:
            cleanup_errors.append(
                "failure_artifact_scrub:"
                f"{type(cleanup_error).__name__}"
            )
        raw_payloads_removed = not any(
            path.is_file()
            for root in (
                layout.evidence,
                layout.logs,
                layout.private,
                layout.work,
            )
            for path in root.rglob("*")
        )
        if not raw_payloads_removed:
            cleanup_errors.append("failure_artifact_scrub:files_remain")
        failure_path = layout.root / "FAILURE.json"
        if not failure_path.exists():
            _write_private_json(
                failure_path,
                {
                    "schema_version": 2,
                    "status": "fail",
                    "run_id": run_id,
                    "cluster_epoch": cluster_epoch,
                    "git_commit": commit,
                    "error_type": type(error).__name__,
                    "reason_code": _failure_reason(error),
                    "cleanup_errors": cleanup_errors,
                    "host_state_restored": failure_host_state_restored,
                    "raw_payloads_removed": raw_payloads_removed,
                },
            )
        if token and token.encode("ascii") in failure_path.read_bytes():
            failure_path.unlink(missing_ok=True)
            raise AcceptanceRunError(
                "failure_receipt_contains_auth_token"
            ) from error
        raise


def main() -> None:
    arguments = _parser().parse_args()
    path = run(arguments)
    print(path)


if __name__ == "__main__":
    main()
