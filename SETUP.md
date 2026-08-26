# 설치 순서

토큰·키는 **전부 직접 입력**한다. 이 저장소에는 어떤 자격증명도 들어 있지 않다.
순서를 지킬 것 — 뒤 단계가 앞 단계 결과를 쓴다.

---

## 1. 텔레그램 봇 만들기

텔레그램에서 **@BotFather** 를 찾아 대화 시작:

```
/newbot
이름:      SOXL 시그널
사용자명:  soxl_signal_xxxx_bot      ← 반드시 _bot 으로 끝나야 한다
```

받은 토큰(`8123456789:AAH...` 형태)을 적어둔다. → 이하 **BOT_TOKEN**

> 알림음을 따로 주고 싶으면: 봇 대화방 → 우측 상단 → 알림 → 소리 변경.
> 하드스탑을 놓치면 안 되므로 권장.

---

## 2. Cloudflare 준비

```bash
cd D:/260508/soxl_bot/worker
npx wrangler login                       # 브라우저에서 승인
npx wrangler kv namespace create STATE
```

마지막 명령이 출력하는 `id = "abc123..."` 를 `wrangler.toml` 의
`PUT_KV_NAMESPACE_ID_HERE` 자리에 붙여넣는다. → 이하 **KV_ID**

시크릿 등록 (각각 값을 물어본다):

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN     # 1번의 BOT_TOKEN
npx wrangler secret put TG_WEBHOOK_SECRET      # 아무 랜덤 문자열 (예: 32자)
npx wrangler secret put DASH_PATH              # 대시보드 비밀 경로 (예: 랜덤 24자)
```

배포:

```bash
npx wrangler deploy
```

출력된 주소를 적어둔다 (`https://soxl-bot.<계정>.workers.dev`). → **WORKER_URL**

---

## 3. 텔레그램 웹훅 연결

`BOT_TOKEN`, `WORKER_URL`, `TG_WEBHOOK_SECRET` 을 채워서:

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=<WORKER_URL>/tg" \
  -d "secret_token=<TG_WEBHOOK_SECRET>"
```

`{"ok":true,...}` 가 나오면 성공.

---

## 4. chat_id 알아내기

텔레그램에서 **내 봇에게** 메시지를 보낸다:

```
/id
```

봇이 `이 대화의 chat_id 는 123456789 입니다.` 라고 답한다. → **CHAT_ID**

> 이게 텔레그램이 쓰는 진짜 주소다. 휴대폰 번호와는 무관하다.

이제 Worker 에도 등록한다 (등록 후에는 이 chat_id 만 명령을 쓸 수 있다):

```bash
npx wrangler secret put TELEGRAM_CHAT_ID       # 4번의 CHAT_ID
```

---

## 5. Cloudflare API 토큰 (GitHub Actions 가 KV 에 쓰기 위함)

dash.cloudflare.com → 우측 상단 프로필 → **API Tokens** → Create Token
→ **Create Custom Token**

| 항목 | 값 |
|---|---|
| Permissions | `Account` · `Workers KV Storage` · **Edit** |
| Account Resources | Include · 본인 계정 |

생성된 토큰 → **CF_API_TOKEN**
같은 화면 우측 또는 대시보드 개요에서 **Account ID** 확인 → **CF_ACCOUNT_ID**

---

## 6. GitHub Secrets

저장소 → **Settings → Secrets and variables → Actions → New repository secret**
아래 5개를 등록한다.

| 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 1번 |
| `TELEGRAM_CHAT_ID` | 4번 |
| `CF_ACCOUNT_ID` | 5번 |
| `CF_KV_NAMESPACE_ID` | 2번의 KV_ID |
| `CF_API_TOKEN` | 5번 |

명령으로 넣어도 된다 (값을 물어본다):

```bash
cd D:/260508/soxl_bot
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set CF_ACCOUNT_ID
gh secret set CF_KV_NAMESPACE_ID
gh secret set CF_API_TOKEN
```

선택 — yfinance 가 막혔을 때 쓸 예비 시세 소스
(twelvedata.com 무료 가입 → 800회/일):

```bash
gh secret set TWELVEDATA_API_KEY
```

---

## 7. 첫 실행

```bash
gh workflow run "SOXL signal check"
gh run watch
```

성공하면:
- 텔레그램으로 **일일 요약**이 온다
- `https://<WORKER_URL>/d/<DASH_PATH>` 에서 대시보드가 보인다
- 텔레그램에 `/status` 를 치면 즉시 답한다

이 세 가지가 다 되면 끝이다. 이후에는 평일 미국장 중 15분마다
자동으로 돌면서 신호가 날 때만 연락이 온다.

---

## 점검 목록

- [ ] `/status` 가 즉시 답한다
- [ ] 대시보드가 열린다
- [ ] Actions 탭에서 스케줄 실행이 초록불이다
- [ ] 장 마감 후(KST 새벽 5~6시) 일일 요약이 온다

**일일 요약이 이틀 이상 안 오면 봇이 죽은 것이다.** Actions 탭을 확인할 것.
Cloudflare 감시견이 12시간 이상 갱신 없음을 감지하면 따로 알려준다.
