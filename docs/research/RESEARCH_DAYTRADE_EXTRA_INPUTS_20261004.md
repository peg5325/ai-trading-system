# 데이트레이딩 "오늘 장 상황" 판단용 추가 입력 조사 (2026-10-04)

- 대상: 신호 QQQ/SPY, 매매 TQQQ/SQQQ, 당일 청산, 하루 0~2회, 왕복 비용 약 0.3%, 규칙만으로 결정(LLM 판단 없음).
- 데이터 경로: Toss 오픈API(미국·한국 호가, 1분봉, 실시간 체결/호가 웹소켓; 옵션 데이터 없음) + 무료/저가 외부 소스.
- 이미 채택된 입력(전일 20/50/200 이평, ATR14, 프리마켓 갭, 첫 15분 범위·VWAP·RVOL, VIX·VIX9D/VIX, 경제지표 일정, 메가캡 실적, 레버리지 ETF 종가 리밸런싱)은 다시 제안하지 않고, 근거가 있으면 보강만 한다.
- 이 문서는 조사 결과다. 효과를 증명한 것이 아니며, 채택은 사전등록 + Paper 검증을 거쳐야 한다.

## 한 줄 결론

새로 넣을 가치가 있는 것은 **딜러 감마 상태(GEX, 무료 일별 데이터 2011~)**, **QQQ 상위 종목으로 직접 계산하는 장중 상승/하락 종목 비율(breadth)**, **월간 옵션 만기·조기폐장 달력 플래그** 정도이고, 나머지 대부분은 데이터가 비싸거나, 시간대가 안 맞거나, 최근 연도에 효과가 사라졌다.

## 요약 표

판정 기준: CORE = v1 필수 / USEFUL = 근거 충분, 넣을 만함 / CANDIDATE = Paper에서 시험 / NOISE = 넣지 않음.

| 후보 | 판정 | 무료 여부 | 과거 검증 가능 | 용도 |
|---|---|---|---|---|
| 딜러 감마(GEX) 부호·크기 | USEFUL | 무료(일별, SqueezeMetrics CSV) | 가능, 2011-05~ 일별 | 추세형 진입 허용/차단, 크기 |
| 0DTE 장중 포지션(실시간 GEX) | NOISE(v1) | 유료($99~$299/월, API 제한) | 어려움(비쌈) | — |
| 풋/콜 비율 | NOISE | 무료(일별) | 2006~2019 CSV, 이후 단절 | — |
| 장중 breadth (자체 계산: QQQ 상위 종목 상승 비율) | CANDIDATE(강) | 무료(Toss 실시간) | 가능, Alpaca 무료 1분봉 2016~ | 방향 확인, 무거래 필터 |
| NYSE $TICK/$TRIN/$ADD 원본 | NOISE(v1) | 유료 플랫폼 필요, API 불명확 | 비쌈/불명확 | 위 자체 계산으로 대체 |
| 메가캡(Mag7) 쏠림 | CANDIDATE | 무료(Toss) | 가능 | breadth와 합쳐 "소수 종목 장" 감지 |
| 시가/종가 동시호가 불균형(NOII) | NOISE(v1) | 유료($49/월~) | 유료 | 판단 시각(09:45)과 안 맞음 |
| 야간 vs 장중 수익 패턴 | 기존 갭 입력 보강 | — | — | 갭 되돌림 근거 |
| ES/NQ 선물 야간 범위·베이시스 | NOISE | 선물 데이터 유료 | 유료 | Toss PRE 세션과 중복 |
| VVIX | CANDIDATE(약) | 무료(일별 CSV 2006~) | 가능 | 극단값일 때 크기 축소 |
| SKEW | NOISE | 무료 | 가능 | — |
| MOVE | NOISE | — | — | 금리 계열은 이미 기각 |
| 월초/월말(turn-of-month) | NOISE | 무료(달력) | 가능 | — |
| 월말 연기금 리밸런싱 | CANDIDATE(약) | 무료(달력+월간 수익률) | 가능 | 월말 4일 방향 편향 |
| 요일 효과 | NOISE | 무료 | 가능 | — |
| 공휴일 전날 효과 | NOISE(방향) / 조기폐장일 무거래는 USEFUL | 무료 | 가능 | 조기폐장일 무거래 |
| 월간 옵션 만기(OPEX)·쿼드러플 위칭 | CANDIDATE | 무료(달력) | 가능 | 크기 축소/추세 진입 억제 |
| CTA·연기금 흐름 추정치 | NOISE | 증권사 리포트(접근 불가) | 불가 | — |
| 공매도 거래량·다크풀(DIX) | NOISE | 무료 | 가능 | 일 단위 아님 |
| ETF 자금 유출입 | NOISE | 일부 무료, 다음날 공개 | 제한적 | — |
| 전일 종가 위치·Inside day·NR7 | CANDIDATE(약) | 무료(일봉) | 가능 | 예상 변동폭 → 크기 |
| 유가·금리 장중 급변 | NOISE | — | — | 이미 기각한 계열 |
| 연준 인사 발언 일정 | CANDIDATE(약) | 무료(연준 일정) | 가능(날짜만) | 의장 발언 시간대 무거래 |
| 국채 입찰 | NOISE | 무료 | 가능 | — |

