"""通知通道：console（開發用）與 LINE Messaging API。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from typing import Iterable

from .models import KIND_LABEL, Match

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_MAX_TEXT = 5000


WEEKDAYS = "一二三四五六日"


def _fmt_date(d) -> str:
    return d.strftime("%m/%d") if d else "?"


def short_date(d) -> str:
    """10/9（五）"""
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）" if d else "?"


class Reply(str):
    """LINE 回覆：就是一段文字，可以另外帶快速回覆按鈕。

    quick 是 [(label, text)]，按下去等於使用者傳出 text；text 為 None 表示「傳送位置」按鈕。
    繼承 str，所以只看文字的地方（測試、console）照常運作。
    """

    def __new__(cls, text: str, quick=None):
        obj = super().__new__(cls, text)
        obj.quick = list(quick or [])
        return obj


def quick_reply_items(quick) -> list:
    items = []
    for label, text in quick[:13]:               # LINE 上限 13 個
        if text is None:
            action = {"type": "location", "label": label[:20]}
        else:
            action = {"type": "message", "label": label[:20], "text": text}
        items.append({"type": "action", "action": action})
    return items


def line_text_message(text: str, quick=None) -> dict:
    quick = quick if quick is not None else getattr(text, "quick", None)
    if len(text) > LINE_MAX_TEXT:
        text = text[: LINE_MAX_TEXT - 20] + "\n…（已截斷）"
    msg = {"type": "text", "text": str(text)}
    if quick:
        msg["quickReply"] = {"items": quick_reply_items(quick)}
    return msg


def format_match(m: Match) -> str:
    ev = m.event
    label = KIND_LABEL.get(ev.kind, ev.kind)
    lines = [f"[{label}] {ev.title}"]
    period = f"{_fmt_date(ev.start)} ~ {_fmt_date(ev.end)}"
    if ev.time_window:
        period += f" {ev.time_window}"
    lines.append(f"期間：{period}")
    if ev.blocks_traffic:
        lines.append("影響交通：是")
    if m.reason == "distance" and m.distance_m is not None:
        where = "位置" if m.subscription.kind == "point" else "路線"
        if m.distance_m < 1:
            lines.append(f"你的{where}就在{'施工' if ev.kind == 'construction' else '管制'}範圍內")
        else:
            lines.append(f"距離你的{where}約 {m.distance_m:.0f} 公尺")
    elif m.matched_roads:
        lines.append("經過路段：" + "、".join(m.matched_roads))
    if ev.agency:
        lines.append(f"單位：{ev.agency}")
    return "\n".join(lines)


MAX_ITEMS_PER_DIGEST = 15


def format_digest(name: str, matches: list[Match], max_items: int = MAX_ITEMS_PER_DIGEST,
                  days: int | None = None) -> str:
    """一則訊息：影響交通的、距離近的排前面，超過 max_items 件只說還有幾件。"""
    ordered = sorted(matches, key=lambda m: (not m.event.blocks_traffic, m.distance_m if m.distance_m is not None else 1e9,
                                             m.event.start or date.max))
    when = f"未來 {days} 天" if days else ""
    head = f"🚧 「{name}」{when}附近有 {len(matches)} 件新異動"
    body = "\n\n".join(format_match(m) for m in ordered[:max_items])
    if len(ordered) > max_items:
        body += f"\n\n…還有 {len(ordered) - max_items} 件，請到來源網站查看"
    text = f"{head}\n\n{body}"
    if len(text) > LINE_MAX_TEXT:
        text = text[: LINE_MAX_TEXT - 20] + "\n…（已截斷）"
    return text


def group_by_subscription(matches: Iterable[Match]) -> dict[int, list[Match]]:
    groups: dict[int, list[Match]] = defaultdict(list)
    for m in matches:
        groups[m.subscription.id or 0].append(m)
    return groups


class Notifier:
    channel = ""

    def send(self, target: str, text: str) -> None:
        raise NotImplementedError

    def send_many(self, target: str, texts: list) -> None:
        """一次送多則；預設逐則 send。"""
        for t in texts:
            self.send(target, t)


class ConsoleNotifier(Notifier):
    channel = "console"

    def send(self, target: str, text: str) -> None:
        print("=" * 60)
        print(f"-> {target or '(console)'}")
        print(text)
        print("=" * 60)


class LineNotifier(Notifier):
    """LINE Messaging API 推播。需要 channel access token（long-lived）。

    LINE Notify 已於 2025 年停止服務，請用 Messaging API。
    免費方案每月 200 則推播；reply 不計費，所以 webhook 回覆盡量用 reply。
    """

    channel = "line"

    def __init__(self, access_token: str | None = None, api_base: str | None = None):
        self.token = access_token or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        if not self.token:
            raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN not set")
        # 測試時可以指到本機的假 LINE API
        base = (api_base or os.environ.get("LINE_API_BASE", "")).rstrip("/")
        self.push_url = base + "/v2/bot/message/push" if base else LINE_PUSH_URL
        self.reply_url = base + "/v2/bot/message/reply" if base else LINE_REPLY_URL

    def _post(self, url: str, payload: dict) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LINE API {e.code}: {body}") from e

    def send(self, target: str, text: str) -> None:
        self.send_many(target, [text])

    def send_many(self, target: str, texts: list) -> None:
        """一次 push 最多 5 則訊息；LINE 依「收件人數」計費，所以合併送比較省。"""
        if not target:
            raise ValueError("LINE push needs a userId as target")
        if not 1 <= len(texts) <= 5:
            raise ValueError("LINE push takes 1–5 messages")
        self._post(self.push_url, {"to": target, "messages": [line_text_message(t, quick=[]) for t in texts]})

    def reply(self, reply_token: str, text: str) -> None:
        """回覆（不計費）。text 若是 Reply，會帶上快速回覆按鈕。"""
        self._post(self.reply_url, {"replyToken": reply_token, "messages": [line_text_message(text)]})


def get_notifier(channel: str, dry_run: bool = False) -> Notifier:
    if dry_run or channel == "console":
        return ConsoleNotifier()
    if channel == "line":
        return LineNotifier()
    raise ValueError(f"unknown channel: {channel}")
