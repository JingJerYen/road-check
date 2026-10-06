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


def _fmt_date(d) -> str:
    return d.strftime("%m/%d") if d else "?"


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

    def __init__(self, access_token: str | None = None):
        self.token = access_token or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        if not self.token:
            raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN not set")

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
        if not target:
            raise ValueError("LINE push needs a userId as target")
        self._post(LINE_PUSH_URL, {"to": target, "messages": [{"type": "text", "text": text}]})

    def reply(self, reply_token: str, text: str) -> None:
        self._post(LINE_REPLY_URL, {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]})


def get_notifier(channel: str, dry_run: bool = False) -> Notifier:
    if dry_run or channel == "console":
        return ConsoleNotifier()
    if channel == "line":
        return LineNotifier()
    raise ValueError(f"unknown channel: {channel}")