## 후보별 상세

### 1. 딜러 감마 노출(GEX) — USEFUL
- **뜻**: GEX는 옵션 딜러(마켓메이커)가 헤지 때문에 가격이 오르면 팔고 내리면 사야 하는지(양의 감마), 반대로 따라 사고팔아야 하는지(음의 감마)를 추정한 값이다. 양의 감마가 크면 "되돌림·좁은 범위" 날, 음이면 "추세·넓은 범위" 날이 되기 쉽다.
- **근거(신뢰 높음)**: Baltussen·Da·Lammers·Martens(2021, JFE)는 1974~2020년 60여 개 선물에서 장중 모멘텀이 감마 헤지 수요(옵션 딜러 + 레버리지 ETF)와 연결된다고 보였다. Dim·Eraker·Vilkov(2024, SSRN/WFA)는 0DTE 시대 S&P500에서 딜러 감마가 평균적으로 **양수**이고, 양수일수록 장중 변동성이 낮고 **되돌림**이 강해지며, 음수면 **모멘텀**이 강해진다고 보고했다. 사용자가 "첫 30분→마지막 30분 모멘텀은 0DTE 시대에 사라졌다"고 기각한 판단과 일치하는 설명이다. Cboe 연구 노트도 0DTE가 변동성을 키운다는 증거가 약하다고 정리했다(거래소 자료라 이해충돌 있음, 신뢰 중).
- **주의**: GEX는 "딜러가 콜을 사고 풋을 팔았다"는 가정에 의존하는 **추정치**다. 벤더마다 값이 다르다. SqueezeMetrics GEX는 S&P500 지수 옵션 기준이라 QQQ와 완전히 같지 않다. 전일 미결제약정(OI) 기준이라 당일 0DTE 거래는 반영되지 않는다.
- **데이터**: SqueezeMetrics가 일별 DIX·GEX를 무료 CSV로 공개한다. 2026-10-04에 직접 받아 확인: 2011-05-02 ~ 2026-10-02, 컬럼 `date,price,dix,gex`. 전일 장 마감 후 갱신되므로 09:45 전에 사용 가능. 직접 계산 대안으로 Cboe 무료 지연 옵션체인 JSON(`cdn.cboe.com/api/global/delayed_quotes/options/SPY.json`, QQQ도 동일 형식)에 OI·감마가 들어있음을 확인했다. 단, Cboe 지연 시세는 이용약관상 자동 수집·재배포 제한이 있을 수 있어 사용 전 약관 확인이 필요하다.
- **유료 대안**: SpotGamma Essential $99/월·Alpha $299/월(연간 결제 시 $74/$224). 공개 API는 사실상 없음(제3자 비교 글 기준, 신뢰 중). 실시간 0DTE GEX는 v1 범위 밖.
- **과거 검증**: SqueezeMetrics CSV로 2011~ 일별 검증 가능(무료). QQQ 전용 GEX 과거값은 옵션 OI 이력을 사야 한다(유료).
- **용도**: 방향이 아니라 **장의 성격**. 예: GEX 상위 구간(양의 감마 큼) → 돌파·추세 진입 금지 또는 확인 조건 강화. GEX 음수 → 추세 진입 허용, 대신 변동성이 크므로 크기 축소. 임계값은 학습 구간 분위수로 사전등록.

