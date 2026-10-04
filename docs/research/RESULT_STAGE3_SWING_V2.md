# RESULT stage 3 swing v2

- status: **NO_PASS**
- prereg blob SHA: `7933dc0c2529bd37b959aa43f5b2870d92bc8380`
- code commit: `fb4d9b1e98b7acd1eb06b14d97ebd01357d38039` (dirty: False)
- data: 1993-02-01 .. 2025-07-18 (8172 rows)

## Raw file SHA-256

- `VIX_History.csv`: `2b28877e77b1b4b76875afb1a84c3a06eced2120a651c3b5ff7d91b32fa660b5`
- `ff_daily.zip`: `2f29e22546069914890a712680a6f81f3680ebc3543b52864484d209ca13a7db`
- `yahoo_IDX_GSPC.json`: `69e98d5533b3cc50b870878d3591445182cbe2534d98eccc3aee0af752e4247a`
- `yahoo_IDX_NDX.json`: `4aa66d83bb38ff2b0985c26eab7b52656d1f5a20fe41594b369044b348fb5306`
- `yahoo_IDX_SP500TR.json`: `d45f6a111733d4e74950aba95da063dbf1ca069ad6bbae51258b3caa031dc8c3`
- `yahoo_IDX_VIX.json`: `6dd651fd81f1f3cfab94cdc0ee9ab1066253e9749ca0806ded37b43769c33223`
- `yahoo_PSQ.json`: `03e6484b3a26a144835b85a67576713e4b00f9d60b3c6e1d73f43a9e8c21ff1e`
- `yahoo_QLD.json`: `bf6cf27688c77d358fc2519f9add0d1ce46b07990716db6fe5889d402301c44b`
- `yahoo_QQQ.json`: `c6a6a0b152d01f410866aa0f98df337bcd0897b92d6804839a8c676286d2d292`
- `yahoo_SH.json`: `ff12ecfade457aa987347a7b116c24add797f2da80c1da24714cdd1833d65b89`
- `yahoo_SPY.json`: `35a1c1c328990fa9f072ea4d07a3143cfce893b5b995e5615e60f17a7546e33c`
- `yahoo_SSO.json`: `522e443610a12d5043e900eb3ff1796eac583938721948d75c264a622b5812f5`
- `yahoo_TQQQ.json`: `1b52549becd44b569e13aa8a40267568cf43c72bd99b2f60d3e3d723c5b400c8`
- `yahoo_UPRO.json`: `592d302b0b56867acb35732f203bdebe8ee574cd543194d4861b01866ca781d1`

## Calibration gate: PASS

| ETF | days | weeks | ann synthetic | ann actual | diff | corr weekly (gate) | corr daily (ref) | pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | 8172 | 1694 | 10.6% | 10.5% | +0.02pp | 0.9954 | 0.9847 | True |
| QQQ | 6630 | 1376 | 10.2% | 10.2% | +0.00pp | 1.0000 | 1.0000 | True |
| SSO | 4798 | 996 | 14.9% | 14.7% | +0.22pp | 0.9988 | 0.9954 | True |
| UPRO | 4040 | 839 | 32.2% | 31.8% | +0.46pp | 0.9995 | 0.9982 | True |
| SH | 4798 | 996 | -12.4% | -11.2% | -1.21pp | 0.9982 | 0.9954 | True |
| QLD | 4798 | 996 | 24.8% | 24.2% | +0.64pp | 0.9991 | 0.9958 | True |
| TQQQ | 3881 | 806 | 42.4% | 41.5% | +0.93pp | 0.9997 | 0.9989 | True |
| PSQ | 4798 | 996 | -17.2% | -16.5% | -0.79pp | 0.9979 | 0.9926 | True |

^SP500TR vs SPY TR + ER: ann diff +0.02pp, corr weekly 0.9954, corr daily 0.9847

## Index sp

| rule | P_reach | P_stop | median days | TWR | MDD | orders | dP | p_holm | eras | stress c1+c2 | same-close P_reach | after-tax P_reach | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 47.3% | 52.7% | 1200 | 10.5% | -55.3% | 1 | - | - | - | - | 47.3% | 47.3% | baseline |
| 2 | 41.1% | 58.9% | 471 | 13.6% | -88.2% | 1 | -6.2pp | 1.0000 | 3/3 | False | 41.1% | 41.1% | fail |
| 3 | 64.3% | 35.7% | 1033 | 12.1% | -48.1% | 225 | +17.1pp | 1.0000 | 2/3 | False | 45.7% | 64.3% | fail |
| 3i | 29.1% | 70.9% | 575 | 7.4% | -58.4% | 449 | -18.2pp | 1.0000 | 1/3 | False | 19.0% | 29.1% | fail |
| 4 | 46.9% | 53.1% | 383 | 16.0% | -64.8% | 225 | -0.4pp | 1.0000 | 3/3 | False | 30.6% | 46.9% | fail |
| 5 | 88.0% | 11.6% | 1058 | 14.2% | -49.5% | 45 | +40.7pp | 1.0000 | 2/3 | True | 89.5% | 88.0% | fail |
| 5i | 61.6% | 38.4% | 910 | 11.2% | -57.3% | 89 | +14.3pp | 1.0000 | 2/3 | False | 71.3% | 61.6% | fail |
| 6 | 51.9% | 43.4% | 915 | 13.7% | -59.2% | 23 | +4.7pp | 1.0000 | 2/3 | False | 50.0% | 51.9% | fail |
| 6i | 54.7% | 45.3% | 1008 | 11.4% | -65.9% | 45 | +7.4pp | 1.0000 | 2/3 | False | 53.5% | 54.7% | fail |
| 7 | 49.6% | 50.4% | 844 | 11.5% | -59.7% | 378 | +2.3pp | 1.0000 | 2/3 | False | 50.4% | 49.6% | fail |
| 8 | 72.9% | 0.0% | 1504 | 9.9% | -32.6% | 500 | +25.6pp | 1.0000 | 2/3 | True | 65.5% | 72.9% | fail |
| 9 | 10.5% | 0.0% | 2326 | 3.2% | -28.8% | 248 | -36.8pp | 1.0000 | 0/3 | False | 8.9% | 10.5% | fail |
| 10 | 0.0% | 0.0% | - | 3.2% | -31.0% | 348 | -47.3pp | 1.0000 | 0/3 | False | 0.0% | 0.0% | fail |
| 11 | 5.0% | 59.7% | 1787 | 1.5% | -43.4% | 756 | -42.2pp | 1.0000 | 0/3 | False | 5.0% | 5.0% | fail |
| 12 | 38.4% | 61.6% | 696 | 9.8% | -65.3% | 64 | -8.9pp | 1.0000 | 2/3 | False | 38.4% | 38.4% | fail |

