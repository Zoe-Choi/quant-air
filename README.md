# quant-air

한국 주식 시장 퀀트 전략 백테스트 도구

## 기능

- KOSPI 12-1 모멘텀 전략 백테스트
- 상장폐지 종목 포함, 생존 편향 완화
- 월말 신호, 다음 거래일 종가 체결로 look-ahead bias 방지
- 수수료, 거래세, 슬리피지 반영
- 상하한가 범위를 벗어난 비정상 가격 변동 정제
- 성과 지표: CAGR, 변동성, Sharpe, MDD, 연도별 수익률

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 실행

```bash
python backtest.py            # 캐시된 가격 사용
python backtest.py --refresh  # 가격 다시 다운로드
```

- 첫 실행 시 약 900종목 가격을 받아 `data/` 에 저장합니다. 몇 분 걸립니다
- 결과 차트와 CSV 는 `output/` 에 저장됩니다

## 설정

`backtest.py` 상단 상수로 조정합니다.

| 상수 | 의미 | 기본값 |
|---|---|---|
| `TOP_N` | 보유 종목 수 | 20 |
| `LOOKBACK_DAYS` | 모멘텀 계산 기간 | 252 |
| `SKIP_DAYS` | 최근 제외 기간 | 21 |
| `MIN_TRADING_VALUE` | 20일 평균 거래대금 하한 | 10억 원 |
| `SELL_TAX` | 매도 거래세 | 0.20% |
| `DELIST_RETURN` | 상장폐지 시 반영 수익률 | 0 |

거래세는 시기별로 바뀌므로 현행 세율을 확인한 뒤 사용하세요.

## 데이터

- 가격: [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader)
- 수집한 데이터는 저장소에 포함하지 않습니다

## 면책

이 프로젝트는 학습과 연구 목적입니다. 투자 권유가 아니며, 백테스트 결과는 미래 수익을 보장하지 않습니다. 이 코드를 이용한 투자 판단과 그 결과에 대한 책임은 사용자에게 있습니다.