### 2. 0DTE 장중 포지션(실시간 GEX/플로우) — NOISE(v1)
- 뜻: 당일 만기 옵션의 장중 거래 방향을 실시간으로 추정한 값.
- 근거: Dim 등(2024), Adams 등(2025, "Do S&P500 Options Increase Market Volatility? Evidence from 0DTEs")이 연구 중이지만 소매용 실시간 데이터는 비싸고 벤더 방법론이 비공개다.
- 데이터: 실시간 OPRA는 Alpaca Algo Trader Plus $99/월 등. 과거 0DTE 체결 이력은 비싸다. 소규모 계좌에 비용 대비 가치 낮음.

### 3. 풋/콜 비율 — NOISE
- 뜻: 하루 동안 거래된 풋옵션 대비 콜옵션 거래량. 높으면 공포, 낮으면 낙관으로 해석한다.
- 근거: 극단값의 역발상 효과는 주로 며칠~몇 주 단위 연구다. 0DTE 확대 이후 지수 옵션 거래량 구성이 바뀌어 과거 수치와 비교가 어렵다. 장중 방향 근거는 찾지 못했다.
- 데이터: Cboe 무료 CSV는 2006-11 ~ **2019-10-04에서 끊겼다**(직접 확인). 이후는 Cboe 일별 통계 페이지·YCharts(유료) 등. 과거 검증이 번거롭다.

### 4. 장중 breadth(상승/하락 종목 수) — CANDIDATE(강), 자체 계산
- **뜻**: 지금 오르는 종목이 내리는 종목보다 얼마나 많은지. $TICK(직전 체결이 오른 종목 수 − 내린 종목 수), $ADD(상승 종목 수 − 하락 종목 수), $TRIN(상승/하락 종목 비율을 거래량 비율로 나눈 값)이 대표 지표다.
- **근거(중)**: "Intraday Market-Wide Ups/Downs and Returns"(Pacific-Basin/ScienceDirect, 2019)는 09:45 시점의 상승/하락 종목 비율이 09:45~15:00 지수 수익을 같은 방향으로 예측한다고 보고했다(R² 1~2.5%, t값 6~10). 다만 **미국 표본은 3년뿐**이고 비용 전 수치이며 0DTE 이전이다. $TICK/$TRIN 관련 나머지 자료는 대부분 트레이딩 교육 블로그(신뢰 낮음).
- **데이터 문제**: NYSE $TICK/$ADD 원본은 증권사 플랫폼·TradingView 유료 데이터에 있고, 소매용 API 경로가 불명확하다. Massive(구 Polygon) Indices $49~$99/월이 어떤 지수를 포함하는지 가격 페이지에 명시되지 않았다(확인 불가).
- **대안(추천)**: Toss 웹소켓/1분봉으로 **QQQ 상위 20~30종목**의 "시가 대비 상승 종목 비율"과 "VWAP 위 종목 비율"을 09:45에 계산한다. 무료·실시간·규칙화가 쉽다. 웹소켓 구독 종목 수 제한은 Toss 문서로 확인 필요.
- **과거 검증**: Alpaca 무료 계정으로 15분 이상 지난 SIP 1분봉을 2016년부터 받을 수 있다(Alpaca 문서). Massive Starter $29/월(5년 1분봉)도 가능.
- **용도**: 방향 확인 필터. 예: 롱(TQQQ) 신호인데 상위 종목 상승 비율 < 50% → 진입 안 함. 양방향 비율이 50% 근처 → 무거래(혼조).

### 5. 메가캡(Mag7) 쏠림 — CANDIDATE (4번과 합쳐서)
- 뜻: QQQ 움직임이 소수 대형주 한두 개 때문인지, 지수 전체가 같이 움직이는지.
- 근거: Mag7은 QQQ의 약 39%(Invesco, 2025-06)라 지수 수익이 몇 종목에 기계적으로 좌우된다. 쏠림이 다음 장중 수익을 예측한다는 동료평가 연구는 찾지 못했다. 장 초반은 개별·섹터 요인이 지배하고 시장 요인은 장 후반에 커진다는 물리학계 연구(arXiv 1009.4785)가 있다(신뢰 중).
- 용도: "지수 상승 기여의 70% 이상이 1~2종목" 같은 날은 신호 신뢰도가 낮다고 보고 무거래 또는 크기 축소. 4번과 같은 데이터로 계산되므로 비용이 거의 없다.

