---
name: icloud-drive
description: "Operate cloud-backed files safely on macOS."
version: 0.1.0
author: "Will Lynas (@will-lynas), Hermes Agent"
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [iCloud, FileProvider, CloudDocs, files, macOS, Apple]
    category: apple
    related_skills: []
    requires_toolsets: [terminal, file]
prerequisites:
  commands: [brctl, fileproviderctl]
---

# iCloud Drive Skill

Operate files under macOS iCloud Drive without treating cloud placeholders,
slow materialization, or FileProvider metadata as ordinary local filesystem
behavior. This covers files and folders, not Apple Notes, Reminders, Messages,
or Find My.

## When to Use

- The user asks to list, read, copy, move, rename, write, or delete an iCloud Drive item.
- A present file reports `Resource deadlock avoided` during a byte read or parser call.
- An exact directory listing hangs or disagrees with Finder.
- A workflow must prove that a newly written backup or document uploaded.
- FileProvider permissions, metadata, or consistency need diagnosis.

Do not use this skill for Apple Notes, Reminders, Messages, Find My, or another
cloud provider.

## Prerequisites

- macOS with iCloud Drive enabled and the user's account signed in.
- Hermes `terminal` and `read_file` tools.
- Native `/usr/bin/brctl` and `/usr/bin/fileproviderctl` commands.
- Bundled helper at
  `${HERMES_HOME:-$HOME/.hermes}/skills/apple/icloud-drive/scripts/icloud_file.py`.

The helper uses only Python's standard library. It sends one exact download
request, performs potentially wedged reads in a killable child, and applies the
remaining caller deadline to every command and probe.

## How to Run

1. Resolve one exact user-supplied path under
   `$HOME/Library/Mobile Documents/com~apple~CloudDocs`.
2. Inspect exact metadata with a bounded `terminal` call before enumerating.
3. For a cloud-only file, invoke `materialize`, then run the expected parser.
4. For a newly written file, invoke `wait-upload`, then verify size, checksum,
   and integrity.
5. Escalate to a private consistency report only when exact operations remain
   inconsistent. Ask before repair, sign-out, or daemon restarts.

## Quick Reference

| Goal | Hermes invocation |
|---|---|
| Inspect exact metadata | `terminal(command='ITEM="<exact-path>"; /usr/bin/fileproviderctl evaluate "$ITEM"', timeout=10)` |
| Materialize exact file | `terminal(command='HELPER="${HERMES_HOME:-$HOME/.hermes}/skills/apple/icloud-drive/scripts/icloud_file.py"; FILE="<exact-path>"; python3 "$HELPER" materialize "$FILE" --timeout 30', timeout=35)` |
| Prove upload | `terminal(command='HELPER="${HERMES_HOME:-$HOME/.hermes}/skills/apple/icloud-drive/scripts/icloud_file.py"; FILE="<exact-path>"; python3 "$HELPER" wait-upload "$FILE" --timeout 60', timeout=65)` |

Useful `fileproviderctl evaluate` fields vary by macOS version. Common fields
include `childItemCount`, `isDownloaded`, `isDownloading`,
`isMostRecentVersionDownloaded`, `isUploaded`, `isUploading`,
`isExcludedFromSync`, and `itemIdentifier`.

## Procedure

### 1. Enforce Safety Boundaries

1. Start with the exact path supplied by the user. Do not begin with recursive
   traversal, size aggregation, broad Spotlight searches, or a scan of the
   entire iCloud root.
2. Give every `terminal` call a hard timeout. A wedged FileProvider open can
   wait indefinitely; repeating equivalent commands adds no evidence.
3. Treat a successful local write as insufficient proof of cloud upload.
4. Do not repair FileProvider, sign out of iCloud, or restart `bird`,
   `fileproviderd`, or Finder without explicit user approval. These actions can
   disrupt sync and unrelated applications.
5. Do not interpret an empty result as “not found” when an operation timed out
   or lacked macOS privacy permission.

Completion criterion: one exact target is identified and no broad operation has
started.

### 2. Inspect an Exact Item

Call `terminal` with a self-contained command and deadline:

