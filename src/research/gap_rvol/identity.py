"""Gap+RVOL snapshot identity uses the shared, frozen four-hash verifier."""
from __future__ import annotations

from collections.abc import Mapping

from src.research.orb.identity import (
    IdentityResult,
    require_identity,
    verify_snapshot_identity,
)


def verify_inputs(
    *,
    contract: Mapping[str, object],
    snapshot_path: str,
    raw_root: str,
    manifest_path: str,
) -> IdentityResult:
    """Verify file/content/manifest/raw hashes; any mismatch is NOT_RUN-grade."""
    result = verify_snapshot_identity(
        contract=contract,
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        manifest_path=manifest_path,
    )
    require_identity(result)
    return result
