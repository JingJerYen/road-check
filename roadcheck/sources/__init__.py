from .base import Source, SourceError
from .taipei_today_construction import TaipeiTodayConstruction
from .taipei_ext_restriction import TaipeiExtRestriction
from .taipei_planned_work import TaipeiPlannedWork

ALL_SOURCES: dict[str, type[Source]] = {
    TaipeiTodayConstruction.name: TaipeiTodayConstruction,
    TaipeiPlannedWork.name: TaipeiPlannedWork,
    TaipeiExtRestriction.name: TaipeiExtRestriction,
}

__all__ = ["Source", "SourceError", "ALL_SOURCES", "TaipeiTodayConstruction", "TaipeiExtRestriction",
           "TaipeiPlannedWork"]