```python
terminal(
    command=r'''
set -euo pipefail
ITEM="$HOME/Library/Mobile Documents/com~apple~CloudDocs/path/to/item"
/usr/bin/stat -x "$ITEM"
/usr/bin/fileproviderctl evaluate "$ITEM"
''',
    timeout=10,
)
```

`evaluate` can prove that a folder exists and has children even when opening
the directory hangs. It does not generally return child names.

For a requested listing, make one bounded non-recursive attempt:

```python
terminal(
    command=r'''
set -euo pipefail
ITEM="$HOME/Library/Mobile Documents/com~apple~CloudDocs/path/to/folder"
ITEM="$ITEM" python3 - <<'PY'
import os
from pathlib import Path

for entry in os.scandir(Path(os.environ["ITEM"])):
    print(entry.name)
PY
''',
    timeout=10,
)
```

If exact metadata succeeds but enumeration times out, report that distinction
and move to diagnostics instead of retrying equivalent traversal.

Completion criterion: exact metadata is captured, and any listing either
completed within ten seconds or is explicitly reported as timed out.

### 3. Materialize a Cloud-Only File

A dataless FileProvider placeholder has metadata but no local content. An exact
read can surface POSIX `EDEADLK`, shown by Python as `OSError: [Errno 11]
Resource deadlock avoided`. For an iCloud path, this can be a materialization
signal rather than a literal process deadlock or database corruption.

Request and poll only the exact file:

```python
terminal(
    command=r'''
set -euo pipefail
HELPER="${HERMES_HOME:-$HOME/.hermes}/skills/apple/icloud-drive/scripts/icloud_file.py"
FILE="$HOME/Library/Mobile Documents/com~apple~CloudDocs/path/to/document.pdf"
python3 "$HELPER" materialize "$FILE" --timeout 30
''',
    timeout=35,
)
```

`brctl download` is asynchronous. Its zero exit status means the request was
accepted, not that content is local. The helper retries only `EDEADLK` and
`EAGAIN`; unrelated I/O errors fail immediately. It supports legitimate
zero-byte files because a successful exact read, not non-empty content, proves
the local open completed.

Then require the expected parser to succeed:

```python
terminal(
    command=r'''
set -euo pipefail
FILE="$HOME/Library/Mobile Documents/com~apple~CloudDocs/path/to/document.pdf"
/usr/bin/file "$FILE"
/usr/bin/fileproviderctl evaluate "$FILE"
if command -v pdfinfo >/dev/null 2>&1; then pdfinfo "$FILE"; fi
''',
    timeout=15,
)
```

Where reported, confirm `isDownloaded = 1` or
`isMostRecentVersionDownloaded = 1`. Non-empty content alone does not prove the
expected format.

Completion criterion: the exact read and expected parser succeed before their
deadlines.

### 4. Publish Atomically Without Clobbering

Build archives and generated documents in a local staging directory. Validate
the local source, copy it to a unique staging file in the destination directory,
then use the helper's macOS `RENAME_EXCL` operation to publish atomically. This
fails with `EEXIST` if a competing destination appears and never falls back to
ordinary replacement:

```python
terminal(
    command=r'''
set -euo pipefail
HELPER="${HERMES_HOME:-$HOME/.hermes}/skills/apple/icloud-drive/scripts/icloud_file.py"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
SRC="$HOME/path/to/staged/archive.zip"
DEST="$ICLOUD/Backups/archive.zip"
DEST_DIR="$(dirname "$DEST")"

unzip -t "$SRC"
mkdir -p "$DEST_DIR"
STAGED="$(mktemp "$DEST_DIR/.archive.zip.hermes-stage.XXXXXX")"
trap 'rm -f "$STAGED"' EXIT
cp "$SRC" "$STAGED"
unzip -t "$STAGED"
python3 "$HELPER" publish "$STAGED" "$DEST" --timeout 10
unzip -t "$DEST"
python3 "$HELPER" wait-upload "$DEST" --timeout 60
''',
    timeout=95,
)
```

Staging inside `DEST_DIR` guarantees the atomic rename stays on one filesystem.
If the destination exists or appears concurrently, stop and request explicit
overwrite approval. If the volume does not support exclusive rename, report the
failure; never fall back to `mv`, `os.rename`, or another replacing operation.
The upload helper succeeds only when exact metadata reports all three invariants:

```text
isUploaded = 1
isUploading = 0
isExcludedFromSync = 0
```

