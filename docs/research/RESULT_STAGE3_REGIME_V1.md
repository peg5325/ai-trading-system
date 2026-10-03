# RESULT stage 3 regime v1

- verdicts: sp: **FAIL**, ndx: **FAIL**
- stop recommendation (criterion 3 fails for both indices): **False**
- confirm window: 2020-01-02 .. 2025-07-18 (fresh USD 2,000)
- prereg blob SHA: `e3a9672dcf3cbf9eab2f326902cb616625369007`
- selection blob SHA: `8c9d8e64c99dbcfcbfe2a7aa9b95310cabbe1be2`
- code commit: `6eb4bb75b3bb2ac912e584a054f0cc9ebcdfddbb` (dirty: False)
- timing: Regime decided at the close of t; exposure set at the close of t+1 (earns t+2 onward). Per-regime sums label each day by the regime that decided the position held that day; each day's log factor includes the trade cost charged at that day's close. The initial buy cost at the opening close is in the totals but in no regime's sum.

## sp: FAIL

- combo: A=1, B=1, C=0, D=0
- criteria: c1 TWR > 1x **False**, c2 max DD < 35% **True**, c3 some regime > 1x **False**

### base (mode lag, cost 0.15%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 3,156 | 8.6% | -25.0% | 0.4561 | no |
| 1x | 4,185 | 14.3% | -33.8% | 0.7384 | 2024-12-04 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 722 | 0.0887 | 0.0962 | -0.0075 | False |
| B | 329 | 0.3974 | 0.4184 | -0.0210 | False |
| C | 281 | -0.0225 | 0.2074 | -0.2299 | False |
| D | 60 | -0.0060 | 0.0179 | -0.0239 | False |

### stress_cost (mode lag, cost 0.25%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 3,035 | 7.8% | -26.5% | 0.4170 | no |
| 1x | 4,181 | 14.2% | -33.8% | 0.7374 | 2024-12-04 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 722 | 0.0837 | 0.0962 | -0.0125 | False |
| B | 329 | 0.3834 | 0.4184 | -0.0350 | False |
| C | 281 | -0.0375 | 0.2074 | -0.2449 | False |
| D | 60 | -0.0100 | 0.0179 | -0.0279 | False |

### same_close (mode same, cost 0.15%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 3,112 | 8.3% | -21.7% | 0.4422 | no |
| 1x | 4,185 | 14.3% | -33.8% | 0.7384 | 2024-12-04 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 722 | 0.2292 | 0.2367 | -0.0075 | False |
| B | 329 | 0.2430 | 0.2640 | -0.0210 | False |
| C | 281 | -0.0225 | -0.2152 | +0.1926 | True |
| D | 60 | -0.0060 | 0.4543 | -0.4603 | False |

### explore (SELECTED, feasible 12/625)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 4,598 | 6.4% | -24.6% | 0.8324 | 2017-11-28 |
| 1x | 6,611 | 9.3% | -55.3% | 1.1956 | 2016-04-19 |

## ndx: FAIL

- combo: A=1, B=1, C=0, D=2
- criteria: c1 TWR > 1x **False**, c2 max DD < 35% **True**, c3 some regime > 1x **True**

### base (mode lag, cost 0.15%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 5,113 | 18.5% | -29.2% | 0.9387 | 2024-06-12 |
| 1x | 5,360 | 19.5% | -35.1% | 0.9858 | 2024-01-23 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 711 | 0.2332 | 0.2392 | -0.0060 | False |
| B | 361 | 0.6698 | 0.6833 | -0.0135 | False |
| C | 273 | -0.0420 | -0.0129 | -0.0291 | False |
| D | 47 | 0.0792 | 0.0777 | +0.0015 | True |

### stress_cost (mode lag, cost 0.25%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 4,781 | 17.0% | -29.7% | 0.8715 | 2024-07-10 |
| 1x | 5,355 | 19.4% | -35.1% | 0.9848 | 2024-01-23 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 711 | 0.2292 | 0.2392 | -0.0100 | False |
| B | 361 | 0.6608 | 0.6833 | -0.0225 | False |
| C | 273 | -0.0701 | -0.0129 | -0.0572 | False |
| D | 47 | 0.0541 | 0.0777 | -0.0235 | False |

### same_close (mode same, cost 0.15%)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 6,706 | 24.4% | -21.9% | 1.2099 | 2023-06-15 |
| 1x | 5,360 | 19.5% | -35.1% | 0.9858 | 2024-01-23 |

| regime | days | strategy log | 1x log | excess | beats 1x |
| --- | --- | --- | --- | --- | --- |
| A | 711 | 0.3634 | 0.3694 | -0.0060 | False |
| B | 361 | 0.5634 | 0.5769 | -0.0135 | False |
| C | 273 | -0.0420 | -0.1604 | +0.1183 | True |
| D | 47 | 0.3266 | 0.2014 | +0.1253 | True |

### explore (SELECTED, feasible 12/625)

| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |
| --- | --- | --- | --- | --- | --- |
| strategy | 10,869 | 13.4% | -32.2% | 1.6927 | 2012-09-06 |
| 1x | 12,258 | 14.4% | -53.4% | 1.8131 | 2013-07-11 |

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

Passing is not order approval (Charter section 4). Blind OOS was not opened.
