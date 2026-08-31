/**
 * inMarketWindow() 판정 테스트.  실행: node worker/test_market_window.mjs
 *
 * 오버나잇(Blue Ocean ATS, ET 20:00~04:00) 추가 이후로는 금요일 애프터장
 * 종료(20:00 ET)부터 일요일 오버나잇 시작(20:00 ET) 까지만 닫혀 있어야 한다.
 * 2026-08-31 은 월요일, 08-28 은 금요일, 08-29 는 토요일, 08-30 은 일요일,
 * 09-02 는 수요일이다. UTC 오프셋은 -04:00 (EDT) 로 고정해 넣는다.
 */
import { inMarketWindow } from "./index.js";

const et = (dateStr, h) => new Date(`${dateStr}T${String(h).padStart(2, "0")}:00:00-04:00`);

const cases = [
  ["월요일 새벽 3시 ET (오버나잇)", et("2026-08-31", 3), true],
  ["월요일 오전 10시 ET (정규장)", et("2026-08-31", 10), true],
  ["수요일 밤 22시 ET (오버나잇)", et("2026-09-02", 22), true],
  ["금요일 오후 7시 ET (애프터장, 아직 열림)", et("2026-08-28", 19), true],
  ["금요일 밤 21시 ET (주말 시작, 닫힘)", et("2026-08-28", 21), false],
  ["토요일 아무 때나 (닫힘)", et("2026-08-29", 12), false],
  ["일요일 오후 7시 ET (아직 닫힘)", et("2026-08-30", 19), false],
  ["일요일 밤 21시 ET (오버나잇 재개, 열림)", et("2026-08-30", 21), true],
];

let failed = 0;
for (const [name, date, expect] of cases) {
  const got = inMarketWindow(date);
  const ok = got === expect;
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) console.log(`      기대 ${expect} / 실제 ${got}`);
}

console.log(failed ? `\n${failed}건 실패` : "\n전부 통과");
process.exit(failed ? 1 : 0);
