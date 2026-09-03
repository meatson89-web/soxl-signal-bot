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

    # prepost=True — 프리장/애프터장 봉까지 받는다. 상태머신은 여전히
    # to_hourly() 가 걸러낸 정규장 봉만 쓴다. 시간외 봉은 예고·급등락 감지 전용.
    df = yf.download(TICKER, interval="5m", period="60d", auto_adjust=False,
                     prepost=True, progress=False, threads=False)
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
        # 예비 소스. 무료 플랜은 시간외를 안 주므로 이쪽으로 넘어가면
        # 매매 판정은 그대로 돌고 시간외 예고만 조용해진다.
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


def fetch_alpaca_quote(feed=None):
    """Alpaca 최신 호가(bid/ask) 중간값. "현재가" 표시의 기본 소스다 (판정에는 안 쓴다).

    체결(trade) 이 아니라 호가(quote) 를 쓴다 — 체결은 실제로 거래가 나야만
    갱신되는데, 오버나잇처럼 거래가 뜸한 시간대엔 몇 시간이고 그대로일 수
    있다(2026-09 초 실측 — 같은 체결이 사흘째 그대로였다). 호가는 거래가
    없어도 마켓메이커가 계속 갱신하므로 "지금 얼마인가"에 더 가깝다. Alpaca
    공식 가이드도 오버나잇 시세는 Trades 가 아니라 Quotes 엔드포인트를 쓰라고
    안내한다.

    feed=None  → 기본(정규장·프리장·애프터장) 피드.
    feed="overnight" → Blue Ocean ATS 오버나잇(ET 20:00~04:00) 피드.
      Yahoo 는 이 구간에 데이터를 아예 안 주므로 이때는 Alpaca 가 유일한 소스다.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        return None
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{TICKER}/quotes/latest",
            params={"feed": feed} if feed else {},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=15,
        )
        r.raise_for_status()
        q = r.json()["quote"]
        print("Alpaca " + (feed or "기본") + " 호가 원본: " + json.dumps(q),
              file=sys.stderr)
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
        elif ask > 0:
            price = ask
        elif bid > 0:
            price = bid
        else:
            return None
        return price, pd.Timestamp(q["t"])
    except Exception as exc:
        print("Alpaca " + (feed or "실시간") + " 시세 실패: " + str(exc), file=sys.stderr)
        return None


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


# 봉 인덱스는 봉의 '시작' 시각이다. 사람에게는 마감 시각으로 보여준다.
# 23:00 KST 봉의 종가는 24:00 KST 의 가격이다.
def kst_bar(iso):
    if not iso:
        return "-"
    return kst((pd.Timestamp(iso) + pd.Timedelta(hours=1)).isoformat())


def et_bar(iso):
    """봉을 ET 구간으로. 예: 08-27 15:00~16:00 ET"""
    if not iso:
        return "-"
    a = pd.Timestamp(iso).tz_convert(ET)
    return (a.strftime("%m-%d %H:%M") + "~"
            + (a + pd.Timedelta(hours=1)).strftime("%H:%M ET"))


# ── 세션 · 예고 · 급등락 ─────────────────────────────────────
# 여기부터는 전부 "알림" 전용이다. state 의 status/entry/peak 은 건드리지 않는다.
# 시간외 봉을 매매 판정에 넣으면 5년 CAGR 100.6%→34.1%, MDD -40.8%→-81.5% 로
# 무너진다는 게 실측이다. 그래서 시간외는 끝까지 "예고" 로만 쓴다.
MOVE_STEP = 5.0        # 급등락 알림 단계 (%). 5 / 10 / 15 … 마다 한 번씩.
TICK_STALE_SEC = 20 * 60   # 이보다 묵은 틱은 시세가 멈춘 것으로 본다.
PRE_LABEL = {
    "PRE_BUY":   ("\U0001F535", "매수 조건 충족"),
    "PRE_ARM":   ("\U0001F7E2", "트레일링 발동선 도달"),
    "PRE_TRAIL": ("\U0001F534", "트레일링 스탑 이탈"),
    "PRE_HARD":  ("\u26AB", "하드스탑 이탈"),
}


def session_of(ts):
    """봉 시각(ET)으로 세션 이름. Pine 쪽 sessName 과 같은 구분."""
    t = ts.tz_convert(ET)
    hm = t.hour * 60 + t.minute
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "정규장"
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "프리장"
    if 16 * 60 <= hm < 20 * 60:
        return "애프터장"
    return "야간"


def in_overnight_window(ts_utc):
    """ET 20:00~04:00 (Blue Ocean ATS 오버나잇) 인가. 요일 경계는 안 따진다 —
    주말·목요일밤 이후에 불러도 Alpaca 응답 자체가 묵어서 stale 재판정으로 걸러진다."""
    h = ts_utc.tz_convert(ET).hour
    return h >= 20 or h < 4


def preview_events(state, price, rsi_live):
    """지금 가격이 다음 정규장 봉 종가라면 신호가 되는가. 상태는 안 바꾼다."""
    lv = S.levels(state)
    out = []
    if state["status"] == "FLAT":
        if rsi_live is not None and rsi_live <= S.RSI_TH:
            out.append({"kind": "PRE_BUY", "price": price, "rsi": rsi_live})
        return out
    if state["status"] == "HOLD" and price >= lv["arm"]:
        out.append({"kind": "PRE_ARM", "price": price, "level": lv["arm"]})
    if state["status"] == "ARMED" and price <= lv["trail"]:
        out.append({"kind": "PRE_TRAIL", "price": price, "level": lv["trail"]})
    if price <= lv["hard"]:
        out.append({"kind": "PRE_HARD", "price": price, "level": lv["hard"]})
    return out


def dedup_previews(state, evs):
    """조건이 한 번 풀렸다 다시 성립할 때만 재알림한다. 붙어 있는 동안은 조용."""
    live = [e["kind"] for e in evs]
    seen = set(state.get("pre_seen") or [])
    state["pre_seen"] = live
    return [e for e in evs if e["kind"] not in seen]


def day_move(raw, now):
    """직전 정규장 종가 대비 현재가. 프리장·애프터장 움직임까지 포함해서 본다."""
    et = raw.index.tz_convert(ET)
    rth = raw[(et.time >= S.RTH_OPEN) & (et.time < S.RTH_CLOSE) & (et.dayofweek < 5)]
    if rth.empty:
        return None
    closes = rth["close"].groupby(rth.index.tz_convert(ET).date).last()
    today = now.tz_convert(ET).date()
    prior = closes[[d < today for d in closes.index]]
    if prior.empty:
        return None
    base = float(prior.iloc[-1])
    price = float(raw["close"].iloc[-1])
    return {"base": base, "base_date": str(prior.index[-1]),
            "price": price, "move": price / base - 1}


def move_alert(state, mv, now):
    """|변동| 이 5%p 단계를 새로 넘었을 때만. 같은 날 같은 방향은 재알림 없음."""
    if mv is None:
        return None
    today = str(now.tz_convert(ET).date())
    box = state.get("move_seen") or {}
    if box.get("date") != today:
        box = {"date": today, "up": 0.0, "down": 0.0}
    side = "up" if mv["move"] >= 0 else "down"
    step = abs(mv["move"]) * 100 // MOVE_STEP * MOVE_STEP
    fired = None
    if step >= MOVE_STEP and step > box[side]:
        box[side] = step
        fired = dict(mv, step=step, side=side)
    state["move_seen"] = box
    return fired


# ── 메시지 ───────────────────────────────────────────────────
def render_preview(ev, sess, at):
    icon, name = PRE_LABEL[ev["kind"]]
    out = (icon + " [예고 · " + sess + "]  SOXL " + name + "\n"
           + kst(at.isoformat()) + " KST   (" + et_label(at.isoformat()) + ")\n\n"
           + "현재가 $%.2f\n" % ev["price"])
    if ev["kind"] == "PRE_BUY":
        out += "예상 RSI(12) %.1f  ≤ %g\n" % (ev["rsi"], S.RSI_TH)
    else:
        out += "기준선 $%.2f  (%+.1f%%)\n" % (
            ev["level"], (ev["price"] / ev["level"] - 1) * 100)
    return out + "\n※ 아직 확정 아닙니다. 정규장 1시간봉 종가로만 확정됩니다."


def render_move(mv, sess):
    arrow = "\U0001F680 급등" if mv["side"] == "up" else "\U0001F4A5 급락"
    return (arrow + " [" + sess + "]  SOXL %+.1f%%\n\n" % (mv["move"] * 100)
            + "직전 정규장 종가 $%.2f  (%s)\n" % (mv["base"], mv["base_date"])
            + "현재가 $%.2f\n\n" % mv["price"]
            + "매매 신호가 아닙니다. 시세 변동 알림입니다.")


def render_event(ev, state):
    # BUY(live=True) 는 ev["at"] 이 봉 시작 시각이 아니라 실제 틱 시각이므로
    # kst_bar/et_bar (+1h 봉마감 보정) 를 쓰면 안 된다.
    if ev["kind"] == "BUY" and ev.get("live"):
        when = kst(ev["at"]) + " KST   (" + et_label(ev["at"]) + ")"
    else:
        when = kst_bar(ev["at"]) + " KST 마감   (봉 " + et_bar(ev["at"]) + ")"
    k = ev["kind"]

    if k == "BUY":
        lv = S.levels(state)
        if ev.get("live"):
            head = (
                "🔵 SOXL 매수 신호 — 실시간\n" + when + "\n\n"
                + "예상 RSI(12) = %.1f  ≤ 25\n" % ev["rsi"]
                + "현재가 $%.2f\n\n" % ev["price"]
                + "※ 봉 마감 전 실시간 값 기준입니다. 이후 이 봉이 25 위로 "
                  "마감돼도 이미 기록된 진입은 되돌리지 않습니다.\n\n"
            )
        else:
            head = (
                "🔵 SOXL 매수 신호\n" + when + "\n\n"
                + "RSI(12) = %.1f  ≤ 25\n" % ev["rsi"]
                + "봉 종가 $%.2f\n\n" % ev["price"]
            )
        return (
            head
            + "하드스탑 $%.2f  (-30%%)\n" % lv["hard"]
            + "발동선   $%.2f  (+10%%)\n\n" % lv["arm"]
            + "이 가격으로 상태를 기록했습니다.\n"
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
            + "기준봉 " + kst_bar(state["last_bar"]) + " KST 마감"
            + "  (" + et_bar(state["last_bar"]) + ")\n\n"
            + "봉 종가 $%.2f   RSI(12) %.1f\n" % (close, rsi))

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
        body += "트레일링 스탑 $%.2f   (종가 대비 %+.1f%%)\n" % (
            lv["trail"], (lv["trail"] / close - 1) * 100)
    else:
        body += "발동선 $%.2f   (종가 대비 %+.1f%%)\n" % (
            lv["arm"], (lv["arm"] / close - 1) * 100)
    body += "하드스탑 $%.2f   (종가 대비 %+.1f%%)" % (
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
    cold = not last_bar
    if last_bar:
        fresh = hourly[hourly.index > pd.Timestamp(last_bar)]
    else:
        # 첫 실행은 최근 이력을 통째로 흘려 현재 포지션을 복원한다.
        # tail(1) 로 시작하면 직전 매수 신호를 건너뛰어 KV 가 FLAT 으로 굳는다.
        # 2026-08 에 실제로 그래서 보유 중인데 미보유로 잡혀 있었다.
        fresh = hourly
    print("[" + source + "] 확정봉 %d개, 신규 %d개, 마지막 %s"
          % (len(hourly), len(fresh), hourly.index[-1]))

    messages = []

    if len(fresh):
        state, events = S.process(state, fresh)
        for ev in events:
            with SIGNAL_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            if cold:          # 복원 중이다. 과거 신호를 쏟아내지 않는다.
                continue
            messages.append(render_event(ev, state))
            if ev["kind"] == "SELL_HARD":
                state["urgent"] = {"text": messages[-1], "left": 2}
        # 대시보드에 띄울 최근 신호 이력
        state["recent"] = (state.get("recent", []) + events)[-8:]
        if cold:
            messages.append("\U0001F195 SOXL 봇 상태 초기화\n최근 %d봉을 재생해 "
                            "현재 상태를 복원했습니다.\n\n" % len(fresh)
                            + render_summary(state))

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

    # ── 실시간 현재가 ────────────────────────────────────────
    # 봉 마감을 기다리지 않는다. 프리장·애프터장·오버나잇에도 그대로 돈다.
    # 여기서 정한 값은 표시(/status·대시보드·예고)에만 쓰고 status/entry/peak
    # 은 건드리지 않으므로 백테스트는 그대로다.
    #
    # 어느 소스도 "믿지" 않는다. 후보를 전부 모아 시각이 가장 최신인 것을 고른다.
    # 소스를 우선순위로 고르면 그 소스가 얼어붙었을 때 신선한 값을 덮어써 버린다 —
    # 2026-09 실측: overnight 피드가 Aug 31 세션 경계값(114.56)에 고정돼 있었는데
    # 실제가는 104~106 이었고, 그걸 우선하는 코드가 9% 틀린 값을 계속 표시했다.
    # 시각으로만 판정하면 얼어붙은 소스는 자동으로 밀려난다.
    #
    # 직전에 저장해둔 틱도 후보에 넣는다. 이번 실행에서 받은 게 전부 그보다
    # 옛날이면(소스 결함으로 데이터가 되돌아간 경우) 기존 값이 그대로 유지된다.
    cands = [("yahoo", float(raw["close"].iloc[-1]), raw.index[-1])]
    for feed in [None] + (["overnight"] if in_overnight_window(now) else []):
        got = fetch_alpaca_quote(feed)
        if got is not None:
            cands.append(("alpaca" + ("-" + feed if feed else ""), got[0], got[1]))
    prev_tick = state.get("tick")
    if prev_tick:
        cands.append((prev_tick.get("src", "yahoo"), float(prev_tick["price"]),
                      pd.Timestamp(prev_tick["at"])))

    src, price, tick_at = max(cands, key=lambda c: c[2])
    print("틱 후보: " + " | ".join(
        "%s %.2f @%s" % (s, p, t.isoformat()) for s, p, t in cands), file=sys.stderr)

    # 시세가 멈추는 시간대에는 마지막 틱이 몇 시간씩 남을 수 있다.
    # 그걸 현재가로 취급하면 예고·급등락이 묵은 가격으로 나간다.
    stale = (now - tick_at).total_seconds() > TICK_STALE_SEC
    sess = session_of(tick_at)

    if not stale:
        rsi_live = S.preview_rsi(hourly.iloc[-1], price)

        # 실시간 진입 — 정규장 중에는 확정봉을 기다리지 않고 실시간 RSI 로도
        # 매수 신호를 낸다. 장중에 반짝 25 아래로 찍었다가 그 봉 종가에는 다시
        # 올라오는 경우, 예전엔 신호가 아예 안 갔지만 이제는 그 순간 진입으로
        # 기록된다(봉이 마감되면 이 진입가를 기준으로 트레일링/스탑을 이어 판정).
        # 정규장 밖은 그대로 preview_events 의 "예고"로만 남는다.
        if (not cold and sess == "정규장" and state["status"] == "FLAT"
                and rsi_live is not None and rsi_live <= S.RSI_TH):
            state.update(status="HOLD", entry=price, peak=price,
                        entry_time=now.isoformat())
            ev = {"kind": "BUY", "at": now.isoformat(), "price": price,
                  "rsi": rsi_live, "live": True}
            with SIGNAL_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            messages.append(render_event(ev, state))
            state["recent"] = (state.get("recent", []) + [ev])[-8:]

        for ev in dedup_previews(state, preview_events(state, price, rsi_live)):
            messages.append(render_preview(ev, sess, tick_at))

        hit = move_alert(state, day_move(raw, now), now)
        if hit:
            messages.append(render_move(hit, sess))

    # 마지막으로 본 시세. 판정에는 안 쓰고 /status·대시보드 표시 전용이다.
    state["tick"] = {"price": price, "at": tick_at.isoformat(), "sess": sess, "src": src}
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

    print("상태: %s  신규봉 %d  메시지 %d  틱 %s %.2f%s"
          % (state["status"], len(fresh), len(messages), sess, price,
             " (묵음)" if stale else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
