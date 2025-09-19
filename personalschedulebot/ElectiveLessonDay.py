from datetime import time, datetime


class ElectiveLessonDay:
    __slots__ = ["id", "week_number", "day_of_week", "begin_time"]
    id: int
    week_number: int
    day_of_week: int
    begin_time: time

    def __init__(self, data: dict):
        self.id = data["id"]
        self.week_number = int(data["weekNumber"])
        self.day_of_week = int(data["dayOfWeek"])
        self.begin_time = datetime.strptime(data["startTime"], "%H:%M:%S").time()
