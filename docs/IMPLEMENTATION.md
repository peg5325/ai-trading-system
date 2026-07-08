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

## Rule Improvement Aggregator (AI 자기개선 #2 — 완료)
ReviewStore 의 회고를 집계해 **전략 개선 후보(제안)** 를 도출한다. **제안만 한다** —
strategy.yaml / Rule Engine 을 자동 수정하지 않는다(실제 변경은 사람 승인 후 별도).
```bash
./.venv/bin/python scripts/aggregate_reviews.py config/runtime_mixed.yaml \
    --review-version v1 --compare-review-version v2 --export-json reports/rule_improve.json
```
- **입력**: ReviewStore(read-only)만. 실시간 매매/주문/Runtime 무관. 결정론적(난수/시각/외부호출 없음).
- **정규화 + 유사 그룹핑**: rule_improvement / missed_signals 텍스트를 소문자·구두점제거·공백정규화 후
  토큰 Jaccard(기본 0.6)로 유사 개선안을 그리디 병합. 빈도 내림차순 `ClusterStat`(대표문구/count/변형/품질분포).
- **집계**: decision_quality 분포, confidence_reassessment 통계(n/avg/min/max), Top-N 클러스터.
- **비교**: `prompt_version`/`model_name`/`review_version`·기간 필터로 두 리포트 산출 →
  `compare_reports`(대표문구별 빈도 delta)로 버전별·기간별 추이 비교.
- **출력**: `RuleImprovementReport.to_dict()` + `export_report_json`(JSON 제안 산출물).
- 구조: `RuleImprovementAggregator ─ RuleImprovementReport(ClusterStat) ─ compare_reports/export_report_json`.

## System Review Engine (AI 자기개선 #4 — 완료)
시스템 **자체(운영 품질)** 를 기간 단위로 GPT 회고. 매매 전략은 평가하지 않는다(그건 AIReviewEngine).
Rule Engine/Trading Pipeline/OrderManager/RiskManager/strategy.yaml 에 **절대 영향 없음 — 제안만**.
```bash
./.venv/bin/python scripts/system_review.py config/runtime_mixed.yaml \
    --start 2026-07-07T00:00:00Z --end 2026-07-08T00:00:00Z --provider fake
# 실제 OpenAI 회고는 이 스크립트에서 --provider openai 로만(수동)
```
- **입력**: RuntimeMetricsStore(read-only) + JournalStore 뿐. Replay/TradingPipeline 미사용.
- **window 요약(결정론)**: `build_window_summary` → runtime(health/api_errors/exceptions/sqlite_locks/
  avg·max loop latency/candidates/AI approve·wait·reject 비율/risk_block 비율/orders/skipped/degraded) +
  journal(status 분포/error_cycles) + data_quality. GPT 입력이자 report.runtime_window 로 저장.
- **SystemReviewReport**: `{grade(A~F), summary, major_issues[], recommendations[], confidence}` + provenance.
- **SystemReviewLog(별도 저장)**: prompt_version/model_name/latency/input_hash/runtime_window 모두 기록.
  `SqliteSystemReviewStore`(인덱스: created_at/status/grade/model_name/window_start) + InMemory.
- **실패 안전 종료**: timeout/rate limit/invalid JSON/schema mismatch → SYSTEM_REVIEW_FAILED(grade UNKNOWN).
- 결정론: input_hash = window+prompt+model (created_at/latency 제외). Fake provider 기반 테스트.
  yaml `system_review.db`(runtime_mixed → data/system_reviews_mixed.sqlite).

## 조회 CLI (inspect) (완료)
4대 SQLite Single Source of Truth(journal/reviews/metrics/system_reviews) 를 터미널에서 훑어보는
**읽기 전용** 조회 도구. 매매/주문/Runtime 제어 기능은 전혀 없다 — 오직 조회.
라이브 Paper Trading 루프가 같은 DB 파일에 계속 쓰는 중에도 안전하게 실행 가능:
Store 클래스(CREATE TABLE + read-write)를 인스턴스화하지 않고, 매번
`sqlite3.connect(f"file:{path}?mode=ro", uri=True) + PRAGMA query_only=ON` 으로만 연다.

