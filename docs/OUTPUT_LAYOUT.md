# 출력 폴더 구조 (OUTPUT_LAYOUT)

_2026-10-14 도입. 이 문서는 git에 올라가지 않는 산출물(`reports/`, `runs/`)이 어디에 쌓이는지 설명한다._

## 한 곳에서 정한다

폴더 규칙은 `src/output_layout.py` 하나에만 있다. 쓰는 코드와 읽는 코드는 모두 이 모듈의 함수
(`premarket_day_dir(root, day)`, `daytrade_paper_dir(root)` 같은 것)로 경로를 받는다.
`root / "reports" / "..."`처럼 폴더 이름을 직접 적지 않는다. 폴더를 바꿀 일이 생기면 이 모듈만 고친다.

파일 이름은 바뀌지 않았다. 바뀐 것은 파일이 들어가는 폴더뿐이다.

## 지금 구조

저장소 루트 기준이다.

| 폴더 | 들어가는 것 |
| --- | --- |
| `reports/premarket/days/<YYYYMMDD>/` | 그날의 premarket 산출물 전부. 단계 파일(`premarket-provider-bundle`·`market-bundle`·`input`·`daily-brief`·`operation`의 `.primary`/`.verification`/`.freeze`와 `.receipt.json`), 장 마감 뒤 평가(`premarket-postclose.<d>.json/.md`, `premarket-operation.<d>.postclose.json`), FREEZE Paper(`premarket-freeze-paper.<d>.json/.md/.drift.json/.alert.json`, `premarket-operation.<d>.paper.json`). 예전에 손으로 만든 `.v2`·`.corrected` 같은 변형도 같은 날짜 폴더에 둔다. |
| `reports/premarket/ledgers/` | 날짜를 넘어 쌓이는 장부. `premarket-postclose-ledger.json`, `premarket-postclose-cumulative.json/.md`, `premarket-freeze-paper-ledger.json`, `premarket-freeze-paper-evaluation.*` |
| `reports/premarket/logs/<d>.<stage>/` | 단계별 실행 로그 (예전 `reports/premarket-operation-logs/`) |
| `reports/premarket/launchd/` | launchd 작업의 stdout/stderr (예전 `reports/premarket-launchd/`) |
| `reports/history-collection/` | 1분봉 수집 영수증 `<d>.operation.json` (그대로) |
| `reports/history-collection/logs/<d>/` | 수집 실행 로그 (예전 `reports/history-collection-logs/`) |
| `reports/daytrade/paper/` | 탐색용 Paper 하루 결과 `<date>.json`, `ledger.json` (예전 `reports/daytrade_paper/`) |
| `reports/daytrade/shadow/<date>/` | R1 shadow 실행 기록 (예전 `reports/daytrade_shadow/`) |
| `reports/daytrade/v11/`, `reports/daytrade/v13/` | v1.1·v1.3 연구 산출물 (예전 `reports/daytrade_v11/`, `daytrade_v13/`) |
| `reports/research/<name>/` | 연구 산출물 폴더: `explore_*`, `daily_screen_v1`, `entry_study_v1`, `reversal_study_v1`, `phase1a_probe` |
| `reports/research/h2-h4/` | H2~H4 검증 산출물과 작업지시서: `h2b_backfill_validation.*`, `h3_regime_validation.*`, `h3b_longhorizon_validation.*`, `h4_screener_ranking.json`, `h2_h3_preliminary_s001.md`, `WORKORDER_H*.md`, `PROMPT_h2b_backfill_cc.md`, `score_correlation.md` |
| `reports/data-ops/` | 데이터 수집·복제 기록: `p0_us30_*`, `P0_US30_*`, `*replica_receipt*`, `raw_snapshot_manifest*` |
| `reports/archive/2026-07_paper_sessions/` | 2026-07 Paper 세션 시절 문서: `SESSION_*_FINAL.md`, `session_*.md`, `paper_report_*.md`, `paper_trading_session_*`, `preflight_report_*`, `phase6_*_preflight.md`, `krx_*`/`us_*` 리뷰·규칙 개선·시스템 리뷰 json/jsonl, `CROSS_SESSION_ANALYSIS.md` 등 |
| `runs/paper_2026-07/<session>/` | 2026-07 Paper 런타임 세션의 `logs/`, `status.json`, `reports/` (예전 루트의 `logs_<session>/`, `status_<session>.json`; 이름 없는 `logs/`·`status.json`은 `default`) |
| `runs/paper/default/` | `config/` 없이 `RuntimeConfig` 기본값으로 돌릴 때의 `logs/`, `status.json`, `reports/` |
| `reports/MOVES.json` | 이전 기록. 아래 참고 |

`runs/`는 `.gitignore`에 들어 있다. `config/runtime*.yaml`의 `logging.dir`, `status_path`, `reports_dir`는
`runs/paper_2026-07/<session>/` 아래를 가리킨다. 이 경로들은 config hash에 들어가지 않는다
(`tests/test_session_isolation.py::test_config_hash_ignores_ops_paths`).

## 2026-10-14 이전 문서의 경로

2026-10-14 전에 쓴 문서(사전등록, 결과 기록, 일지, `PROJECT_STATUS.md`의 지난 항목 등)는 예전 경로를 그대로
적고 있다. 그 문서들은 고치지 않는다. 예전 경로가 지금 어디 있는지는 `reports/MOVES.json`에서 찾는다.

