# 구현 현황 (Implementation Status)

로드맵 단계별 구현 요약과 각 계층 사용법. 프로젝트 개요는 [../README.md](../README.md),
설계 문서는 [design/](design/) 참고.

> **상태**: 데이터 수집 → 지표 → 종목선별 → AI 필터 → 리스크 → 정합성 → 백테스트 →
> 주문(Paper) → 파이프라인 → 실시간 루프 → 저널 → Replay → Review → Paper Runtime 까지
> 구현 완료. **실주문 없음(Paper only).** US/KR 시장 인식. 180 tests passing.

## 셋업
```bash
cd ai_trading_system
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Toss API 키 설정
`.env` 파일에 발급받은 키를 채웁니다 (`.env` 는 git 에 커밋되지 않음):
```
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
```

## 스모크 테스트 (읽기 전용, 주문 없음)
```bash
python spikes/toss_smoke_test.py
```
확인 항목:
1. OAuth2 토큰 발급
2. 계좌 조회 (accountSeq)
3. 보유 종목 조회
4. **미국주식(SOXL) 1분봉 데이터** — 설계의 핵심 미확인 항목

## 데이터 수집 (로드맵 3 — 완료)
Toss 캔들을 canonical 형식으로 수집해 SQLite(`data/market.sqlite`)에 저장:
```bash
./.venv/bin/python -m src.ingest.backfill SOXL 1m --pages 3
./.venv/bin/python -m src.ingest.backfill SOXL 1d --pages 5
```

## 지표 계산 (로드맵 4 — 완료)
저장된 canonical 캔들 → (선택) 5분봉 리샘플 → 지표 계산. 순수 모듈(Provider 미접근),
backtest·live 공용. EMA7/EMA20/RSI14/VWAP(세션 앵커)/Bollinger(20,2)/StochRSI(14,14,3,3).
```python
from src.storage.candle_store import CandleStore
from src.config import load_settings
from src.engines.indicator_engine import IndicatorEngine, candles_to_frame, resample

store = CandleStore(load_settings().db_path)
df = candles_to_frame(store.query("SOXL", "1m"))
df5 = resample(df, "5min")          # 진행 중 버킷 자동 제외
result = IndicatorEngine().compute(df5)   # ts_utc 인덱스로 정렬된 지표
```

## 종목 선별 (로드맵 5 — 완료)
사용자 지정 유니버스에서 Rule 기반 점수화로 매매 후보를 압축. AI 미개입, candidate list 만 생성.
점수 요소: trend/momentum/volume/vwap_position/volatility. 레버리지·유동성·변동성·마감임박·연장시간 리스크 반영.
```python
from src.universe.screener import UniverseScreener, SymbolData
# ind_by_symbol: {symbol: IndicatorEngine.compute(...) 결과}
inputs = [SymbolData.create(sym, ind) for sym, ind in ind_by_symbol.items()]
candidates = UniverseScreener().screen(inputs)   # score 내림차순 랭킹된 후보
# 각 후보: symbol, score, rank, candidate_type, reasons[], risk_flags[]  (Rule/AI 로 전달)
```

## 리스크 관리 (로드맵 6 — 완료)
AI/Rule/Screener 신호와 무관하게 최종 주문 가부를 결정. 실질 노출(effective=notional×leverage) 기준.
Broker 미접근 — PortfolioSnapshot/Position/OrderIntent 만 평가. 결정론적(현재 시각 미의존).
```python
from src.risk.risk_manager import RiskManager
from src.risk.models import OrderIntent, PortfolioSnapshot, Position, NEW_ENTRY, EXIT, BUY, SELL

decision = RiskManager().evaluate(
    OrderIntent("SOXL", NEW_ENTRY, BUY, qty=10, price=194, leverage_factor=3),
    PortfolioSnapshot(account_value=100_000))
