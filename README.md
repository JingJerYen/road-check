# roadcheck

訂閱一條台北市的通勤路線，或一個固定停車位置。每天自動比對台北市政府公開資料，
路線或停車點附近有**道路施工**、**活動封路／集會／臨時佔用道路**時，透過 **LINE Bot** 推播通知。

純 Python 3.11 標準庫，零外部依賴。資料存 SQLite。

## 現況

| 項目 | 狀態 |
| --- | --- |
| 幾何比對（點半徑、路線緩衝、施工多邊形內＝0 m、路線穿過多邊形） | ✅ 完成，有測試 |
| TWD97 → WGS84 座標轉換（市府資料都是 TWD97） | ✅ 完成，對照 pyproj 誤差 < 1 cm |
| 路名比對（給沒座標的公告） | ✅ 完成，有測試 |
| 資料來源：臺北市今日施工資訊（Todaywork.json，GeoJSON） | ✅ **已接真資料驗證** |
| 資料來源：dig.taipei 外部管制路段（活動管制／使用道路集會／臨時使用道路） | ✅ **已接真資料驗證**，列表＋地圖 API 合併，有多邊形 |
| 資料來源：停管處禁停公告 | ❌ 未做 |
| 通知去重（同事件不重複推，日期變更會重推） | ✅ 完成，有測試 |
| LINE Messaging API 推播 | ✅ 完成，需 token 實測 |
| LINE webhook（傳位置即訂閱） | ✅ 完成，需 channel 實測 |
| 排程 | 用 cron 跑 `roadcheck run` |

還沒做的事見 [HANDOFF.md](HANDOFF.md)。

## 快速開始

```bash
git clone https://github.com/JingJerYen/road-check && cd road-check
python3 -m unittest discover -s tests      # 66 tests，離線
python3 -m roadcheck demo                   # 用真實資料的樣本離線跑一遍，看通知長什麼樣
python3 -m roadcheck fetch                  # 真的去抓（dig.taipei 要翻頁＋逐案抓幾何，約 5–10 分鐘）
```

`pip install -e .` 之後可以直接用 `roadcheck` 指令，不裝也能用 `python3 -m roadcheck`。

需要能連到：`tpnco.blob.core.windows.net`（施工 JSON）、`dig.taipei`、`api.line.me`。

## 使用

```bash
# 訂閱停車位置（半徑 100 m），並加上路名讓沒座標的公告也能比對
roadcheck subscribe point --name 公司停車 --lat 25.0418 --lon 121.5440 --radius 100 --roads 忠孝東路四段 復興南路

# 訂閱通勤路線（線兩側 50 m）。點可以手打，或貼 Google Directions / OSRM 的 encoded polyline
roadcheck subscribe route --name 通勤 --points "25.0330,121.5654;25.0415,121.5495;25.0580,121.5440"
roadcheck subscribe route --name 通勤 --polyline "_p~iF~ps|U_ulLnnqC_mqNvxq`@" --line-user U1234567890abcdef

roadcheck list
roadcheck fetch                       # 抓所有來源存進 SQLite
roadcheck fetch --source taipei_ext_restriction --dump   # 只抓一個來源並印原始資料
roadcheck run --dry-run               # 抓資料 + 比對 + 印出通知，不真的推播
roadcheck run --horizon-days 7        # 正式跑；放進 cron 每天早上跑一次
```

`run` 預設只看今天起 2 天內的事件（`--horizon-days`）。施工資料只有「今天在施工」的案件，
dig.taipei 的集會／臨時使用道路列表則可以看到未來（抓取時預設往後 7 天，`ROADCHECK_EXT_DAYS`），
想提早幾天收到預告就把 `--horizon-days` 調大。同一事件只通知一次，除非它的日期、範圍或影響交通旗標改變。

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
  models.py    Event（事件，含 shapes 多邊形／折線）、Subscription（訂閱）、Match；路名抽取
  geo.py       haversine、點到折線、點在多邊形內、線段相交、TWD97 轉換、encoded polyline 解碼
  dates.py     容錯日期解析（西元／民國、各種分隔符）
  store.py     SQLite：subscriptions / events / notifications（舊資料庫會自動補欄位）
  matcher.py   訂閱 × 事件 → Match（有 shapes 對形狀算距離，否則對代表點，都沒有就比路名）
  notify.py    ConsoleNotifier / LineNotifier，訊息格式
  linebot.py   LINE webhook 與指令處理（CommandHandler 與 HTTP 分離，可單測）
  cli.py       命令列
tests/fixtures 從真實資料擷取並裁短的樣本（聯絡人已去識別），離線測試用
```

