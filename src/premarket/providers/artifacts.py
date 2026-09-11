"""Content-addressed, parse-before storage for provider response bodies."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_component(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


class RawEvidenceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self._content_seen: set[str] = set()
        self._receipts_seen: set[str] = set()

    def emit(
        self,
        *,
        provider: str,
        resource: str,
        requested_at: str,
        received_at: str,
        request_url: str,
        status_code: int,
        content_type: str,
        body: bytes,
        retry_delay_seconds: float | None = None,
    ) -> dict[str, Any]:
        body_sha = hashlib.sha256(body).hexdigest()
        provider_part = _safe_component(provider)
        resource_part = _safe_component(resource)
        rel_path = Path("content") / provider_part / resource_part / body_sha[:2] / f"{body_sha}.body.gz"
        self._write_once(self.root / rel_path, gzip.compress(body, mtime=0))
        self._content_seen.add(rel_path.as_posix())
        receipt = {
            "schema": "premarket-provider-receipt/v1",
            "provider": provider,
            "resource": resource,
            "requested_at": requested_at,
            "received_at": received_at,
            "request_url": request_url,
            "status_code": int(status_code),
            "content_type": content_type.split(";", 1)[0].strip().lower(),
            "body_sha256": body_sha,
            "body_bytes": len(body),
            "raw_rel_path": rel_path.as_posix(),
        }
        if retry_delay_seconds is not None:
            receipt["retry_delay_seconds"] = float(retry_delay_seconds)
        receipt_id = hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
        receipt["receipt_id"] = receipt_id
        receipt_rel = Path("receipts") / receipt_id[:2] / f"{receipt_id}.json"
        self._write_once(self.root / receipt_rel, _canonical_bytes(receipt) + b"\n")
        self._receipts_seen.add(receipt_rel.as_posix())
        return receipt

    def _write_once(self, path: Path, content: bytes) -> None:
        if path.exists():
            if path.read_bytes() != content:
                raise FileExistsError(f"immutable artifact differs: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        partial = path.with_name(f"{path.name}.{os.getpid()}.{self._sequence}.partial")
        try:
            with open(partial, "xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, path)
            self._fsync_directory(path.parent)
        finally:
            if partial.exists():
                partial.unlink()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def read_body(self, rel_path: str) -> bytes:
        with gzip.open(self.root / rel_path, "rb") as stream:
            return stream.read()

    def manifest(self) -> dict[str, Any]:
        content = self._manifest_entries(self._content_seen, ".body.gz", "content_sha256")
        receipts = self._manifest_entries(self._receipts_seen, ".json", "receipt_id")
        payload = {"content": content, "receipts": receipts}
        return {
            "schema": "premarket-provider-raw-manifest/v1",
            "root_basename": self.root.name,
            "content_file_count": len(content),
            "receipt_file_count": len(receipts),
            "raw_content_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
            "content_files": content,
            "receipt_files": receipts,
        }

    @staticmethod
    def _manifest_entries(paths: set[str], suffix: str, key: str) -> list[dict[str, str]]:
        return [
            {"rel_path": rel_path, key: Path(rel_path).name.removesuffix(suffix)}
            for rel_path in sorted(paths)
        ]
