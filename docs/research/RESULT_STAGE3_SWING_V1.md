# RESULT stage 3 swing v1

- status: **CALIBRATION_FAIL**
- prereg blob SHA: `177eafef0534ba45ef46a3a739b05f3b97684e04`
- code commit: `d8d6f9105b6741f429543b651cd3301e51427571` (dirty: False)
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

## Calibration gate: CALIBRATION_FAIL

| ETF | days | ann synthetic | ann actual | diff | corr | pass |
| --- | --- | --- | --- | --- | --- | --- |
| SPY | 8172 | 10.6% | 10.5% | +0.02pp | 0.9847 | False |
| QQQ | 6630 | 10.2% | 10.2% | +0.00pp | 1.0000 | True |
| SSO | 4798 | 14.9% | 14.7% | +0.22pp | 0.9954 | True |
| UPRO | 4040 | 32.2% | 31.8% | +0.46pp | 0.9982 | True |
| SH | 4798 | -12.4% | -11.2% | -1.21pp | 0.9954 | True |
| QLD | 4798 | 24.8% | 24.2% | +0.64pp | 0.9958 | True |
| TQQQ | 3881 | 42.4% | 41.5% | +0.93pp | 0.9989 | True |
| PSQ | 4798 | -17.2% | -16.5% | -0.79pp | 0.9926 | True |

^SP500TR vs SPY TR + ER: ann diff +0.02pp, corr 0.9847

No strategy result was computed.