```bash
./.venv/bin/python scripts/inspect_cli.py summary                       # 4개 DB 전체 개요
./.venv/bin/python scripts/inspect_cli.py latest --limit 5               # 최신 사이클 5개(최신순)
./.venv/bin/python scripts/inspect_cli.py cycles --limit 20              # 최근 사이클(시간순)
./.venv/bin/python scripts/inspect_cli.py errors --since today            # 오늘의 에러 사이클/지표
./.venv/bin/python scripts/inspect_cli.py candidates --symbol SOXL        # 후보 종목 outcome
./.venv/bin/python scripts/inspect_cli.py orders --limit 20               # 체결(FILLED) 주문
./.venv/bin/python scripts/inspect_cli.py reviews --limit 20              # 최근 AI 회고
./.venv/bin/python scripts/inspect_cli.py improvements --top 5            # Rule Improvement 집계
./.venv/bin/python scripts/inspect_cli.py metrics --last 24h              # 운영 지표 윈도우 집계
./.venv/bin/python scripts/inspect_cli.py system-review --latest          # 최신 시스템 회고 상세
./.venv/bin/python scripts/inspect_cli.py health                          # health 스냅샷
./.venv/bin/python scripts/inspect_cli.py summary --json                  # 모든 커맨드 --json 지원
```
- `--config`(기본 `config/runtime_mixed.yaml`) 로 DB 경로를 읽고, `--db PATH` 로 커맨드별 기본
  DB 를 오버라이드할 수 있다(config 파일이 없어도 문서화된 기본 경로로 폴백).
- DB 파일/테이블이 없으면 traceback 없이 친절한 한글 메시지 + non-zero exit; 테이블은 있지만
  행이 없으면 "데이터 없음" + exit 0.
- `improvements` 는 `RuleImprovementAggregator` 를 그대로 재사용한다(작은 read-only 어댑터로
  review 테이블 → `ReviewRecord` 변환).
- reader 함수는 모두 명시적 start/end datetime 만 받는다(`datetime.now()` 는 argparse 의
  `--since`/`--last` 파싱에서만 호출) → 테스트가 결정론적.

## Realized PnL Engine (FIFO — 완료)
BUY/SELL 을 **FIFO 로 매칭**해 실현손익을 계산하는 **단일 엔진**. 실거래/Paper/Backtest 가 모두 사용.
```bash
./.venv/bin/python scripts/inspect_cli.py pnl              # journal FILLED 체결 기준 실현손익
./.venv/bin/python scripts/inspect_cli.py pnl --symbol SOXL --since today
```
- **FifoMatcher**(`src/pnl/fifo.py`): 부분청산/여러 번 매수/여러 번 매도 지원. 오래된 롯부터 소진.
  각 매칭 단위마다 `RealizedTrade` 1건, 수수료는 매칭 수량 비례 배분. 결정론적(now/난수 없음).
- **RealizedTrade**: `realized_pnl`/`realized_return_pct`/`holding_minutes`/`holding_bars`/
  `commission`/`slippage`/`net_pnl`/`gross_pnl`. **불변식**: `net_pnl == gross_pnl - slippage - commission`.
  슬리피지 분해는 의도가(ref_price) 필요 → `OrderRecord.ref_price` 저장(신규). backtest 는 ref 보유.
- **summarize**(`src/pnl/metrics.py`): 누적손익/평균손익/Profit Factor/Expectancy/평균 보유시간·Holding Bar/승률.
- **journal_pnl**(`src/pnl/journal_pnl.py`): Trade Journal 의 FILLED 체결 = SSoT → 시간순 FifoMatcher.
  실현손익은 여기서 결정론적으로 재계산(별도 중복 저장 없이 fills 를 단일 진실원으로 사용).
- **통합**: Backtest(`BacktestEngine` 이 동일 FifoMatcher 사용) · Replay(`ReplayContext.realized_pnl`,
  as_of 까지 look-ahead 없이) · Runtime 종료 리포트(실현손익 요약 자동 집계) · inspect CLI(`pnl`).
- Long-only 가정: 보유 초과 매도는 방어적으로 `unmatched_sells` 기록(크래시 없음).

## 자금관리 / Portfolio (PortfolioAccount — 완료)
Paper Trading 이 **고정 계좌값이 아니라 실제 자금관리**로 성과를 측정하도록
`PortfolioAccount`(`src/portfolio/account.py`)를 도입했다. FifoMatcher(위 PnL 엔진)를
재사용해 계산 코드를 단일화한다. 결정론적(now/난수 없음).

- **PortfolioValue**(`cash, market_value, realized_pnl, unrealized_pnl, total_equity`):
  `total_equity == cash + market_value`. `.to_dict()` 는 소수 6자리 반올림.