# decision.to_dict(): {allowed, reason_codes[], adjusted_quantity, risk_flags[]}
```
핵심: kill switch·손실한도 하에서도 **청산/손절/EOD flatten 은 항상 허용**. 레버리지 ETF 는 flatten 창에서 신규 진입 차단.

## 정합성 대조 (로드맵 7 — 완료)
내부 포지션/주문 vs 브로커 실계좌를 대조해 불일치 감지. BrokerProvider/PositionStore/OrderStore
인터페이스(Protocol)에만 의존 — Toss/KIS 구현체 비의존. 결정론적.
```python
from src.reconcile.service import ReconciliationService
report = ReconciliationService(broker, position_store, order_store).reconcile()
# report.to_dict(): {status, severity(INFO/WARNING/CRITICAL), mismatches[], recommended_action, block_new_entries}
```
CRITICAL(수량/포지션 불일치) → `block_new_entries=True` 를 RiskManager 의 신규진입 차단으로 반영.
청산/리스크 축소 주문은 그대로 허용(RiskManager 가 보장).

## 백테스트 (로드맵 8 — 완료)
저장 캔들로 long-only 전략을 검증. live 와 동일한 IndicatorEngine/Screener/RiskManager 재사용.
look-ahead 방지(t 종가 판단 → t+1 시가 체결), slippage/fee(ExecutionModel), 레버리지 EOD flatten.
```python
from src.backtest.engine import BacktestEngine
from src.backtest.models import BacktestConfig
report = BacktestEngine(BacktestConfig()).run(candles, "SOXL", leverage_factor=3)
# report.to_dict(): {symbol, period, summary{total_return_pct,win_rate,profit_factor,
#   max_drawdown_pct,total_trades,sample_status,...}, trades[]}
in_rep, out_rep = BacktestEngine().run_in_out(candles, "SOXL", 3, ratio=0.7)  # in/out 분리
```
sample_status 가 INSUFFICIENT_SAMPLE(거래<min_trades)면 검증 불충분. walk-forward 는 인터페이스만 예약(TODO).

## AI Entry Filter (로드맵 9 — 완료)
Screener 후보에 대해 OpenAI 를 호출해 entry confidence/reason 반환. **AI 는 진입 필터일 뿐** —
청산/손절/EOD flatten 에 절대 관여하지 않고, 실패(timeout/rate limit/invalid JSON)는 WAIT 폴백.
```python
from src.ai.engine import AIDecisionEngine, AIConfig, is_entry_permitted
from src.ai.providers import OpenAIProvider   # 테스트는 FakeAIProvider
engine = AIDecisionEngine(OpenAIProvider("gpt-5.5-mini"), AIConfig(temperature=0.0))
d = engine.decide(candidate, indicator_snapshot, portfolio, risk_decision, market_ctx)
tradeable = is_entry_permitted(d, risk_decision)   # AI APPROVE 이고 Risk allow 여야 True
```
temperature=0·JSON schema 강제·input_hash 캐시·dry_run·max_calls_per_day(비용 보호).
실제 호출은 `scripts/ai_entry_filter_manual.py` (수동). 테스트는 FakeAIProvider 로 실호출 없음.

## 주문 실행 (로드맵 10 — Paper 우선)
OrderManager 는 BrokerProvider 인터페이스에만 의존. **RiskDecision.allowed=True 인 주문만** 전달.
흐름: OrderIntent → (RiskManager) → OrderRequest → BrokerProvider → BrokerOrderResult → OrderRecord.
```python
from src.order.manager import OrderManager
from src.order.models import InMemoryOrderStore, InMemoryPositionStore
from src.order.paper_broker import PaperBrokerProvider
om = OrderManager(PaperBrokerProvider(), InMemoryOrderStore(), InMemoryPositionStore())
record = om.submit(intent, risk_decision, client_order_id="SOXL-20260706-0001", ref_price=194.0)
```
- **PaperBrokerProvider**: 모의 체결(토스 샌드박스 부재 대체), slippage/fee 설정. **실주문 없음.**
- **TossBrokerProvider**: skeleton + TODO만 (Paper 통과 + 승인 후 구현).
- client_order_id 멱등, kill switch 하에서도 RiskManager 가 허용한 EXIT/EOD_FLATTEN 실행.

## 오케스트레이션 (파이프라인 — 완료)
TradingPipeline 이 전 컴포넌트를 주입받아 5분봉 마감 1사이클을 실행: MarketData→Indicator→
Screener→AI→Risk→Order(Paper). 연결자일 뿐 — 판단은 각 컴포넌트가 한다.
```python
from src.pipeline.pipeline import TradingPipeline
from src.pipeline.models import SystemState
pipe = TradingPipeline(indicator_engine, screener, ai_engine, risk_manager, order_manager)
result = pipe.run_once(cycle_id="...", universe=["SOXL"], candles_by_symbol={...},
                       portfolio=snapshot, system_state=SystemState(), market=market_ctx,
                       exit_intents=[...])   # EXIT/EOD_FLATTEN 은 AI 우회, Risk→Order