Inverse holding 3i: 2044 days, log contribution -1.011

Inverse holding 5i: 1944 days, log contribution -0.770

Inverse holding 6i: 1646 days, log contribution -0.609

## Index ndx

| rule | P_reach | P_stop | median days | TWR | MDD | orders | dP | p_holm | eras | stress c1+c2 | same-close P_reach | after-tax P_reach | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 61.6% | 38.4% | 833 | 14.1% | -83.0% | 1 | - | - | - | - | 61.6% | 61.6% | baseline |
| 2 | 57.4% | 42.6% | 380 | 17.0% | -98.8% | 1 | -4.3pp | 1.0000 | 2/3 | False | 57.4% | 57.4% | fail |
| 3 | 23.6% | 76.4% | 239 | 20.1% | -72.3% | 221 | -38.0pp | 1.0000 | 3/3 | False | 44.6% | 23.6% | fail |
| 3i | 19.0% | 81.0% | 199 | 14.8% | -72.1% | 441 | -42.6pp | 1.0000 | 1/3 | False | 17.4% | 19.0% | fail |
| 4 | 36.8% | 63.2% | 215 | 24.4% | -87.9% | 221 | -24.8pp | 1.0000 | 3/3 | False | 29.5% | 36.8% | fail |
| 5 | 47.3% | 52.7% | 357 | 22.5% | -64.2% | 53 | -14.3pp | 1.0000 | 2/3 | False | 47.3% | 47.3% | fail |
| 5i | 41.9% | 58.1% | 332 | 19.1% | -65.6% | 105 | -19.8pp | 1.0000 | 1/3 | False | 36.0% | 41.9% | fail |
| 6 | 54.3% | 45.7% | 473 | 21.2% | -84.0% | 35 | -7.4pp | 1.0000 | 2/3 | False | 45.7% | 54.3% | fail |
| 6i | 39.5% | 60.5% | 398 | 18.5% | -87.9% | 69 | -22.1pp | 1.0000 | 2/3 | False | 39.5% | 39.5% | fail |
| 7 | 56.2% | 43.8% | 726 | 14.0% | -56.3% | 378 | -5.4pp | 1.0000 | 1/3 | False | 54.3% | 56.2% | fail |
| 8 | 74.4% | 24.8% | 1114 | 11.9% | -35.0% | 498 | +12.8pp | 1.0000 | 0/3 | True | 72.5% | 74.4% | fail |
| 9 | 0.0% | 0.0% | - | 2.6% | -26.6% | 244 | -61.6pp | 1.0000 | 0/3 | False | 19.4% | 0.0% | fail |
| 10 | 16.7% | 24.8% | 743 | 4.3% | -54.0% | 376 | -45.0pp | 1.0000 | 0/3 | False | 23.3% | 16.7% | fail |
| 11 | 7.0% | 62.0% | 392 | 2.5% | -69.9% | 756 | -54.7pp | 1.0000 | 0/3 | False | 7.0% | 7.0% | fail |
| 12 | 48.1% | 51.9% | 586 | 8.2% | -94.0% | 64 | -13.6pp | 1.0000 | 0/3 | False | 48.1% | 48.1% | fail |

Inverse holding 3i: 1918 days, log contribution -1.097

Inverse holding 5i: 2042 days, log contribution -0.798

Inverse holding 6i: 1645 days, log contribution -0.666

## Bootstrap

- stationary bootstrap, mean block 250, reps 1000, seed 20261002, Holm over 28 comparisons at alpha 0.05

## Verdict: NO_PASS

Passing: none

Passing does not authorise Paper or live orders (Charter section 4).

## Limitations

- Thresholds are monitored on daily closes only; intraday crossings are missed.
- Rules 7 and 8 hold a drifting 3x sleeve rebalanced monthly (and on 200-day switches for 8); full-weight rules have no drift.
- Cash earns 0% (Charter section 3); no validated interest rate.
- Post-tax view is approximate: gains realised only on sells, tax paid at calendar-year end, no loss carry-forward, unrealised gains untaxed.
- Pre-1999-03 NDX return is price-only (no dividends), a bias against longs.
- The 10-year horizon is 2520 trading days.