- **PortfolioAccount**: `apply_fill(FillEvent)` 로 체결 시 cash 즉시 반영
  (BUY: `-= qty*price+fee`, SELL: `+= qty*price-fee`) 후 FifoMatcher 에 ingest.
  `positions()` 는 심볼별 보유 롯을 수량가중평균가로 집계(`qty<=1e-9` 는 제외).
  `market_value(marks)`/`unrealized_pnl(marks)`/`equity(marks)`/`portfolio_value(marks)` 는
  외부에서 주입한 시가(marks) 기준으로 계산(계좌 자체는 가격을 모른다).
  `can_afford(notional)` 로 현금 충분성 확인. `mark_equity(marks, day_key)` 로 자산곡선을
  누적(`equity_curve`)하고 peak/drawdown/일일수익률을 갱신(`peak_equity`,
  `last_drawdown_pct`, `last_daily_return_pct`). `from_fills(...)` 로 Journal 체결 재생 → 계좌 재구성
  (재시작 안전성).
- **사이징 규칙(RiskManager 추가)**: `RiskConfig.max_positions`(기본 2, 동시 보유 종목 수 상한 —
  신규 진입에만 적용, 기존 종목 추가매수는 무관) + `RC_MAX_POSITIONS`. 스냅샷에 `cash` 가 주어지면
  주문 명목이 현금을 초과할 때 `RC_INSUFFICIENT_CASH` 로 차단(`cash=None` 이면 미적용, 후방호환).
  기존 종목당 20% 명목 상한(`max_symbol_notional_pct`)은 그대로 유지.
- **Paper Loop 통합**(`src/live/paper_loop.py`): `PaperTradingLoop` 가 `self.account` 를 보유하고,
  journal 이 있으면 `fills_from_journal` 로 이전 체결을 재생해 재구성한다. 매 사이클
  `_portfolio()` 가 `self.account` 로부터 `PortfolioSnapshot`(equity/positions/cash/drawdown/
  일일수익률)을 만들고, 체결 결과는 `_apply_fills()` 로 다음 사이클을 위해 계좌에 즉시 반영한다.
  기존 `InMemoryPositionStore` 는 EOD flatten 판단용으로 그대로 유지.
- **성과 지표 공용화**(`src/portfolio/performance.py::performance_summary`): 초기자본/자산곡선/
  실현거래로부터 `total_return_pct, win_rate, profit_factor, avg_win, avg_loss, expectancy,
  max_drawdown_pct, sharpe_ratio, total_trades, max_consecutive_losses` 산출.
  **Backtest 는 동일 함수를 사용**한다(`BacktestEngine._metrics` 가 `performance_summary` 호출 후
  `exposure_time`/`sample_status` 만 추가) — Backtest·Paper 수치 산식이 완전히 동일(parity 유지,
  기존 254 테스트 회귀 없음). Sharpe 는 자산곡선의 스텝 수익률로 계산(모집단 표준편차, 2점 미만/
  분산 0 이면 `None`), `periods_per_year` 지정 시 연율화.
- **Runtime 노출**: `CycleMetric` 에 `equity/peak_equity/drawdown_pct/daily_return_pct` 컬럼 추가
  (SqliteMetricsStore 스키마 포함). `status.json` 에 `equity` 블록(initial/current/peak/drawdown_pct/
  daily_return_pct) 추가. 종료 리포트에 `## Performance` 섹션(초기자산/최종자산/수익률/승률/PF/
  Expectancy/MDD/Sharpe) 추가 — 모두 계좌 없으면(getattr 가드) 조용히 생략, 실패해도 종료를 막지 않음.
- **운영 설정**: `config/runtime_us.yaml`(US 본장, 초기자본 3,000,000·종목당 20%·동시 2종목).
  통화 캐비어트: 자본은 KRW 명목, 종목가는 USD — 상대 성과 지표(수익률/PF/Sharpe/MDD)는 단위가
  상쇄돼 유효하지만 절대 금액은 참고용(v1 한계, 통화 분리는 향후 과제).

## Exit Signal Generator (청산 신호 생성 — 완료)
- **독립 모듈** `src/exits/generator.py`: `ExitSignalGenerator`(Stop Loss / Take Profit / Time Exit / EOD Flatten).
  순수 결정론(현재시각/난수 미사용, AI 무관). live/paper/backtest 가 **동일 엔진 공유**.
