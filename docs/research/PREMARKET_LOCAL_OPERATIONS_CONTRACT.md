# 장전·사후평가 로컬 운영 계약

## 목적

장전 증거 수집과 사후평가가 Codex 사용량이나 대화형 에이전트의 연속 실행에 의존하지 않도록,
저장소의 결정론적 CLI와 macOS `launchd`가 PRIMARY → VERIFICATION → FREEZE → 장마감
사후평가를 실행한다. 이 계약은 주문·브로커·승격 권한을 추가하지 않는다. FREEZE Paper
세션 실행기(아래)는 `PaperBrokerProvider` 모의 체결만 기록하며 실계좌·Toss 주문 API를 쓰지 않는다.

## 실행기

`scripts/run_premarket_pipeline.py`는 다음 두 가지 동작만 제공한다.

- `--stage PRIMARY|VERIFICATION|FREEZE`: 해당 NYSE stage의 provider bundle, market bundle,
  normalized input, Daily Brief를 순서대로 생성한다.
- `--postclose`: FREEZE Brief의 watchlist와 SPY만 최신 정규장 1분봉으로 보충한 뒤,
  read-only post-close 방향성 평가기를 실행한다.

거래일 인자를 생략하면 현재 New York 날짜를 사용한다. NYSE 비거래일 또는 DST 보조 trigger의
비해당 시간에는 네트워크를 호출하지 않고 `SKIPPED`로 끝난다. 실제 stage window에서 실패해
Daily Brief가 완성되지 않으면 immutable `INCOMPLETE` operation receipt를 남긴다. 각 stage는
기존 immutable artifact가 있으면 동일 파일을 재사용하며, 완결 stage는 immutable `COMPLETE`
operation receipt를 쓴다.

## 실패 처리

Toss market collector는 OAuth·HTTP·timeout·네트워크 예외를 traceback만 남기고 끝내지 않는다.
`premarket-market-bundle/v1`의 표준 `FAIL_CLOSED` artifact와 sanitized receipt를 생성한다.
따라서 다음 정규화/Brief 단계는 계속 실행되어 감사 가능한 `NO_TRADE`로 닫힌다.

이 artifact에는 candle을 추측하거나 채우지 않는다. 결함은 예를 들어
`TOSS_HTTP_STATUS_403`, `TOSS_HTTP_TIMEOUT`, `TOSS_NETWORK_ERROR`로만 기록한다. 원문 오류,
credential, request header는 산출물·로그·Git에 기록하지 않는다.

FREEZE Brief가 없거나 변조되었으면 post-close는 backfill과 평가를 시작하지 않고
`INCOMPLETE/MISSING_FREEZE_BRIEF`를 보고한다. 불완전 1분봉도 기존 post-close 계약대로
0 수익으로 대체하지 않는다.

## launchd 일정

`scripts/install_premarket_launchd.py --install`은 현재 macOS 사용자의 LaunchAgents에
아래 열세 jobs를 설치한다. New York DST에 따라 같은 stage의 두 KST 시각 중 하나만 실제
NYSE window에 들어가며, 다른 하나는 `SKIPPED/STAGE_WINDOW_NOT_REACHED`로 끝난다. 시간은
macOS 로컬 시간대(KST)이며 NYSE 휴장일은 실행기가 스스로 skip한다.

| Job | KST | 동작 |
| --- | ---: | --- |
| primary | 21:01, 22:01 | PRIMARY stage, DST-safe pair |
| verification | 22:01, 23:01 | VERIFICATION stage, DST-safe pair |
| freeze | 22:11, 23:11 | FREEZE stage, DST-safe pair |
| postclose | 05:20, 06:20 | 같은 시점의 ET 날짜 FREEZE 정규장 사후평가, DST-safe pair |
| freeze-paper | 05:35, 06:35, 07:35 | 같은 ET 날짜의 FREEZE Paper 세션. DST-safe pair + 재시도 slot(EST 17:35 ET). 기록된 세션은 이후 실행에서 no-op |
| history-collect | 10:40, 13:40 | POST가 닫힌 마지막 NYSE 거래일의 P0-US30 30종 1분봉(PRE/REGULAR/POST)을 공식 backfill 경로로 tail-repair. 공유 market-history lock 아래 실행, 종목별 REGULAR 충족을 `reports/history-collection/YYYYMMDD.operation.json`에 기록. COMPLETE 영수증이 있으면 재시도 slot은 no-op |

