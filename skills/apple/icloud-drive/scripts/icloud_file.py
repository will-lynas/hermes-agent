#!/usr/bin/env python3
"""Bounded helpers for macOS iCloud Drive placeholder and upload state."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

_MATERIALIZATION_ERRNOS = {errno.EAGAIN, errno.EDEADLK}
_RENAME_EXCL = 0x00000004


def _finite_positive(value: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    try:
        return _finite_positive(float(value), "value")
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "must be finite and greater than zero"
        ) from exc


def _command(name: str) -> str:
    resolved = Path("/usr/bin") / name
    if not resolved.is_file():
        raise RuntimeError(f"required macOS command not found: {name}")
    return str(resolved)


def _run_command(
    argv: list[str],
    *,
    timeout: float,
    stdout: int,
    stderr: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"command exceeded the remaining {timeout:g}s deadline: {argv[0]}"
        ) from exc


def _request_download(path: Path, timeout: float) -> tuple[int, str]:
    proc = _run_command(
        [_command("brctl"), "download", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stderr.strip()


def _probe_worker(path: Path) -> int:
    """Read and stat one exact file in a child that the parent can terminate."""
    try:
        with path.open("rb") as handle:
            prefix = handle.read(16)
            size = os.fstat(handle.fileno()).st_size
    except OSError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errno": exc.errno,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "ok": True,
                "prefixHex": prefix.hex(),
                "size": size,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_worker(
    argv: list[str],
    *,
    timeout: float,
    description: str,
    on_spawn: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if on_spawn is not None:
        on_spawn(proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        if proc.poll() is None:
            raise RuntimeError(f"failed to reap timed-out {description}") from exc
        raise TimeoutError(
            f"{description} exceeded the remaining {timeout:g}s deadline"
        ) from exc
    return subprocess.CompletedProcess(
        argv,
        proc.returncode,
        stdout,
        stderr,
    )


def _probe_in_child(
    path: Path,
    timeout: float,
    *,
    on_spawn: Callable[[int], None] | None = None,
) -> tuple[bytes, int]:
    proc = _run_worker(
        [sys.executable, str(Path(__file__).resolve()), "_probe", str(path)],
        timeout=timeout,
        description=f"exact file probe for {path}",
        on_spawn=on_spawn,
    )

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"child exited {proc.returncode}"
        raise RuntimeError(f"exact file probe failed for {path}: {detail}")

    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"exact file probe returned invalid output for {path}") from exc

    if result.get("ok") is not True:
        number = result.get("errno")
        if not isinstance(number, int):
            raise RuntimeError(f"exact file probe returned no errno for {path}")
        raise OSError(number, str(result.get("error", "file probe failed")), str(path))

    try:
        return bytes.fromhex(result["prefixHex"]), int(result["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"exact file probe returned invalid metadata for {path}") from exc


def _rename_exclusive(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(
        os.fsencode(source),
        os.fsencode(destination),
        _RENAME_EXCL,
    ) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def _publish_worker(source: Path, destination: Path) -> int:
    try:
        _rename_exclusive(source, destination)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errno": exc.errno,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 0

    print(json.dumps({"ok": True}, sort_keys=True))
    return 0


def _publish_in_child(
    source: Path,
    destination: Path,
    timeout: float,
    *,
    on_spawn: Callable[[int], None] | None = None,
) -> None:
    proc = _run_worker(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_publish",
            str(source),
            str(destination),
        ],
        timeout=timeout,
        description=f"exclusive publish to {destination}",
        on_spawn=on_spawn,
    )

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"child exited {proc.returncode}"
        raise RuntimeError(f"exclusive publish failed for {destination}: {detail}")

    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"exclusive publish returned invalid output for {destination}"
        ) from exc

    if result.get("ok") is not True:
        number = result.get("errno")
        if not isinstance(number, int):
            raise RuntimeError(f"exclusive publish returned no errno for {destination}")
        raise OSError(
            number,
            str(result.get("error", "exclusive publish failed")),
            str(destination),
        )


def publish_exclusive(
    source: Path,
    destination: Path,
    *,
    timeout: float = 10,
    publish: Callable[[Path, Path, float], None] = _publish_in_child,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Atomically publish one same-filesystem file without replacing a target."""
    timeout = _finite_positive(timeout, "timeout")
    source = source.expanduser()
    destination = destination.expanduser()
    started = clock()
    deadline = started + timeout
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError(f"exclusive publish exceeded {timeout:g}s")
    publish(source, destination, remaining)
    finished = clock()
    if finished >= deadline:
        raise TimeoutError(f"exclusive publish exceeded {timeout:g}s")
    return {
        "source": str(source),
        "destination": str(destination),
        "elapsedSeconds": round(finished - started, 3),
    }