事件流程：`fetch` 把每個來源的資料 upsert 進 `events`（以來源＋案號為主鍵，內容指紋用來偵測變更）
→ `match_all` 先用日期視窗過濾，再對每個訂閱算距離或比路名
→ 每個訂閱彙整成一則訊息（影響交通、距離近的排前面，最多列 15 件）→ 推播成功才寫入 `notifications`。

## 資料來源

### 臺北市今日施工資訊（工務局道路挖掘管理中心）

- 資料集：<https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a>，
  實際檔案 `https://tpnco.blob.core.windows.net/blobfs/Todaywork.json`（每 10 分鐘更新，約 2.7 MB、1200 筆）。
  data.taipei 的 resourceAquire API 對這個資料集**不能用**（回傳空陣列），要直接抓 blob。
- GeoJSON FeatureCollection。座標是 **TWD97 二度分帶（EPSG:3826）**，`Positions` 是施工範圍的
  MultiPolygon／MultiLineString，程式會轉成 WGS84 並用整個範圍算距離。
- 日期是民國 `yyy/mm/dd`，`IsBlock`／`IsStay` 是「是／否」，`C_Name` 沒有「區」字，
  `AppMode` 代碼：0 施工通報、3 銑鋪、4 搶修、5 道路維護、6 人手孔、B 建案公設復舊。
- 同一核備文號 `Ac_no` 會拆成很多筆 `sno`（一段路一筆，多的有 89 筆），程式合併成一個事件，
  所以約 1200 筆資料是 140 多件工程。裡面有市府的測試資料（「全市APP測試」，多邊形蓋住整個台北市），會被丟掉。
- 只有「今天在施工」的案件，沒有未來排程。可用 `ROADCHECK_TAIPEI_TODAY_URL` 覆寫 URL。

### 臺北市道路挖掘管理中心「外部管制路段」（dig.taipei）

- 頁面：<https://dig.taipei/TpdigR.net/Public/ExtRestData.aspx>，三種「管制來源」：
  **活動管制**（遶境、路跑、大型活動、電影拍攝；也混有里長／議員要求、文資管制區這種長年挖掘管理規定，會被濾掉）、
  **使用道路集會**（集會遊行路段）、**臨時使用道路**（吊車、搬家等臨時佔用，量很大，一天上百件）。
- 列表是 ASP.NET WebForms：要帶 cookie 與 `__VIEWSTATE`，POST 切換模式與查詢日期區間，
  `__doPostBack('GridViewN','Page$N')` 翻頁。每列的地圖連結 `ShowExtRest.aspx?key=` 就是案號。
- 幾何來自地圖頁用的 `Map/caseMap3.ashx?cmode=EXTREST|RALLY|URGENT&caseid=…&qds=…&qde=…`，
  回 JSON，每筆一個 TWD97 多邊形。活動管制依日期區間一次拿完；集會／臨時使用道路不帶案號只回今天的，
  未來的案件要逐案抓（每案約 1 秒，預設最多 150 案，`ROADCHECK_EXT_MAX_CASE_FETCHES`），
  抓不到幾何的就退回路名比對。
- 環境變數：`ROADCHECK_EXT_DAYS`（往後看幾天，預設 7）、`ROADCHECK_EXT_MODES`（預設 `EXTREST,RALLY,URGENT`，
  不想要臨時使用道路就設 `EXTREST,RALLY`，抓取會快很多）、`ROADCHECK_EXT_MAX_DAYS`（活動管制超過幾天視為長期規定，預設 90）。

## 後續

1. LINE channel 實測（見 HANDOFF.md）。
2. 停管處「暫停收費並禁止停放」公告：純文字，需抽路段，對回路邊停車格座標資料集。
3. 活動管制的「管制原因」目前靠列表文字過濾；若列表掛了只剩地圖 API，會改用「超過 90 天」的規則，可能漏掉長期活動。
4. 廟會遶境：分局公告／新聞稿爬取與 LLM 抽取（dig.taipei 的活動管制已涵蓋有申請的遶境）。
5. 新北市：data.gov.tw 177913 有同類施工資料，加一個 adapter 即可。
