from datetime import time, datetime
from typing import Optional


class ElectiveLessonDaySpecific:
    __slots__ = ["id", "type", "week_number", "day_of_week", "start_time"]
    id: int
    type: Optional[str]
    week_number: bool
    day_of_week: int
    start_time: time

    def __init__(self, data: dict):
        self.type = data.get("type")
        self.week_number = data["weekNumber"]
        # DayOfWeek comes as integer (0–6)
        self.day_of_week = int(data["dayOfWeek"])
        self.id = int(data["id"])
        self.start_time = datetime.strptime(data["startTime"], "%H:%M:%S").time()