Also verify final size, checksum, and format or archive integrity. If these
fields are unavailable on that macOS version, report that remote upload could
not be proven rather than treating the local write as sufficient.

For other mutations, verify both sides:

- creation: destination exists and content or checksum matches;
- rename or move: new path exists and old path is absent;
- flattening a folder: remove the former wrapper only if empty;
- deletion: exact target is absent; never broaden deletion scope.

Completion criterion: local integrity passes, no existing destination was
replaced, and all three upload invariants are explicit.

### 5. Diagnose FileProvider State Privately

Allocate and print a private unpredictable directory in a separate short call
before starting any command that could wedge:

```python
terminal(
    command=r'''
set -euo pipefail
umask 077
PRIVATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-fileprovider.XXXXXX")"
printf 'PRIVATE_DIR=%s\nREPORT=%s/report.txt\n' "$PRIVATE_DIR" "$PRIVATE_DIR"
''',
    timeout=5,
)
```

Copy the printed exact paths into the bounded consistency check:

```python
terminal(
    command=r'''
set -u
ITEM="$HOME/Library/Mobile Documents/com~apple~CloudDocs/path/to/item"
PRIVATE_DIR="<PRIVATE_DIR_FROM_ALLOCATION_OUTPUT>"
REPORT="$PRIVATE_DIR/report.txt"
/usr/bin/fileproviderctl check -a "$ITEM" -o "$REPORT" -P -v
''',
    timeout=30,
)
```

The allocation output makes cleanup possible even when the check times out or
exits nonzero. If a report exists, inspect only that exact file with
`read_file(path='<REPORT_FROM_ALLOCATION_OUTPUT>')`. After success, failure, or
timeout, validate and remove only the allocated directory:

```python
terminal(
    command=r'''python3 - "<PRIVATE_DIR_FROM_ALLOCATION_OUTPUT>" <<'PY'
import shutil
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1]).resolve()
root = Path(tempfile.gettempdir()).resolve()
if path.parent != root or not path.name.startswith("hermes-fileprovider."):
    raise SystemExit(f"refusing unexpected cleanup path: {path}")
shutil.rmtree(path)
PY''',
    timeout=10,
)
```

A successful disk/FSSnapshot/FPSnapshot/reconciliation report with zero broken
invariants is evidence against database corruption. Dataless items are normal
and do not alone justify repair.

Completion criterion: the allocation path was captured before the check, and
the exact private directory was removed regardless of the check outcome.

### 6. Check Permissions and Runtime Identity

Access may depend on Full Disk Access, Files and Folders, or FileProvider
privacy approval for the executable responsible for the operation. Resolve
symlinks and inspect the actual runtime identity before asking for a permission
change. A versioned Python runtime can acquire a new executable path after an
upgrade, so an earlier grant may not apply to its replacement.

Grant only the minimum permission needed. Never ask the user to disable macOS
privacy controls globally.

## Pitfalls

- `EDEADLK` on an exact cloud-backed read can mean “not materialized yet.” It is
  not proof of a process deadlock.
- `brctl download` acknowledges a request asynchronously; poll the read rather
  than trusting its return code.
- Local existence does not prove off-device upload; require all three metadata
  invariants.
- A folder can have valid FileProvider metadata while directory enumeration is
  blocked.
- Broad scans can trigger many downloads, disclose unrelated filenames, and
  amplify a FileProvider stall.
- `nan`, infinity, zero, and negative timeout values are rejected. Each native
  command and child probe receives only the caller's remaining budget.
- Overwrite, repair, iCloud sign-out, and process restart are separate
  side effects that require explicit approval.

## Verification

Before reporting success, verify every applicable item:

- exact target path used throughout;
- each iCloud-opening `terminal` call finished before its declared timeout;
- placeholder content was read successfully after one exact download request;
- the expected parser or archive integrity check passed;
- no pre-existing destination was replaced;
- upload state explicitly showed uploaded, not uploading, and not excluded;
- move or rename verified both old and new paths;
- deletion verified only the exact target became absent;
- diagnostic report used a private unpredictable directory and was removed;
- no repair, sign-out, permission change, or restart occurred without approval.

If any check is missing, report the narrower verified result instead of saying
the iCloud operation fully succeeded.
