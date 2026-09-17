# Premarket Normalization Replay — 2026-09-15

## Scope

This document records a retrospective reconstruction of the 2026-09-15
PRIMARY, VERIFICATION, and FREEZE premarket normalization artifacts.

It is calculation evidence only. It is not a clean live validation, trading
recommendation, strategy performance report, PnL analysis, forward-return
study, or OOS claim.

## Replay classification

All normalized inputs are classified as `RETROSPECTIVE_RECONSTRUCTION`.
The Toss market snapshots and local calculations succeeded, but the
normalization/brief artifacts were generated after each original stage window.
Therefore every operational Daily Brief remains fail-closed as `NO_TRADE`.

The sanitized deterministic receipt for this replay is
`PREMARKET_NORMALIZATION_REPLAY_20260915_RECEIPT.json`.
Its normalized SHA-256 is
`4dc9a542d493575fec46db78dcadde85d4a3a2b1cc516a112b24163dec52d055`.

## Stage results

| Stage | Market bundle | Market defects | Boundary rows clipped | Input regime | Candidates | Brief status | Brief reason |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| PRIMARY | `OK` | 0 | 19 forming 1m, 28 trading-day daily | `RISK_ON` | 11 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |
| VERIFICATION | `OK` | 0 | 22 forming 1m, 28 trading-day daily | `RISK_ON` | 11 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |
| FREEZE | `OK` | 0 | 24 forming 1m, 28 trading-day daily | `RISK_ON` | 8 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |

## Artifact hashes

| Stage | Market bundle SHA-256 | Normalized input v5 SHA-256 | Daily Brief v5 SHA-256 |
| --- | --- | --- | --- |
| PRIMARY | `5a4ed6de3d7a9a32cf0a9045ba8e60995544bf68690471930c3b106d371a867b` | `443af378f4b2a5a1c11b359309420d7b8b0e4eb49586eb979b5b2253b6ef3eae` | `8329badadae4e394414979896f3d75510014f75e0f1b91d4ec93530213a8aaeb` |
| VERIFICATION | `ffec834f56fec1fc17786b2f8fb086aa70f65228af28f3bfa02bb20fa08e23d1` | `10b2aac4ffa1f8968a40502b868a14b353030aa3f1ae43e1105955748e0501a9` | `f98e4ba23ec52e4a2a541008c36b331357c62b11b4ac4dde5a9eb489ed79f3da` |
| FREEZE | `112eb5b377c5e19afe190a1d4419decc7a9c89278c27cc6418193dedada53424` | `b5ad1bf0c5b575482b17d155ce9a396d22685e3d4eed8470f80c331b0d503b1d` | `e30e142e7a9727d37ccef9193cd5fa5cb8ab7b6d3a4bab37652edd3130810a5e` |

## Safety boundary

- No orders were generated.
- `executable` is `false`.
- `promotion_allowed` is `false`.
- Broker/live account modules were not used.
- No PnL, returns, forward returns, strategy performance, or OOS metrics were calculated.
- The replay reused existing market bundles; no additional market API recollection was required for v5 input/brief generation.
