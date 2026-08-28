/**
 * SOXL B전략 봇 — Cloudflare Worker
 *
 *   POST /tg              텔레그램 웹훅. 명령을 즉시 처리한다.
 *   GET  /d/{DASH_PATH}   대시보드. KV 를 읽어 매 요청마다 렌더한다.
 *   cron                  감시견. GitHub Actions 가 멈추면 알린다.
 *
 * 상태의 단일 원본은 KV 의 "state" 키다. bot.py 와 이 파일이 같이 읽고 쓴다.
 */

const ARM_PCT = 0.10;
const TRAIL_PCT = 0.03;
const HARD_PCT = 0.30;
const RSI_TH = 25;

// 감시견 cron. 나머지 cron 은 전부 GitHub Actions 를 깨우는 용도다.
const WATCHDOG_CRON = "0 22 * * 1-5";

// ── KV ────────────────────────────────────────────────────────
async function getState(env) {
  return (await env.STATE.get("state", "json")) || {
    status: "FLAT", entry: null, peak: null, entry_time: null,
    last_bar: null, last_close: null, last_rsi: null, last_run: null,
  };
}
const putState = (env, s) => env.STATE.put("state", JSON.stringify(s));

function levels(s) {
  if (s.status === "FLAT" || !s.entry) return { arm: null, hard: null, trail: null };
  return {
    arm: s.entry * (1 + ARM_PCT),
    hard: s.entry * (1 - HARD_PCT),
    trail: s.status === "ARMED" ? s.peak * (1 - TRAIL_PCT) : null,
  };
}

// ── 텔레그램 ──────────────────────────────────────────────────
function tg(env, chatId, text) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
  });
}

