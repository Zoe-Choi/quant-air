"""KOSPI 12-1 momentum backtest.

Strategy
- Universe: KOSPI common stocks, including stocks delisted during the test period
- Signal: 12-1 momentum, return from 12 months ago to 1 month ago
- Filter: enough price history and 20-day average trading value above a floor
- Portfolio: top N stocks, equal weight, rebalanced monthly
- Timing: signal at month-end close, trade at the next trading day close

Usage
    python backtest.py            # uses cached prices when available
    python backtest.py --refresh  # re-download prices
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import FinanceDataReader as fdr
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_START = "2014-01-01"      # extra year for the 12-month lookback
TEST_START = "2015-01-01"
TOP_N = 20
LOOKBACK_DAYS = 252            # about 12 months
SKIP_DAYS = 21                 # skip the most recent month (short-term reversal)
MIN_HISTORY_DAYS = 260
MIN_TRADING_VALUE = 1e9        # 20-day average trading value floor, KRW

# Trading costs as fractions of traded amount.
# Check the current securities transaction tax rate before relying on results.
COMMISSION = 0.00015           # per side
SELL_TAX = 0.0020              # transaction tax on sells
SLIPPAGE = 0.0010              # per side

# Return applied on the last trading day of a delisted holding.
# 0.0 assumes cash-out at the last price (mergers, tender offers).
# Set to -1.0 for a worst case where the position is lost entirely.
DELIST_RETURN = 0.0

# KRX daily price limit is +-30% (since 2015-06). Larger daily moves in the
# data are mostly unadjusted corporate actions such as capital reductions,
# so they are treated as data errors and replaced with 0.
MAX_DAILY_MOVE = 0.30

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "output"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def build_universe(start: str) -> pd.DataFrame:
    """Current KOSPI common stocks plus KOSPI common stocks delisted after start."""
    cur = fdr.StockListing("KOSPI")[["Code", "Name"]]
    cur = cur.rename(columns={"Code": "code", "Name": "name"})
    cur["delisted"] = pd.NaT

    dl = fdr.StockListing("KRX-DELISTING")
    dl["DelistingDate"] = pd.to_datetime(dl["DelistingDate"], errors="coerce")
    dl = dl[
        (dl["Market"] == "KOSPI")
        & (dl["SecuGroup"] == "주권")
        & (dl["Kind"] == "보통주")
        & (dl["DelistingDate"] >= start)
    ]
    dl = dl.rename(columns={"Symbol": "code", "Name": "name", "DelistingDate": "delisted"})
    dl = dl[["code", "name", "delisted"]]

    uni = pd.concat([cur, dl], ignore_index=True).drop_duplicates("code", keep="first")
    # Common stock codes end with 0; drop SPACs and REIT-like names.
    uni = uni[uni["code"].str.fullmatch(r"\d{5}0")]
    uni = uni[~uni["name"].str.contains("스팩|리츠", na=False)]
    return uni.reset_index(drop=True)


def _fetch_one(code: str, start: str):
    try:
        df = fdr.DataReader(code, start)
    except Exception:
        return code, None
    if df is None or df.empty:
        return code, None
    return code, df[["Close", "Volume"]]


def load_prices(refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    DATA_DIR.mkdir(exist_ok=True)
    close_p, vol_p, uni_p = DATA_DIR / "close.pkl", DATA_DIR / "volume.pkl", DATA_DIR / "universe.pkl"
    if not refresh and close_p.exists():
        return pd.read_pickle(close_p), pd.read_pickle(vol_p), pd.read_pickle(uni_p)

    uni = build_universe(DATA_START)
    print(f"종목 {len(uni)}개 다운로드 중...", flush=True)
    closes, vols = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_fetch_one, c, DATA_START) for c in uni["code"]]
        for i, f in enumerate(futures, 1):
            code, df = f.result()
            if df is not None:
                closes[code] = df["Close"]
                vols[code] = df["Volume"]
            if i % 100 == 0:
                print(f"  {i}/{len(uni)}", flush=True)

    close = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(vols).reindex(close.index)
    # Zero close means no trade or bad data.
    close = close.where(close > 0)
    close.to_pickle(close_p)
    volume.to_pickle(vol_p)
    uni.to_pickle(uni_p)
    print(f"완료: {close.shape[1]}종목, {close.shape[0]}거래일")
    return close, volume, uni


def clean_prices(close: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Rebuild a price index from returns with impossible daily moves removed."""
    r = close.pct_change(fill_method=None)
    bad = r.abs() > MAX_DAILY_MOVE + 1e-3
    r = r.mask(bad, 0.0).fillna(0.0)
    idx = (1 + r).cumprod().where(close.notna())
    return idx, int(bad.sum().sum())


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
def rebalance_schedule(index: pd.DatetimeIndex, start: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """(signal_date, trade_date) pairs: month-end close, then next trading day."""
    s = pd.Series(index, index=index)
    month_ends = s.groupby([index.year, index.month]).max()
    first_signal = pd.Timestamp(start) - pd.Timedelta("7D")
    pairs = []
    for sig in map(pd.Timestamp, month_ends):
        pos = index.get_loc(sig)
        if pos + 1 < len(index) and sig >= first_signal:
            pairs.append((sig, index[pos + 1]))
    return pairs


def select(close: pd.DataFrame, tv_all: pd.DataFrame, sig: pd.Timestamp) -> list[str]:
    """close is the cleaned price index; tv_all is raw daily trading value in KRW."""
    pos = close.index.get_loc(sig)
    if pos < LOOKBACK_DAYS:
        return []
    window = close.iloc[: pos + 1]
    p_now = window.iloc[-1]
    p_recent = window.iloc[-1 - SKIP_DAYS]
    p_past = window.iloc[-1 - LOOKBACK_DAYS]
    history = window.notna().sum()

    tv = tv_all.iloc[pos - 19 : pos + 1].mean()

    mom = p_recent / p_past - 1
    ok = p_now.notna() & (history >= MIN_HISTORY_DAYS) & (tv >= MIN_TRADING_VALUE)
    mom = mom[ok].dropna()
    return list(mom.nlargest(TOP_N).index)


def run_backtest(close: pd.DataFrame, tv_all: pd.DataFrame, with_costs: bool):
    rets = close.pct_change(fill_method=None)
    last_valid = close.apply(pd.Series.last_valid_index)
    alive_cutoff = close.index[-1] - pd.Timedelta("10D")
    dead = {
        c for c, last in last_valid.items()
        if pd.notna(last) and pd.Timestamp(last) < alive_cutoff
    }

    # Delisted stocks: apply DELIST_RETURN on their last day, then 0 forever.
    rets = rets.fillna(0.0)
    for code in dead:
        last = last_valid[code]
        rets.at[last, code] = (1 + rets.at[last, code]) * (1 + DELIST_RETURN) - 1

    pairs = rebalance_schedule(close.index, TEST_START)
    trade_days = {t: s for s, t in pairs}
    days = close.index[close.index >= pairs[0][1]]

    value = 1.0
    hold = pd.Series(dtype=float)        # current value per stock
    equity, turnovers, picks_log = [], [], []
    dead_picked = set()

    for d in days:
        if not hold.empty:
            hold = hold * (1 + rets.loc[d, hold.index])
            value = hold.sum()

        if d in trade_days:
            picks = select(close, tv_all, trade_days[d])
            target = pd.Series(value / len(picks), index=picks) if picks else pd.Series(dtype=float)
            all_codes = hold.index.union(target.index)
            diff = target.reindex(all_codes, fill_value=0) - hold.reindex(all_codes, fill_value=0)
            buys, sells = diff.clip(lower=0).sum(), (-diff).clip(lower=0).sum()
            cost = 0.0
            if with_costs:
                cost = buys * (COMMISSION + SLIPPAGE) + sells * (COMMISSION + SELL_TAX + SLIPPAGE)
            value -= cost
            hold = target * (value / target.sum()) if picks else target
            turnovers.append(sells / max(value + cost, 1e-12))
            picks_log.append({"date": d, "picks": picks})
            dead_picked.update(c for c in picks if c in dead)

        equity.append((d, value))

    eq = pd.Series(dict(equity), name="strategy")
    info = {"turnover": float(np.mean(turnovers)), "delisted_picks": len(dead_picked), "picks": picks_log}
    return eq, info


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def stats(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(252)
    mdd = (eq / eq.cummax() - 1).min()
    monthly = eq.resample("ME").last().pct_change().dropna()
    return {
        "CAGR": f"{cagr:.1%}",
        "변동성": f"{vol:.1%}",
        "Sharpe": f"{(r.mean() * 252) / vol:.2f}",
        "MDD": f"{mdd:.1%}",
        "월 승률": f"{(monthly > 0).mean():.0%}",
        "최종 배수": f"{eq.iloc[-1]:.2f}x",
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    close_raw, volume, uni = load_prices(args.refresh)
    tv_all = close_raw * volume
    close, n_bad = clean_prices(close_raw)
    print(f"비정상 일간 변동 {n_bad}건을 0으로 대체")

    eq_net, info = run_backtest(close, tv_all, with_costs=True)
    eq_gross, _ = run_backtest(close, tv_all, with_costs=False)

    kospi = fdr.DataReader("KS11", eq_net.index[0])["Close"].reindex(eq_net.index).ffill()
    kospi = kospi / kospi.iloc[0]

    table = pd.DataFrame({
        "전략 비용 반영": stats(eq_net),
        "전략 비용 제외": stats(eq_gross),
        "KOSPI 지수": stats(kospi),
    })
    print(f"\n기간: {eq_net.index[0].date()} ~ {eq_net.index[-1].date()}")
    print(table.to_string())
    print(f"\n평균 월 회전율: {info['turnover']:.0%}")
    print(f"편입 이력이 있는 상장폐지 종목: {info['delisted_picks']}개")

    yearly = pd.DataFrame({
        "전략": eq_net.resample("YE").last().pct_change(),
        "KOSPI": kospi.resample("YE").last().pct_change(),
    })
    yearly.iloc[0] = [eq_net.resample("YE").last().iloc[0] - 1, kospi.resample("YE").last().iloc[0] - 1]
    yearly.index = yearly.index.year
    print("\n연도별 수익률")
    print(yearly.map(lambda x: f"{x:+.1%}").to_string())

    names = uni.set_index("code")["name"]
    last = info["picks"][-1]
    print(f"\n최근 편입 종목 ({last['date'].date()})")
    print(", ".join(names.get(c, c) for c in last["picks"]))

    OUT_DIR.mkdir(exist_ok=True)
    pd.DataFrame({"전략": eq_net, "전략_비용제외": eq_gross, "KOSPI": kospi}).to_csv(
        OUT_DIR / "equity.csv", encoding="utf-8-sig"
    )
    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 1]})
        ax1.plot(eq_net, label="모멘텀 Top20 비용 반영")
        ax1.plot(eq_gross, label="비용 제외", alpha=0.5)
        ax1.plot(kospi, label="KOSPI", color="gray")
        ax1.set_yscale("log")
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:g}x"))
        ax1.yaxis.set_minor_formatter(plt.NullFormatter())
        ax1.legend()
        ax1.set_title("KOSPI 12-1 모멘텀 백테스트")
        ax2.fill_between(eq_net.index, eq_net / eq_net.cummax() - 1, 0, alpha=0.4)
        ax2.set_ylabel("낙폭")
        fig.tight_layout()
        fig.savefig(OUT_DIR / "equity.png", dpi=120)
        print(f"\n차트 저장: {OUT_DIR / 'equity.png'}")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
