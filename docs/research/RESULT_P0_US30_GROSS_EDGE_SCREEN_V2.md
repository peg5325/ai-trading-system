# P0-US30 Intraday Gross-Edge Screen v2 — 결과

- verdict: **NO_GROSS_EDGE**
- inconclusive reason: 없음
- 거래 평가: 2023-05-18~2026-01-20 (670 거래일, 제외 블록 44일, 평가 619일)
- data end: 2026-01-20
- blind OOS accessed: `false`
- holdout H2 accessed: `false`
- prereg JSON SHA-256: `e94024a207ed607062bab21e791012cdc7c0e36f111444babc920f6c44787c4c`
- prereg MD SHA-256: `5cd63e4ce048a7baae803b075c78bca88b598d6666f65969849432bf76517d48`
- implementation commit: `6ddd42b2b4e5115487d32cf172962f67246fdf97`
- payload SHA-256: `ee0a842f28dc98edc388caf545a571554fc87d67044f0bd47bd02e5bd7f6d362`

## 판정

- SHORTLIST (LONG 후보): 없음
- INVERSE_ONLY: 없음
- SAMPLE (G1·G2 미달): C1
- 이 결과는 blind OOS 개봉·H2 접근·Paper 실행을 자동 승인하지 않는다.

## 무결성

- manifest_data_receipt: `match`
- raw_content_index: `match`
- snapshot_content_receipt: `match`
- snapshot_file: `match`
- snapshot_receipt_file: `match`

## 데이터 품질

- 무효 symbol-day: 0 / 12380 (0.000000, 한도 0.05)
- max loaded day: 2026-01-20
- evaluation re-run (reversed input) identical: `true`

## arm별 결과

| arm | status | trades | days | mean %p | G1 | G2 | G3 | G4 | G5 | G6 | G7 | G8 | raw p | Holm p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 | FAIL | 185 | 152 | 0.137090 | Y | Y | N | N | N | N | N | N | 0.971903 | 1 |
| A2 | FAIL | 418 | 234 | -0.014185 | Y | Y | N | N | N | N | N | N | 1 | 1 |
| B1 | FAIL | 1245 | 412 | -0.037236 | Y | Y | N | N | N | N | N | N | 1 | 1 |
| B2 | FAIL | 626 | 296 | -0.032618 | Y | Y | N | N | N | N | N | N | 1 | 1 |
| B3 | FAIL | 2529 | 555 | 0.029636 | Y | Y | N | N | N | N | N | N | 1 | 1 |
| C1 | SAMPLE | 99 | 54 | -0.014436 | N | Y | N | N | N | N | N | N | 0.89831 | 1 |
| C2 | FAIL | 697 | 267 | 0.059097 | Y | Y | N | N | N | N | N | N | 0.905509 | 1 |
| D1 | FAIL | 407 | 199 | 0.277330 | Y | Y | N | N | N | N | N | Y | 0.0813919 | 0.651135 |

## 인버스·비인버스 다리

| arm | leg | trades | days | mean %p | G3 | G4 | G5 | G7 | G8 |
|---|---|---|---|---|---|---|---|---|---|
| A1 | non_inverse | 95 | 82 | -0.132603 | N | N | N | N | N |
| A1 | inverse | 90 | 70 | 0.421765 | N | N | Y | Y | Y |
| A2 | non_inverse | 418 | 234 | -0.014185 | N | N | N | N | N |
| B1 | non_inverse | 1112 | 404 | -0.062061 | N | N | N | N | N |
| B1 | inverse | 133 | 107 | 0.170316 | N | N | N | N | N |
| B2 | non_inverse | 572 | 291 | -0.073383 | N | N | N | N | N |
| B2 | inverse | 54 | 46 | 0.399191 | N | N | N | N | N |
| B3 | non_inverse | 2424 | 525 | 0.002372 | N | N | N | N | N |
| B3 | inverse | 105 | 75 | 0.659035 | Y | Y | Y | Y | Y |
| C1 | non_inverse | 99 | 54 | -0.014436 | N | N | N | N | N |
| C1 | inverse | 0 | 0 | n/a | N | N | N | N | N |
| C2 | non_inverse | 639 | 227 | -0.029399 | N | N | N | N | N |
| C2 | inverse | 58 | 47 | 1.034083 | Y | N | Y | N | N |
| D1 | non_inverse | 353 | 169 | 0.296020 | N | N | N | N | Y |
| D1 | inverse | 54 | 36 | 0.155158 | N | N | N | N | N |

## 진단 (판정 비반영)

| arm | baseline mean %p | mean − baseline | best day | best-day contribution | after 0.20 | after 0.30 | after 0.34 | after 0.51 |
|---|---|---|---|---|---|---|---|---|
| A1 | -0.018541 | 0.155631 | 2025-03-03 | 0.040843 | -0.062910 | -0.162910 | -0.202910 | -0.372910 |
| A2 | -0.034918 | 0.020733 | 2025-02-06 | 0.007701 | -0.214185 | -0.314185 | -0.354185 | -0.524185 |
| B1 | 0.021771 | -0.059007 | 2024-10-15 | 0.013400 | -0.237236 | -0.337236 | -0.377236 | -0.547236 |
| B2 | 0.023343 | -0.055961 | 2025-10-10 | 0.024183 | -0.232618 | -0.332618 | -0.372618 | -0.542618 |
| B3 | 0.019596 | 0.010040 | 2025-11-20 | 0.010528 | -0.170364 | -0.270364 | -0.310364 | -0.480364 |
| C1 | 0.039838 | -0.054275 | 2025-04-07 | 0.044304 | -0.214436 | -0.314436 | -0.354436 | -0.524436 |
| C2 | 0.016630 | 0.042467 | 2025-04-08 | 0.037847 | -0.140903 | -0.240903 | -0.280903 | -0.450903 |
| D1 | 0.021245 | 0.256086 | 2025-04-10 | 0.026577 | 0.077330 | -0.022670 | -0.062670 | -0.232670 |

## 결측·스킵 집계

- A1: no_signal=1053
- A2: missing_required_bar=3, no_signal=7626
- B1: missing_required_bar=4, no_fill=609, no_signal=10383, t_minus_1_invalid=139
- B2: missing_required_bar=4, no_fill=251, no_signal=11360, t_minus_1_invalid=139
- B3: missing_required_bar=4, no_fill=4769, no_signal=4938, t_minus_1_invalid=139, undefined_indicator=1
- C1: missing_required_bar=4, no_fill=183, no_signal=9617, t_minus_1_invalid=139, undefined_indicator=2338
- C2: missing_required_bar=4, no_fill=615, no_signal=10925, t_minus_1_invalid=139
- D1: missing_required_bar=3, no_signal=5099, t_minus_1_invalid=62