### 6. 시가/종가 동시호가 불균형(NOII) — NOISE(v1)
- 뜻: 동시호가(개장·마감 때 한꺼번에 체결하는 경매)에 사자/팔자 주문이 어느 쪽으로 남았는지.
- 근거: Nasdaq은 NOII를 종가 예측 정보로 홍보한다(거래소 자료). 종가 불균형은 15:50부터 공개되어 v1의 판단 시각(09:45)과 맞지 않는다. 개장 불균형(09:25~09:30)은 갭 정보와 겹친다.
- 데이터: Massive "NYSE Order Imbalances" $49/월(가격 페이지 확인), Nasdaq NOII는 TotalView 계약 필요.

### 7. 야간 vs 장중 수익 패턴 — 기존 "프리마켓 갭" 입력 보강
- 뜻: 전일 종가→당일 시가(야간)와 시가→종가(장중) 수익이 다르게 움직이는 현상.
- 근거: Lou·Polk·Skouras(2019, JFE)는 종목 간 전략 수익이 야간/장중으로 갈린다고 보였다(지수 타이밍 근거는 아님). Iwanaga·Sakemoto(SSRN 5807282)는 미국 지수에서 **야간 수익이 첫 30분 수익을 반대 방향으로 예측**(갭 되돌림)한다고 보고했다. Grant·Wolf·Yu(2005)도 S&P 선물 개장 후 큰 움직임의 되돌림을 보고했다. 즉 갭 되돌림의 상당 부분은 09:30~10:00에 일어나며, 09:45 진입 시스템은 그 일부를 이미 놓친다.
- 용도: 새 입력 아님. 갭 규칙 설계 시 "큰 갭 + 첫 15분 되돌림 진행 중"을 조건으로 쓰는 근거.

### 8. ES/NQ 선물 야간 범위·베이시스 — NOISE
- 뜻: 지수 선물의 밤사이 고가/저가 범위와 현물 대비 가격 차이.
- 판단: 베이시스(선물−현물 차이)는 대부분 금리·배당으로 정해지는 기계적 값이라 정보가 적다. 야간 범위는 Toss PRE 세션 QQQ 고저와 정보가 겹친다. CME 실시간 데이터는 유료.

### 9. VVIX — CANDIDATE(약) / SKEW — NOISE / MOVE — NOISE
- VVIX: VIX 자체의 변동성(VIX 옵션으로 계산). Cboe 무료 CSV(2006~, 일별 종가, 직접 확인). 지수 방향 예측 근거는 약하고, 꼬리위험(급락) 보험 가격과 관련된 연구가 주다. 용도: 학습 구간 상위 5% 같은 극단일 때만 크기 축소. VIX·VIX9D와 상관이 높아 추가 정보가 적을 수 있으니 Paper에서 중복 여부부터 확인.
- SKEW: 심한 하락에 대비한 옵션 가격 지수. Cboe 무료 CSV(1990~, 확인). S&P500 수익과 상관 약 0.12 수준으로 예측력이 약하다는 연구가 있다(ScienceDirect 2020). NOISE.
- MOVE: 채권 변동성 지수. 금리 계열은 이미 기각했으므로 NOISE.

### 10. 달력 효과
- **월초/월말(turn-of-month) — NOISE**: "The disappearing turn-of-month effect"(Finance Research Letters, 2024)는 거래비용 하락·거래량 증가로 효과가 약해졌다고 보고했다. ETF 시대에는 첫 거래일로 몰렸다는 연구도 있다. 장중 방향 신호로 쓸 근거 부족.
- **월말 연기금 리밸런싱 — CANDIDATE(약)**: Harvey·Mazzoleni·Melone(NBER w33554, 2025, 2026 개정)은 주식이 목표비중보다 많아진 달에는 월말 마지막 4일 무렵 연기금 매도로 다음날 주식 수익이 약 17bp 낮아진다고 보고했다. 일 단위 효과라 장중 0.3% 비용을 넘기 어렵다. 용도: 월말 4일 + 그 달 주식 강세 → 롱 진입 크기 축소 정도. 데이터는 달력+월간 수익률로 무료 계산.
- **요일 효과 — NOISE**: 최근 표본에서 S&P500 요일 차이가 통계적으로 없다는 연구와 약하게 남아 있다는 연구가 섞여 있다. 신뢰할 근거 아님.
- **공휴일 전날 효과 — 방향은 NOISE**: 미국에서 1987년 이후 크게 약해지고 1990년대에 반대로 바뀌었다가 사라졌다(Journal of International Money and Finance 2005). 다만 **조기폐장일(오후 1시 마감)**은 거래량이 적고 시간이 짧아 v1에서 무거래로 두는 것이 실무적으로 안전하다(USEFUL, 근거는 유동성 상식 수준).
- **월간 옵션 만기(OPEX, 셋째 금요일)·쿼드러플 위칭(분기 만기) — CANDIDATE**: 뜻: 대량의 옵션·선물이 한꺼번에 만기되는 날. Ni·Pearson·Poteshman(2005, JFE)은 만기일 종가가 행사가 근처로 몰리는 "피닝"을 보였고, Golez·Jackwerth(2012, JFE)는 S&P500 선물에서도 피닝을 확인했다(1992~2009). 0DTE 이후 매일이 만기일이라 효과가 희석되었을 수 있다. 용도: OPEX 당일은 범위장 가능성 → 추세 진입 억제 또는 크기 축소. 데이터 무료, 과거 검증 쉬움.
- **CTA·연기금 흐름 추정 — NOISE**: 증권사(골드만·노무라 등)의 추정 리포트는 소매가 체계적으로 받을 수 없고 과거 시점 기록도 없다.

