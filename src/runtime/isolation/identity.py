"""SessionIdentity + provenance 캡처.

세션(run)의 정체성과 재현 근거(provenance)를 한 곳에 고정한다. 매매 로직과 무관하며,
startup 게이트/manifest 가 이 값으로 namespace 재사용·resume 정당성을 판정한다.

- config_hash: 거래 행동을 정의하는 config 필드의 canonical sha256(경로/로그 등 운영 필드 제외).
- producer_commit: 코드 출처(git HEAD). 조회 실패는 None(가드가 막지 않음).
- market/currency: 세션이 선언(미지정 시 심볼로 추론).
결정론: 같은 config → 같은 config_hash. now()/난수 미사용.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

# ---- 상수 ----
SESSION_SCHEMA_VERSION = "1.0"      # manifest/identity 스키마 버전
ID_VERSION = "1"                    # identity 산출 규약 버전
BROKER_PAPER = "PAPER"             # 실주문 금지 — 항상 Paper

START_FRESH = "FRESH"
START_RESUME = "RESUME"
START_RESTART = "RESTART"

# RESUME 시 기존 manifest 와 반드시 일치해야 하는 provenance 필드(계약서 §3-5).
_MATCH_FIELDS = (
    "session_id", "run_id", "config_hash", "producer_commit", "schema_version",
    "market", "currency", "initial_equity", "broker_mode", "ai_provider",
)

# config_hash 에 포함하는 '거래 행동' 필드(운영/경로 필드는 제외).
_HASH_FIELDS = (
    "symbols", "initial_capital", "account_value", "max_positions",
    "entry_notional_pct", "ai_provider", "ai_model", "ai_dry_run",
    "slippage_bps", "fee_bps", "max_calls_per_session",
)


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    run_id: str
    parent_run_id: str | None
    start_mode: str                 # FRESH | RESUME | RESTART
    config_hash: str
    producer_commit: str | None
    schema_version: str
    market: str                     # US | KR
    currency: str                   # USD | KRW
    initial_equity: float
    broker_mode: str                # PAPER
    ai_provider: str                # fake | openai
    id_version: str = ID_VERSION

    def mismatched_fields(self, other: "SessionIdentity") -> list[str]:
        """RESUME 정당성 판정용 — 불일치하는 provenance 필드 목록(빈 리스트=일치)."""
        return [f for f in _MATCH_FIELDS if getattr(self, f) != getattr(other, f)]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionIdentity":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def canonical_config_hash(cfg) -> str:
    """거래 행동 config 의 canonical sha256(hex). 운영/경로 필드 변경엔 불변."""
    payload = {}
    for name in _HASH_FIELDS:
        val = getattr(cfg, name, None)
        payload[name] = list(val) if isinstance(val, (list, tuple)) else val
    payload["exit_config"] = _exit_config_dict(getattr(cfg, "exit_config", None))
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _exit_config_dict(ex) -> dict | None:
    if ex is None:
        return None
    try:
        return asdict(ex)
    except TypeError:
        return {k: getattr(ex, k) for k in vars(ex)} if hasattr(ex, "__dict__") else None


def producer_commit(*, cwd: str | None = None, run=None) -> str | None:
    """현재 코드의 git HEAD SHA. 조회 실패 시 None(가드를 막지 않음)."""
    if run is None:
        import subprocess
        run = subprocess.run
    try:
        r = run(["git", "rev-parse", "HEAD"], cwd=cwd,
                capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = (getattr(r, "stdout", "") or "").strip()
    return out or None


def infer_market_currency(symbols) -> tuple[str, str]:
    """심볼로 시장/통화 추론. 6자리 숫자 티커(KRX) → KR/KRW, 그 외 → US/USD."""
    for s in symbols or []:
        t = str(s).strip()
        if len(t) == 6 and t.isdigit():
            return ("KR", "KRW")
    return ("US", "USD")


def capture_identity(cfg, *, commit: str | None = None, commit_fn=producer_commit) -> SessionIdentity:
    """RuntimeConfig → SessionIdentity. commit 미주입 시 git 에서 조회(실패=None)."""
    market, currency = cfg.market, cfg.currency
    if not market or not currency:
        im, ic = infer_market_currency(cfg.symbols)
        market = market or im
        currency = currency or ic
    pc = commit if commit is not None else commit_fn()
    return SessionIdentity(
        session_id=cfg.session_id,
        run_id=cfg.run_id or cfg.session_id,
        parent_run_id=cfg.parent_run_id,
        start_mode=(cfg.start_mode or START_FRESH).upper(),
        config_hash=canonical_config_hash(cfg),
        producer_commit=pc,
        schema_version=SESSION_SCHEMA_VERSION,
        market=market, currency=currency,
        initial_equity=float(cfg.initial_capital),
        broker_mode=BROKER_PAPER,
        ai_provider=cfg.ai_provider,
    )
