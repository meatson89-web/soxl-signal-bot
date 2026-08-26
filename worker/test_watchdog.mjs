/**
 * 감시견 판정 테스트.  실행: node worker/test_watchdog.mjs
 *
 * 이 봇은 한 달에 한 번 신호가 날까 말까 하므로, 침묵이 정상인지 고장인지
 * 구분되지 않으면 안 된다. 감시견은 평일마다 반드시 셋 중 하나를 한다.
 */
import { watchdog } from "./index.js";

const now = new Date("2026-08-26T22:00:00Z"); // ET 18:00 수요일 = cron 시각
const TODAY = "2026-08-26";
const fresh = new Date(now - 2 * 3600e3).toISOString();
const stale = new Date(now - 30 * 3600e3).toISOString();

const base = {
  status: "FLAT", entry: null, peak: null,
  last_close: 115.84, last_rsi: 47.7,
};

const cases = [
  ["첫 실행 전 (last_run 없음)",
    { ...base }, null],

  ["거래일 · 요약 이미 발송 → 조용",
    { ...base, last_run: fresh, last_bar: "2026-08-26T19:00:00+00:00",
      last_summary_date: TODAY }, null],

  ["휴장일 (오늘 봉 없음) → 휴장 안내",
    { ...base, last_run: fresh, last_bar: "2026-08-25T19:00:00+00:00",
      last_summary_date: "2026-08-25" }, "📭"],

  ["거래일인데 요약이 늦음 → 지연 발송",
    { ...base, last_run: fresh, last_bar: "2026-08-26T19:00:00+00:00",
      last_summary_date: "2026-08-25" }, "📊"],

  ["갱신 30시간 끊김 → 경보",
    { ...base, last_run: stale, last_bar: "2026-08-24T19:00:00+00:00",
      last_summary_date: "2026-08-24" }, "🚨"],

  ["끊김 · 오늘 이미 경보함 → 중복 없음",
    { ...base, last_run: stale, last_bar: "2026-08-24T19:00:00+00:00",
      watchdog_fired: TODAY }, null],
];

let failed = 0;
for (const [name, state, expect] of cases) {
  const { text } = watchdog(state, now);
  const got = text ? text.slice(0, 2) : null;
  const ok = expect === null ? text === null : got === expect;
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) console.log(`      기대 ${expect} / 실제 ${got}`);
}

// 같은 날 두 번 돌아도 한 번만 보내야 한다.
let s = { ...base, last_run: fresh, last_bar: "2026-08-25T19:00:00+00:00",
          last_summary_date: "2026-08-25" };
const first = watchdog(s, now);
s = { ...s, ...first.patch };
const second = watchdog(s, now);
const dedup = first.text !== null && second.text === null;
if (!dedup) failed++;
console.log(`${dedup ? "PASS" : "FAIL"}  같은 날 재실행 시 중복 발송 없음`);

console.log(failed ? `\n${failed}건 실패` : "\n전부 통과");
process.exit(failed ? 1 : 0);
