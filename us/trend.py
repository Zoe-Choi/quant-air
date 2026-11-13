"""US leveraged ETF trend-filter backtest.

Strategy
- Hold a 3x leveraged ETF only while its underlying index closes above its
  moving average; otherwise hold cash (T-bill yield)
- Signal at the daily close, trade at the next trading day close
- Leveraged ETF history is extended before its launch with a synthetic series:
  3x daily index return minus expense ratio and borrowing cost

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
    "반도체": ("^SOX", "SOXX", "SOXL"),
    "나스닥100": ("^NDX", "QQQ", "TQQQ"),
}
CASH_RATE = "^IRX"            # 13-week T-bill yield, percent per year

MA_DAYS = 200
BAND = 0.0                    # extra margin above or below MA to cut whipsaws
EXEC_LAG = 1                  # trading days between signal and trade

LEVERAGE = 3.0
EXPENSE_RATIO = 0.0095        # per year, roughly SOXL and TQQQ
SPREAD_OVER_CASH = 0.005      # extra borrowing cost on the levered part, per year
SWITCH_COST = 0.001           # commission plus slippage per buy or sell

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
# Backtest
# ---------------------------------------------------------------------------
def trend_signal(index_close: pd.Series) -> pd.Series:
    """1 when the index is in an uptrend, with hysteresis when BAND > 0."""
    ma = index_close.rolling(MA_DAYS).mean()
    upper, lower = ma * (1 + BAND), ma * (1 - BAND)
    state, out = 0, []
    for c, u, l in zip(index_close, upper, lower):
        if np.isnan(u):
            out.append(np.nan)
            continue
        if c > u:
            state = 1
        elif c < l:
            state = 0
        out.append(state)
    return pd.Series(out, index=index_close.index)


def run(asset_ret: pd.Series, signal: pd.Series, cash_ret: pd.Series) -> tuple[pd.Series, int]:
    # Signal at close t, trade at close t+EXEC_LAG, earns returns after that.
    pos = signal.shift(1 + EXEC_LAG).reindex(asset_ret.index)
    start = pos.first_valid_index()
    pos = pos.loc[start:]
    a, c = asset_ret.loc[start:], cash_ret.reindex(pos.index).fillna(0.0)
    switches = pos.diff().abs().fillna(0.0)
    r = pos * a + (1 - pos) * c - switches * SWITCH_COST
    return (1 + r).cumprod(), int(switches.sum())


def buy_hold(asset_ret: pd.Series, start: pd.Timestamp) -> pd.Series:
    return (1 + asset_ret.loc[start:]).cumprod()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def stats(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    vol = r.std() * np.sqrt(252)
    mdd = (eq / eq.cummax() - 1).min()
    return {
        "CAGR": f"{cagr:.1%}",
        "변동성": f"{vol:.1%}",
        "Sharpe": f"{(r.mean() * 252) / vol:.2f}",
        "MDD": f"{mdd:.1%}",
        "최종 배수": f"{eq.iloc[-1] / eq.iloc[0]:,.1f}x",
    }


def check_synthetic(name: str, synth: pd.Series, actual: pd.Series):
    both = pd.concat([synth, actual], axis=1, keys=["synth", "actual"]).dropna()
    yearly = (1 + both).resample("YE").prod() - 1
    gap = (yearly["synth"] - yearly["actual"]).abs().median()
    corr = both.corr().iloc[0, 1]
    print(f"  {name} 합성 검증: 일간 상관 {corr:.3f}, 연수익률 차이 중앙값 {gap:.1%}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    prices = load_prices(args.refresh)
    cash_ret = cash_daily_return(prices)
    OUT_DIR.mkdir(exist_ok=True)
    curves = {}

    print(f"설정: {MA_DAYS}일 이동평균, 밴드 {BAND:.0%}, 신호 후 {EXEC_LAG}일 뒤 체결")
    for name, (index, etf1, etf3) in PAIRS.items():
        lev_ret, synth, actual = levered_series(prices, index, etf3, cash_ret)
        idx_ret = prices[index].pct_change(fill_method=None).dropna()
        signal = trend_signal(prices[index].dropna())

        eq_trend3, n3 = run(lev_ret, signal, cash_ret)
        start = eq_trend3.index[0]
        eq_trend1, n1 = run(idx_ret, signal, cash_ret)
        eq_trend1 = eq_trend1.loc[start:]
        eq_bh1 = buy_hold(idx_ret, start)
        eq_bh3 = buy_hold(lev_ret, start)

        years = (eq_trend3.index[-1] - start).days / 365.25
        print(f"\n[{name}] {index} 기준, {start.date()} ~ {eq_trend3.index[-1].date()}")
        check_synthetic(etf3, synth, actual)
        table = pd.DataFrame({
            "지수 1x 보유": stats(eq_bh1),
            f"{etf3} 3x 보유": stats(eq_bh3),
            "지수 1x 추세필터": stats(eq_trend1),
            f"{etf3} 3x 추세필터": stats(eq_trend3),
        })
        print(table.to_string())
        print(f"  연평균 매매 횟수: {n3 / years:.1f}회")

        yearly = pd.DataFrame({
            "지수 1x": eq_bh1.resample("YE").last().pct_change(),
            f"{etf3} 보유": eq_bh3.resample("YE").last().pct_change(),
            f"{etf3} 추세": eq_trend3.resample("YE").last().pct_change(),
        }).dropna()
        yearly.index = yearly.index.year
        worst = yearly.sort_values("지수 1x").head(5)
        print("  지수 하락 상위 5개 연도")
        print(worst.map(lambda x: f"{x:+.0%}").to_string())

        curves[name] = {
            "지수 1x 보유": eq_bh1,
            f"{etf3} 3x 보유": eq_bh3,
            f"{etf3} 3x 추세필터": eq_trend3,
        }
        pd.DataFrame(curves[name]).to_csv(OUT_DIR / f"{etf3}.csv", encoding="utf-8-sig")

    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
        fig, axes = plt.subplots(len(curves), 1, figsize=(11, 4.5 * len(curves)))
        for ax, (name, cs) in zip(np.atleast_1d(axes), curves.items()):
            for label, eq in cs.items():
                ax.plot(eq / eq.iloc[0], label=label)
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:g}x"))
            ax.yaxis.set_minor_formatter(plt.NullFormatter())
            ax.set_title(f"{name} {MA_DAYS}일 이동평균 추세필터")
            ax.legend()
        fig.tight_layout()
        fig.savefig(OUT_DIR / "trend.png", dpi=110)
        print(f"\n차트 저장: {OUT_DIR / 'trend.png'}")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
