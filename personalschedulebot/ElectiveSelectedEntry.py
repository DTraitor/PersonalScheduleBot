from datetime import time, datetime
from typing import Optional


class ElectiveSelectedEntry:
    __slots__ = ["selected_entry_id", "entry_name", "type", "week_number", "day_of_week", "start_time"]
    selected_entry_id: int
    entry_name: str
    type: Optional[str]
    week_number: bool
    day_of_week: int
    start_time: time

    def __init__(self, data: dict):
        self.selected_entry_id = data["selectedEntryId"]
        self.entry_name = data["entryName"]
        self.type = data.get("type")
        self.week_number = data["weekNumber"]
        self.day_of_week = int(data["dayOfWeek"])
        self.start_time = datetime.strptime(data["startTime"], "%H:%M:%S").time()
