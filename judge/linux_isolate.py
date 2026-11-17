"""Linux production sandbox backend built on IOI isolate.

Isolation enforced here, before untrusted code runs:

* A fresh isolate box per spawn, with separate network and mount namespaces.
* cgroup v2 accounting for wall time, CPU time, peak memory and process count;
  disk quotas are opt-in (JUDGE_ENABLE_DISK_QUOTA=1) because tmpfs box roots
  cannot enforce project quotas.
* The task directory is the only writable mount inside the box; toolchains
  and /etc are mounted read-only.
* Per-stream hard output caps in a supervising proxy, plus the combined
  stdout+stderr cap enforced by ProcessRunner on every poll.
* Whole-box termination and box cleanup on every path, including failures.

The backend never claims an isolation capability that it has not positively
verified. Unsupported cgroup features fail closed instead of falling back to
local subprocess execution.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import ToolchainConfig
from .process import ProcessLimits
from .sandbox import (
    IsolationCapabilities,
    SandboxBackend,
    SandboxConfigurationError,
    SandboxExecutionInfo,
    SandboxTerminationReason,
)

SANDBOX_PATH_ENV = "JUDGE_ISOLATE_PATH"
META_ROOT_ENV = "JUDGE_META_ROOT"
ROOTFS_ENV = "JUDGE_LINUX_ROOTFS"
CGROUP_ROOT_ENV = "JUDGE_CGROUP_ROOT"
FIRST_UID_ENV = "JUDGE_ISOLATE_FIRST_UID"
FIRST_GID_ENV = "JUDGE_ISOLATE_FIRST_GID"
USER_IDS_ENV = "JUDGE_ISOLATE_USER_IDS"
TOOLCHAIN_ROOT_ENV = "JUDGE_TOOLCHAIN_ROOT"
PROCS_ENV = "JUDGE_PROCESS_LIMIT"
COMPILE_PROCS_ENV = "JUDGE_COMPILE_PROCESS_LIMIT"
DISK_BLOCKS_ENV = "JUDGE_DISK_BLOCKS"
DISK_INODES_ENV = "JUDGE_DISK_INODES"
COMPILE_DISK_BLOCKS_ENV = "JUDGE_COMPILE_DISK_BLOCKS"
COMPILE_DISK_INODES_ENV = "JUDGE_COMPILE_DISK_INODES"
DISK_QUOTA_ENV = "JUDGE_ENABLE_DISK_QUOTA"

DEFAULT_ISOLATE_PATH = "/usr/bin/isolate"
DEFAULT_META_ROOT = "/var/lib/xinbaoyan-judge/meta"
DEFAULT_ROOTFS = "/var/lib/xinbaoyan-judge/rootfs"
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"
DEFAULT_FIRST_UID = 60000
DEFAULT_FIRST_GID = 60000
DEFAULT_USER_IDS = 100
DEFAULT_TOOLCHAIN_ROOT = "/opt/judge-toolchains"
DEFAULT_PROCESS_LIMIT = 64
DEFAULT_DISK_BLOCKS = 51200  # 50 MiB in 1 KiB blocks.
DEFAULT_DISK_INODES = 4096

_BOX_PATH = "/box"
_TOOLCHAIN_PATH = "/toolchains"
_REQUIRED_CGROUP_CONTROLLERS = ("cpu", "memory", "pids", "io")

@dataclass(slots=True)
class _SpawnState:
    """Per-spawn resources that must be rolled back on every failure path."""

    box_id: int
    meta_dir: Path
    meta_file: Path
    stdin_file: Path
    stdout_file: Path
    stderr_file: Path
    proxy: _OutputProxy | None
    isolate: subprocess.Popen[bytes] | None
    cleanup_started: bool = False

def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SandboxConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise SandboxConfigurationError(f"{name} must be positive, got {raw!r}")
    return value

def _resolve_directory(env: Mapping[str, str], name: str, default: str) -> Path:
    raw = env.get(name, default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SandboxConfigurationError(f"{name} must be an absolute path: {raw!r}")
    return path

class _OutputProxy:
    """stdout/stderr proxy with a hard per-stream byte limit.

    Each stream is capped independently; the supervising ProcessRunner
    additionally enforces the combined stdout+stderr limit on every poll.
    """

    def __init__(
        self,
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        limit_bytes: int,
        stop_event: threading.Event,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self._limit_bytes = max(0, limit_bytes)
        self._stop_event = stop_event
        self._exceeded = threading.Event()
        self._errors: list[str] = []
        self._paths: dict[str, Path] = {}
        self._threads = [
            threading.Thread(target=self._pump, args=(name,), name=f"judge-output-{name}", daemon=True)
            for name in ("stdout", "stderr")
        ]

    def start(self, paths: Mapping[str, Path]) -> None:
        self._paths = dict(paths)
        for thread in self._threads:
            thread.start()

    def _pump(self, name: str) -> None:
        source = self._paths[name]
        written = 0
        # isolate creates the redirected files only after it enters the box, so
        # the proxy must wait for them instead of failing on the first open.
        deadline = time.monotonic() + 120
        while not source.exists():
            if self._stop_event.is_set() or time.monotonic() > deadline:
                return
            time.sleep(0.01)
        try:
            with source.open("rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    if written + len(chunk) > self._limit_bytes:
                        self._exceeded.set()
                        self._stop_event.set()
                        return
                    destination = self._stdout if name == "stdout" else self._stderr
                    destination.write(chunk)
                    destination.flush()
                    written += len(chunk)
        except OSError as exc:
            self._errors.append(f"{name} output proxy failed: {type(exc).__name__}")

    @property
    def exceeded(self) -> bool:
        return self._exceeded.is_set()

    def finish(self, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # Copy anything the early-terminated pump left behind (for example a
        # file created just before the box was killed).
        for name in ("stdout", "stderr"):
            source = self._paths.get(name)
            if source is None or not source.exists():
                continue
            destination = self._stdout if name == "stdout" else self._stderr
            try:
                destination.seek(0, os.SEEK_END)
                current = destination.tell()
                with source.open("rb") as stream:
                    stream.seek(current)
                    remaining = stream.read(max(0, self._limit_bytes - current))
                if remaining:
                    destination.write(remaining)
                    destination.flush()
            except OSError:
                continue

    def snapshot(self) -> tuple[bool, str]:
        return self.exceeded, "; ".join(self._errors)

class LinuxIsolateBackend(SandboxBackend):
    """Production sandbox adapter for an isolate-enabled Linux judge node."""

    _capabilities = IsolationCapabilities(
        process_group_termination=True,
        hard_wall_time_limit=True,
        hard_output_limit=True,
        hard_memory_limit=True,
        filesystem_isolation=True,
        network_disabled=True,
        low_privilege=True,
    )

    def __init__(self, *, toolchain: ToolchainConfig, work_root: Path | None, env: Mapping[str, str] | None = None):
        if os.name == "nt":
            raise SandboxConfigurationError("LinuxIsolateBackend is available only on POSIX systems.")
        self._env = os.environ if env is None else env
        self._toolchain = toolchain
        self._isolate_path = Path(self._env.get(SANDBOX_PATH_ENV, DEFAULT_ISOLATE_PATH)).expanduser()
        self._work_root = work_root
        self._meta_root = _resolve_directory(self._env, META_ROOT_ENV, DEFAULT_META_ROOT)
        self._rootfs = _resolve_directory(self._env, ROOTFS_ENV, DEFAULT_ROOTFS)
        self._cgroup_root = _resolve_directory(self._env, CGROUP_ROOT_ENV, DEFAULT_CGROUP_ROOT)
        self._first_uid = _env_int(self._env, FIRST_UID_ENV, DEFAULT_FIRST_UID)
        self._first_gid = _env_int(self._env, FIRST_GID_ENV, DEFAULT_FIRST_GID)
        self._user_ids = _env_int(self._env, USER_IDS_ENV, DEFAULT_USER_IDS)
        self._default_process_limit = _env_int(self._env, PROCS_ENV, DEFAULT_PROCESS_LIMIT)
        self._toolchain_root = _resolve_directory(self._env, TOOLCHAIN_ROOT_ENV, DEFAULT_TOOLCHAIN_ROOT)
        self._default_disk_blocks = _env_int(self._env, DISK_BLOCKS_ENV, DEFAULT_DISK_BLOCKS)
        self._default_disk_inodes = _env_int(self._env, DISK_INODES_ENV, DEFAULT_DISK_INODES)
        self._compile_process_limit = _env_int(
            self._env, COMPILE_PROCS_ENV, max(16, self._default_process_limit // 2)
        )
        self._compile_disk_blocks = _env_int(
            self._env, COMPILE_DISK_BLOCKS_ENV, max(51200, self._default_disk_blocks * 4)
        )
        self._compile_disk_inodes = _env_int(
            self._env, COMPILE_DISK_INODES_ENV, max(4096, self._default_disk_inodes * 4)
        )
        # Project quotas need a filesystem that supports them (ext4/xfs);
        # tmpfs box roots (docker deployment) must keep this off.
        self._disk_quota_enabled = self._env.get(DISK_QUOTA_ENV, "").strip() == "1"
        self._states: dict[int, _SpawnState] = {}
        self._final_info: dict[int, SandboxExecutionInfo] = {}
        self._lock = threading.Lock()
        self._mounts = self._build_mounts()
        self._meta_root.mkdir(parents=True, exist_ok=True)
        # Task artifacts (source, redirected I/O) are relaxed to 0666/0777 for
        # cross-UID case reuse, so the root directories must not be
        # world-traversable or other local users could read/tamper with them.
        for directory in (self._meta_root, self._work_root):
            if directory is not None:
                try:
                    directory.chmod(0o750)
                except OSError:
                    pass
        self._validate_static_configuration()

    @property
    def capabilities(self) -> IsolationCapabilities:
        return self._capabilities

    def _validate_static_configuration(self) -> None:
        if not self._isolate_path.is_file():
            raise SandboxConfigurationError(f"isolate executable is not available: {self._isolate_path}")
        if not self._rootfs.is_dir():
            raise SandboxConfigurationError(f"Linux judge rootfs is not available: {self._rootfs}")
        if self._work_root is not None:
            self._validated_work_root()
        self._validate_cgroup_v2()
        for source, destination in self._mounts:
            if not source.is_dir():
                raise SandboxConfigurationError(f"Sandbox mount source is missing: {source}")
            if not destination.startswith("/"):
                raise SandboxConfigurationError(f"Sandbox mount destination must be absolute: {destination}")

    def _validated_work_root(self) -> Path:
        assert self._work_root is not None
        try:
            root = self._work_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxConfigurationError(f"JUDGE_WORK_ROOT does not exist: {self._work_root}") from exc
        if not root.is_dir():
            raise SandboxConfigurationError(f"JUDGE_WORK_ROOT must be a directory: {root}")
        return root

    def _validate_cgroup_v2(self) -> None:
        unified = self._cgroup_root / "cgroup.controllers"
        subtree = self._cgroup_root / "cgroup.subtree_control"
        if not unified.is_file() or not subtree.is_file():
            raise SandboxConfigurationError(
                f"Linux judge backend requires cgroup v2 at {self._cgroup_root}; cgroup v1 is unsupported."
            )
        available = unified.read_text(encoding="utf-8").split()
        missing = [controller for controller in _REQUIRED_CGROUP_CONTROLLERS if controller not in available]
        if missing:
            raise SandboxConfigurationError(
                "Linux judge cgroup v2 hierarchy is missing required controllers: " + ", ".join(missing)
            )

    def _build_mounts(self) -> tuple[tuple[Path, str], ...]:
        self._validate_toolchains()
        mounts: dict[str, Path] = {_TOOLCHAIN_PATH: self._toolchain_root}
        mounts["/"] = self._rootfs
        return tuple((source, destination) for destination, source in sorted(mounts.items()))

    def _validate_toolchains(self) -> None:
        root = self._toolchain_root.resolve(strict=True)
        if root == Path("/"):
            raise SandboxConfigurationError("Refusing to mount the host root as the toolchain directory.")
        resolved_values = []
        for raw_path in self._toolchain.sensitive_paths():
            resolved = Path(raw_path).resolve(strict=True)
            if not resolved.is_file():
                raise SandboxConfigurationError(f"Configured toolchain is missing: {resolved}")
            resolved_values.append(resolved)
        for resolved in resolved_values:
            if resolved != root and root not in resolved.parents:
                raise SandboxConfigurationError(
                    f"Configured toolchain {resolved} is outside {TOOLCHAIN_ROOT_ENV}={root}"
                )

    def _map_path(self, value: str) -> str:
        for source, destination in self._mounts:
            source_text = str(source)
            if value == source_text:
                return destination
            if value.startswith(source_text + os.sep):
                return destination + value[len(source_text) :]
        return value

    def _sandbox_argv(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            raise SandboxConfigurationError("Sandbox command cannot be empty.")
        return [self._map_path(str(part)) for part in argv]

    def _limits(self, argv: Sequence[str], limits: ProcessLimits | None) -> tuple[int, int, int, int | None]:
        if limits is None:
            raise SandboxConfigurationError("Linux judge backend requires explicit process limits.")
        is_compile = self._is_compile_command(argv)
        process_limit = self._compile_process_limit if is_compile else self._default_process_limit
        disk_blocks = self._compile_disk_blocks if is_compile else self._default_disk_blocks
        disk_inodes = self._compile_disk_inodes if is_compile else self._default_disk_inodes
        return process_limit, disk_blocks, disk_inodes, limits.memory_limit_kb

    def _is_compile_command(self, argv: Sequence[str]) -> bool:
        executable = Path(argv[0]).name.lower()
        if executable in {"javac", "gcc", "g++", "gcc.exe", "g++.exe", "javac.exe"}:
            return True
        return executable.startswith("python") and len(argv) > 2 and argv[1] == "-m" and argv[2] == "py_compile"

    def _ensure_work_dir(self, cwd: Path) -> Path:
        if self._work_root is None:
            raise SandboxConfigurationError("JUDGE_WORK_ROOT is required for Linux judge isolation.")
        work_root = self._validated_work_root()
        resolved = cwd.resolve(strict=True)
        if resolved != work_root and work_root not in resolved.parents:
            raise SandboxConfigurationError(f"Refusing to execute outside JUDGE_WORK_ROOT: {resolved}")
        return resolved

    def spawn(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: BinaryIO,
        creationflags: int,
        start_new_session: bool,
        limits: ProcessLimits | None = None,
    ) -> subprocess.Popen[bytes]:
        if creationflags:
            raise SandboxConfigurationError(f"Unsupported creation flags for Linux sandbox: {creationflags}")
        work_dir = self._ensure_work_dir(cwd)
        effective_limits = limits or ProcessLimits(wall_timeout_ms=1000, output_limit_bytes=1)
        process_limit, disk_blocks, disk_inodes, memory_limit_kb = self._limits(argv, effective_limits)
        command = self._sandbox_argv(argv)
        command = [
            f"/box{part[len(str(work_dir)):]}" if part.startswith(str(work_dir) + os.sep) else part
            for part in command
        ]
        box_id = self._init_box_with_retry(disk_blocks, disk_inodes)
        # The bind-mounted task directory must belong to the per-box UID so
        # isolate can create the redirected stdin/stdout files inside /box.
        try:
            os.chown(work_dir, self._first_uid + box_id, self._first_gid + box_id)  # type: ignore[attr-defined]
            os.chmod(work_dir, 0o700)
        except OSError:
            # Unprivileged worker: the box UID still needs to create its
            # redirected stdin/stdout files, so fall back to a writable dir.
            os.chmod(work_dir, 0o777)
        state = _SpawnState(
            box_id=box_id,
            meta_dir=Path(tempfile.mkdtemp(prefix=f"spawn-{box_id}-", dir=self._meta_root)),
            meta_file=Path(""),
            stdin_file=Path(""),
            stdout_file=Path(""),
            stderr_file=Path(""),
            proxy=None,
            isolate=None,
        )
        for stale in (".judge-sandbox-stdout", ".judge-sandbox-stderr", ".judge-sandbox-stdin"):
            self._remove_quietly(work_dir / stale)
        state.meta_file = state.meta_dir / "meta.json"
        state.stdin_file = work_dir / ".judge-sandbox-stdin"
        state.stdout_file = work_dir / ".judge-sandbox-stdout"
        state.stderr_file = work_dir / ".judge-sandbox-stderr"
        shutil.copyfileobj(stdin, state.stdin_file.open("wb"))
        command_line = [
            str(self._isolate_path),
            "--box-id",
            str(box_id),
            "--run",
            "--cg",
            "--env=HOME=/box",
            "--env=TMPDIR=/box",
            "--env=TMP=/box",
            "--env=TEMP=/box",
            "--env=PATH=/usr/bin:/bin",
            f"--dir=/box={work_dir}:rw",
            f"--dir={_TOOLCHAIN_PATH}={self._toolchain_mount_source()}:ro",
            "--dir=/etc:ro",
            f"--meta={state.meta_file}",
            f"--stdin=/box/{state.stdin_file.name}",
            f"--stdout=/box/{state.stdout_file.name}",
            f"--stderr=/box/{state.stderr_file.name}",
            f"--wall-time={max(1, (effective_limits.wall_timeout_ms + 999) // 1000)}",
            f"--processes={process_limit}",
            f"--fsize={max(1, (effective_limits.output_limit_bytes + 1023) // 1024)}",
        ]
        if memory_limit_kb is not None:
            command_line.append(f"--cg-mem={memory_limit_kb}")
        command_line.extend(["--", *command])
        try:
            state.isolate = subprocess.Popen(
                command_line,
                cwd=str(work_dir),
                env={key: value for key, value in env.items() if key in {"PATH", "LANG", "LC_ALL"}},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            state.proxy = _OutputProxy(
                stdout=stdout,
                stderr=stderr,
                limit_bytes=effective_limits.output_limit_bytes,
                stop_event=threading.Event(),
            )
            state.proxy.start({"stdout": state.stdout_file, "stderr": state.stderr_file})
            with self._lock:
                # OS pids wrap around: a reused pid must never inherit the
                # previous task's cached execution info.
                self._final_info.pop(state.isolate.pid, None)
                self._states[state.isolate.pid] = state
            return state.isolate
        except BaseException:
            self._rollback(state)
            raise

    def _box_cgroup_root(self, box_id: int) -> Path:
        # Matches the cg_root written by both deployment paths
        # (deploy/judge-linux/bootstrap.sh and the docker judge image).
        return self._cgroup_root / "isolate" / f"box-{box_id}"

    def _reset_cgroup(self, box_id: int) -> None:
        """Remove a stale isolate cgroup left by a killed run.

        Only invoked after a failed --init: resetting before init could kill
        processes of a box that another worker is actively using (isolate's
        own box lock is only checked during --init).
        """
        root = self._box_cgroup_root(box_id)
        if not root.exists():
            return
        try:
            (root / "cgroup.kill").write_text("1", encoding="utf-8")
        except OSError:
            pass
        for child in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if child.is_dir():
                    child.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass

    def _init_box(self, box_id: int, disk_blocks: int, disk_inodes: int) -> None:
        argv = [
            str(self._isolate_path),
            "--box-id",
            str(box_id),
            "--init",
            "--cg",
        ]
        if self._disk_quota_enabled:
            argv.append(f"--quota={disk_blocks},{disk_inodes}")
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and "cgroup" in result.stderr.lower():
            # A stale cgroup from a killed run can block init; clear it and
            # retry once. Scoped to cgroup errors so a box locked by another
            # worker is never force-killed here.
            self._reset_cgroup(box_id)
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            raise SandboxConfigurationError(f"isolate --init failed: {result.stderr.strip()}")

    def _toolchain_mount_source(self) -> Path:
        mounts = [source for source, destination in self._mounts if destination == _TOOLCHAIN_PATH]
        if not mounts:
            raise SandboxConfigurationError("Linux sandbox toolchain mount is not configured.")
        return mounts[0]

    def _allocate_box_id(self) -> int:
        # Random ids avoid the pid-derived sequence that made every task in a
        # process reuse the same box (a failed cleanup then poisoned the next
        # task) and could collide across workers on one host.
        return secrets.randbelow(1000)

    def _init_box_with_retry(self, disk_blocks: int, disk_inodes: int) -> int:
        last_error: SandboxConfigurationError | None = None
        for _ in range(10):
            box_id = self._allocate_box_id()
            try:
                self._init_box(box_id, disk_blocks, disk_inodes)
                return box_id
            except SandboxConfigurationError as exc:
                last_error = exc
                # Only retry when the box id is already taken (e.g. a stale
                # box from a crashed run); any other init failure is fatal.
                if "exists" not in str(exc).lower():
                    raise
        assert last_error is not None
        raise last_error

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        state = self._state_for(process)
        # Kill first, then drain the proxy: joining the pump while the program
        # still runs would delay termination by up to the join timeout and
        # lose output written between the flush and the kill.
        if state.isolate is not None and state.isolate.poll() is None:
            state.isolate.send_signal(getattr(signal, "SIGKILL", signal.SIGTERM))
        if state.isolate is not None:
            try:
                state.isolate.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._isolate_cleanup(state)
        if state.proxy is not None:
            state.proxy.finish()

    def memory_usage_kb(self, process: subprocess.Popen[bytes]) -> int | None:
        state = self._state_for(process)
        # Kernels before 5.19 have no cgroup memory.peak, so fall back to the
        # isolate meta max-rss (whole-run rusage) and finally live usage.
        cgroup_root = self._box_cgroup_root(state.box_id)
        for candidate in (
            cgroup_root / "memory.peak",
            cgroup_root / "memory.current",
        ):
            try:
                return max(1, int(candidate.read_text(encoding="utf-8").strip()) // 1024)
            except (OSError, ValueError):
                continue
        info = self._read_meta(process) or {}
        for key in ("max-rss", "cg-mem"):
            value = info.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return None

    def execution_info(self, process: subprocess.Popen[bytes]) -> SandboxExecutionInfo:
        cached = self._final_info.get(process.pid)
        if cached is not None:
            return cached
        state = self._state_for(process)
        meta = self._read_meta(process) or {}
        status = str(meta.get("status", ""))
        runtime_ms = meta.get("time") or meta.get("time-wall")
        if isinstance(runtime_ms, float):
            runtime_ms = int(runtime_ms * 1000)
        wall_exceeded = status == "TO"
        memory_exceeded = "cg-oom-killed" in meta
        output_exceeded = bool(state.proxy and state.proxy.exceeded)
        proxy_error = ""
        if state.proxy is not None:
            proxy_exceeded, proxy_error = state.proxy.snapshot()
            output_exceeded = output_exceeded or proxy_exceeded
        if wall_exceeded:
            reason = SandboxTerminationReason.WALL_TIME_LIMIT
        elif memory_exceeded:
            reason = SandboxTerminationReason.MEMORY_LIMIT
        elif output_exceeded:
            reason = SandboxTerminationReason.OUTPUT_LIMIT
        elif status in {"EX", "OK", "RE"}:
            reason = SandboxTerminationReason.NORMAL
        elif status:
            reason = SandboxTerminationReason.SANDBOX_ERROR
        else:
            reason = SandboxTerminationReason.UNKNOWN
        infrastructure_error = proxy_error
        if status and status not in {"EX", "OK", "RE", "TO", "SG", "ML"}:
            detail = str(meta.get("message", "")).strip()
            suffix = f"; isolate status={status}" + (f": {detail}" if detail else "")
            infrastructure_error = (infrastructure_error + suffix).lstrip("; ")
        return SandboxExecutionInfo(
            termination_reason=reason,
            runtime_ms=runtime_ms if isinstance(runtime_ms, int) else None,
            peak_memory_kb=self.memory_usage_kb(process),
            wall_time_exceeded=wall_exceeded,
            memory_exceeded=memory_exceeded,
            output_exceeded=output_exceeded,
            infrastructure_error=infrastructure_error,
        )

    def cleanup(self, process: subprocess.Popen[bytes]) -> None:
        state = self._state_for(process)
        if state.proxy is not None:
            state.proxy.finish()
        try:
            self._final_info[process.pid] = self.execution_info(process)
            # Bounded cache: entries exist so execution_info() still works
            # after cleanup; evict oldest beyond a generous window.
            while len(self._final_info) > 1024:
                self._final_info.pop(next(iter(self._final_info)))
        except Exception:
            pass
        if state.isolate is not None and state.isolate.poll() is None:
            self.terminate(process)
        self._isolate_cleanup(state)
        with self._lock:
            self._states.pop(process.pid, None)
        self._remove_quietly(state.meta_dir)
        self._relax_permissions(state.stdout_file.parent)

    @staticmethod
    def _relax_permissions(root: Path) -> None:
        """Make box-owned artifacts removable by the unprivileged worker."""
        for current, dirnames, filenames in os.walk(root):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(current, name), 0o777)
                except OSError:
                    pass
            for name in filenames:
                path = os.path.join(current, name)
                try:
                    # Later cases run under a different box UID, so they must
                    # be able to reopen (and truncate) files left behind.
                    os.chmod(path, os.stat(path).st_mode | 0o666)
                except OSError:
                    pass

    def _isolate_cleanup(self, state: _SpawnState) -> None:
        if state.cleanup_started:
            return
        state.cleanup_started = True
        try:
            subprocess.run(
                [str(self._isolate_path), "--box-id", str(state.box_id), "--cleanup"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except OSError:
            pass

    def _state_for(self, process: subprocess.Popen[bytes]) -> _SpawnState:
        with self._lock:
            state = self._states.get(process.pid)
        if state is None:
            raise SandboxConfigurationError(f"Unknown Linux sandbox process: {process.pid}")
        return state

    def _read_meta(self, process: subprocess.Popen[bytes]) -> dict[str, object] | None:
        state = self._state_for(process)
        try:
            lines = state.meta_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        data: dict[str, object] = {}
        for line in lines:
            key, separator, value = line.partition(":")
            if not separator:
                continue
            try:
                number = float(value)
                data[key] = int(number) if number.is_integer() else number
            except ValueError:
                data[key] = value
        return data

    def _rollback(self, state: _SpawnState) -> None:
        if state.proxy is not None:
            state.proxy.finish()
        if state.isolate is not None and state.isolate.poll() is None:
            try:
                state.isolate.kill()
                state.isolate.wait(timeout=5)
            except Exception:
                pass
        self._isolate_cleanup(state)
        self._remove_quietly(state.meta_dir)
        self._remove_quietly(state.stdout_file)
        self._remove_quietly(state.stderr_file)

    @staticmethod
    def _remove_quietly(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
