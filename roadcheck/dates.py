"""容錯的日期解析：台北市資料常混用西元與民國、各種分隔符。"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_DIGITS = re.compile(r"\d+")


def parse_date(value) -> Optional[date]:
    """支援：
    2025/10/05、2025-10-05、20251005、2025/10/05 08:00
    114/10/05、114-10-05、1141005（民國年）
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.split()[0]  # 丟掉時間部分
    parts = _DIGITS.findall(s)
    if not parts:
        return None
    if len(parts) == 1:
        digits = parts[0]
        if len(digits) == 8:        # YYYYMMDD
            y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        elif len(digits) == 7:      # 民國 YYYMMDD
            y, m, d = int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:])
        elif len(digits) == 6:      # 民國 YYMMDD
            y, m, d = int(digits[:2]) + 1911, int(digits[2:4]), int(digits[4:])
        else:
            return None
    elif len(parts) >= 3:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if y < 1000:                # 民國年
            y += 1911
    else:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"y", "yes", "true", "1", "是", "有", "v"}:
        return True
    if s in {"n", "no", "false", "0", "否", "無", ""}:
        return False
    return None