plist에는 credential을 넣지 않는다. Python CLI가 프로젝트 `.env`를 로드한다. stdout/stderr는
`reports/premarket-launchd/`에 남고, 생성 산출물과 operation receipt는 `reports/` 및 `data/`
gitignore 경로에만 보관한다.

설치 후에는 `scripts/install_premarket_launchd.py --status`로 plist 존재와 launchd load 상태를
확인한다. 실제 Toss OAuth 403은 로컬 코드가 해결할 수 없는 외부 계정/IP/credential 상태이므로
`NO_TRADE`로 닫고, 별도 연결 점검 뒤에만 다음 거래일을 유효 관측으로 취급한다.

## FREEZE Paper 세션 실행기

`scripts/run_freeze_paper_session.py [--trading-day YYYY-MM-DD] [--reports-root reports] [--dry-run] [--recompute]`
는 `src/freeze_paper/runner.py`를 호출한다. 확인 순서와 상태는 다음과 같다.

| 상태 | 조건 | 기록 |
| --- | --- | --- |
| `SKIPPED/NON_TRADING_DAY` | NYSE 휴장일 | 없음 |
| `NOT_ACTIVATED` | 첫 세션 영수증 없음·무효, 계약 JSON/MD 또는 Daily Brief config 해시 불일치, 영수증 `code_commit`이 실행 HEAD의 조상이 아님(`CODE_COMMIT_NOT_ANCESTOR`)·git 사용 불가(`GIT_UNAVAILABLE`), 예정 세션이 아님 | operation receipt만 |
| `SKIPPED/PAPER_WINDOW_NOT_REACHED` | 정규장 마감(조기폐장 포함) + 20분 이전 | 없음 |
| `INCOMPLETE` | 이전 예정 세션 artifact 없음·무효(서명·날짜·순번·영수증 불일치), 1분봉 backfill 실패, 잘못된 종목 코드(`INVALID_SYMBOL`), 공유 DB lock 대기 초과(`LOCK_TIMEOUT`), stale `.partial`, 예외(`EXCEPTION:<예외 타입>`, 메시지·traceback 없음) | operation receipt만, 재시도 가능 |
| `COMPLETE` | 세션 artifact(+Markdown) 기록. 이미 기록·검증된 세션은 backfill·네트워크 없이 `SESSION_ALREADY_RECORDED` | 세션 artifact, 장부, operation receipt |
| `DATA_DRIFT` | `--recompute`에서만: 현재 DB로 다시 계산한 결과가 기존 세션 artifact와 다름 | `premarket-freeze-paper.YYYYMMDD.drift.json`만 |

`COMPLETE` operation receipt는 이후 어떤 실행도 교체하지 않는다. `--recompute`는 backfill 없이 현재 DB로
재계산하며 세션 artifact와 operation receipt를 건드리지 않는다.

- 세션 순서는 엄격하다. 이전 예정 세션 artifact의 `artifact_sha256`을
  `prev_session_artifact_sha256`로 잇고, 없으면 건너뛰지 않고 `INCOMPLETE`로 멈춘다.
- 거래 종목(게이트 통과 종목, 인버스 매핑 `SQQQ`·`SOXS`, 이월된 `UNRESOLVED_EXIT` 종목)만
  공식 tail-repair backfill(`scripts/history_backfill_official.py`)을 현재 가상환경 Python으로 실행해
  보충하고, 1분봉은 blind-OOS 가드 로더(`research_load`)로만 읽는다.
- `reports/premarket-freeze-paper.YYYYMMDD.json`(+`.md`)은 입력 해시, 게이트, 이벤트, 주문, 체결,
  평가가, 중단, 대조, 공식·stress 파생 비용, `end_state`를 담으며 벽시계 시각이 없다.
  `reports/premarket-freeze-paper-ledger.json`은 세션 artifact의 재생성 가능한 접기다.
  `reports/premarket-operation.YYYYMMDD.paper.json`은 최신 실행의 상태·사유·실행 시각·코드 commit이며
  재시도를 위해 원자적으로 교체된다.
