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

## Stage results

| Stage | Market bundle | Market defects | Boundary rows clipped | Input regime | Candidates | Brief status | Brief reason |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| PRIMARY | `OK` | 0 | 19 forming 1m, 28 trading-day daily | `RISK_ON` | 11 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |
| VERIFICATION | `OK` | 0 | 22 forming 1m, 28 trading-day daily | `RISK_ON` | 11 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |
| FREEZE | `OK` | 0 | 24 forming 1m, 28 trading-day daily | `RISK_ON` | 8 | `NO_TRADE` | `GENERATED_AFTER_STAGE_WINDOW` |

## Artifact hashes

| Stage | Market bundle SHA-256 | Normalized input v4 SHA-256 | Daily Brief v4 SHA-256 |
| --- | --- | --- | --- |
| PRIMARY | `5a4ed6de3d7a9a32cf0a9045ba8e60995544bf68690471930c3b106d371a867b` | `881b13a449c5dff7a1d5f1387a2e45403ae0e26657309a2b90d58ea89e117fe1` | `0e76bc37e1d3ba95c4ef0515fd51d224081b7669e394e91398d83ea9cbb786f0` |
| VERIFICATION | `ffec834f56fec1fc17786b2f8fb086aa70f65228af28f3bfa02bb20fa08e23d1` | `42745ecab6dff5d0f29f173c42f4fbf193b6610f4573f9517f086eb4de4458a9` | `f47992e02fe375176f0e0d041a4cc9a4dc435b2f0af853a5207fb34731d4acd2` |
| FREEZE | `112eb5b377c5e19afe190a1d4419decc7a9c89278c27cc6418193dedada53424` | `de842932b35e014bcd54257364c8ce6356c1e591acd8f38d30d065af05ab32c6` | `410a02317a9dc0847f2481d82e680af539f826e2ae116389ac84f947585a7cae` |

## Safety boundary

- No orders were generated.
- `executable` is `false`.
- `promotion_allowed` is `false`.
- Broker/live account modules were not used.
- No PnL, returns, forward returns, strategy performance, or OOS metrics were calculated.
- The replay reused existing market bundles; no additional market API recollection was required for v4 input/brief generation.
