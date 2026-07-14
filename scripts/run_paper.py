"""Paper Trading Runtime 운영 진입점.

runtime.yaml 을 읽어 실제 Toss 시세 + Paper 체결 + (기본)Fake AI 로 런타임을 구동한다.
실주문 없음(PaperBrokerProvider). Ctrl+C 시 graceful shutdown(현재 사이클 종료 후 flush/report).

준비:
  ./.venv/bin/pip install PyYAML          # runtime.yaml 로드
  # AI 를 실제 OpenAI 로 쓰려면: pip install openai; export OPENAI_API_KEY=...
  # .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET
실행:
  ./.venv/bin/python scripts/run_paper.py config/runtime.yaml
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ai.engine import AIConfig, AIDecisionEngine  # noqa: E402
from src.ai.models import DEC_APPROVE  # noqa: E402
from src.ai.providers import FakeAIProvider  # noqa: E402
from src.backtest.execution import ExecutionModel  # noqa: E402
from src.config import Settings  # noqa: E402
from src.engines.indicator_engine import IndicatorEngine  # noqa: E402
from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402
from src.live.models import LoopConfig  # noqa: E402
from src.live.paper_loop import PaperTradingLoop  # noqa: E402
from src.order.manager import OrderManager  # noqa: E402
from src.order.models import InMemoryOrderStore, InMemoryPositionStore  # noqa: E402
from src.order.paper_broker import PaperBrokerProvider  # noqa: E402
from src.pipeline.pipeline import PipelineConfig, TradingPipeline  # noqa: E402
from src.providers.toss_client import TossClient  # noqa: E402
from src.providers.toss_market_data import TossMarketDataProvider  # noqa: E402
from src.risk.risk_manager import RiskConfig, RiskManager  # noqa: E402
from src.runtime.config import load_yaml  # noqa: E402
from src.runtime.isolation import (capture_identity, resolve_session_paths,  # noqa: E402
                                   startup_guard)
from src.runtime.metrics_store import SqliteMetricsStore  # noqa: E402
from src.runtime.paper_runtime import PaperRuntime  # noqa: E402
from src.storage.candle_store import CandleStore  # noqa: E402
from src.universe.screener import UniverseScreener  # noqa: E402


def _ai_provider(cfg):
    if cfg.ai_provider == "openai":
        from src.ai.providers import OpenAIProvider
        return OpenAIProvider(model_name=cfg.ai_model)
    # 기본 Fake — 데이터 수집/안정성 검증용. 결정론적 APPROVE.
    return FakeAIProvider(response={
        "decision": DEC_APPROVE, "confidence_score": 70, "entry_quality": "GOOD",
        "risk_level": "HIGH", "reason": ["fake-approve for paper data"],
        "warnings": ["leveraged ETF"]}, model_name=cfg.ai_model)


def main(config_path: str) -> None:
    cfg = load_yaml(config_path)
    settings = Settings(  # Toss 자격증명 (.env)
        toss_base=os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com"),
        toss_client_id=os.getenv("TOSS_CLIENT_ID", "").strip(),
        toss_client_secret=os.getenv("TOSS_CLIENT_SECRET", "").strip(),
        db_path=cfg.candle_db)
    if not settings.has_credentials:
        raise SystemExit("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 필요합니다 (.env)")

    # Session Isolation Guard — 반드시 stateful store 생성 '이전'에.
    # 과거 DB namespace 재사용/동시 실행을 시작 전에 fail-fast(실패 시 신규 쓰기 0).
    # session.id 미지정 config 는 guard 비활성(기존 동작 유지).
    guard = None
    session_manifest_path = None
    session_identity = None
    if cfg.session_id:
        session_identity = capture_identity(cfg)
        session_manifest_path, lock_path, db_paths = resolve_session_paths(cfg)
        guard = startup_guard(session_identity, manifest_path=session_manifest_path,
                              lock_path=lock_path, db_paths=db_paths)
        print(f"[run_paper] session guard OK — {session_identity.session_id}/{session_identity.run_id} "
              f"mode={session_identity.start_mode} market={session_identity.market} "
              f"commit={session_identity.producer_commit}")

    market = TossMarketDataProvider(settings)
    candle_store = CandleStore(cfg.candle_db)
    position_store = InMemoryPositionStore()
    order_manager = OrderManager(
        PaperBrokerProvider(ExecutionModel(cfg.slippage_bps, cfg.fee_bps)),
        InMemoryOrderStore(), position_store)
    ai = AIDecisionEngine(_ai_provider(cfg),
                          AIConfig(dry_run=cfg.ai_dry_run, max_calls_per_day=cfg.max_calls_per_session))
    risk_manager = RiskManager(RiskConfig(max_positions=cfg.max_positions))
    pipeline = TradingPipeline(IndicatorEngine(), UniverseScreener(), ai,
                               risk_manager, order_manager,
                               PipelineConfig(entry_notional_pct=cfg.entry_notional_pct))
    journal = SqliteJournalStore(cfg.journal_db)
    loop = PaperTradingLoop(
        market, candle_store, pipeline, position_store,
        LoopConfig(universe=tuple(cfg.symbols), account_value=cfg.account_value,
                   polling_interval_s=cfg.poll_interval_s,
                   fetch_spacing_s=cfg.fetch_spacing_s,
                   max_calls_per_session=cfg.max_calls_per_session,
                   initial_capital=cfg.initial_capital,
                   max_positions=cfg.max_positions,
                   exit_config=cfg.exit_config),
        journal=journal)

    metrics_store = SqliteMetricsStore(cfg.metrics_db)
    runtime = PaperRuntime(loop, cfg, metrics_store=metrics_store,
                           session_manifest_path=session_manifest_path,
                           session_identity=session_identity,
                           session_lock=(guard.lock if guard is not None else None))
    print(f"[run_paper] 운영 시작 — symbols={cfg.symbols}, Paper 체결만, Ctrl+C 로 graceful shutdown")
    report = runtime.run(install_signal_handler=True)
    print(f"[run_paper] 종료. report={report}")
    try:
        market.close()
        candle_store.close()
        journal.close()
        metrics_store.close()
    except Exception:
        pass
    # single-writer lock 해제는 PaperRuntime._finalize_session 이 담당한다
    # (manifest finalize 성공 후에만 해제 → 기록 실패 시 RUNNING+lock 을 남겨 crash-resume 신호 보존).


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config/runtime.yaml")
