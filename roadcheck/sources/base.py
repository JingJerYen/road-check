from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..models import Event

USER_AGENT = "roadcheck/0.1 (+https://github.com/JingJerYen/road-check)"


class SourceError(RuntimeError):
    pass


def http_get(url: str, timeout: float = 30.0, headers: Optional[dict] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"network error fetching {url}: {e.reason}") from e


def http_get_json(url: str, timeout: float = 30.0):
    raw = http_get(url, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as e:
        raise SourceError(f"invalid JSON from {url}: {e}") from e


class Source(ABC):
    """一個資料來源：抓回原始資料，轉成 Event 清單。

    子類別要實作 fetch_raw() 與 parse()。fetch() 會串起來。
    也可以用 from_file() 讀本地檔（離線測試或網路被擋時用）。
    """

    name: str = ""
    kind: str = ""
    url: str = ""

    @abstractmethod
    def fetch_raw(self):
        ...

    @abstractmethod
    def parse(self, raw) -> list[Event]:
        ...

    def fetch(self) -> list[Event]:
        return self.parse(self.fetch_raw())

    def from_file(self, path: str | Path) -> list[Event]:
        data = Path(path).read_bytes()
        return self.parse(self.load_bytes(data))

    def load_bytes(self, data: bytes):
        """子類別可覆寫：預設當 JSON。"""
        return json.loads(data.decode("utf-8-sig"))
