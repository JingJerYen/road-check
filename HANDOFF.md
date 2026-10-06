# 交接：目前狀態與下一步

## 這一輪做了什麼（2026-10-05）

網域開通後，兩個資料來源都接上真資料並驗證過，adapter 幾乎重寫：

- **今日施工**：原本的 data.taipei rid 是錯的（回傳資料集目錄），resourceAquire API 對這個資料集也不能用。
  真正的檔案是 `https://tpnco.blob.core.windows.net/blobfs/Todaywork.json`（GeoJSON，TWD97 座標，含施工範圍多邊形）。
  加了 TWD97→WGS84 轉換（`geo.twd97_to_wgs84`，對照 pyproj 誤差 < 1 cm）與 `Event.shapes`，
  比對時在多邊形裡＝0 m、路線穿過多邊形＝0 m。
- **外部管制路段**：頁面實際上是三個模式（活動管制／使用道路集會／臨時使用道路）的 WebForms GridView，
  要 cookie、viewstate、POST 翻頁；原本的 HTML 解析還會被巢狀分頁表格吃掉。改成「列表 HTML（看未來）＋
  地圖 API `caseMap3.ashx`（多邊形）」依案號合併。活動管制裡的里長／議員要求、文資管制區等長年規定會濾掉。
- SQLite 加 `shapes` 欄位，舊資料庫會自動 `ALTER TABLE`。
- 今日施工依核備文號合併分段（原本一件工程 28 段就推 28 次），丟掉「全市APP測試」假資料；一則訊息最多列 15 件。
- 測試 41 → 66，fixture 全部換成真實資料擷取（`tests/fixtures/*.real.json`，聯絡人已去識別）。

抓取時間：施工 JSON 幾秒；dig.taipei 預設 7 天、三種模式約 5–10 分鐘（臨時使用道路一天上百件，
每件逐案抓幾何約 1 秒）。不要臨時使用道路就設 `ROADCHECK_EXT_MODES=EXTREST,RALLY`。

## 網頁（2026-10-06 加入）

`python3 -m roadcheck web` → <http://localhost:8080>。在 Chromium 實測過：放圖釘、拖曳、切天數與半徑、
點結果卡片縮放地圖、重新整理保留設定、分享網址、手機版與深色模式、空資料庫自動抓資料（Python 3.8 也測過）。
開發環境連不到 OpenStreetMap 圖磚與 Nominatim，所以截圖底圖是灰的、地址搜尋與自動路名沒實測，
在一般網路下應該正常；連不上時頁面會提示改用地圖點選。

查詢只用每個來源「最近一批」的資料（`Store.list_current_events`），已撤銷的案件不會出現。
`roadcheck run` 目前仍用全部歷史事件（靠日期過濾），之後可以考慮也改用最近一批。

## 0. 環境

雲端環境 Network access 需要允許：

```
tpnco.blob.core.windows.net
dig.taipei
maps.googleapis.com
data.taipei
api.line.me
pypi.org
files.pythonhosted.org
github.com
```

## 1. 地址轉座標實測（還沒做）

預設 Google Geocoding API。開發環境已確認 `maps.googleapis.com` 連得到、無效金鑰會回
`{"status":"REQUEST_DENIED","error_message":...}`，正常回應的結構依官方文件寫在 `tests/test_geocode.py`。

- [ ] Google Cloud Console 啟用 Geocoding API、建立金鑰（限制只能用 Geocoding API），設 `GOOGLE_MAPS_API_KEY`。
- [ ] `python3 -m roadcheck geocode "台北市西園路二段255號"`，應該回 25.027x, 121.493x 附近。
- [ ] 把一份真實回應換進 `tests/test_geocode.py` 的 `GOOGLE_OK`。

TGOS（`ROADCHECK_GEOCODER=tgos`）保留但未實測，申請對象沒有個人；若日後要用，確認項目同上：
GET 參數是否被接受、XML 包 JSON 的格式、`AddressList` 欄位名、是否需要 `TGOS_REFERER`。

## 2. LINE 實測（還沒做）

- [ ] 建 Messaging API channel，設 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`。
- [ ] `python3 -m roadcheck serve-line`，用 ngrok 或 Cloudflare Tunnel 暴露，填 Webhook URL，開 "Use webhook"。
- [ ] 加好友 → 傳位置 → 收到「已訂閱」；傳「地址 台北市西園路二段255號」也要能訂閱。
- [ ] `python3 -m roadcheck run --dry-run` 看比對結果；`run` 真推。
- [ ] LINE 傳「天數 7」確認天數會改、通知標題變「未來 7 天」。

## 3. 排程

```cron
0 7 * * * cd /path/to/road-check && ROADCHECK_DB=/path/to/roadcheck.sqlite3 LINE_CHANNEL_ACCESS_TOKEN=... python3 -m roadcheck run >> run.log 2>&1
```

## 4. 已知限制與可以再做的事

- **活動管制的過濾**靠列表的「管制原因」欄（里長要求、議員要求、文資管制區域、配合管制）＋「超過 90 天」規則。
  地圖 API 本身沒有原因欄位，列表掛掉時只剩天數規則。
- **臨時使用道路**量很大，未來案件逐案抓幾何有上限（預設 150 案，依開始日由近到遠），超過的只剩路名比對。
  若嫌慢可以把 `ROADCHECK_EXT_DAYS` 調小。
- **路名抽取**是 regex（`models.extract_roads`），「至善路」這種以連接詞開頭的路名會被切壞；有座標的事件不受影響。
- 施工資料只有「今天在施工」的案件。真正的未來排程在 dig.taipei「預定施工路段」
  <https://dig.taipei/Tpdig/PWorkData.aspx>，結構應該類似，可複用 `_AspxSession` 與 `parse_listing`。
- 停管處禁停公告未做。
- `caseMap3.ashx` 還有 `fno`（發文字號）參數與 `URL`（公告圖檔）欄位，目前只存進 `extra`。
