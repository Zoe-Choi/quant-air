"""US leveraged ETF trend-filter backtest.

Building blocks
- Trend filter: hold risk only while the underlying index is above its MA
- Whipsaw band: enter above MA*(1+BAND), exit below MA*(1-BAND)
- Volatility filter: in an uptrend, hold 1x instead of 3x while short-term
  realized volatility is above its own 1-year average
- Weight split: only LEV_WEIGHT of capital runs the strategy, the rest is cash,
  rebalanced monthly

Timing: signal at the daily close, trade at the next trading day close.
Leveraged ETF history before launch is a synthetic 3x series.

Usage
    python trend.py            # uses cached prices when available
    python trend.py --refresh  # re-download prices
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# name: (underlying index, 1x ETF, 3x ETF)
PAIRS = {
    "나스닥100": ("^NDX", "QQQ", "TQQQ"),
    "반도체": ("^SOX", "SOXX", "SOXL"),
}
CASH_RATE = "^IRX"            # 13-week T-bill yield, percent per year

MA_DAYS = 200
EXEC_LAG = 1                  # trading days between signal and trade

# Parameters below are common defaults chosen before looking at results.
BAND = 0.03                   # whipsaw band around the MA
VOL_WINDOW = 20               # realized volatility window, trading days
VOL_AVG_WINDOW = 252          # window of the volatility average it is compared to
LEV_WEIGHT = 0.5              # capital share for the strategy sleeve

LEVERAGE = 3.0
EXPENSE_RATIO = 0.0095        # per year, roughly SOXL and TQQQ
SPREAD_OVER_CASH = 0.005      # extra borrowing cost on the levered part, per year
SWITCH_COST = 0.001           # commission plus slippage per buy or sell

SPLIT_DATE = "2010-01-01"     # synthetic history before, real ETF after

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "output"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load_prices(refresh: bool) -> pd.DataFrame:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / "prices.pkl"
    if not refresh and path.exists():
        return pd.read_pickle(path)
    tickers = sorted({t for p in PAIRS.values() for t in p} | {CASH_RATE})
    print(f"{len(tickers)}개 티커 다운로드 중...", flush=True)
    df = yf.download(tickers, start="1985-01-01", auto_adjust=True, progress=False)["Close"]
    df.to_pickle(path)
    return df


def cash_daily_return(prices: pd.DataFrame) -> pd.Series:
    return (prices[CASH_RATE].ffill() / 100 / 252).fillna(0.0)


def synthetic_levered(index_ret: pd.Series, cash_ret: pd.Series) -> pd.Series:
    """Daily return of a daily-rebalanced 3x fund on the index."""
    borrow = (LEVERAGE - 1) * (cash_ret + SPREAD_OVER_CASH / 252)
    return LEVERAGE * index_ret - borrow - EXPENSE_RATIO / 252


def levered_series(prices: pd.DataFrame, index: str, etf: str, cash_ret: pd.Series):
    """Actual ETF returns where available, synthetic returns before launch."""
    idx_ret = prices[index].pct_change(fill_method=None)
    synth = synthetic_levered(idx_ret, cash_ret)
    actual = prices[etf].pct_change(fill_method=None)
    combined = actual.where(actual.notna(), synth)
    return combined.dropna(), synth, actual


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def trend_signal(index_close: pd.Series, ma_days: int, band: float) -> pd.Series:
    """1 in an uptrend, 0 otherwise, with hysteresis when band > 0."""
    ma = index_close.rolling(ma_days).mean()
    upper, lower = (ma * (1 + band)).to_numpy(), (ma * (1 - band)).to_numpy()
    close = index_close.to_numpy()
    out = np.full(len(close), np.nan)
    state = 0
    for i in range(len(close)):
        if np.isnan(upper[i]):
            continue
        if close[i] > upper[i]:
            state = 1
        elif close[i] < lower[i]:
            state = 0
        out[i] = state
    return pd.Series(out, index=index_close.index)


def high_vol_flag(index_ret: pd.Series) -> pd.Series:
    vol = index_ret.rolling(VOL_WINDOW).std()
    return vol > vol.rolling(VOL_AVG_WINDOW).mean()


def exposure(index_close: pd.Series, index_ret: pd.Series, ma_days: int,
             band: float, use_vol: bool) -> pd.Series:
    """Target exposure per day: 3 = 3x ETF, 1 = index, 0 = cash."""
    trend = trend_signal(index_close, ma_days, band)
    if not use_vol:
        return trend * 3
    hv = high_vol_flag(index_ret).reindex(trend.index)
    level = pd.Series(np.where(hv, 1.0, 3.0), index=trend.index)
    level[hv.isna()] = np.nan
    return trend * level


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
def run_exposure(target: pd.Series, lev_ret: pd.Series, idx_ret: pd.Series,
                 cash_ret: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Daily strategy returns and the held exposure."""
    pos = target.shift(1 + EXEC_LAG).reindex(lev_ret.index).dropna()
    days = pos.index
    lev, idx, cash = lev_ret.reindex(days), idx_ret.reindex(days), cash_ret.reindex(days).fillna(0.0)
    r = np.select([pos == 3, pos == 1], [lev, idx], default=cash)
    prev = pos.shift(1).fillna(pos.iloc[0])
    changed = prev != pos
    sides = np.where(changed & ((prev == 0) | (pos == 0)), 1, np.where(changed, 2, 0))
    r = pd.Series(r, index=days) - sides * SWITCH_COST
    return r, pos


