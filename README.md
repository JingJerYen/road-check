# roadcheck

訂閱一條台北市的通勤路線，或一個固定停車位置。每天自動比對台北市政府公開資料，
路線或停車點附近有即將開始的**道路施工**、**活動封路／交通管制**時，透過 **LINE Bot** 推播通知。

純 Python 3.11 標準庫，零外部依賴。資料存 SQLite。

## 現況

| 項目 | 狀態 |
| --- | --- |
| 幾何比對（點半徑、路線緩衝） | ✅ 完成，有測試 |
| 路名比對（給沒座標的封路公告） | ✅ 完成，有測試 |
| 資料來源：臺北市今日施工資訊（data.taipei，有座標） | ⚠️ 解析完成，資料檔在 Azure blob，**開發環境被擋，欄位尚未驗證** |
| 資料來源：dig.taipei 預定施工路段（新工處道路／人行道更新預告，無座標） | ✅ 真資料驗證，約 320 筆，一次匯出 |
| 資料來源：dig.taipei 外部管制路段（使用道路集會、臨時使用道路，無座標） | ✅ 真資料驗證，ASP.NET 翻頁 |
| 資料來源：停管處禁停公告 | ❌ 未做 |
| 通知去重（同事件不重複推，日期變更會重推） | ✅ 完成，有測試 |
| LINE Messaging API 推播 | ✅ 完成，需 token 實測 |
| LINE webhook（傳位置即訂閱） | ✅ 完成，需 channel 實測 |
| 排程 | 用 cron 跑 `roadcheck run` |

今日施工資訊的檔案放在 `tpnco.blob.core.windows.net`，開發環境的網路政策擋住了它，剩下的驗證步驟見 [HANDOFF.md](HANDOFF.md)。

## 快速開始

```bash
git clone https://github.com/JingJerYen/road-check && cd road-check
python3 -m unittest discover -s tests      # 63 tests，含真實頁面 fixture
python3 -m roadcheck demo                   # 用樣本資料離線跑一遍，看通知長什麼樣
```

`pip install -e .` 之後可以直接用 `roadcheck` 指令，不裝也能用 `python3 -m roadcheck`。

## 使用

```bash
# 訂閱停車位置（半徑 100 m），並加上路名讓沒座標的封路公告也能比對
roadcheck subscribe point --name 公司停車 --lat 25.0418 --lon 121.5440 --radius 100 --roads 忠孝東路四段 復興南路

# 訂閱通勤路線（線兩側 50 m）。點可以手打，或貼 Google Directions / OSRM 的 encoded polyline
roadcheck subscribe route --name 通勤 --points "25.0330,121.5654;25.0415,121.5495;25.0580,121.5440"
roadcheck subscribe route --name 通勤 --polyline "_p~iF~ps|U_ulLnnqC_mqNvxq`@" --line-user U1234567890abcdef

roadcheck list
roadcheck fetch                       # 抓所有來源存進 SQLite（外部管制路段要翻幾十頁，約 1 分鐘）
roadcheck fetch --source taipei_planned_work --dump   # 只抓一個來源，並印出原始回應前 4000 字
roadcheck run --dry-run               # 抓資料 + 比對 + 印出通知，不真的推播
roadcheck run                         # 正式跑；放進 cron 每天早上跑一次
```

`run` 預設只看今天起 2 天內的事件（`--horizon-days`），同一事件只通知一次，
除非它的日期、範圍或影響交通旗標改變。

### LINE Bot

1. 到 LINE Developers 建一個 Messaging API channel，拿 **channel secret** 與 **channel access token (long-lived)**。
2. 設定環境變數：

   ```bash
   export LINE_CHANNEL_SECRET=...
   export LINE_CHANNEL_ACCESS_TOKEN=...
   ```

3. 啟動 webhook，並把公開網址（ngrok、Cloudflare Tunnel 皆可）填到 channel 的 Webhook URL：

   ```bash
   roadcheck serve-line --port 8000
   ```

4. 使用者加好友後，在 LINE 裡**傳送位置**就完成訂閱。其他指令：`列表`、`刪除 2`、`半徑 200`、`路線 lat,lon;lat,lon`、`路名 忠孝東路四段`、`幫助`。
5. cron 每天跑 `roadcheck run`，有異動就推播給對應的 LINE userId。

免費方案每月 200 則推播，webhook 回覆走 reply 不計費。LINE Notify 已停止服務，這裡用的是 Messaging API。

## 架構

```
roadcheck/
  sources/     每個資料來源一個 adapter：fetch_raw() 抓原始資料，parse() 轉成 Event
               dig_taipei.py 是 dig.taipei 兩個 adapter 共用的 WebForms／表格工具
  models.py    Event（事件）、Subscription（訂閱）、Match；路名抽取
  geo.py       haversine、點到折線距離、encoded polyline 解碼
  dates.py     容錯日期解析（西元／民國、各種分隔符）
  store.py     SQLite：subscriptions / events / notifications
  matcher.py   訂閱 × 事件 → Match（距離優先，無座標時用路名）
  notify.py    ConsoleNotifier / LineNotifier，訊息格式
  linebot.py   LINE webhook 與指令處理（CommandHandler 與 HTTP 分離，可單測）
  cli.py       命令列
