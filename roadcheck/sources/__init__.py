from .base import Source, SourceError
from .taipei_today_construction import TaipeiTodayConstruction
from .taipei_ext_restriction import TaipeiExtRestriction

ALL_SOURCES: dict[str, type[Source]] = {
    TaipeiTodayConstruction.name: TaipeiTodayConstruction,
    TaipeiExtRestriction.name: TaipeiExtRestriction,
}

__all__ = ["Source", "SourceError", "ALL_SOURCES", "TaipeiTodayConstruction", "TaipeiExtRestriction"]
