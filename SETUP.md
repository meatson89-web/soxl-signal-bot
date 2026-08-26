# 설치 기록 / 재구축 순서

이 저장소에는 어떤 자격증명도 들어 있지 않다.
토큰은 Cloudflare Worker Secrets 와 GitHub Secrets 에만 있다.

## 자격증명이 어디에 있나

```
Cloudflare Worker Secrets        GitHub Secrets
├ TELEGRAM_BOT_TOKEN             ├ WORKER_URL
├ TELEGRAM_CHAT_ID               └ BOT_SYNC_SECRET
├ TG_WEBHOOK_SECRET
├ DASH_PATH                      (선택) TWELVEDATA_API_KEY
└ BOT_SYNC_SECRET
```

GitHub 에는 **텔레그램 토큰도 Cloudflare API 토큰도 두지 않는다.**
`bot.py` 는 Worker 의 `/sync`·`/notify` 를 거쳐 KV 를 읽고 쓰고 알림을 보낸다.
`BOT_SYNC_SECRET` 하나가 그 통로를 지킨다.

로컬 사본은 `worker/.secrets.local` 에 있다 (gitignore 처리됨).

---

## 1. 텔레그램 봇

@BotFather → `/newbot` → 이름·사용자명(`_bot` 으로 끝날 것) → 토큰 발급.

> 하드스탑 알림을 놓치면 안 되므로, 봇 대화방 → 알림 → 소리를 따로 지정해 둘 것.

## 2. Cloudflare

```bash
cd worker
npx wrangler login
npx wrangler kv namespace create STATE     # 출력된 id 를 wrangler.toml 에 기입
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TG_WEBHOOK_SECRET  # 랜덤 32자
npx wrangler secret put DASH_PATH          # 랜덤 24자 (대시보드 비밀 경로)
npx wrangler secret put BOT_SYNC_SECRET    # 랜덤 32자
npx wrangler deploy
```

## 3. 텔레그램 웹훅

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=<WORKER_URL>/tg" \
  --data-urlencode "secret_token=<TG_WEBHOOK_SECRET>" \
  --data-urlencode 'allowed_updates=["message"]'
```

## 4. chat_id

봇에게 `/id` 를 보내면 답해준다. 그 값을 등록한다:

```bash
npx wrangler secret put TELEGRAM_CHAT_ID
```

등록 후에는 이 chat_id 만 명령을 쓸 수 있다.
(휴대폰 번호와는 무관한 값이다.)

## 5. GitHub

```bash
gh auth refresh -h github.com -s workflow   # 워크플로 푸시에 필요. 대화형 터미널에서.
gh secret set WORKER_URL                    # https://soxl-bot.<계정>.workers.dev
gh secret set BOT_SYNC_SECRET
gh secret set TWELVEDATA_API_KEY            # 선택 — yfinance 예비 소스
```

## 6. 첫 실행

```bash
gh workflow run "SOXL signal check"
gh run watch
```

---

## 점검 목록

- [ ] 텔레그램에 `/status` → 즉시 답한다
- [ ] `https://<WORKER_URL>/d/<DASH_PATH>` → 대시보드가 열린다
- [ ] Actions 탭 스케줄 실행이 초록불
- [ ] 장 마감 후(KST 새벽 5~6시) 일일 요약이 온다

**일일 요약이 이틀 이상 안 오면 봇이 죽은 것이다.** Actions 탭을 확인할 것.
12시간 이상 갱신이 없으면 Cloudflare 감시견이 따로 알려준다.

## 로컬 테스트

```bash
python bot.py --local --dry                  # Worker·텔레그램 없이
WORKER_URL=... BOT_SYNC_SECRET=... python bot.py --dry   # Worker 만 붙여서
```