def _materialization_timeout(
    path: Path,
    timeout: float,
    last_error: BaseException | None,
    request_error: str,
) -> TimeoutError:
    detail = f"; brctl: {request_error}" if request_error else ""
    return TimeoutError(
        f"iCloud did not materialize {path} within {timeout:g}s: "
        f"{last_error or 'deadline exhausted'}{detail}"
    )


def wait_for_materialization(
    path: Path,
    *,
    timeout: float = 30,
    interval: float = 0.5,
    request_download: Callable[[Path, float], tuple[int, str]] = _request_download,
    probe: Callable[[Path, float], tuple[bytes, int]] = _probe_in_child,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Request an exact iCloud item and wait until an exact read succeeds."""
    timeout = _finite_positive(timeout, "timeout")
    interval = _finite_positive(interval, "interval")
    path = path.expanduser()
    started = clock()
    deadline = started + timeout
    request_error = ""
    last_error: BaseException | None = None

    remaining = deadline - clock()
    if remaining <= 0:
        raise _materialization_timeout(path, timeout, None, request_error)
    try:
        request_rc, request_error = request_download(path, remaining)
    except TimeoutError as exc:
        raise _materialization_timeout(path, timeout, exc, request_error) from exc

    if clock() >= deadline:
        raise _materialization_timeout(
            path, timeout, RuntimeError("download request exhausted deadline"), request_error
        )

    attempts = 0
    while True:
        now = clock()
        if now >= deadline:
            raise _materialization_timeout(path, timeout, last_error, request_error)

        attempts += 1
        try:
            prefix, size = probe(path, deadline - now)
        except TimeoutError as exc:
            raise _materialization_timeout(path, timeout, exc, request_error) from exc
        except OSError as exc:
            if exc.errno not in _MATERIALIZATION_ERRNOS:
                raise
            last_error = exc
        else:
            finished = clock()
            if finished >= deadline:
                raise _materialization_timeout(
                    path,
                    timeout,
                    RuntimeError("file probe completed after deadline"),
                    request_error,
                )
            return {
                "path": str(path),
                "size": size,
                "prefixBytes": len(prefix),
                "attempts": attempts,
                "elapsedSeconds": round(finished - started, 3),
                "downloadRequestReturnCode": request_rc,
            }

        now = clock()
        if now >= deadline:
            raise _materialization_timeout(path, timeout, last_error, request_error)
        sleeper(min(interval, deadline - now))


def _evaluate(path: Path, timeout: float) -> tuple[int, str]:
    proc = _run_command(
        [_command("fileproviderctl"), "evaluate", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def _metadata_flag(output: str, name: str) -> bool | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([01])\b", output)
    if not match:
        return None
    return match.group(1) == "1"


def upload_state(output: str) -> dict[str, bool | None]:
    """Extract the three fields required to prove an iCloud upload."""
    return {
        "isUploaded": _metadata_flag(output, "isUploaded"),
        "isUploading": _metadata_flag(output, "isUploading"),
        "isExcludedFromSync": _metadata_flag(output, "isExcludedFromSync"),
    }


def _upload_timeout(
    path: Path,
    timeout: float,
    last_rc: int | None,
    state: dict[str, bool | None],
) -> TimeoutError:
    return TimeoutError(
        f"FileProvider did not confirm upload for {path} within {timeout:g}s "
        f"(returnCode={last_rc}, state={state})"
    )


def wait_for_upload(
    path: Path,
    *,
    timeout: float = 60,
    interval: float = 1,
    evaluate: Callable[[Path, float], tuple[int, str]] = _evaluate,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Wait for exact FileProvider metadata proving upload completion."""
    timeout = _finite_positive(timeout, "timeout")
    interval = _finite_positive(interval, "interval")
    path = path.expanduser()
    started = clock()
    deadline = started + timeout
    attempts = 0
    last_rc: int | None = None
    state: dict[str, bool | None] = {
        "isUploaded": None,
        "isUploading": None,
        "isExcludedFromSync": None,
    }

    while True:
        now = clock()
        if now >= deadline:
            raise _upload_timeout(path, timeout, last_rc, state)

        attempts += 1
        try:
            last_rc, output = evaluate(path, deadline - now)
        except TimeoutError as exc:
            raise _upload_timeout(path, timeout, last_rc, state) from exc

        command_finished = clock()
        if command_finished >= deadline:
            raise _upload_timeout(path, timeout, last_rc, state)

        state = upload_state(output)
        parsed_at = clock()
        if parsed_at >= deadline:
            raise _upload_timeout(path, timeout, last_rc, state)
        if state["isExcludedFromSync"] is True:
            raise RuntimeError(f"FileProvider excludes this item from sync: {path}")
        if (
            last_rc == 0
            and state["isUploaded"] is True
            and state["isUploading"] is False
            and state["isExcludedFromSync"] is False
        ):
            accepted_at = clock()
            if accepted_at >= deadline:
                raise _upload_timeout(path, timeout, last_rc, state)
            return {
                "path": str(path),
                "attempts": attempts,
                "elapsedSeconds": round(accepted_at - started, 3),
                **state,
            }

        before_sleep = clock()
        if before_sleep >= deadline:
            raise _upload_timeout(path, timeout, last_rc, state)
        sleeper(min(interval, deadline - before_sleep))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize an iCloud file or wait for upload confirmation."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("path", type=Path)
    materialize.add_argument("--timeout", type=_positive_float, default=30.0)
    materialize.add_argument("--interval", type=_positive_float, default=0.5)

    upload = subparsers.add_parser("wait-upload")
    upload.add_argument("path", type=Path)
    upload.add_argument("--timeout", type=_positive_float, default=60.0)
    upload.add_argument("--interval", type=_positive_float, default=1.0)

    publish = subparsers.add_parser("publish")
    publish.add_argument("source", type=Path)
    publish.add_argument("destination", type=Path)
    publish.add_argument("--timeout", type=_positive_float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_probe":
        if len(argv) != 2:
            raise SystemExit("internal probe requires exactly one path")
        return _probe_worker(Path(argv[1]))
    if argv and argv[0] == "_publish":
        if sys.platform != "darwin":
            raise SystemExit("exclusive publish requires macOS")
        if len(argv) != 3:
            raise SystemExit("internal publish requires source and destination")
        return _publish_worker(Path(argv[1]), Path(argv[2]))

    if sys.platform != "darwin":
        raise SystemExit("icloud_file.py requires macOS")
    args = build_parser().parse_args(argv)
    if args.operation == "materialize":
        result = wait_for_materialization(
            args.path, timeout=args.timeout, interval=args.interval
        )
    elif args.operation == "wait-upload":
        result = wait_for_upload(args.path, timeout=args.timeout, interval=args.interval)
    else:
        result = publish_exclusive(
            args.source,
            args.destination,
            timeout=args.timeout,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
