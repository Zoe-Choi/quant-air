# quant-air

미국과 한국 주식 시장 퀀트 전략 백테스트 도구

## 구조

| 폴더 | 내용 |
|---|---|
| `us/` | 미국 레버리지 ETF 추세 필터 전략. 현재 주력 |
| `kr/` | KOSPI 12-1 모멘텀 전략 |

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## us: 레버리지 ETF 추세 필터

```bash
cd us
python trend.py            # 캐시된 가격 사용
python trend.py --refresh  # 가격 다시 다운로드
```

- 대상: SOXL 기초지수 `^SOX`, TQQQ 기초지수 `^NDX`
- 추세: 기초지수 종가가 200일 이동평균 위면 3배 ETF 보유, 아래면 현금
- 휩쏘 감소: 이동평균 위 3% 돌파 시 진입, 아래 3% 이탈 시 청산
- 변동성 필터: 상승 추세라도 20일 변동성이 1년 평균보다 높으면 3배 대신 1배 보유
- 비중 분산: 자금 50%만 전략에 투입, 나머지 현금, 월간 리밸런싱
- 각 장치 단독과 전부 결합 비교, 기간 분할 검증, 파라미터 강건성 검사 출력
- 신호는 종가 기준, 체결은 다음 거래일 종가
- 현금 구간 수익률은 13주 T-bill 금리 `^IRX`
- ETF 상장 이전 구간은 지수 일간 수익률 3배에서 보수와 차입 비용을 뺀 합성 데이터
- 합성 데이터는 지수 배당 미포함으로 실제 ETF보다 연 4~5%p 낮아 보수적

주요 설정은 `trend.py` 상단 상수로 조정합니다.

| 상수 | 의미 | 기본값 |
|---|---|---|
| `MA_DAYS` | 이동평균 기간 | 200 |
| `BAND` | 이동평균 위아래 여유 폭 | 3% |
| `VOL_WINDOW` | 단기 변동성 기간 | 20 |
| `LEV_WEIGHT` | 전략 투입 비중 | 50% |
| `EXEC_LAG` | 신호 후 체결까지 거래일 | 1 |
| `SWITCH_COST` | 매수 또는 매도 1회 비용 | 0.1% |

## kr: KOSPI 모멘텀

```bash
cd kr
python backtest.py
```

- 12-1 모멘텀 상위 20종목 동일 비중, 월간 리밸런싱
- 상장폐지 종목 포함, 수수료와 거래세, 슬리피지 반영

## 데이터

- 미국: [yfinance](https://github.com/ranaroussi/yfinance)
- 한국: [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader)
- 수집한 데이터는 저장소에 포함하지 않습니다

## 면책

이 프로젝트는 학습과 연구 목적입니다. 투자 권유가 아니며, 백테스트 결과는 미래 수익을 보장하지 않습니다. 레버리지 ETF는 원금 전액 손실이 가능한 고위험 상품입니다. 이 코드를 이용한 투자 판단과 그 결과에 대한 책임은 사용자에게 있습니다.