```
AI WAIT/REJECT 또는 Risk block 이면 미주문(사유 기록). 주문 시 OrderRecord+AI+Risk 연결.
결정론적. **실주문 없음 — PaperBrokerProvider 만.**

## Paper Trading 실시간 루프 (완료)
Toss 시세를 주기 폴링 → 1분봉 저장 → 5분봉 리샘플 → **새로 닫힌 5분봉에만** run_once() 실행.
**PaperBrokerProvider 만 사용, 실주문 금지.** 세션 구분, 레버리지 EOD flatten, stop/pause/resume, kill switch.
```python
from src.live.paper_loop import PaperTradingLoop
from src.live.models import LoopConfig
loop = PaperTradingLoop(toss_market_data, candle_store, pipeline, position_store,
                        LoopConfig(universe=("SOXL",)))
loop.tick()   # 1회 폴링 사이클 (run() = 주기 반복)
```
- 중복 실행 방지(동일 5분봉 skip), 데이터/파이프라인 오류 시 SKIP/ERROR 후 **루프 생존**.
- AI 는 FakeAIProvider/dry_run 로 시작, 실제 OpenAI 는 옵션. rate limit(세션당 호출 상한).
- 결과는 result_log 저장. **여전히 실주문 0.**

## Trading Journal (감사 로그 — 완료)
사이클별 전 의사결정 과정을 저장해 "왜 이 시점에 매수했나/안 했나"를 추적. **판단은 안 하고 기록만.**
```python
from src.journal.sqlite_store import SqliteJournalStore
from src.journal.models import JournalCycle
journal = SqliteJournalStore("data/journal.sqlite")     # 인터페이스 동일, PG/Timescale 교체 가능
loop = PaperTradingLoop(..., journal=journal)           # 루프가 매 사이클 기록
journal.get_cycle(cycle_id)                             # 사이클 전체
journal.get_by_symbol("SOXL", start, end)              # 종목별
journal.get_by_time_range(start, end)                  # 기간별
```
저장 대상: universe·candidate·indicator snapshot·screener·AI decision(raw/parsed/input_hash)·
risk(reason_codes/risk_flags)·order_record·no-order 사유·error. **주문 없는 사이클도 저장.**

## AI Replay System (완료)
Journal 의 cycle 하나를 **읽기 전용**으로 완전 재생 → GPT 입력 가능한 ReplayContext.
**과거 재생만. 분석/판단/주문 없음. 파이프라인과 분리.**
```python
from src.replay.system import ReplaySystem
ctx = ReplaySystem(journal, candle_store).replay(cycle_id)   # read-only
gpt_input = ctx.to_prompt_dict()    # GPT Provider 가 그대로 입력받는 구조 (JSON)
```
복원 대상: market snapshot·candle·indicator·candidate·AI decision·risk·order·position·portfolio·journal.
원본 상태 불변(read-only), look-ahead 방지(cycle 시점 이하 캔들만). 이후 AI Review Engine 이 이 구조를 사용.

## AI Review Engine (완료)
ReplayContext 를 GPT 로 사후 회고. **과거 평가만 — 실시간 주문/판단에 관여 안 함.**
전략 개선의 근거 데이터 생성.
```python
from src.review.engine import AIReviewEngine, ReviewConfig
from src.ai.providers import OpenAIProvider     # 테스트는 FakeReviewProvider
report = AIReviewEngine(OpenAIProvider("gpt-5.5-mini")).review(replay_context)
```
분석 항목: decision_quality·missed_signals·risk_assessment·alternative_action·rule_improvement·
confidence_reassessment. temperature=0·JSON schema·input_hash. 실패(timeout/rate limit/invalid JSON)
→ REVIEW_FAILED. ReplayContext read-only. ai_review_log 저장 구조 제공.

## Paper Trading Operation Mode v1 (완료)
PaperTradingLoop 을 감싼 운영 런타임. **실주문 없음, 안정성·데이터 수집 우선.**
```bash
./.venv/bin/pip install PyYAML
./.venv/bin/python scripts/run_paper.py config/runtime.yaml   # Ctrl+C = graceful shutdown
```
- 상태관리 run/stop/pause/resume, 운영 config(runtime.yaml)
- 로그 rotation(runtime/error/trade/review.log), 매 Cycle Health Check + status.json 대시보드
- Recovery(API/Network/AI/SQLite Lock 자동 복구 — 생존 우선), Graceful Shutdown(flush+report)
- 종료 리포트 자동 생성(reports/paper_report_YYYYMMDD.md): Cycle/AI/Order/Exception/지연/자원

## AI Learning Dataset Builder (완료)
Journal(+Candle)을 읽는 **read-only batch processor**. 실시간 매매에 관여하지 않음.
한 row = 한 cycle×symbol 진입 관찰 지점의 **Feature(진입시점) + Label(사후 outcome) + Meta(재현성 버전)**.
```bash
./.venv/bin/pip install pyarrow        # Parquet export 용
./.venv/bin/python scripts/build_dataset.py config/runtime_mixed.yaml \
    --out-dir datasets --format both --primary-window 30 --windows 5,15,30,60