### 11. 공매도 거래량·다크풀(DIX) — NOISE
- 뜻: 장외에서 공매도로 보고된 거래 비율. DIX는 이를 S&P500 전체로 합친 SqueezeMetrics 지표.
- 근거: Diether 등(2009)은 개별 종목에서 공매도 증가가 이후 하락을 예측한다고 보였지만, ETF·지수에서는 시장조성자 공매도가 섞여 의미가 흐려진다. DIX 효과 주장은 벤더 자체 자료(신뢰 낮음)이고 수십 일 단위다.
- 데이터: FINRA 일별 공매도 거래량 무료(다음날 공개), DIX 무료 CSV(1번과 같은 파일). 장중 판단엔 시간이 안 맞음.

### 12. ETF 자금 유출입 — NOISE
- 뜻: SPY/QQQ에 새 돈이 들어왔는지(설정) 나갔는지(환매).
- 근거: 차익거래 목적 유출입은 다음날 수익과 **반대**, 수요 목적 유출입은 관계 약함(Brown 등; Xu 2022, Financial Management). 조건부·일 단위 효과이고 데이터가 하루 늦게 나온다.

### 13. 전일 종가 위치·Inside day·NR7 — CANDIDATE(약)
- 뜻: 전일 종가가 그날 고저 범위 어디에 있었는지, 전일이 그 전날 범위 안에 갇혔는지(inside day), 최근 7일 중 범위가 가장 좁았는지(NR7).
- 근거: Toby Crabel(1990) 책과 블로그 백테스트가 출처(신뢰 낮음). "좁은 범위 다음엔 넓은 범위"라는 변동성 군집 성질 자체는 학계에서 잘 확립되어 있으나, 방향 예측력은 확인되지 않았다.
- 용도: 방향이 아니라 **예상 변동폭** → 손절폭·크기. 이미 ATR14가 있으므로 NR7 플래그가 ATR 위에 추가 정보를 주는지 Paper/백테스트에서 확인. 일봉으로 무료, 수십 년 검증 가능.

### 14. 유가·금리 장중 급변 — NOISE
- 금리·달러를 이미 기각했고, 유가는 QQQ 직접 영향이 약하다. 지정학 급변일은 VIX·갭에서 대부분 드러난다.

### 15. 연준 인사 발언 — CANDIDATE(약) / 국채 입찰 — NOISE
- 연준 발언: Neuhierl·Weber(2019, JME) 등은 FOMC 외 연준 커뮤니케이션도 주가를 움직인다고 보였다. 연준 일정은 무료로 미리 공개된다. 용도: **의장 발언 예정 시각 전후 1시간 무거래** 정도만. 다른 위원 발언까지 넣으면 거래일이 너무 줄어든다.
- 국채 입찰: Lou·Yan·Zhang(2013, RFS)은 입찰 전 국채 약세가 주식에도 번진다고 보였지만 며칠 단위·2010년 이전 표본이다. 13:00 ET 결과 발표 때 순간 변동은 있으나 금리 계열 기각과 일관되게 NOISE.