`reports/MOVES.json`은 `{old, new, moved_at_utc}` 항목의 목록이다. 경로는 저장소 루트 기준이다.
폴더를 통째로 옮긴 경우 항목은 폴더 하나뿐이다. 예를 들어 예전 `reports/daytrade_paper/2026-10-06.json`은
`old: reports/daytrade_paper` → `new: reports/daytrade/paper` 항목을 보고 `reports/daytrade/paper/2026-10-06.json`으로 찾는다.
이 파일은 덧붙이기만 한다. 앞선 항목을 고치거나 지우지 않는다.

`config/daily_screen_v1.json`의 `outputs` 문구는 SHA-256이 사전등록에 고정돼 있어 예전 경로
(`reports/daily_screen_v1/...`)를 그대로 둔다. 실제 기본 출력 폴더는 `reports/research/daily_screen_v1/`이다.

`analysis/screener_ranking_h4.py`는 H4 사전등록 v1.4의 확정 실행본이라 고치지 않는다. 다시 실행하면
예전처럼 `reports/h4_screener_ranking.json`에 쓴다. 정리 대상이면 같은 이동 스크립트로 다시 옮긴다.

`src/entry_study_v1/cli.py`, `src/reversal_study_v1/cli.py`도 고치지 않는다. 두 파일은 OOS 잠금 해제 때
리뷰받은 구현 commit과 바이트가 같아야 하는 구현 고정 대상이다. 기본 출력은 예전처럼
`reports/entry_study_v1/`, `reports/reversal_study_v1/`이고, 이동 스크립트가 다시 `reports/research/`로 옮긴다.

사전등록 문서의 경로 문구도 그대로다. 예: `PREREG_DAILY_SAMEDAY_SCREEN_V1.md`·`..._HOLDOUT_ADDENDUM.md`의
`reports/daily_screen_v1/main/...`은 지금 `reports/research/daily_screen_v1/main/...`에 있고,
`PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json`의 `reports/premarket-daily-brief.<YYYYMMDD>.freeze.json`은
지금 `reports/premarket/days/<YYYYMMDD>/` 아래에 있다. 그 문서의 명령을 그대로 붙여 넣으면 파일이 없다고
거부하므로(안전한 실패), 경로만 새 구조로 바꿔 실행한다.

## 옮기는 방법 (원래 체크아웃, 한 번만)

`scripts/migrate_output_layout.py`가 옮긴다. 분류 규칙은 `src/output_layout.py`의 `classify`를 쓴다.
루트의 `logs*/`·`status*.json`과 `reports/` 바로 아래 항목만 본다. `data/`, `datasets/`, 이미 새 구조에 있는 것은
건드리지 않는다. `.DS_Store`는 건너뛴다. 분류되지 않는 항목은 그 자리에 두고 목록으로 보여 준다.
옮길 자리에 이미 파일이 있으면 아무것도 옮기지 않고 멈춘다. 같은 디스크 안에서 `os.rename`으로 옮긴다.

launchd는 원래 체크아웃의 코드를 실행한다. fast-forward 하는 순간 새 코드가 살아나므로, 새 코드가 옛 구조에서
한 번이라도 돌면 안 된다. 예를 들어 post-close가 옛 자리의 FREEZE brief를 못 찾으면 `MISSING_FREEZE_BRIEF`
INCOMPLETE 영수증을 영구히 남긴다. 그래서 2~6을 한 번에, 작업을 모두 내린 상태에서 한다.

**시간대(KST)**: 15:45~20:50 또는 07:45~10:30. 그 밖에는 하지 않는다. 21:01 PRIMARY 전에 6단계까지 끝내야 한다. `daytrade-shadow-21`(21:50)은 미국 장 마감
(05:00~06:00)까지 살아 있고, history-collect(10:40·13:40)는 최대 2시간 걸린다(13:40 실행은 15:40까지).

1. 이 변경을 main에 병합한다.
2. 돌고 있는 작업이 없는지 확인한다. 아무것도 나오지 않아야 한다:
   `pgrep -fl 'run_daytrade_shadow|run_premarket_pipeline|run_freeze_paper_session|run_history_collection|run_daytrade_paper'`
   그다음 작업 18개를 모두 내린다:
   `for p in ~/Library/LaunchAgents/com.peg5325.ai-trading.premarket.*.plist; do launchctl bootout gui/$(id -u) "$p"; done`
3. 원래 체크아웃을 병합된 main으로 fast-forward 하고 전체 시험을 돌린다.
4. 미리 보기: `.venv/bin/python scripts/migrate_output_layout.py --root <체크아웃>`
   옮길 목록, 목적지별 개수, 분류되지 않은 항목이 나온다. 아무것도 바뀌지 않는다.
5. 실제로 옮긴다: `.venv/bin/python scripts/migrate_output_layout.py --root <체크아웃> --apply`
   옮긴 항목은 `reports/MOVES.json` 끝에 붙는다. 다시 실행하면 옮길 것이 0개여야 한다.
6. launchd 정의를 다시 설치하고 확인한다(로그 폴더가 `reports/premarket/launchd/`로 바뀌었다):
   `.venv/bin/python scripts/install_premarket_launchd.py --install` → `--status`
7. `reports/MOVES.json` 사본을 `docs/OUTPUT_LAYOUT_MOVES_2026-10-14.json`으로 커밋한다. `reports/`는 이 기기에만
   있으므로, 예전 경로 대응표가 기기와 함께 사라지지 않게 하려는 것이다. 경로만 담고 비밀정보는 없다.