- `--dry-run`은 backfill과 모든 파일 쓰기를 생략하고 결과만 출력한다.
- FREEZE snapshot의 `policy_config`가 영수증이 고정한 활성화 config와 다르면 그 세션은
  `NO_TRADE/POLICY_CONFIG_MISMATCH`로 기록한다(건너뛰지 않는다).
- 1분봉 `ts_utc`는 **봉 시작 시각**이다. 세션은 `market_open_utc <= ts_utc < market_close_utc`만 쓰므로
  EDT의 09:35 ET 진입 봉은 `13:35Z`이고 마감 시각에 시작하는 봉은 제외된다.
- 대조에는 세션 중간 봉에서 엔진 상태를 JSON으로 저장·재적재한 뒤 이어서 실행한 결과가 끊김 없는 실행과
  같은지(재시작 경로) 포함된다. 장부는 각 artifact의 서명·순번·연결을 검증하고 실패하면 `INVALID` 행으로 남긴다.
- post-close 평가기와 FREEZE Paper 실행기는 `data/.market_history.lock`(`fcntl.flock` 배타 lock)을
  공유해 `data/market_history.sqlite` backfill·조회를 직렬화한다. 대기 초과는 `INCOMPLETE/LOCK_TIMEOUT`이며
  기록을 남기지 않거나(post-close) 재시도 가능한 receipt만 남긴다(Paper).
- `LOCK_TIMEOUT`·`INCOMPLETE`로 끝난 날은 원인을 해소한 뒤 수동으로 복구한다:
  `.venv/bin/python scripts/run_freeze_paper_session.py --trading-day YYYY-MM-DD`.
  이미 기록된 세션은 no-op이고, 기존 artifact가 검증에 실패하면 receipt는 그대로 두고
  `premarket-freeze-paper.YYYYMMDD.alert.json`에 사유 코드를 남긴다. Markdown이 없으면 검증된 JSON에서
  결정론적으로 다시 만든다.
- 매 예정 세션의 Paper 실행은 SPY 1분봉 tail repair를 별도 단계(`spy_tail_repair`)로 먼저 실행한다. 이 단계는
  엔진 입력과 세션 artifact에 들어가지 않으며, 실패해도 세션은 계속되고 receipt에
  `spy_step.status=SPY_BACKFILL_FAILED`로 남는다(평가기 SPY 벤치마크는 `INCONCLUSIVE_BENCHMARK`).
- 첫 세션 영수증 전: 구간 시작일 저녁(마감 + 20분 이후, 24:00 ET 이전)에 SPY tail repair를 한 번 실행하고
  저장된 15:59/12:59 ET 봉 증거를 영수증 `window_start_spy_close_evidence`에 적는다. 정확한 명령과 운영자 신뢰
  가정(로컬 시계 정확, DB 행 수동 삭제·재삽입 금지)은 `src/freeze_paper/README.md` runbook에 있다.
- 10세션 평가는 launchd job 없이 수동으로 실행한다:
  `.venv/bin/python scripts/evaluate_freeze_paper_window.py [--through YYYY-MM-DD] [--dry-run]`.
  FINAL은 예정 10개 세션 artifact가 모두 존재하고 검증될 때만 쓰이며 write-once다. 그 외에는 재생성 가능한
  INTERIM이고 ELIGIBLE이 될 수 없다. 결과는 실거래 승인이 아니며 자동 승격이 없다. 세부는
  `src/freeze_paper/README.md`.
- launchd를 다시 설치하면 freeze-paper 세 job도 설치된다. 활성화 전에는 매 거래일
  `NOT_ACTIVATED` operation receipt를 남기는 것이 정상이다.

## 2026-09-24 장애 근거

해당 거래일은 전향적 유효 관측이 아니다.

- PRIMARY는 Marketaux timeout과 Toss OAuth 403을 `FAIL_CLOSED → NO_TRADE`로 보존했다.
- VERIFICATION은 같은 Toss 403 뒤에 Codex workspace credit 부족으로 중단됐다.
- FREEZE는 시작 직후 Codex workspace credit 부족으로 실행되지 않았다.

이 계약은 Codex credit 부족을 제거하는 것이 아니라, 해당 의존성을 데이터 수집 경로에서
제거한다. Marketaux 및 Toss 공급자 오류는 계속 fail-closed다.