### 참고: 기존 채택 입력에 대한 추가 근거
- **레버리지 ETF 종가 리밸런싱**: Barbon·Beckmeyer·Buraschi·Moerke("The Role of Leveraged ETFs and Option Market Imbalances on End-of-Day Price Dynamics")는 레버리지 ETF 효과가 옵션 감마 효과보다 크고 **종가 직전**에 집중된다고 보고했다. 당일 청산 시스템에서 청산 시각을 15:45 이전으로 둘지, 리밸런싱 방향으로 보유를 늘릴지 판단할 때 근거로 쓸 수 있다.
- **첫 30분→마지막 30분 모멘텀 기각**: Dim 등(2024)의 "딜러 감마가 평균적으로 양수 → 되돌림" 결과와 일치한다. 같은 계열의 Zarattini·Aziz·Barbon(2024) "Beat the Market" SPY 장중 모멘텀도 독립 재현(GitHub codecat-ops)에서 **2025년 이후 샤프 ≈ 0**으로 보고됐다(개인 재현, 신뢰 중). 장중 추세추종 계열은 최근 약해졌다는 증거가 쌓이고 있다.

## v1에 추가 추천 (최대 3개)

1. **딜러 감마(GEX) 상태 — 일별, 09:45 전 확정**
   - 이유: 동료평가 근거(JFE 2021, 0DTE 시대 연구 2024)가 있고, 기각한 "장중 모멘텀 소멸"을 설명하는 같은 메커니즘이다. 무료 일별 데이터가 2011년부터 있어 바로 과거 검증이 된다.
   - 사용: 방향 신호가 아니라 "추세형 진입을 허용할지"의 국면 필터. GEX 높은 날 = 돌파 진입 금지(또는 확인 조건 강화), 음수인 날 = 진입 허용 + 크기 축소. 임계값은 학습 구간 분위수로 사전등록.
   - 한계: S&P 지수 기준 추정치, 벤더 가정 의존, 당일 0DTE 미반영.

2. **QQQ 상위 종목 breadth(상승 비율·VWAP 위 비율) + 쏠림 — 09:45 실시간 자체 계산**
   - 이유: 유료 $TICK/$ADD 없이 Toss 데이터만으로 만들 수 있고, 09:45 시점 breadth가 이후 장중 수익과 같은 방향이라는 연구가 있다. 무료 과거 1분봉(Alpaca 2016~)으로 검증 가능하다.
   - 사용: 진입 확인 필터(신호 방향과 breadth가 반대면 진입 안 함), 50% 근처 혼조면 무거래, 1~2종목 쏠림이면 크기 축소.
   - 한계: 미국 표본 연구가 짧고(3년) 비용 전 결과다. 효과는 Paper에서 비용 후로 다시 확인해야 한다.

3. **달력 플래그: 월간 OPEX·쿼드러플 위칭·조기폐장일**
   - 이유: 비용 0, 결정적(미래 날짜가 이미 정해짐)이라 룩어헤드 위험이 없고 과거 검증이 쉽다. 조기폐장일 무거래는 유동성 측면에서 근거가 분명하다.
   - 사용: 조기폐장일 = 무거래. OPEX/위칭일 = 추세 진입 억제 또는 크기 축소(효과 유무는 Paper에서 판정, 효과 없으면 제거).

추천하지 않는 것: 실시간 0DTE 플로우, NYSE 원본 internals, NOII, 선물 베이시스는 비용·시간대 문제로 v1에서 뺀다. 풋/콜, SKEW, 요일·공휴일·월초 효과, DIX·공매도, ETF 유출입, CTA 추정치는 최근 근거가 약하거나 일 단위 이상이라 NOISE로 둔다.

## 출처

