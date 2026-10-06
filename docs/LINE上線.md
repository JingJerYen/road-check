# 讓用戶用 LINE 訂閱：上線步驟

做完之後，任何人都可以：

- 加你的 LINE 官方帳號好友，按「傳送位置」選停車點，就訂閱了；
- 或在網站上放好圖釘，按綠色的「用 LINE 訂閱這個位置」，LINE 會開啟並帶入訂閱訊息，按送出就好；
- 每天早上（預設台灣時間 07:00）那附近有**新的**施工、封路、集會、臨時佔用道路，才會收到一則通知。

全部由一個程式負責：`roadcheck serve` = 網站 + LINE webhook + 每天定時推播。

## 1. 建 LINE 官方帳號與 Messaging API channel（約 10 分鐘）

1. 到 [LINE Developers](https://developers.line.biz/console/) 用 LINE 帳號登入，建立 Provider，再建立 **Messaging API** channel
   （過程中會一起建立 LINE 官方帳號）。
2. 在 channel 的 **Basic settings** 頁抄下 **Channel secret**。
3. 在 **Messaging API** 頁：
   - 最下面 **Channel access token (long-lived)** 按 Issue，抄下來。
   - 抄下 **Bot basic ID**（像 `@123abcde`），網站的按鈕要用。
4. 到 [LINE 官方帳號管理後台](https://manager.line.biz/) → 設定 → **回應設定**：
   - 「聊天」關閉、**Webhook 開啟**、「自動回應訊息」**關閉**（不然每則訊息都會多收到一則罐頭回覆）。

## 2. 啟動伺服器

```bash
export LINE_CHANNEL_SECRET=抄下的 channel secret
export LINE_CHANNEL_ACCESS_TOKEN=抄下的 access token
export LINE_BOT_BASIC_ID=@123abcde
export ROADCHECK_PUBLIC_URL=https://你的公開網址      # 第 3 步拿到；推播和回覆會附地圖連結
python3 -m roadcheck serve                            # 預設 http://localhost:8080，每天 07:00 推播
```

可調參數：`--notify-at 08:30`（台灣時間，空字串表示不推播）、`--fetch-days 7`、`--port 8080`。
資料庫預設是目前目錄的 `roadcheck.sqlite3`（`--db` 或 `ROADCHECK_DB` 可改），網站和 LINE 共用同一份。

## 3. 給它一個公開的 https 網址

LINE 只能把訊息送到公開的 https 網址。在自己電腦上跑的話，用通道服務把本機的 8080 埠公開出去，擇一：

- **ngrok**（免費帳號送一個固定網址，最省事）：註冊後在後台拿到固定網域，執行
  `ngrok http --url=你的網域.ngrok-free.app 8080`（舊版 ngrok 參數叫 `--domain`）。
- **Cloudflare Tunnel**：
  - 臨時試用：`cloudflared tunnel --url http://localhost:8080`，會印出一個 `https://xxx.trycloudflare.com`。
    **每次重開網址都會變**，要重新填 webhook。
  - 長期使用：需要一個自己的網域，照 Cloudflare 文件建立 named tunnel。

拿到網址後：

1. LINE Developers → Messaging API → **Webhook URL** 填 `https://你的網址/line/webhook`，按 **Verify**，應該顯示 Success。
2. 打開 **Use webhook**。
3. 把 `ROADCHECK_PUBLIC_URL` 設成 `https://你的網址`，重開 `roadcheck serve`。

## 4. 試一下

1. 用手機掃 Messaging API 頁上的 QR code 加好友，會收到歡迎訊息和按鈕。
2. 按「📍 傳送位置」選一個地點 → 收到「已訂閱」和那裡目前的狀況。
3. 按「未來 7 天」「半徑 200 m」之類的按鈕調整；傳「查詢」隨時看現況。
4. 用手機開 `https://你的網址/`，放圖釘，按綠色按鈕，確認 LINE 會帶入「訂閱 緯度,經度 100m 3天」。

## 5. 讓它一直開著

推播靠伺服器在跑，電腦關機就不會送。可以：

- 放在一台一直開著的電腦或小主機（樹莓派也行），用 systemd 開機自動啟動：

  ```ini
  # /etc/systemd/system/roadcheck.service
  [Unit]
  Description=roadcheck
  After=network-online.target

  [Service]
  WorkingDirectory=/home/你/road-check
  Environment=LINE_CHANNEL_SECRET=...
  Environment=LINE_CHANNEL_ACCESS_TOKEN=...
  Environment=LINE_BOT_BASIC_ID=@123abcde
  Environment=ROADCHECK_PUBLIC_URL=https://你的網址
  ExecStart=/usr/bin/python3 -m roadcheck serve
  Restart=always

  [Install]
  WantedBy=multi-user.target
  ```

  `sudo systemctl enable --now roadcheck`；通道（ngrok／cloudflared）也要一起常駐。
- 或放到雲端主機上，記得資料庫要放在不會被清掉的磁碟。

## 費用與額度

- 回覆（使用者傳訊息、bot 回）不計費。
- 推播有每月則數上限，依你的官方帳號方案而定（到官方帳號後台查）。roadcheck 只在**有新異動**時推播，
  而且同一個人的多個訂閱合併成**一次**推播，所以一個人一天最多用掉 1 則。
- 回覆裡已經給使用者看過的事件，隔天不會再推一次。

## 驗證狀況

開發環境連不到 LINE，所以沒有接真的 LINE 帳號。已經實測的：用假的 LINE API 跑完整流程
（簽章驗證、傳位置訂閱、網站按鈕帶入的訂閱訊息、查詢、加好友歡迎訊息、到點推播），
回覆與推播的 JSON 格式照 Messaging API 文件組（文字訊息、quickReply、push 最多 5 則）。
第一次接真帳號時，請照第 4 步走一遍；有問題看 `roadcheck serve -v` 的輸出。
