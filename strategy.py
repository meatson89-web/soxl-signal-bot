# -*- coding: utf-8 -*-
"""
SOXL B전략 엔진.

봉 조립·RSI·상태머신을 백테스트(soxl_1h_backtest.py 계열)와 1:1로 맞춘다.
검증: verify_backtest.py 가 이 파일만 써서 문서의 +2,600% / MDD -66.3% /
      58거래 / 승률 87.9% 를 재현한다. 이 파일을 고치면 반드시 다시 돌릴 것.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

# ── 전략 파라미터 (SOXL B전략.txt) ────────────────────────────
RSI_LEN = 12
RSI_TH = 25.0
ARM_PCT = 0.10      # 평단 +10% 도달 시 트레일링 발동
TRAIL_PCT = 0.03    # 발동 후 고점 대비 -3%
HARD_PCT = 0.30     # 진입가 -30%. 이 전략의 본체다.

# ── 봉 구조 ──────────────────────────────────────────────────
# ET 09:30 ~ 15:59 를 UTC 정각으로 resample → 하루 7봉.
# 16:00 딱 1분짜리 봉을 포함하면(8봉/일) 누적이 +2600% → +518% 로 무너진다.
RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)   # 미포함


def to_hourly(bars: pd.DataFrame) -> pd.DataFrame:
    """분봉(UTC tz-aware index, open/high/low/close) → 정규장 1시간봉."""
    et = bars.index.tz_convert("America/New_York")
    keep = (
        (et.time >= RTH_OPEN)
        & (et.time < RTH_CLOSE)
        & (et.dayofweek < 5)
    )
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    cols = {c: a for c, a in agg.items() if c in bars.columns}
    return bars[keep].resample("1h").agg(cols).dropna(subset=["close"])


def add_rsi(hourly: pd.DataFrame, length: int = RSI_LEN) -> pd.DataFrame:
    """Wilder RSI. Pine 의 ta.rsi 와 동일 (RMA = ewm alpha=1/n, adjust=False)."""
    out = hourly.copy()
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    out["rsi"] = 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def drop_incomplete(hourly: pd.DataFrame, now_utc: pd.Timestamp,
                    settle_sec: int = 120) -> pd.DataFrame:
    """미완성 봉 제거. 봉 H 는 H+1h 가 지나고 settle_sec 이 더 흘러야 확정."""
    deadline = now_utc - pd.Timedelta(hours=1) - pd.Timedelta(seconds=settle_sec)
    return hourly[hourly.index <= deadline]


# ── 상태 ─────────────────────────────────────────────────────
def blank_state() -> dict:
    return {
        "status": "FLAT",      # FLAT | HOLD | ARMED
        "entry": None,
        "peak": None,
        "entry_time": None,
        "last_bar": None,
        "last_close": None,
        "last_rsi": None,
        "last_run": None,
        "last_summary_date": None,
    }


def levels(state: dict) -> dict:
    """현재 상태에서의 트리거 가격들. 미보유면 전부 None."""
    if state["status"] == "FLAT" or not state.get("entry"):
        return {"arm": None, "hard": None, "trail": None}
    entry = float(state["entry"])
    peak = float(state["peak"])
    return {
        "arm": entry * (1 + ARM_PCT),
        "hard": entry * (1 - HARD_PCT),
        "trail": peak * (1 - TRAIL_PCT) if state["status"] == "ARMED" else None,
    }


def process(state: dict, bars: pd.DataFrame) -> tuple[dict, list[dict]]:
    """새로 확정된 봉들을 순서대로 흘려보내며 상태를 전이시킨다.

    판정 순서는 Pine 원본 그대로다 — 청산이 진입보다 먼저이고,
    같은 봉에서 청산 후 재진입이 허용된다.
    """
    st = dict(state)
    events: list[dict] = []

    for ts, row in bars.iterrows():
        close = float(row["close"])
        rsi = row["rsi"]
        stamp = ts.isoformat()

        if st["status"] != "FLAT":
            entry = float(st["entry"])
            st["peak"] = max(float(st["peak"]), close)

            if st["status"] == "HOLD" and close >= entry * (1 + ARM_PCT):
                st["status"] = "ARMED"
                events.append({
                    "kind": "ARM", "at": stamp, "price": close,
                    "entry": entry, "ret": close / entry - 1,
                })

            reason = None
            if st["status"] == "ARMED" and close <= float(st["peak"]) * (1 - TRAIL_PCT):
                reason = "TRAIL"
            elif close <= entry * (1 - HARD_PCT):
                reason = "HARD"

            if reason:
                events.append({
                    "kind": f"SELL_{reason}", "at": stamp, "price": close,
                    "entry": entry, "peak": float(st["peak"]),
                    "ret": close / entry - 1,
                    "held_since": st["entry_time"],
                })
                st.update(status="FLAT", entry=None, peak=None, entry_time=None)

        if st["status"] == "FLAT" and pd.notna(rsi) and float(rsi) <= RSI_TH:
            st.update(status="HOLD", entry=close, peak=close, entry_time=stamp)
            events.append({
                "kind": "BUY", "at": stamp, "price": close, "rsi": float(rsi),
            })

        st["last_bar"] = stamp
        st["last_close"] = close
        st["last_rsi"] = None if pd.isna(rsi) else float(rsi)

    return st, events