학술·준학술 (신뢰 높음~중)
- Baltussen, Da, Lammers, Martens (2021), Hedging demand and market intraday momentum, JFE — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365 , https://www.sciencedirect.com/science/article/abs/pii/S0304405X21001598
- Dim, Eraker, Vilkov (2024), 0DTEs: Trading, Gamma Risk and Volatility Propagation — https://dx.doi.org/10.2139/ssrn.4692190
- Adams, Dim, Eraker, Fontaine, Ornthanalai, Vilkov (2025), Do S&P500 Options Increase Market Volatility? Evidence from 0DTEs — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5641974
- Cboe, 0DTE Index Options and Market Volatility: How Large is Their Impact? (거래소 자료) — https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf
- Barbon, Beckmeyer, Buraschi, Moerke, The Role of Leveraged ETFs and Option Market Imbalances on End-of-Day Price Dynamics — https://www.researchgate.net/publication/355381896
- Lou, Polk, Skouras (2019), A tug of war: Overnight versus intraday expected returns, JFE — http://eprints.lse.ac.uk/87481/7/Polk_Tug%20of%20War.pdf
- Iwanaga, Sakemoto, Does overnight return predict the first half-hour return for U.S. market indices? — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5807282
- Grant, Wolf, Yu (2005), Intraday price reversals in the US stock index futures market: A 15-year study — https://www.sciencedirect.com/science/article/abs/pii/S0378426604000949
- Intraday Market-Wide Ups/Downs and Returns (2019) — https://www.sciencedirect.com/science/article/pii/S2096232019300083
- Zarattini, Aziz, Barbon (2024), Beat the Market — https://www.researchgate.net/publication/380582442 ; 독립 재현(개인, 신뢰 중) — https://github.com/codecat-ops/zarattini-2024-momentum-spy
- Harvey, Mazzoleni, Melone (2025), The Unintended Consequences of Rebalancing, NBER w33554 — https://www.nber.org/papers/w33554
- The disappearing turn-of-month effect (2024), Finance Research Letters — https://www.sciencedirect.com/science/article/abs/pii/S1544612324014909
- Pre-holiday effects: decline and reversal (2005), JIMF — https://www.sciencedirect.com/science/article/abs/pii/S0261560605000938
- Kurov, Wolfe, Gilbert, The disappearing pre-FOMC announcement drift — https://www.sciencedirect.com/science/article/pii/S1544612320315956
- Ni, Pearson, Poteshman (2005), Stock Price Clustering on Option Expiration Dates — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=519044
- Golez, Jackwerth (2012), Pinning in the S&P 500 futures, JFE — https://www.sciencedirect.com/science/article/abs/pii/S0304405X12001365
- Lou, Yan, Zhang (2013), Anticipated and Repeated Shocks in Liquid Markets, RFS — https://personal.lse.ac.uk/loud/Shocks.pdf
- Diether, Lee, Werner (2009), Short-sale strategies and return predictability — https://www.cis.upenn.edu/~mkearns/finread/short_sellers_predict_returns.pdf
- Xu (2022), Are the flows of exchange-traded funds informative?, Financial Management — https://onlinelibrary.wiley.com/doi/10.1111/fima.12396
- Skew index: descriptive analysis, predictive power (2020) — https://www.sciencedirect.com/science/article/abs/pii/S1062940820302370
- Individual and collective stock dynamics: intra-day seasonalities — https://arxiv.org/pdf/1009.4785

데이터·가격 (2026-10-04 확인)
- SqueezeMetrics DIX/GEX 무료 CSV(직접 다운로드 확인, 2011-05-02~2026-10-02) — https://squeezemetrics.com/monitor/dix
- Cboe VVIX/SKEW 무료 CSV(직접 확인) — https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv , https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv
- Cboe 지연 옵션체인 JSON(OI·감마 포함 확인, 약관 확인 필요) — https://www.cboe.com/delayed_quotes/
- Cboe 풋/콜 CSV(2019-10-04에서 끝남, 직접 확인) — https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv
- Massive(구 Polygon) 가격: Stocks 무료/$29/$79/$199, Indices 무료/$49/$99, NYSE Order Imbalances $49 — https://massive.com/pricing
- Alpaca 시장데이터(무료: 15분 지난 SIP 2016~, Algo Trader Plus $99/월) — https://docs.alpaca.markets/us/docs/about-market-data-api
- SpotGamma 가격(Essential $99, Alpha $299/월; 제3자 정리 포함) — https://support.spotgamma.com/hc/en-us/articles/1500002666102 , https://flashalpha.com/articles/spotgamma-review-2026-pricing-features-alternatives
- Nasdaq 시가/종가 동시호가·NOII — https://www.nasdaqtrader.com/content/productsservices/trading/crosses/fact_sheet.pdf
- FINRA 일별 공매도 거래량 — https://www.finra.org/finra-data/browse-catalog/short-sale-volume
- Invesco, Mag7 비중 — https://www.invesco.com/qqq-etf/en/innovation/navigating-magnificent-7-turbulence.html

블로그·벤더(신뢰 낮음, 아이디어 출처로만)
- NR7(QuantifiedStrategies, StockCharts) — https://www.quantifiedstrategies.com/nr7-trading-strategy-toby-crabel/ , https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/narrow-range-day-nr7
- 시장 internals 설명(TrendSpider 등) — https://trendspider.com/learning-center/market-internals/
