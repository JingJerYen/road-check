# 交接：下一個 session 要做的事

這個 session 的環境被網路政策擋住 `data.taipei` 與 `dig.taipei`，所以兩個資料來源 adapter
是**依官方欄位說明與樣本**寫的，沒接過真資料。程式其他部分（幾何、儲存、比對、去重、LINE）都有測試覆蓋。

## 0. 環境設定（你來做）

雲端環境 → Edit → Network access 選 Custom，Allowed domains 貼：

```
data.taipei
dig.taipei
api.line.me
pypi.org
files.pythonhosted.org
github.com
```

儲存後**新開 session** 才會生效。

## 1. 驗證「臺北市今日施工資訊」

```bash
python3 -m roadcheck fetch --source taipei_today_construction --dump
```

要確認：

- [ ] 預設 rid `875ea014-3ad0-4c79-93d3-049adb813c47` 是否正確。不對的話到
      <https://data.taipei/dataset/detail?id=c208dabd-2da0-4e6d-8dbd-a004b9782b0a> 找「下載」連結裡的 rid，
      改 `roadcheck/sources/taipei_today_construction.py` 的 `DEFAULT_RID`，或用 `ROADCHECK_TAIPEI_TODAY_URL`。
- [ ] 回傳形狀：`{"result":{"results":[...]}}` 或直接 list，`_records()` 兩種都接。
- [ ] 欄位名大小寫（`Ac_no` / `AC_NO`…）；`_get()` 不分大小寫找，但若名稱不同要加別名。
- [ ] `X`/`Y` 是不是經度/緯度（有自動對調修正）；是否為 TWD97 而非 WGS84。若是 TWD97（數值像 3xxxxx / 27xxxxx），
      要加投影轉換。
- [ ] `Cb_Da`/`Ce_Da` 的格式；`roadcheck/dates.py` 已支援西元、民國、多種分隔符。
- [ ] 資料筆數與 `count` 翻頁是否正常。
- [ ] 把一份真實回應存成 `tests/fixtures/taipei_today_construction.real.json`，加進測試。

## 2. 驗證「外部管制路段」HTML

```bash
python3 -m roadcheck fetch --source taipei_ext_restriction --dump
```

要確認：

- [ ] 頁面是不是需要 POST（ASP.NET WebForms 常見 `__VIEWSTATE`）才會列資料。若是，`fetch_raw()` 要先 GET 拿
      viewstate 再 POST。
- [ ] 表頭文字是否能被 `HEADER_MAP` 對到（期間／原因／路段／單位／案號）。
- [ ] 期間欄的分隔符（`~`、`至`…）是否被 `_split_period()` 正確切開。
- [ ] 同樣存一份真實 HTML 當 fixture。

## 3. LINE 實測

- [ ] 建 Messaging API channel，設 `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`。
- [ ] `python3 -m roadcheck serve-line`，用 ngrok 或 Cloudflare Tunnel 暴露，填 Webhook URL，開 "Use webhook"。
- [ ] 加好友 → 傳位置 → 收到「已訂閱」。
- [ ] `python3 -m roadcheck run --dry-run` 看比對結果；`run` 真推。

## 4. 排程

```cron
0 7 * * * cd /path/to/road-check && ROADCHECK_DB=/path/to/roadcheck.sqlite3 LINE_CHANNEL_ACCESS_TOKEN=... python3 -m roadcheck run >> run.log 2>&1
```

## 已知限制

- 外部管制路段沒座標，目前靠路名比對；訂閱時要給 `--roads` 或在 LINE 傳「路名 …」才會命中。
- 施工資料是「今日」核備案件，不是未來排程。要真正「預告」要再接 dig.taipei「預定施工路段」
  <https://dig.taipei/Tpdig/PWorkData.aspx>，結構應與外部管制路段類似，可複用 `_TableParser`。
- 停管處禁停公告未做。
