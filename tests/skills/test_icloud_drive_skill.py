"""Behavior contracts for the bundled macOS iCloud Drive skill helper."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import math
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "apple" / "icloud-drive"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "icloud_file.py"


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value



def _load_helper():
    spec = importlib.util.spec_from_file_location("icloud_file_skill_helper", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper():
    return _load_helper()


def test_skill_is_macos_gated_and_routes_through_hermes_tools():
    from agent.skill_utils import parse_frontmatter

    frontmatter, body = parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    hermes_metadata = frontmatter["metadata"]["hermes"]

    assert frontmatter["name"] == "icloud-drive"
    assert frontmatter["platforms"] == ["macos"]
    assert len(frontmatter["description"]) <= 60
    assert hermes_metadata["requires_toolsets"] == ["terminal", "file"]
    assert "scripts/icloud_file.py" in body
    assert "terminal(" in body
    assert "read_file(" in body


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_cli_rejects_nonfinite_and_nonpositive_durations(helper, value):
    with pytest.raises(argparse.ArgumentTypeError):
        helper._positive_float(value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0, -1])
def test_runtime_rejects_nonfinite_and_nonpositive_durations(helper, value):
    with pytest.raises(ValueError, match="finite and greater than zero"):
        helper.wait_for_upload(Path("unused"), timeout=value)


def test_materialization_requests_download_then_retries_edeadlk(tmp_path, helper):
    path = tmp_path / "placeholder.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    requests: list[tuple[Path, float]] = []
    sleeps: list[float] = []
    probes: list[BaseException | tuple[bytes, int]] = [
        OSError(errno.EDEADLK, "Resource deadlock avoided"),
        (b"%PDF-1.7", path.stat().st_size),
    ]

    def probe(_path: Path, _remaining: float) -> tuple[bytes, int]:
        result = probes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    result = helper.wait_for_materialization(
        path,
        request_download=lambda requested, remaining: (
            requests.append((requested, remaining)) or (0, "")
        ),
        probe=probe,
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert requests == [(path, 30.0)]
    assert sleeps == [0.5]
    assert result["attempts"] == 2
    assert result["size"] == path.stat().st_size


def test_materialization_accepts_successful_zero_byte_read(tmp_path, helper):
    path = tmp_path / "empty.txt"
    path.touch()

    result = helper.wait_for_materialization(
        path,
        request_download=lambda _path, _remaining: (0, ""),
        probe=lambda _path, _remaining: (b"", 0),
        clock=lambda: 0.0,
    )

    assert result["size"] == 0
    assert result["prefixBytes"] == 0


def test_materialization_does_not_swallow_unrelated_io_error(tmp_path, helper):
    path = tmp_path / "document.pdf"

    def denied(_path: Path, _remaining: float) -> tuple[bytes, int]:
        raise PermissionError(errno.EACCES, "denied")

    with pytest.raises(PermissionError):
        helper.wait_for_materialization(
            path,
            request_download=lambda _path, _remaining: (0, ""),
            probe=denied,
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )


def test_materialization_timeout_is_bounded(tmp_path, helper):
    path = tmp_path / "placeholder.pdf"
    clock = ManualClock()

    def blocked(_path: Path, remaining: float) -> tuple[bytes, int]:
        assert remaining == 1.0
        clock.value = 1.0
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    with pytest.raises(TimeoutError, match="within 1s"):
        helper.wait_for_materialization(
            path,
            timeout=1,
            request_download=lambda _path, _remaining: (0, ""),
            probe=blocked,
            clock=clock,
            sleeper=lambda _seconds: None,
        )


def test_materialization_rejects_success_after_deadline(tmp_path, helper):
    path = tmp_path / "late.pdf"
    clock = ManualClock()

    def late_success(_path: Path, remaining: float) -> tuple[bytes, int]:
        assert remaining == 1.0
        clock.value = 1.0
        return b"%PDF", 4

    with pytest.raises(TimeoutError, match="completed after deadline"):
        helper.wait_for_materialization(
            path,
            timeout=1,
            request_download=lambda _path, _remaining: (0, ""),
            probe=late_success,
            clock=clock,
        )


def test_materialization_rejects_download_finishing_at_deadline(tmp_path, helper):
    path = tmp_path / "late.pdf"
    clock = ManualClock()

    def late_request(_path: Path, remaining: float) -> tuple[int, str]:
        assert remaining == 1.0
        clock.value = 1.0
        return 0, ""

    with pytest.raises(TimeoutError, match="download request exhausted deadline"):
        helper.wait_for_materialization(
            path,
            timeout=1,
            request_download=late_request,
            probe=lambda _path, _remaining: (b"data", 4),
            clock=clock,
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires a POSIX FIFO")
def test_wedged_probe_child_is_killed_and_reaped(tmp_path, helper):
    fifo = tmp_path / "wedged-fileprovider-read"
    os.mkfifo(fifo)
    child_pids: list[int] = []
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="exact file probe .* exceeded"):
        helper._probe_in_child(fifo, 0.1, on_spawn=child_pids.append)

    assert time.monotonic() - started < 1.0
    assert len(child_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pids[0], os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is macOS-only")
def test_exclusive_publish_preserves_competing_destination(tmp_path, helper):
    source = tmp_path / "staged.zip"
    destination = tmp_path / "archive.zip"
    source.write_bytes(b"ours")
    destination.write_bytes(b"competitor")

    with pytest.raises(FileExistsError):
        helper.publish_exclusive(source, destination, timeout=2)

    assert source.read_bytes() == b"ours"
    assert destination.read_bytes() == b"competitor"


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is macOS-only")
def test_exclusive_publish_moves_source_when_destination_is_absent(tmp_path, helper):
    source = tmp_path / "staged.zip"
    destination = tmp_path / "archive.zip"
    source.write_bytes(b"ours")

    result = helper.publish_exclusive(source, destination, timeout=2)

    assert not source.exists()
    assert destination.read_bytes() == b"ours"
    assert result["destination"] == str(destination)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "isUploaded = 1; isUploading = 0; isExcludedFromSync = 0;",
            {
                "isUploaded": True,
                "isUploading": False,
                "isExcludedFromSync": False,
            },
        ),
        (
            "isUploaded = 0; isUploading = 1;",
            {
                "isUploaded": False,
                "isUploading": True,
                "isExcludedFromSync": None,
            },
        ),
    ],
)
def test_upload_state_requires_explicit_metadata(helper, output, expected):
    assert helper.upload_state(output) == expected


def test_wait_for_upload_retries_until_all_invariants_hold(tmp_path, helper):
    path = tmp_path / "archive.zip"
    outputs = iter(
        [
            (0, "isUploaded = 0; isUploading = 1; isExcludedFromSync = 0;"),
            (0, "isUploaded = 1; isUploading = 0; isExcludedFromSync = 0;"),
        ]
    )
    sleeps: list[float] = []
    remaining_budgets: list[float] = []

    def evaluate(_path: Path, remaining: float) -> tuple[int, str]:
        remaining_budgets.append(remaining)
        return next(outputs)

    result = helper.wait_for_upload(
        path,
        evaluate=evaluate,
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert remaining_budgets == [60.0, 60.0]
    assert sleeps == [1]
    assert result["attempts"] == 2
    assert result["isUploaded"] is True
    assert result["isUploading"] is False
    assert result["isExcludedFromSync"] is False


def test_wait_for_upload_fails_fast_when_item_is_excluded(tmp_path, helper):
    path = tmp_path / "archive.zip"
    with pytest.raises(RuntimeError, match="excludes this item"):
        helper.wait_for_upload(
            path,
            evaluate=lambda _path, _remaining: (
                0,
                "isUploaded = 0; isUploading = 0; isExcludedFromSync = 1;",
            ),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )


def test_wait_for_upload_times_out_on_missing_metadata_and_nonzero_result(
    tmp_path, helper
):
    path = tmp_path / "archive.zip"
    clock = ManualClock()

    def incomplete(_path: Path, remaining: float) -> tuple[int, str]:
        assert remaining == 1.0
        clock.value = 1.0
        return 2, "metadata unavailable"

    with pytest.raises(TimeoutError, match="returnCode=2"):
        helper.wait_for_upload(
            path,
            timeout=1,
            evaluate=incomplete,
            clock=clock,
            sleeper=lambda _seconds: None,
        )


def test_wait_for_upload_rejects_success_after_deadline(tmp_path, helper):
    path = tmp_path / "archive.zip"
    clock = ManualClock()

    def late_success(_path: Path, remaining: float) -> tuple[int, str]:
        assert remaining == 1.0
        clock.value = 1.0
        return 0, "isUploaded = 1; isUploading = 0; isExcludedFromSync = 0;"

    with pytest.raises(TimeoutError, match="within 1s"):
        helper.wait_for_upload(
            path,
            timeout=1,
            evaluate=late_success,
            clock=clock,
        )


def test_wait_for_upload_rejects_parser_crossing_deadline(
    tmp_path, helper, monkeypatch
):
    path = tmp_path / "archive.zip"
    clock = ManualClock()
    original_upload_state = helper.upload_state

    def delayed_parse(output: str):
        state = original_upload_state(output)
        clock.value = 1.0
        return state

    monkeypatch.setattr(helper, "upload_state", delayed_parse)
    with pytest.raises(TimeoutError, match="within 1s"):
        helper.wait_for_upload(
            path,
            timeout=1,
            evaluate=lambda _path, _remaining: (
                0,
                "isUploaded = 1; isUploading = 0; isExcludedFromSync = 0;",
            ),
            clock=clock,
        )


def test_evaluate_timeout_is_reported_as_upload_deadline(tmp_path, helper):
    path = tmp_path / "archive.zip"

    def wedged(_path: Path, _remaining: float) -> tuple[int, str]:
        raise TimeoutError("fileproviderctl timed out")

    with pytest.raises(TimeoutError, match="did not confirm upload"):
        helper.wait_for_upload(
            path,
            timeout=1,
            evaluate=wedged,
            clock=lambda: 0.0,
        )