- **우선순위**: `STOP_LOSS > TAKE_PROFIT > TIME_EXIT > (TREND_BREAK) > EOD_FLATTEN`.
- 사유는 곧 `OrderIntent.kind`(`STOP_LOSS`/`TAKE_PROFIT`/`TIME_EXIT`/`EOD_FLATTEN`, `RISK_REDUCING`에 포함)
  → journal→FIFO 로 `RealizedTrade.exit_reason` 자동 전파. RiskManager 가 risk-reducing 으로 허용
  (kill switch 에도), 보유수량으로 clamp. 청산 대상 포지션은 `PortfolioAccount`(FIFO, entry_time·재시작복원)에서 소스.
- **설정(yaml `exits:` 섹션)** — 섹션이 없으면 보수적 기본(전종목 EOD off, SL/TP off, leverage EOD on):
```yaml
exits:
  enable_all_symbols_eod_flatten: true   # 전 종목 세션마감 청산(Paper 검증: closed trade 보장)
  stop_loss_pct: 0.01                    # -1.0% 손절 (0/미설정=끔)
  take_profit_pct: 0.015                 # +1.5% 익절 (0/미설정=끔)
  time_exit_minutes: 60                  # N분 보유 후 청산 (time_exit_bars 도 지원)
  leverage_eod_flatten: true             # 레버리지 ETF(>1x) 당일청산
```
- 검증용 config: `config/runtime_krx_exit_test.yaml`, `config/runtime_us_exit_test.yaml`.
- backtest 는 동일 generator 로 리팩터(engine.py) — 마감봉 EOD 는 close 체결, 신호청산은 next-open 로 기존 타이밍 보존(리팩터 전후 golden-diff 일치).

## Toss stale-token 자동 복구 (완료)
- `TossClient.get_candles` 가 **401 시 캐시 토큰을 강제 폐기하고 1회 재시도** → 장시간 운영 중 서버측
  토큰 무효화(만료 전 revoke)를 무중단 자동 복구. (`expires_in` 만 믿고 죽은 토큰을 계속 쓰던 문제 해결.)

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
      aggregator.py        # RuleImprovementAggregator + RuleImprovementReport + compare_reports (제안 전용)
    system_review/  (운영 회고 전용)
      models.py            # SystemReviewReport / SystemReviewLog / SYSTEM_REVIEW_SCHEMA / GRADES
      window.py            # build_window_summary (MetricsStore+Journal → 운영 요약, read-only)
      engine.py            # SystemReviewEngine (GPT 회고, 실패 안전 종료)
      store.py             # SqliteSystemReviewStore / InMemorySystemReviewStore
    pnl/  (실현손익 단일 엔진)
      models.py            # FillEvent / RealizedTrade / OpenLot / PnlSummary
      fifo.py              # FifoMatcher (FIFO 매칭, 부분청산) + match_fills
      metrics.py           # summarize (PF/Expectancy/보유시간/승률)
      journal_pnl.py       # Journal FILLED → 실현손익 (Paper/실거래 경로)
    portfolio/  (자금관리 — cash/포지션/성과)
      account.py           # PortfolioValue / PortfolioAccount (cash·FIFO·자산곡선)
      performance.py       # performance_summary (Backtest/Paper 공용 성과지표 + Sharpe)
    runtime/
      config.py            # RuntimeConfig + load_yaml
      logging_setup.py     # rotating loggers
      metrics.py           # RuntimeMetrics (누적 집계)
      metrics_store.py     # CycleMetric + SqliteMetricsStore(SSoT) + InMemoryMetricsStore
      report.py            # 종료 리포트(markdown)
      paper_runtime.py     # PaperRuntime (run/stop/pause/resume, recovery, shutdown, cycle metrics)
    cli/
      inspect.py           # 조회 CLI reader/formatter/main (read-only, 4대 DB)
  config/runtime.yaml      # 운영 설정
  scripts/
    ai_entry_filter_manual.py   # 실제 OpenAI 수동 호출
    run_paper.py                # 운영 진입점 (Toss 시세 + Paper 체결)
    build_dataset.py            # AI Learning Dataset 빌드 (read-only batch)
    run_reviews.py              # Review Batch Runner (Replay→Review, read-only)
    aggregate_reviews.py        # Rule Improvement Aggregator (제안 전용, read-only)
    system_review.py            # System Review Engine (운영 회고, read-only)
    inspect_cli.py              # 조회 CLI 진입점 (read-only, 4대 SQLite DB) — stdlib inspect shadow 회피 위해 _cli
  tests/                   # 전 계층 (275 tests)
  data/                    # SQLite (git 무시)
  datasets/                # 빌드된 학습 데이터셋 (git 무시)
```
다음: 실 Toss 시세로 Paper 루프 장기 관찰 → (검증 + 승인 후) TossBrokerProvider 실주문.
