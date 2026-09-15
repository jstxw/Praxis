"""Content-addressed workspace snapshots — the file half of a checkpoint.

A trajectory checkpoint at step ``k`` is ``(workspace snapshot, agent
state, harness id, memory version)``. Workspace snapshots live OUTSIDE
the workspace (under the state dir), so the agent never sees a ``.git``
or snapshot directory that could change its behavior.

Blobs are stored once per content hash; a snapshot is a manifest
``{relative_path: blob_sha}``, itself stored as a blob. Its id is the
manifest's hash, so identical workspaces share one id — which is how a
fork's "identical state up to d−1" is checkable, not asserted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".mypy_cache", ".ruff_cache"})
MAX_FILE_BYTES = 5 * 1024 * 1024


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)

    def _blob_path(self, sha: str) -> Path:
        return self.root / "blobs" / sha[:2] / sha

    def put_bytes(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._blob_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            tmp.replace(path)
        return sha

    def get_bytes(self, sha: str) -> bytes:
        return self._blob_path(sha).read_bytes()

    def manifest(self, workspace: Path) -> dict[str, str]:
        """Hash every file in the workspace (skipping caches)."""
        workspace = Path(workspace)
        entries: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(workspace):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                rel = path.relative_to(workspace).as_posix()
                entries[rel] = self.put_bytes(path.read_bytes())
        return entries

    def snapshot(self, workspace: Path) -> str:
        manifest = self.manifest(workspace)
        return self.put_bytes(json.dumps(manifest, sort_keys=True).encode())

    def load_manifest(self, snapshot_id: str) -> dict[str, str]:
        return json.loads(self.get_bytes(snapshot_id))

    def restore(self, snapshot_id: str, dest: Path) -> None:
        """Materialize a snapshot into ``dest`` exactly (extra files removed)."""
        dest = Path(dest)
        manifest = self.load_manifest(snapshot_id)
        if dest.exists():
            for child in dest.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        dest.mkdir(parents=True, exist_ok=True)
        for rel, sha in manifest.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.get_bytes(sha))

    def diff(self, before_id: str, after_id: str) -> dict[str, list[str]]:
        before = self.load_manifest(before_id)
        after = self.load_manifest(after_id)
        return {
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "modified": sorted(p for p in set(before) & set(after) if before[p] != after[p]),
        }

    def read_file(self, snapshot_id: str, rel: str) -> bytes | None:
        sha = self.load_manifest(snapshot_id).get(rel)
        return self.get_bytes(sha) if sha else None
