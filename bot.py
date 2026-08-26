# -*- coding: utf-8 -*-
"""SOXL B전략 알림 봇 — GitHub Actions 에서 15분마다 실행된다.

멱등(idempotent): "지금 몇 시냐"가 아니라 "KV 의 last_bar 이후 확정된 봉이
있냐"로 판단한다. cron 이 늦거나 건너뛰어도 다음 실행이 밀린 봉을 전부 처리한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time

import pandas as pd
import requests

import strategy as S

# 윈도우 콘솔(cp949)에서 이모지 출력 시 죽지 않게 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KST = dt.timezone(dt.timedelta(hours=9))
ET = "America/New_York"
LOCAL_STATE = pathlib.Path(__file__).with_name("state.local.json")
SIGNAL_LOG = pathlib.Path(__file__).with_name("signals.log")
CHART_BARS = 160

TICKER = "SOXL"


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit("환경변수 " + name + " 가 비어 있습니다.")
    return v


# ── 시세 ─────────────────────────────────────────────────────
def fetch_yahoo():
    import yfinance as yf

    df = yf.download(TICKER, interval="5m", period="60d", auto_adjust=False,
                     prepost=False, progress=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance 응답이 비었습니다")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
    df.index = pd.to_datetime(df.index, utc=True)
    return df.dropna().sort_index()


def fetch_twelvedata(api_key):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": TICKER, "interval": "5min", "outputsize": 5000,
                "timezone": "UTC", "apikey": api_key},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if "values" not in payload:
        raise RuntimeError("twelvedata: " + str(payload.get("message", payload)))
    df = pd.DataFrame(payload["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.set_index("datetime")[["open", "high", "low", "close"]].sort_index()


def fetch_bars():
    errors = []
    sources = [("yahoo", fetch_yahoo)]
    if os.environ.get("TWELVEDATA_API_KEY"):
        sources.append(("twelvedata",
                        lambda: fetch_twelvedata(env("TWELVEDATA_API_KEY"))))
    for name, fn in sources:
        for attempt in range(3):
            try:
                return fn(), name
            except Exception as exc:
                errors.append(name + "#" + str(attempt + 1) + ": " + str(exc))
                time.sleep(3 * (attempt + 1))
    raise RuntimeError("시세 수신 실패 — " + " | ".join(errors[-4:]))


# ── 상태 저장소 ──────────────────────────────────────────────
# KV 에 직접 붙지 않고 Worker 의 /sync 를 거친다. 그래야 GitHub 쪽에
# Cloudflare API 토큰도 텔레그램 토큰도 두지 않아도 된다.
class Store:
    def __init__(self, local):
        self.local = local
        if local:
            return
        self.base = env("WORKER_URL").rstrip("/")
        self.headers = {"X-Bot-Secret": env("BOT_SYNC_SECRET")}

    def get(self, key):
        if self.local:
            if key != "state" or not LOCAL_STATE.exists():
                return None
            return json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
        r = requests.get(self.base + "/sync", headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def put(self, key, value):
        if self.local:
            if key == "state":
                LOCAL_STATE.write_text(json.dumps(value, ensure_ascii=False),
                                       encoding="utf-8")
            return
        r = requests.post(self.base + "/sync", headers=self.headers, timeout=20,
                          json={key: value})
        r.raise_for_status()


# ── 텔레그램 (Worker 가 대신 보낸다) ─────────────────────────
def send(text, dry=False):
    print("--- TELEGRAM ---\n" + text + "\n----------------")
    if dry:
        return
    r = requests.post(
        env("WORKER_URL").rstrip("/") + "/notify",
        headers={"X-Bot-Secret": env("BOT_SYNC_SECRET")},
        json={"text": text},
        timeout=20,
    )
    if not r.ok:
        print("알림 전송 실패: " + str(r.status_code) + " " + r.text,
              file=sys.stderr)


def kst(iso):
    if not iso:
        return "-"
    return pd.Timestamp(iso).tz_convert(KST).strftime("%Y-%m-%d %H:%M")


def et_label(iso):
    if not iso:
        return "-"
    return pd.Timestamp(iso).tz_convert(ET).strftime("%m-%d %H:%M ET")


# ── 메시지 ───────────────────────────────────────────────────
def render_event(ev, state):
    when = kst(ev["at"]) + " KST   (봉 " + et_label(ev["at"]) + ")"
    k = ev["kind"]

    if k == "BUY":
        lv = S.levels(state)
        return (
            "🔵 SOXL 매수 신호\n" + when + "\n\n"
            + "RSI(12) = %.1f  ≤ 25\n" % ev["rsi"]
            + "봉 종가 $%.2f\n\n" % ev["price"]
            + "하드스탑 $%.2f  (-30%%)\n" % lv["hard"]
            + "발동선   $%.2f  (+10%%)\n\n" % lv["arm"]
            + "이 종가로 상태를 기록했습니다.\n"
            + "실제 체결가가 다르면  /entry 123.45\n"
            + "매수 안 하셨으면      /skip"
        )
    if k == "ARM":
        lv = S.levels(state)
        return (
            "🟢 트레일링 발동  (+%.1f%%)\n" % (ev["ret"] * 100) + when + "\n\n"
            + "진입 $%.2f → 현재 $%.2f\n" % (ev["entry"], ev["price"])
            + "이제부터 고점 대비 -3% 에서 매도 신호가 나갑니다.\n"
            + "현재 스탑 $%.2f" % lv["trail"]
        )
    if k == "SELL_TRAIL":
        return (
            "🔴 SOXL 매도 신호 — 트레일링\n" + when + "\n\n"
            + "진입 $%.2f → 현재 $%.2f   (%+.1f%%)\n" % (
                ev["entry"], ev["price"], ev["ret"] * 100)
            + "진입후 고점 $%.2f\n" % ev["peak"]
            + "스탑 $%.2f  (고점 -3%%)\n\n" % (ev["peak"] * (1 - S.TRAIL_PCT))
            + "보유 시작 " + kst(ev["held_since"]) + " KST\n\n"
            + "매도 완료하시면  /sold"
        )
    if k == "SELL_HARD":
        return (
            "⚫ SOXL 매도 신호 — 하드스탑\n" + when + "\n\n"
            + "진입 $%.2f → 현재 $%.2f   (%+.1f%%)\n\n" % (
                ev["entry"], ev["price"], ev["ret"] * 100)
            + "⚠️ 하드스탑은 이 전략의 본체입니다.\n"
            + "2022년이 -57.7% → -4.9% 로 바뀐 게 이 규칙 하나 때문입니다.\n"
            + "버티지 마시고 실행하세요.\n\n"
            + "매도 완료하시면  /sold"
        )
    return k + " " + when


def render_summary(state):
    lv = S.levels(state)
    close = state["last_close"]
    rsi = state["last_rsi"]
    head = ("📊 SOXL 일일 요약\n"
            + "기준봉 " + et_label(state["last_bar"]) + "\n\n"
            + "현재가 $%.2f   RSI(12) %.1f\n" % (close, rsi))

    if state["status"] == "FLAT":
        return head + ("\n상태: FLAT (미보유)\n"
                       + "진입 조건 RSI ≤ 25 까지 %+.1fp" % (rsi - 25))

    entry = float(state["entry"])
    peak = float(state["peak"])
    label = "ARMED (발동 후)" if state["status"] == "ARMED" else "HOLD (발동 전)"
    body = ("\n상태: " + label + "\n"
            + "진입 $%.2f  (" % entry + kst(state["entry_time"]) + " KST)\n"
            + "평가손익 %+.2f%%\n" % ((close / entry - 1) * 100)
            + "진입후 고점 $%.2f\n\n" % peak)
    if state["status"] == "ARMED":
        body += "트레일링 스탑 $%.2f   (현재가 대비 %+.1f%%)\n" % (
            lv["trail"], (lv["trail"] / close - 1) * 100)
    else:
        body += "발동선 $%.2f   (현재가 대비 %+.1f%%)\n" % (
            lv["arm"], (lv["arm"] / close - 1) * 100)
    body += "하드스탑 $%.2f   (현재가 대비 %+.1f%%)" % (
        lv["hard"], (lv["hard"] / close - 1) * 100)
    return head + body


# ── 메인 ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="KV 대신 로컬 파일 사용")
    ap.add_argument("--dry", action="store_true", help="텔레그램 전송 안 함")
    ap.add_argument("--force-summary", action="store_true")
    args = ap.parse_args()

    store = Store(local=args.local)
    state = store.get("state") or S.blank_state()
    now = pd.Timestamp.now(tz="UTC")

    try:
        raw, source = fetch_bars()
    except Exception as exc:
        state["last_error"] = str(exc)[:300]
        store.put("state", state)
        send("🚨 SOXL 봇 — 시세 수신 실패\n" + kst(now.isoformat())
             + " KST\n\n" + str(exc), args.dry)
        return 1

    full = S.add_rsi(S.to_hourly(raw))
    hourly = S.drop_incomplete(full, now)
    if hourly.empty:
        print("확정된 봉이 없습니다.")
        return 0

    last_bar = state.get("last_bar")
    if last_bar:
        fresh = hourly[hourly.index > pd.Timestamp(last_bar)]
    else:
        fresh = hourly.tail(1)
    print("[" + source + "] 확정봉 %d개, 신규 %d개, 마지막 %s"
          % (len(hourly), len(fresh), hourly.index[-1]))

    messages = []

    if len(fresh):
        state, events = S.process(state, fresh)
        for ev in events:
            messages.append(render_event(ev, state))
            with SIGNAL_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            if ev["kind"] == "SELL_HARD":
                state["urgent"] = {"text": messages[-1], "left": 2}
        # 대시보드에 띄울 최근 신호 이력
        state["recent"] = (state.get("recent", []) + events)[-8:]

    # 하드스탑은 놓치면 안 되므로 15분 간격으로 2회 더 재발송한다.
    urgent = state.get("urgent")
    if urgent and urgent.get("left", 0) > 0 and not messages:
        messages.append("🔁 재알림 (하드스탑)\n\n" + urgent["text"])
        urgent["left"] -= 1
        if urgent["left"] <= 0:
            state.pop("urgent", None)

    # 일일 요약 — 그날 마지막 봉(ET 15시)을 처리했을 때 1회.
    if state.get("last_bar"):
        bar_et = pd.Timestamp(state["last_bar"]).tz_convert(ET)
        day = bar_et.strftime("%Y-%m-%d")
        if (bar_et.hour == 15 and state.get("last_summary_date") != day) \
                or args.force_summary:
            messages.append(render_summary(state))
            state["last_summary_date"] = day

    state["last_run"] = now.isoformat()
    state.pop("last_error", None)

    chart = full.tail(CHART_BARS)
    store.put("bars", [
        {"t": int(ts.timestamp()), "c": round(float(r["close"]), 4),
         "r": None if pd.isna(r["rsi"]) else round(float(r["rsi"]), 2)}
        for ts, r in chart.iterrows()
    ])
    store.put("state", state)

    for m in messages:
        send(m, args.dry)

    print("상태: %s  신규봉 %d  메시지 %d"
          % (state["status"], len(fresh), len(messages)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
