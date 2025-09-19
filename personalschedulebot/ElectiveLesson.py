from datetime import timedelta, time, datetime
from typing import List


class ElectiveLesson:
    __slots__ = ["id", "title", "lesson_type", "teacher", "week_number", "day_of_week", "location", "begin_time", "duration"]
    id: int
    title: str
    lesson_type: str | None
    location: str | None
    teacher: List[str] | None
    week_number: int
    day_of_week: int
    begin_time: time
    duration: timedelta

    def __init__(self, data: dict):
        self.id = data["id"]
        self.title = data["title"]
        self.week_number = data["weekNumber"]
        self.day_of_week = data["dayOfWeek"]

        if "type" in data:
            self.lesson_type = data["type"]
        else:
            self.lesson_type = None

        if "teacher" in data:
            self.teacher = data["teacher"][0]
        else:
            self.teacher = None

        if "location" in data:
            self.location = data["location"]
        else:
            self.location = None

        self.begin_time = datetime.strptime(data["startTime"], "%H:%M:%S").time()
        h, m, s = map(int, data["length"].split(":"))
        self.duration = timedelta(hours=h, minutes=m, seconds=s)
