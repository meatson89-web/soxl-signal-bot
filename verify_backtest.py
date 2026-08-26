# -*- coding: utf-8 -*-
"""strategy.py 가 SOXL B전략.txt 의 백테스트를 재현하는지 검증한다.

production 과 똑같이 한 봉씩 process() 에 흘려보낸다.
로컬에서만 돌린다 (Polygon 원본 CSV 필요). CI 에서는 실행하지 않는다.
"""
import sys
import io

import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from strategy import add_rsi, blank_state, process, to_hourly  # noqa: E402

SRC = "D:/260508/soxl_1min_raw.csv"
FEE = 0.0005  # Pine commission_value = 0.05 (편도 %)

TARGET = {"ret": 26.00, "mdd": -0.663, "trades": 58, "winrate": 0.879}

raw = pd.read_csv(SRC, parse_dates=["datetime"])
raw["datetime"] = pd.to_datetime(raw["datetime"], utc=True)
raw = raw.set_index("datetime").sort_index()

bars = add_rsi(to_hourly(raw))
print(f"1시간봉 {len(bars)}개  {bars.index[0]} ~ {bars.index[-1]}")

state = blank_state()
cash, shares = 10000.0, 0.0
equity = []
rets = []

for ts in bars.index:
    one = bars.loc[[ts]]
    state, events = process(state, one)
    close = float(one["close"].iloc[0])
    for ev in events:
        if ev["kind"] == "BUY":
            shares = cash * (1 - FEE) / ev["price"]
            cash = 0.0
        elif ev["kind"].startswith("SELL"):
            cash = shares * ev["price"] * (1 - FEE)
            shares = 0.0
            rets.append(ev["ret"])
    equity.append(cash + shares * close)

eq = pd.Series(equity, index=bars.index)
total = eq.iloc[-1] / 10000 - 1
mdd = ((eq - eq.cummax()) / eq.cummax()).min()
wins = sum(1 for r in rets if r > 0)
winrate = wins / len(rets) if rets else float("nan")
exposure = (pd.Series([s != "FLAT" for s in [state["status"]]]).mean()
            if False else None)

print(f"\n{'':14}{'재현':>12}{'문서':>12}")
print(f"{'누적수익':<14}{total*100:>11.1f}%{TARGET['ret']*100:>11.0f}%")
print(f"{'MDD':<14}{mdd*100:>11.1f}%{TARGET['mdd']*100:>11.1f}%")
print(f"{'거래수':<14}{len(rets):>12}{TARGET['trades']:>12}")
print(f"{'승률':<14}{winrate*100:>11.1f}%{TARGET['winrate']*100:>11.1f}%")

ok = (
    abs(total - TARGET["ret"]) / TARGET["ret"] < 0.02
    and abs(mdd - TARGET["mdd"]) < 0.01
    and len(rets) == TARGET["trades"]
    and abs(winrate - TARGET["winrate"]) < 0.01
)
print("\n" + ("PASS — 엔진이 백테스트와 일치한다." if ok else "FAIL — 엔진이 백테스트와 다르다!"))
print(f"현재 상태: {state['status']}"
      + (f"  진입 ${state['entry']:.2f}  고점 ${state['peak']:.2f}" if state["entry"] else ""))
sys.exit(0 if ok else 1)