```
- **Feature**: market/session(종목별), 지표 스냅샷(close/ema/rsi/vwap/bb/stochrsi),
  candidate(score/type/rank/risk_flags), AI(decision/confidence/quality/risk), risk_allowed, 포트폴리오(account_value/open_positions/kill_switch)
- **Label**(Candle 로 계산, look-ahead 방지): 창별 `forward_return`/`mfe`/`mae`/`n_bars`(기본 5·15·30·60분),
  대표 창 기준 `label_class`(WIN/LOSS/NEUTRAL, 임계 ±0.2%), `holding_minutes`, `complete`,
  진입 체결 passthrough(`trade_taken`/`entry_fill_price`/`entry_qty`/`entry_side`)
- **Meta**: `feature_version`/`dataset_version`/`prompt_version`/`strategy_version`/`model_name` (재현성 보장)
- **Exporter**: `JsonlExporter`(중첩 `{feature,label,meta}` — GPT/Claude review·few-shot·회귀평가·XAI),
  `ParquetExporter`(평탄화 컬럼 — Pandas/DuckDB/Polars·feature engineering·ML). CSV 는 후순위.
- 구조: `Dataset ─ DatasetRecord(FeatureRecord/LabelRecord/MetaRecord) ─ DatasetExporter`.
  `DatasetBuilder`만 Journal/Candle 을 앎(Replay/Review/Runtime 은 Dataset 을 모름). 결정론적(built_at 외부주입).
- **MVP 범위 주의**: 라벨은 **관찰 기반 forward return 중심**. 진입↔청산 매칭에 의한
  `realized_pnl`/`exit_reason` 은 후속(현재 `None`, entry 체결 정보만 passthrough).

## Review Batch Runner (AI 자기개선 #1 — 완료)
기간 내 cycle 을 **Replay→Review 순회 + 영속화 + 경량 요약**하는 배치. read-only, 매매 무관.
기본 provider 는 FakeReviewProvider(비용/네트워크 0, 결정론). 실제 회고는 `--provider openai`.
```bash
./.venv/bin/python scripts/run_reviews.py config/runtime_mixed.yaml \
    --provider fake --export-jsonl reports/reviews_mixed.jsonl
