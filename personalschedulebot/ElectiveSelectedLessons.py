from typing import List

from personalschedulebot.ElectiveSelectedEntry import ElectiveSelectedEntry
from personalschedulebot.ElectiveSelectedSource import ElectiveSelectedSource


class ElectiveSelectedLessons:
    __slots__ = ["sources", "entries"]
    sources: List[ElectiveSelectedSource]
    entries: List[ElectiveSelectedEntry]

    def __init__(self, data: dict):
        self.sources = [ElectiveSelectedSource(src) for src in data.get("sources", [])]
        self.entries = [ElectiveSelectedEntry(e) for e in data.get("entries", [])]