const kst = (iso) => {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("sv-SE", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).replace("T", " ");
};
// 봉 인덱스는 봉의 '시작' 시각이다. 표시는 마감 시각(+1h)으로 한다.
const barEnd = (iso) => (iso == null ? null : new Date(new Date(iso).getTime() + 3600e3));
const kstBar = (iso) => (iso == null ? "-" : kst(barEnd(iso)));
const barAge = (iso) => {
  if (iso == null) return "";
  const h = (Date.now() - barEnd(iso).getTime()) / 3600e3;
  if (h < 1.6) return "";
  return h < 48 ? `  (${Math.round(h)}시간 전)` : `  (${Math.round(h / 24)}일 전)`;
};
const tickLabel = (t) => {
  const m = (Date.now() - new Date(t.at).getTime()) / 60000;
  if (m <= 20) return `${t.sess} · ${kst(t.at)} KST`;
  const ago = m < 90 ? `${Math.round(m)}분 전` : `${Math.round(m / 60)}시간 전`;
  return `장 종료 · ${kst(t.at)} KST (${ago})`;
};
const money = (v) => (v == null ? "-" : "$" + Number(v).toFixed(2));
const pct = (v) => (v == null ? "-" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%");

function statusText(s) {
  const lv = levels(s);
  const c = s.last_close;
  let out = "SOXL 상태\n";
  if (s.tick) {
    out += `현재가 ${money(s.tick.price)}   ${tickLabel(s.tick)}\n`;
  }
  out += `기준봉 ${kstBar(s.last_bar)} KST 마감${barAge(s.last_bar)}\n`;
  out += `봉 종가 ${money(c)}   RSI(12) ${s.last_rsi == null ? "-" : s.last_rsi.toFixed(1)}\n`;
  out += `(매매 판정은 정규장 1시간봉 종가로만 합니다)\n\n`;

  if (s.status === "FLAT") {
    out += "상태: FLAT (미보유)\n";
    if (s.last_rsi != null) {
      out += `진입 조건 RSI ≤ ${RSI_TH} 까지 ${(s.last_rsi - RSI_TH).toFixed(1)}p 남음`;
    }
    return out;
  }
  out += `상태: ${s.status === "ARMED" ? "ARMED (발동 후)" : "HOLD (발동 전)"}\n`;
  out += `진입 ${money(s.entry)}  (${kst(s.entry_time)} KST)\n`;
  out += `평가손익 ${pct(c / s.entry - 1)}\n`;
  out += `진입후 고점 ${money(s.peak)}\n\n`;
  if (s.status === "ARMED") {
    out += `트레일링 스탑 ${money(lv.trail)}  (종가 대비 ${pct(lv.trail / c - 1)})\n`;
  } else {
    out += `발동선 ${money(lv.arm)}  (종가 대비 ${pct(lv.arm / c - 1)})\n`;
  }
  out += `하드스탑 ${money(lv.hard)}  (종가 대비 ${pct(lv.hard / c - 1)})`;
  return out;
}

const HELP = [
  "SOXL B전략 알림봇",
  "",
  "/status         현재 상태와 트리거까지 거리",
  "/entry 123.45   진입가를 실제 체결가로 정정",
  "/skip           신호 났지만 안 삼 → 미보유로 되돌림",
  "/sold           매도 완료 → 미보유",
  "/hold 123.45    수동으로 보유 등록",
  "/peak 130.00    진입후 고점 수동 보정 (분할 등)",
  "/id             이 대화의 chat_id 확인",
].join("\n");

// ── 명령 처리 ─────────────────────────────────────────────────
async function handleCommand(env, chatId, text) {
  const parts = text.trim().split(/\s+/);
  const cmd = parts[0].toLowerCase().split("@")[0];
  const num = parseFloat(parts[1]);

  if (cmd === "/id") return `이 대화의 chat_id 는 ${chatId} 입니다.`;
  if (cmd === "/start" || cmd === "/help") return HELP;

  const s = await getState(env);

  if (cmd === "/status") return statusText(s);

  if (cmd === "/entry") {
    if (!isFinite(num) || num <= 0) return "사용법: /entry 123.45";
    if (s.status === "FLAT") return "보유 중이 아닙니다. 먼저 /hold 123.45 로 등록하세요.";
    s.entry = num;
    s.peak = Math.max(num, s.peak ?? num);
    s.status = s.peak >= num * (1 + ARM_PCT) ? "ARMED" : "HOLD";
    delete s.urgent;
    await putState(env, s);
    return `진입가를 ${money(num)} 로 정정했습니다.\n\n${statusText(s)}`;
  }

  if (cmd === "/hold") {
    if (!isFinite(num) || num <= 0) return "사용법: /hold 123.45";
    s.status = "HOLD";
    s.entry = num;
    s.peak = Math.max(num, s.last_close ?? num);
    s.entry_time = new Date().toISOString();
    if (s.peak >= num * (1 + ARM_PCT)) s.status = "ARMED";
    delete s.urgent;
    await putState(env, s);
    return `보유로 등록했습니다.\n\n${statusText(s)}`;
  }

  if (cmd === "/peak") {
    if (!isFinite(num) || num <= 0) return "사용법: /peak 130.00";
    if (s.status === "FLAT") return "보유 중이 아닙니다.";
    s.peak = num;
    s.status = num >= s.entry * (1 + ARM_PCT) ? "ARMED" : "HOLD";
    await putState(env, s);
    return `고점을 ${money(num)} 로 보정했습니다.\n\n${statusText(s)}`;
  }

  if (cmd === "/skip" || cmd === "/sold") {
    if (s.status === "FLAT") return "이미 미보유 상태입니다.";
    const was = s.entry;
    s.status = "FLAT";
    s.entry = null; s.peak = null; s.entry_time = null;
    delete s.urgent;
    await putState(env, s);
    const verb = cmd === "/skip" ? "건너뛴 것으로" : "매도한 것으로";
    return `${money(was)} 진입분을 ${verb} 처리했습니다. 이제 FLAT 입니다.\n\n`
      + (cmd === "/skip"
        ? "주의: RSI 가 계속 25 이하면 다음 봉에 또 매수 신호가 옵니다."
        : "다음 RSI ≤ 25 신호를 기다립니다.");
  }

  return "모르는 명령입니다.\n\n" + HELP;
}

// ── 대시보드 ──────────────────────────────────────────────────
function sparkline(bars, key, w, h, color) {
  const pts = bars.map((b) => b[key]).filter((v) => v != null);
  if (pts.length < 2) return "";
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const span = hi - lo || 1;
  const step = w / (pts.length - 1);
  const d = pts.map((v, i) =>
    `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - ((v - lo) / span) * h).toFixed(1)}`
  ).join(" ");
  return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"
    stroke-linejoin="round" stroke-linecap="round"/>`;
}

function hline(bars, key, value, w, h, color, label) {
  const pts = bars.map((b) => b[key]).filter((v) => v != null);
  if (!pts.length || value == null) return "";
  const lo = Math.min(...pts, value), hi = Math.max(...pts, value);
  const y = h - ((value - lo) / ((hi - lo) || 1)) * h;
  return `<line x1="0" y1="${y.toFixed(1)}" x2="${w}" y2="${y.toFixed(1)}"
    stroke="${color}" stroke-width="1.2" stroke-dasharray="4 3"/>
    <text x="4" y="${(y - 4).toFixed(1)}" fill="${color}" font-size="10">${label}</text>`;
}

function dashboard(s, bars) {
  const lv = levels(s);
  const c = s.last_close;
  const badge = { FLAT: "#64748b", HOLD: "#d97706", ARMED: "#059669" }[s.status] || "#64748b";
  const W = 680, H = 150, HR = 70;

  const rows = [];
  if (s.status === "FLAT") {
    rows.push(["진입 조건", `RSI(12) ≤ ${RSI_TH}`,
      s.last_rsi == null ? "-" : `${(s.last_rsi - RSI_TH).toFixed(1)}p 남음`]);
  } else {
    rows.push(["진입가", money(s.entry), kst(s.entry_time) + " KST"]);
    rows.push(["평가손익", pct(c / s.entry - 1), ""]);
    rows.push(["진입후 고점", money(s.peak), ""]);
    if (s.status === "ARMED") {
      rows.push(["트레일링 스탑", money(lv.trail), `종가 대비 ${pct(lv.trail / c - 1)}`]);
    } else {
      rows.push(["발동선 (+10%)", money(lv.arm), `종가 대비 ${pct(lv.arm / c - 1)}`]);
    }
    rows.push(["하드스탑 (-30%)", money(lv.hard), `종가 대비 ${pct(lv.hard / c - 1)}`]);
  }

  const KIND = {
    BUY: ["🔵", "매수 신호"], ARM: ["🟢", "트레일링 발동"],
    SELL_TRAIL: ["🔴", "매도 — 트레일링"], SELL_HARD: ["⚫", "매도 — 하드스탑"],
  };
  const recent = (s.recent || []).slice().reverse().map((e) => {
    const [icon, name] = KIND[e.kind] || ["•", e.kind];
    const ret = e.ret == null ? "" : ` (${pct(e.ret)})`;
    return `<li><span>${icon}</span><b>${name}</b>${ret}
      <time>${kstBar(e.at)} · ${money(e.price)}</time></li>`;
  }).join("") || "<li class=empty>아직 기록된 신호가 없습니다.</li>";

  const stale = s.last_run
    && (Date.now() - new Date(s.last_run).getTime()) > 6 * 3600e3;

  return `<title>SOXL 시그널</title>
<style>
  :root{--bg:#f8fafc;--card:#fff;--ink:#0f172a;--dim:#64748b;--line:#e2e8f0;--accent:#7c5cd6}
  :root:not([data-theme=light]){}
  @media (prefers-color-scheme:dark){:root:not([data-theme=light]){
    --bg:#0b1120;--card:#131c31;--ink:#e2e8f0;--dim:#94a3b8;--line:#243049}}
  :root[data-theme=dark]{--bg:#0b1120;--card:#131c31;--ink:#e2e8f0;--dim:#94a3b8;--line:#243049}
  *{box-sizing:border-box}
  body{margin:0;padding:20px;background:var(--bg);color:var(--ink);
    font:15px/1.55 -apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif}
  .wrap{max-width:720px;margin:0 auto;display:grid;gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
  .top{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .badge{background:${badge};color:#fff;font-weight:700;letter-spacing:.04em;
    padding:7px 16px;border-radius:999px;font-size:15px}
  .price{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
  .rsi{color:var(--dim);font-size:14px}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  td{padding:9px 0;border-bottom:1px solid var(--line)}
  tr:last-child td{border-bottom:0}
  td:first-child{color:var(--dim);width:38%}
  td:nth-child(2){font-weight:600;text-align:right;white-space:nowrap}
  td:last-child{color:var(--dim);text-align:right;font-size:13px;padding-left:12px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
    margin:0 0 12px}
  .chart{overflow-x:auto}
  svg{display:block;max-width:100%}
  ul{list-style:none;margin:0;padding:0}
  li{display:flex;align-items:baseline;gap:8px;padding:8px 0;
    border-bottom:1px solid var(--line);font-size:14px}
  li:last-child{border-bottom:0}
  li time{margin-left:auto;color:var(--dim);font-size:12px}
  li.empty{color:var(--dim)}
  footer{color:var(--dim);font-size:12px;text-align:center}
  .warn{background:#7f1d1d;color:#fff;padding:10px 14px;border-radius:10px;font-weight:600}
</style>
<div class="wrap">
  ${stale ? '<div class="warn">⚠️ 6시간 넘게 갱신되지 않았습니다. 봇이 멈췄을 수 있습니다.</div>' : ""}
  <div class="card">
    <div class="top">
      <span class="badge">${s.status}</span>
      <span class="price">${money(s.tick ? s.tick.price : c)}</span>
      <span class="rsi">RSI(12) ${s.last_rsi == null ? "-" : s.last_rsi.toFixed(1)}</span>
    </div>
    <div class="rsi" style="margin-top:8px">
      ${s.tick ? `현재가 · ${tickLabel(s.tick)}` : "확정 봉 종가"}
      &nbsp;·&nbsp; 기준봉 ${kstBar(s.last_bar)} KST 마감 ${money(c)}${barAge(s.last_bar)}</div>
  </div>
  <div class="card">
    <h2>트리거까지 거리</h2>
    <table>${rows.map((r) =>
      `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`).join("")}</table>
  </div>
  <div class="card chart">
    <h2>SOXL 1시간봉 · 최근 ${bars.length}봉</h2>
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      ${sparkline(bars, "c", W, H, "var(--accent)")}
      ${hline(bars, "c", lv.hard, W, H, "#dc2626", "하드스탑")}
      ${hline(bars, "c", lv.trail, W, H, "#ea580c", "트레일링")}
      ${hline(bars, "c", lv.arm, W, H, "#16a34a", "발동선")}
    </svg>
    <h2 style="margin-top:14px">RSI(12)</h2>
    <svg viewBox="0 0 ${W} ${HR}" width="${W}" height="${HR}">
      ${sparkline(bars, "r", W, HR, "#0891b2")}
      ${hline(bars, "r", RSI_TH, W, HR, "#dc2626", "25")}
    </svg>
  </div>
  <div class="card"><h2>최근 신호</h2><ul>${recent}</ul></div>
  <footer>마지막 갱신 ${kst(s.last_run)} KST</footer>
</div>`;
}

// ── GitHub Actions 깨우기 ─────────────────────────────────────
// GitHub 의 예약 실행(schedule)은 대부분 그냥 건너뛴다.
// 실측: 2026-08-26~27 이틀간 기대 ~64회 중 1회만 실행됐다.
// 그래서 15분 트리거는 Cloudflare 가 잡고, 여기서 workflow_dispatch 로 깨운다.
async function dispatchCheck(env) {
  if (!env.GH_TOKEN || !env.GH_REPO) return;
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/check.yml/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_TOKEN}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "soxl-bot",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (!r.ok) console.log("dispatch 실패 " + r.status + " " + (await r.text()));
}

// ── 라우팅 ────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/tg") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TG_WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json().catch(() => null);
      const msg = update?.message || update?.edited_message;
      const text = msg?.text;
      const chatId = msg?.chat?.id;
      if (!text || !chatId) return new Response("ok");

      // chat_id 가 아직 설정 전이면 /id 만 허용한다.
      const allowed = !env.TELEGRAM_CHAT_ID
        || String(chatId) === String(env.TELEGRAM_CHAT_ID);
      const reply = allowed
        ? await handleCommand(env, chatId, text)
        : `이 봇은 등록된 사용자만 씁니다. (이 대화 chat_id: ${chatId})`;
      await tg(env, chatId, reply);
      return new Response("ok");
    }

    // GitHub Actions 전용. bot.py 가 KV 읽기·쓰기와 알림 발송을 여기로 보낸다.
    // 덕분에 GitHub 에는 Cloudflare API 토큰도 텔레그램 토큰도 둘 필요가 없다.
    if (url.pathname === "/sync" || url.pathname === "/notify") {
      if (!env.BOT_SYNC_SECRET
          || request.headers.get("X-Bot-Secret") !== env.BOT_SYNC_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      if (url.pathname === "/notify") {
        const { text } = await request.json();
        await tg(env, env.TELEGRAM_CHAT_ID, text);
        return new Response("ok");
      }
      if (request.method === "GET") {
        return Response.json(await getState(env));
      }
      const body = await request.json();
      if (body.state) await putState(env, body.state);
      if (body.bars) await env.STATE.put("bars", JSON.stringify(body.bars));
      return new Response("ok");
    }

    if (request.method === "GET" && url.pathname === `/d/${env.DASH_PATH}`) {
      const [s, bars] = await Promise.all([
        getState(env),
        env.STATE.get("bars", "json").then((b) => b || []),
      ]);
      return new Response(dashboard(s, bars), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
      });
    }

    return new Response("not found", { status: 404 });
  },

  // 감시견 — GitHub Actions 가 죽어도 이건 Cloudflare 가 돌린다.
  // 평일 UTC 22:00 (KST 07:00). 미국장 마감 후라 그날 결과가 확정돼 있다.
  async scheduled(event, env, ctx) {
    if (event.cron !== WATCHDOG_CRON) {
      ctx.waitUntil(dispatchCheck(env));
      return;
    }
    const s = await getState(env);
    const { text, patch } = watchdog(s, new Date());
    if (patch) await putState(env, { ...s, ...patch });
    if (text) ctx.waitUntil(tg(env, env.TELEGRAM_CHAT_ID, text));
  },
};

/** 뉴욕 기준 날짜 (YYYY-MM-DD). */
function etDate(d) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(d);
}

/**
 * 하루 한 번 판정. 셋 중 하나다.
 *   1) 갱신이 12시간 넘게 끊김        → 🚨 경보 (하루 1회만)
 *   2) 정상인데 오늘 요약이 없었음    → 📭 휴장일 안내 (침묵 방지)
 *   3) 오늘 요약이 이미 나갔음        → 조용히 넘어감
 * 순수 함수라 로컬에서 테스트한다 (test_watchdog.mjs).
 */
export function watchdog(s, now) {
  if (!s.last_run) return { text: null, patch: null };

  const today = etDate(now);
  const hours = (now - new Date(s.last_run)) / 3600e3;

  if (hours > 12) {
    if (s.watchdog_fired === today) return { text: null, patch: null };
    return {
      patch: { watchdog_fired: today },
      text: `🚨 SOXL 봇이 ${hours.toFixed(0)}시간째 갱신되지 않았습니다.\n\n`
        + `마지막 실행 ${kst(s.last_run)} KST\n\n`
        + `GitHub Actions 가 멈췄거나 비활성화됐을 수 있습니다.\n`
        + `저장소 → Actions 탭을 확인하세요.`,
    };
  }

  const patch = s.watchdog_fired ? { watchdog_fired: null } : null;

  if (s.last_summary_date === today) return { text: null, patch };

  // 오늘 봉이 있었으면 거래일인데 요약이 늦은 것이고, 없으면 휴장일이다.
  const barDay = s.last_bar ? etDate(new Date(s.last_bar)) : null;
  const head = barDay === today
    ? `📊 SOXL 일일 요약 (지연 발송)\n\n`
    : `📭 오늘은 미국 증시 휴장일이었습니다.\n`
      + `봇은 정상 동작 중입니다.\n\n`;

  return {
    patch: { ...(patch || {}), last_summary_date: today },
    text: head + statusText(s),
  };
}

// 로컬 스모크 테스트용. Worker 런타임에서는 무시된다.
export { dashboard, levels, statusText, handleCommand, HELP };
