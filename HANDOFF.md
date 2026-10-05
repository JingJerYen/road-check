# 交接：目前狀態與下一步

更新：2026-10-05。這個 session 的環境可以連 `data.taipei` 與 `dig.taipei`，
dig.taipei 的兩個 adapter 已經用真資料跑過並留下 fixture；
**今日施工資訊的 JSON 放在 `tpnco.blob.core.windows.net`，被網路政策擋住，還沒驗證**。

## 0. 環境設定（你來做）

雲端環境 → Edit → Network access 選 Custom，Allowed domains 要有：

```
data.taipei
dig.taipei
tpnco.blob.core.windows.net
api.line.me
pypi.org
files.pythonhosted.org
github.com
```

多了一個 `tpnco.blob.core.windows.net`。儲存後**新開 session** 才會生效。

## 1. 驗證「臺北市今日施工資訊」（唯一還沒接到真資料的來源）

```bash
python3 -m roadcheck fetch --source taipei_today_construction --dump
```

已查清楚的事：

- data.taipei 頁面上這個資料集是「系統介接」，「下載」按鈕直接連到
  `https://tpnco.blob.core.windows.net/blobfs/Todaywork.json`，**不在 data.taipei datastore**：
  頁面上的 rid `afb20478-f915-4ffa-a8e8-f78738b2e732` 用 `resourceAquire` 查只回 `[]`，
  舊的預設 rid `875ea014-…` 根本是另一個資料集（資料集目錄）。
  `DEFAULT_URL` 已改成 blob URL；datastore 形式留在 `DATASTORE_URL` 備用。
- data.taipei 沒有代理下載／預覽的 API 可以繞過 blob（`resource.download?rid=` 回 0 bytes）。

放行 blob 後要確認：

- [ ] 回傳形狀：直接 list，還是 `{"result":{"results":[...]}}`；`_records()` 兩種都接。
- [ ] 欄位名大小寫（`Ac_no` / `AC_NO`…）；`_get()` 不分大小寫找，名稱不同要加別名。
- [ ] `X`/`Y` 是經緯度還是 TWD97（數值像 3xxxxx / 27xxxxx 就是 TWD97，要加投影轉換）。
- [ ] `Cb_Da`/`Ce_Da` 的格式；`roadcheck/dates.py` 支援西元、民國、多種分隔符。
- [ ] 把一份真實回應存成 `tests/fixtures/taipei_today_construction.real.json`，比照
      `tests/test_dig_taipei.py` 加測試。

## 2. 「外部管制路段」— 已驗證 ✅

`roadcheck/sources/taipei_ext_restriction.py`，共用工具在 `roadcheck/sources/dig_taipei.py`。

- 頁面是 ASP.NET WebForms：GET 拿 `__VIEWSTATE` 與 session cookie，之後全部 POST 回同一 URL。
  查詢欄位是民國 `yyyMMdd`（`TxtQDate0`/`TxtQDate1`），篩選採期間重疊；翻頁用
  `__EVENTTARGET=GridView1|GridView2` + `__EVENTARGUMENT=Page$N`，翻頁要一併送查詢欄位。
- GridView 第一列是巢狀的分頁 `<table>`，舊的 `_TableParser` 會被它吃掉整張表，已改成 stack 版。
- 三個分頁：
  - `RadQMode0` 活動管制：表頭「挖掘管制日期｜管制原因｜施工單位｜管制路段」。**內容其實是挖掘管制**
    （議員要求、里長要求、文資管制區域…，期間到 2031 年都有），對通勤無意義，**預設不抓**，
    `ROADCHECK_TAIPEI_EXT_MODES=0,1,2` 可打開；抓到的 `blocks_traffic=None`。
  - `RadQMode1` 使用道路集會、`RadQMode2` 臨時使用道路：表頭「使用道路日期｜使用道路路段」，
    日期含時分秒（已抽成 `time_window`）。同一申請（`key`）常重複多列，已去重。
- 「匯出Excel」在這頁一律回「執行緒已經中止」，所以只能翻頁。每頁 10 筆；
  臨時使用道路 3 天窗口約 30 頁，整個 fetch 約 40–90 秒。`ROADCHECK_TAIPEI_EXT_DAYS` 控制窗口，
  要 ≥ `run --horizon-days`。
- fixture：`tests/fixtures/taipei_ext_restriction.real_mode{0,1}.html`、`real_mode2_lastpage.html`。

## 3. 「預定施工路段」— 新增並驗證 ✅

`roadcheck/sources/taipei_planned_work.py`，來源 <https://dig.taipei/Tpdig/PWorkData.aspx>。
新工處道路更新／人行道更新的未來排程，這才是真正的「預告」。

- 沒有查詢條件；「匯出Excel」(`ButExcel`) 一次回整張 HTML 表（約 320 筆），失敗才逐頁翻。
- `ShowPWorkData.aspx?caseid=NNN` 的 `caseid` 當事件 ID。沒座標，靠路名比對。
- fixture：`tests/fixtures/taipei_planned_work.real.html`（前 12 筆）。

## 4. LINE 實測（需要你的 channel）

- [ ] 建 Messaging API channel，設 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`。
- [ ] `python3 -m roadcheck serve-line`，用 ngrok 或 Cloudflare Tunnel 暴露，填 Webhook URL，開 "Use webhook"。
- [ ] 加好友 → 傳位置 → 收到「已訂閱」；再傳「路名 忠孝東路四段」讓沒座標的來源也能命中。
- [ ] `python3 -m roadcheck run --dry-run` 看比對結果；`run` 真推。

## 5. 排程

```cron
0 7 * * * cd /path/to/road-check && ROADCHECK_DB=/path/to/roadcheck.sqlite3 LINE_CHANNEL_ACCESS_TOKEN=... python3 -m roadcheck run >> run.log 2>&1
```

## 已知限制

- dig.taipei 兩個來源都沒座標，靠路名比對；訂閱時要給 `--roads` 或在 LINE 傳「路名 …」才會命中。
  路名抽取（`models.extract_roads`）已針對真資料清掉 【】。： 與「臺北市」前綴，
  但「金龍路(112巷至內湖路3段)」這類仍可能多抽到鄰近路名。
- 臨時使用道路量很大（一天上百筆，多是單一門牌前的占用），訂閱的路名若是大馬路會收到不少通知；
  可考慮之後對這類事件只在 `time_window` 覆蓋通勤時段時才通知。
- 停管處禁停公告未做。
