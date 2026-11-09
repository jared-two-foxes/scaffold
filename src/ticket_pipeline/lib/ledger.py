"""Append-only, hash-chained feature ledger.

One ledger per feature *instance*, stored under
``.scaffold/features/<feature-id>.<instance-id>/``::

    ledger.jsonl        one JSON entry per line, each naming the hash of the previous
    artifacts/<sha256>  content-addressed blobs cited by entries

Each entry records the action and validated payload (for audit) and a *patch*: the
exact change the action made to the feature state. State is rebuilt by applying
patches in order, so tightening a rule later can never make an old ledger unreadable.
Every entry also carries the digest of the state it produces, verified on read.

The chain makes edits to history *detectable*. It cannot stop a process with write
access from appending a forged entry, and it cannot judge the truth of supplied evidence.

All functions here assume the caller holds ``state_store.state_lock()`` for writes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ticket_pipeline.lib import state_store

GENESIS = "0" * 64
LEDGER_FILE = "ledger.jsonl"
ARTIFACTS = "artifacts"
FEATURE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
INSTANCE_ID = re.compile(r"[a-f0-9]{32}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class LedgerError(ValueError):
    """Corrupt, tampered or inconsistent ledger. Never repaired automatically."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# --- patches -----------------------------------------------------------------
# Ops: {"op":"set","path":[..],"value":v} | {"op":"del","path":[..]}
#      {"op":"append","path":[..],"values":[..]}   (list grew; existing prefix unchanged)
# Paths address dict keys only. A list that changed other than by appending is replaced whole.


def diff(old: object, new: object, path: tuple[str, ...] = ()) -> list[dict]:
    if isinstance(old, dict) and isinstance(new, dict):
        ops: list[dict] = []
        for key in sorted(old.keys() - new.keys()):
            ops.append({"op": "del", "path": [*path, key]})
        for key in sorted(new):
            if key not in old:
                ops.append({"op": "set", "path": [*path, key], "value": copy.deepcopy(new[key])})
            else:
                ops.extend(diff(old[key], new[key], (*path, key)))
        return ops
    if not path:
        raise LedgerError("Feature state must remain an object")
    if isinstance(old, list) and isinstance(new, list):
        if len(new) > len(old) and canonical(new[: len(old)]) == canonical(old):
            return [{"op": "append", "path": list(path), "values": copy.deepcopy(new[len(old) :])}]
        if canonical(new) == canonical(old):
            return []
    elif type(old) is type(new) and old == new:
        return []
    return [{"op": "set", "path": list(path), "value": copy.deepcopy(new)}]


def apply_patch(state: dict, ops: list[dict]) -> dict:
    result = copy.deepcopy(state)
    for op in ops:
        kind, path = op.get("op"), op.get("path")
        if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
            raise LedgerError("Invalid patch path")
        parent = result
        for key in path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                raise LedgerError("Patch path does not exist")
            parent = parent[key]
        if not isinstance(parent, dict):
            raise LedgerError("Patch path does not address an object")
        leaf = path[-1]
        if kind == "set":
            parent[leaf] = copy.deepcopy(op["value"])
        elif kind == "del":
            if leaf not in parent:
                raise LedgerError("Patch deletes a missing key")
            del parent[leaf]
        elif kind == "append":
            if not isinstance(parent.get(leaf), list):
                raise LedgerError("Patch appends to a non-list")
            parent[leaf].extend(copy.deepcopy(op["values"]))
        else:
            raise LedgerError("Unknown patch op")
    return result


# --- filesystem layout ---------------------------------------------------------


def features_dir() -> Path:
    return state_store.SCAFFOLD_DIR / "features"


def require_feature_id(feature_id: object) -> str:
    if not isinstance(feature_id, str) or not FEATURE_ID.fullmatch(feature_id):
        raise ValueError("Feature id may contain only letters, digits, '_' and '-'")
    return feature_id


def instance_dir(feature_id: str, instance_id: str) -> Path:
    require_feature_id(feature_id)
    if not INSTANCE_ID.fullmatch(instance_id):
        raise ValueError("Invalid feature instance identity")
    return features_dir() / f"{feature_id}.{instance_id}"


def _safe_dir(path: Path, create: bool = False) -> None:
    for candidate in (state_store.SCAFFOLD_DIR, features_dir(), path):
        if candidate.is_symlink():
            raise ValueError("Refusing symbolic link in Scaffold ledger paths")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_dir():
        raise ValueError("Scaffold ledger path must be a directory")


def _features_root() -> Path | None:
    root = features_dir()
    if root.is_symlink():
        raise ValueError("Refusing symbolic link in Scaffold ledger paths")
    if root.exists() and not root.is_dir():
        raise ValueError("Scaffold features path must be a directory")
    return root if root.is_dir() else None


def instance_directories(feature_id: str) -> list[Path]:
    """Every instance directory ever created for this feature id, oldest name first."""
    require_feature_id(feature_id)
    root = _features_root()
    if root is None:
        return []
    found = []
    for child in sorted(root.iterdir()):
        name, _, instance = child.name.rpartition(".")
        if name == feature_id and INSTANCE_ID.fullmatch(instance) and child.is_dir():
            found.append(child)
    return found


# --- reading -------------------------------------------------------------------


def _parse_lines(path: Path) -> tuple[list[dict], int]:
    """Return (entries, byte length of the committed prefix). A final line without a
    newline is a torn write: it was never committed and is ignored."""
    data = path.read_bytes()
    committed = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
    entries = []
    for raw in committed.split(b"\n")[:-1]:
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LedgerError(f"Ledger line is not valid JSON: {error}") from error
        if not isinstance(entry, dict):
            raise LedgerError("Ledger entry must be an object")
        entries.append(entry)
    return entries, len(committed)