def blend_monthly(strat_ret: pd.Series, pos: pd.Series, cash_ret: pd.Series,
                  weight: float) -> pd.Series:
    """Strategy sleeve plus cash sleeve, rebalanced to weight each month."""
    if weight >= 1.0:
        return (1 + strat_ret).cumprod()
    cash = cash_ret.reindex(strat_ret.index).fillna(0.0).to_numpy()
    sr, p = strat_ret.to_numpy(), pos.to_numpy()
    month = strat_ret.index.to_period("M")
    new_month = np.r_[False, month[1:] != month[:-1]]
    s, c = weight, 1 - weight
    out = np.empty(len(sr))
    for i in range(len(sr)):
        s *= 1 + sr[i]
        c *= 1 + cash[i]
        total = s + c
        if new_month[i]:
            target = total * weight
            if p[i] != 0:
                total -= abs(s - target) * SWITCH_COST
                target = total * weight
            s, c = target, total - target
        out[i] = s + c
    return pd.Series(out, index=strat_ret.index)


def buy_hold(asset_ret: pd.Series, start: pd.Timestamp) -> pd.Series:
    return (1 + asset_ret.loc[start:]).cumprod()


def variant(prices, index, lev_ret, idx_ret, cash_ret, ma_days, band, use_vol, weight):
    target = exposure(prices[index].dropna(), idx_ret, ma_days, band, use_vol)
    r, pos = run_exposure(target, lev_ret, idx_ret, cash_ret)
    eq = blend_monthly(r, pos, cash_ret, weight)
    trades = int((pos.diff().fillna(0) != 0).sum())
    return eq, trades


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def cagr_mdd(eq: pd.Series) -> tuple[float, float]:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    return cagr, (eq / eq.cummax() - 1).min()


def stats(eq: pd.Series, trades: int | None = None) -> dict:
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr, mdd = cagr_mdd(eq)
    vol = r.std() * np.sqrt(252)
    return {
        "CAGR": f"{cagr:.1%}",
        "변동성": f"{vol:.1%}",
        "Sharpe": f"{(r.mean() * 252) / vol:.2f}",
        "MDD": f"{mdd:.1%}",
        "CAGR/MDD": f"{cagr / abs(mdd):.2f}",
        "최종 배수": f"{eq.iloc[-1] / eq.iloc[0]:,.0f}x",
        "연 매매": "-" if trades is None else f"{trades / years:.1f}",
    }


def period_table(curves: dict) -> pd.DataFrame:
    rows = {}
    for name, eq in curves.items():
        a, b = eq.loc[:SPLIT_DATE], eq.loc[SPLIT_DATE:]
        (ca, ma), (cb, mb) = cagr_mdd(a), cagr_mdd(b)
        rows[name] = {
            f"~{SPLIT_DATE[:4]} CAGR": f"{ca:.1%}", f"~{SPLIT_DATE[:4]} MDD": f"{ma:.0%}",
            f"{SPLIT_DATE[:4]}~ CAGR": f"{cb:.1%}", f"{SPLIT_DATE[:4]}~ MDD": f"{mb:.0%}",
        }
    return pd.DataFrame(rows)