```
- **ReviewStore (SQLite, SSoT)**: Aggregator/Dashboard/System Review 가 공용으로 읽는 Primary Storage.
  resume 키 = `(cycle_id + prompt_version + model_name + review_version)` → 중복 회고 방지/덮어쓰기.
  `review_version` 도입(같은 cycle 을 다른 모델·프롬프트로 재회고해도 비교 가능),
  `review_source`(AI/MANUAL/BATCH/SYSTEM, 기본 BATCH). 인덱스: cycle_id·symbol·created_at·
  model_name·prompt_version·review_version·decision_quality·review_source.
- **ReviewBatchRunner**: `journal.get_by_time_range` 순회 → `ReplaySystem.replay` → `AIReviewEngine.review`
  → `ReviewStore.save`. 실패 회고도 REVIEW_FAILED 로 영속(resume 이 재호출 차단). `spacing_s`로 rate limit 완화.
  결정론(created_at/elapsed 외부 주입).
- **경량 요약(RunSummary)**: ok/failed/skipped(resumed·no_outcomes), decision_quality 분포,
  confidence_reassessment 평균, rule_improvement Top-N, failed cycle 목록, 처리시간. (본격 Aggregator 는 #2)
- **ReviewJsonlExporter**: SSoT(SQLite)에서 조회한 회고를 JSONL 로 export(GPT/Claude 분석·회귀·LLM batch).
- 구조: `ReviewStore(+ReviewRecord) ─ ReviewBatchRunner(+RunSummary) ─ ReviewJsonlExporter`.

## Runtime Metrics Store (AI 자기개선 #3 — 완료)
status.json 은 최신 스냅샷만 담지만, **매 cycle 을 SQLite 에 append-only 로 영속**한다.
System Review Engine / Dashboard 가 읽는 운영 지표의 Primary Source. `run_paper.py` 에 자동 배선됨.
- **CycleMetric (per-cycle delta)**: cycle_id/ts/session/status/reason, `loop_ms`, `ai_ms`,
  `fetch_latency_ms`(루프 미측정 → None, 향후), health(status/api/ai/broker/journal/replay),
  per-cycle 카운트(cycles_run/skipped/api_errors/pipeline_errors/exceptions/sqlite_locks/
  candidates/ai_approve·wait·reject/risk_block/orders/buys/sells/exits/eods). created_at 외부 주입(결정론).
- **SqliteMetricsStore**: `metric_cycle` append-only + 인덱스(created_at/cycle_id/status/health_status/cycle_ts).
  `record` / `query`(status·health·기간 필터) / `aggregate`(누적 SUM·avg/max loop_ms·degraded_cycles).
  `InMemoryMetricsStore` 동일 인터페이스(테스트).
- **PaperRuntime 통합**: `record_cycle`+`health_check` 직후 per-cycle delta 계산→ MetricsStore 에 기록.
  store 미주입 시 no-op(하위호환). 관측 실패가 매매/생존을 막지 않음(흡수+error 로그).
- 원천: cumulative `RuntimeMetrics` 는 그대로(status.json/종료리포트), MetricsStore 는 cycle 단위 SSoT.
  yaml `metrics.db`(runtime_mixed → data/metrics_mixed.sqlite).

## 테스트
```bash
./.venv/bin/python -m pytest -q
```

## 구조
```
ai_trading_system/
  .env / .env.example      # Toss 키 (.env 커밋 금지)
  requirements.txt
  conftest.py
  spikes/                  # feasibility 검증 스크립트 (읽기 전용)
    toss_smoke_test.py
    toss_history_depth.py
    toss_reference_symbols.py
  src/
    config.py              # .env 로딩
    models.py              # Canonical Candle + 세션 분류
    providers/
      toss_client.py       # Toss 저수준 HTTP (auth + candles)
      toss_market_data.py  # raw -> Canonical 변환 Provider
    storage/
      candle_store.py      # SQLite 저장/조회
    ingest/
      backfill.py          # 과거 캔들 백필 entrypoint
    engines/
      indicators.py        # 순수 지표 함수 (ema/rsi/vwap/bollinger/stoch_rsi)
      indicator_engine.py  # IndicatorEngine + candles_to_frame + resample
    universe/
      screener.py          # UniverseScreener + Candidate + ScreenerConfig
    risk/
      models.py            # OrderIntent / Position / PortfolioSnapshot / RiskDecision
      risk_manager.py      # RiskManager + RiskConfig + reason codes
    reconcile/
      models.py            # PositionView / OrderView / Mismatch / Report + 인터페이스
      service.py           # ReconciliationService + ReconcileConfig
    backtest/
      execution.py         # ExecutionModel (slippage/fee, 확장 가능한 fill)
      models.py            # Trade / BacktestConfig / BacktestReport / split
      engine.py            # BacktestEngine + ScreenerEntryStrategy
    ai/
      models.py            # AIEntryDecision / AIDecisionLog / schema / 인터페이스
      engine.py            # AIDecisionEngine + prompt + fallback + cache
      providers.py         # OpenAIProvider(lazy) + FakeAIProvider
    order/
      models.py            # OrderRequest / BrokerOrderResult / OrderRecord / stores
      paper_broker.py      # PaperBrokerProvider (모의 체결)
      toss_broker.py       # TossBrokerProvider (skeleton + TODO)
      manager.py           # OrderManager
    pipeline/
      models.py            # SystemState / SymbolOutcome / PipelineResult
      pipeline.py          # TradingPipeline (run_once)
    live/
      models.py            # LoopConfig / CycleReport / FakeMarketDataProvider
      paper_loop.py        # PaperTradingLoop (tick/run)
    journal/
      models.py            # JournalCycle / JournalStore / InMemoryJournalStore
      sqlite_store.py      # SqliteJournalStore (MVP)
    replay/
      models.py            # ReplayContext / CycleNotFoundError
      system.py            # ReplaySystem (read-only)
    review/
      models.py            # AIReviewReport / AIReviewLog / REVIEW_SCHEMA
      engine.py            # AIReviewEngine
      providers.py         # FakeReviewProvider (실호출은 OpenAIProvider 재사용)
    dataset/
      models.py            # Feature/Label/Meta/Dataset Record + *_VERSION
      builder.py           # DatasetBuilder + LabelConfig (Journal+Candle → Dataset, read-only)
      exporters.py         # JsonlExporter / ParquetExporter
    review/  (batch)
      store.py             # ReviewRecord / SqliteReviewStore(SSoT) / InMemoryReviewStore + REVIEW_VERSION/source
      batch_runner.py      # ReviewBatchRunner + RunnerConfig + RunSummary
      exporters.py         # ReviewJsonlExporter
    runtime/
      config.py            # RuntimeConfig + load_yaml
      logging_setup.py     # rotating loggers
      metrics.py           # RuntimeMetrics (누적 집계)
      metrics_store.py     # CycleMetric + SqliteMetricsStore(SSoT) + InMemoryMetricsStore
      report.py            # 종료 리포트(markdown)
      paper_runtime.py     # PaperRuntime (run/stop/pause/resume, recovery, shutdown, cycle metrics)
  config/runtime.yaml      # 운영 설정
  scripts/
    ai_entry_filter_manual.py   # 실제 OpenAI 수동 호출
    run_paper.py                # 운영 진입점 (Toss 시세 + Paper 체결)
    build_dataset.py            # AI Learning Dataset 빌드 (read-only batch)
    run_reviews.py              # Review Batch Runner (Replay→Review, read-only)
  tests/                   # 전 계층 (205 tests)
  data/                    # SQLite (git 무시)
  datasets/                # 빌드된 학습 데이터셋 (git 무시)
```
다음: 실 Toss 시세로 Paper 루프 장기 관찰 → (검증 + 승인 후) TossBrokerProvider 실주문.