ENTRY_FIELDS = {"seq", "prev", "type", "at", "body", "patch", "after", "hash"}


def verify(entries: list[dict]) -> dict:
    """Authenticate the chain and fold the patches into the feature state."""
    state: dict = {}
    previous = GENESIS
    for index, entry in enumerate(entries):
        if set(entry) != ENTRY_FIELDS:
            raise LedgerError(f"Entry {index} has unexpected fields")
        if entry["seq"] != index or type(entry["seq"]) is not int:
            raise LedgerError(f"Entry {index} is out of sequence")
        if entry["prev"] != previous:
            raise LedgerError(f"Entry {index} does not follow the previous entry")
        claimed = entry["hash"]
        if claimed != digest({k: v for k, v in entry.items() if k != "hash"}):
            raise LedgerError(f"Entry {index} was modified")
        state = apply_patch(state, entry["patch"])
        if entry["after"] != digest(state):
            raise LedgerError(f"Entry {index} does not produce its recorded state")
        previous = claimed
    return state


class Ledger:
    """A verified view of one feature instance's ledger."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / LEDGER_FILE
        self.entries: list[dict] = []
        self.state: dict = {}
        self._committed = 0
        self.reload()

    def reload(self) -> None:
        _safe_dir(self.directory)
        if self.path.is_symlink():
            raise ValueError("Refusing symbolic link in Scaffold ledger paths")
        if self.path.exists():
            self.entries, self._committed = _parse_lines(self.path)
            self.state = verify(self.entries)
        else:
            self.entries, self._committed, self.state = [], 0, {}

    @property
    def head(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def find(self, entry_hash: str) -> dict | None:
        return next((e for e in self.entries if e["hash"] == entry_hash), None)

    # --- writing (caller holds the state lock) ---------------------------------

    def append(
        self, action: str, body: object, new_state: dict, expected_seq: int | None = None
    ) -> dict:
        """Append one entry that turns the current state into ``new_state``."""
        if expected_seq is not None and expected_seq != len(self.entries):
            raise ValueError("Stale ledger sequence")
        patch = diff(self.state, new_state)
        entry = {
            "seq": len(self.entries),
            "prev": self.head,
            "type": action,
            "at": datetime.now(UTC).isoformat(),
            "body": copy.deepcopy(body),
            "patch": patch,
            "after": digest(new_state),
        }
        entry["hash"] = digest(entry)
        line = canonical(entry) + b"\n"
        _safe_dir(self.directory, create=True)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            # Drop a torn, never-committed tail so it cannot corrupt this entry.
            os.ftruncate(descriptor, self._committed)
            os.lseek(descriptor, self._committed, os.SEEK_SET)
            try:
                written = 0
                while written < len(line):
                    written += os.write(descriptor, line[written:])
                os.fsync(descriptor)
                # Durably publish the new directory entry inside the rollback-protected block.
                self._fsync_directory()
            except BaseException:
                # A failed append must leave the ledger exactly as it was.
                try:
                    os.ftruncate(descriptor, self._committed)
                    os.fsync(descriptor)
                except OSError:  # pragma: no cover - best effort; a torn tail is ignored on read
                    pass
                raise
        finally:
            os.close(descriptor)
        self._committed += len(line)
        self.entries.append(entry)
        self.state = copy.deepcopy(new_state)
        return entry

    def _fsync_directory(self) -> None:  # pragma: no cover - platform-specific durability
        if os.name == "nt":
            return
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    # --- artifacts ---------------------------------------------------------------

    def put_artifact(self, data: bytes) -> str:
        """Store a blob before the entry that cites it. An orphaned blob is harmless."""
        name = hashlib.sha256(data).hexdigest()
        target = self.directory / ARTIFACTS / name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != name:
                raise LedgerError("Stored artifact does not match its digest")
            return name
        _safe_dir(self.directory / ARTIFACTS, create=True)
        state_store.atomic_write_bytes(target, data)
        return name

    def get_artifact(self, name: str) -> bytes:
        if not DIGEST.fullmatch(name or ""):
            raise ValueError("Invalid artifact digest")
        target = self.directory / ARTIFACTS / name
        if target.is_symlink() or not target.is_file():
            raise LedgerError("Artifact is missing")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != name:
            raise LedgerError("Artifact does not match its digest")
        return data


def create(feature_id: str, instance_id: str) -> Ledger:
    """Open the (empty) ledger for a new instance. Refuses to reuse a directory."""
    directory = instance_dir(feature_id, instance_id)
    _safe_dir(directory)
    if directory.exists():
        raise ValueError("Feature instance already exists")
    return Ledger(directory)


def open_current(feature_id: str) -> Ledger | None:
    """The one open (not closed) instance of this feature, or None."""
    open_instances = []
    for directory in instance_directories(feature_id):
        ledger = Ledger(directory)
        if ledger.entries and "closed" not in ledger.state:
            open_instances.append(ledger)
    if len(open_instances) > 1:
        raise LedgerError("More than one open instance exists for this feature")
    return open_instances[0] if open_instances else None


def all_open() -> list[Ledger]:
    """Every open feature instance, ordered by feature id."""
    root = _features_root()
    if root is None:
        return []
    ids = set()
    for child in root.iterdir():
        name, _, instance = child.name.rpartition(".")
        if FEATURE_ID.fullmatch(name) and INSTANCE_ID.fullmatch(instance) and child.is_dir():
            ids.add(name)
    found = []
    for feature_id in sorted(ids):
        current = open_current(feature_id)
        if current is not None:
            found.append(current)
    return found
