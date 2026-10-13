# 부록: 일봉 당일 거래 선별 v1 — 주 묶음 HOLDOUT 선택 고정

사전등록 [PREREG_DAILY_SAMEDAY_SCREEN_V1.md](PREREG_DAILY_SAMEDAY_SCREEN_V1.md) §8의 부록. 내용은 사전등록이 허용한 네 가지뿐이며 규칙·본문은 바꾸지 않는다.

## ① 선택된 후보

selected: [128]

| k | 지수 | 조건(§4 그대로) | 방향 | 거래 ETF |
| --- | --- | --- | --- | --- |
| 128 | ^SOX | C07: `g > 0` 그리고 `\|g\| ≥ mg` (큰 상승 갭) | 롱 | SOXL |

## ② 실행 산출물 SHA-256

| 파일 | SHA-256 |
| --- | --- |
| `config/daily_screen_v1.json` | `e015dd587f8d8ceb60b6ec1425d846213a0f42b69aff7fd62e29bbebaf92bcfa` |
| `reports/daily_screen_v1/main/precount.json` | `31eb5c89df46a8ddbd8a256c2f7c3e1ceae66a5ca420ef4dbbd15a94514b7038` |
| `reports/daily_screen_v1/main/dev_result.json` | `3417ca04e76bb660470c34a0cfca0efb810d433fdc3995a3e054e578421d711a` |

## ③ 데이터 SHA-256

| 파일 | SHA-256 |
| --- | --- |
| `data/external/longhist/manifest.json` | `cca29640d110da0445676666012920fec7fedd01d36862db7681fadf36f5cbbf` |
| `config/longhist_events_v1.yaml` | `2e46f3d6a687adf528a939b3104330de793fc30a25c14adc0b8d65d3fd84116b` |

## ④ 고정 문턱 (DEV 값, HOLDOUT에서 다시 계산하지 않음)

| 항목 | 값 |
| --- | --- |
| ^SOX `mg`(DEV \|g\| 중앙값) | 0.004003308912217229 |