tests/fixtures *.sample.* 依官方欄位做的樣本；*.real*.html 真實頁面，離線測試用
```

事件流程：`fetch` 把每個來源的資料 upsert 進 `events`（以來源＋案號為主鍵，內容指紋用來偵測變更）
→ `match_all` 先用日期視窗過濾，再對每個訂閱算距離或比路名
→ 每個訂閱彙整成一則訊息 → 推播成功才寫入 `notifications`。

## 資料來源

- 臺北市今日施工資訊（工務局）：<https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a>
  欄位 X/Y 是經緯度，Cb_Da/Ce_Da 核准起迄日，IsBlock 是否影響交通。
  這個資料集是「系統介接」，頁面的「下載」直接指向 <https://tpnco.blob.core.windows.net/blobfs/Todaywork.json>，
  data.taipei 的 datastore API 查不到資料；預設抓 blob，可用 `ROADCHECK_TAIPEI_TODAY_URL` 覆寫。
- 臺北市道路挖掘管理中心 預定施工路段（新工處道路更新／人行道更新，未來排程）：
  <https://dig.taipei/Tpdig/PWorkData.aspx>，ASP.NET GridView；按「匯出Excel」可一次拿到整張 HTML 表，
  失敗才逐頁翻。`caseid` 當事件 ID。可用 `ROADCHECK_TAIPEI_PWORK_URL` 覆寫。
- 臺北市道路挖掘管理中心 外部管制路段：<https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx>，
  ASP.NET WebForms，用民國日期查詢、`__doPostBack` 翻頁（每頁 10 筆）。
  三個分頁裡「活動管制」其實是**挖掘管制**（議員要求、文資區等，期間動輒數年，與通勤無關），
  預設只抓「使用道路集會」與「臨時使用道路」。
  `ROADCHECK_TAIPEI_EXT_DAYS`（查詢窗口，預設 3 天）、`ROADCHECK_TAIPEI_EXT_MODES`（預設 `1,2`）、
  `ROADCHECK_TAIPEI_EXT_URL`。

dig.taipei 兩個來源共用 `roadcheck/sources/dig_taipei.py`（WebForms session、巢狀表格解析、表頭對應、期間解析）。
`tests/fixtures/*.real*.html` 是 2026-10 抓回來的真實頁面（`__VIEWSTATE` 已去掉）。

## 後續

1. 放行 `tpnco.blob.core.windows.net` 後驗證今日施工 JSON 的欄位（見 HANDOFF.md）。
2. 停管處「暫停收費並禁止停放」公告：純文字，需抽路段，對回路邊停車格座標資料集。
3. 文字路段轉幾何：用 OSM 路網把「忠孝東路四段 復興南路口至敦化南路口」變成線段，取代路名比對。
4. 廟會遶境：分局公告／新聞稿爬取與 LLM 抽取。
5. 新北市：data.gov.tw 177913 有同類施工資料，加一個 adapter 即可。