def robustness(prices, index, lev_ret, idx_ret, cash_ret, start) -> pd.DataFrame:
    """Combined strategy across MA lengths and bands: CAGR / MDD."""
    grid = {}
    for ma in (100, 150, 200, 250):
        row = {}
        for band in (0.0, 0.02, 0.05):
            eq, _ = variant(prices, index, lev_ret, idx_ret, cash_ret, ma, band, True, LEV_WEIGHT)
            c, m = cagr_mdd(eq.loc[start:])
            row[f"밴드 {band:.0%}"] = f"{c:.1%} / {m:.0%}"
        grid[f"MA {ma}"] = row
    return pd.DataFrame(grid).T


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    pd.set_option("display.width", 200)
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    prices = load_prices(args.refresh)
    cash_ret = cash_daily_return(prices)
    OUT_DIR.mkdir(exist_ok=True)
    plots = {}

    print(f"공통: {MA_DAYS}일 이동평균, 신호 후 {EXEC_LAG}일 뒤 체결, 1회 매매비용 {SWITCH_COST:.1%}")
    print(f"휩쏘 밴드 {BAND:.0%}, 변동성 {VOL_WINDOW}일 대 {VOL_AVG_WINDOW}일 평균, 비중 {LEV_WEIGHT:.0%}")

    for name, (index, _, etf3) in PAIRS.items():
        lev_ret, _, _ = levered_series(prices, index, etf3, cash_ret)
        idx_ret = prices[index].pct_change(fill_method=None).dropna()

        specs = {
            "추세": (0.0, False, 1.0),
            "+휩쏘": (BAND, False, 1.0),
            "+변동성": (0.0, True, 1.0),
            "+비중": (0.0, False, LEV_WEIGHT),
            "전부 결합": (BAND, True, LEV_WEIGHT),
        }
        results = {
            k: variant(prices, index, lev_ret, idx_ret, cash_ret, MA_DAYS, b, v, w)
            for k, (b, v, w) in specs.items()
        }
        # Align every curve to the latest common start.
        start = max(eq.index[0] for eq, _ in results.values())
        curves = {"지수 1x 보유": buy_hold(idx_ret, start), f"{etf3} 보유": buy_hold(lev_ret, start)}
        trades = {}
        for k, (eq, n) in results.items():
            curves[k] = eq.loc[start:] / eq.loc[start]
            trades[k] = n

        end = curves["추세"].index[-1]
        print(f"\n[{name}] {index} 기준, {start.date()} ~ {end.date()}")
        print(pd.DataFrame({k: stats(v, trades.get(k)) for k, v in curves.items()}).to_string())
        print(f"\n  기간 분할. {SPLIT_DATE[:4]} 이전은 합성 데이터")
        print(period_table(curves).to_string())
        print(f"\n  강건성 검사. 전부 결합 전략의 CAGR / MDD")
        print(robustness(prices, index, lev_ret, idx_ret, cash_ret, start).to_string())

        plots[name] = {k: curves[k] for k in ("지수 1x 보유", f"{etf3} 보유", "추세", "전부 결합")}
        pd.DataFrame(curves).to_csv(OUT_DIR / f"{etf3}.csv", encoding="utf-8-sig")

    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
        fig, axes = plt.subplots(len(plots), 2, figsize=(14, 4.8 * len(plots)),
                                 gridspec_kw={"width_ratios": [3, 2]})
        for row, (name, cs) in zip(np.atleast_2d(axes), plots.items()):
            for label, eq in cs.items():
                row[0].plot(eq, label=label)
                row[1].plot(eq / eq.cummax() - 1, label=label, linewidth=0.8)
            row[0].set_yscale("log")
            row[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:g}x"))
            row[0].yaxis.set_minor_formatter(plt.NullFormatter())
            row[0].set_title(f"{name} 누적 수익")
            row[0].legend()
            row[1].set_title(f"{name} 고점 대비 낙폭")
            row[1].set_ylim(-1, 0.02)
        fig.tight_layout()
        fig.savefig(OUT_DIR / "trend.png", dpi=105)
        print(f"\n차트 저장: {OUT_DIR / 'trend.png'}")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
