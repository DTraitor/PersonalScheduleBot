from datetime import timedelta, time, datetime
from typing import List


class Lesson:
    __slots__ = ["title", "lesson_type", "teacher", "location", "cancelled", "begin_time", "duration"]
    title: str
    lesson_type: str | None
    teacher: List[str] | None
    location: str | None
    cancelled: bool

    begin_time: time
    duration: timedelta

    def __init__(self, data: dict):
        self.title = data["title"]
        if "lessonType" in data:
            self.lesson_type = data["lessonType"]
        else:
            self.lesson_type = None

        if "teacher" in data:
            self.teacher = data["teacher"]
        else:
            self.teacher = None

        if "location" in data:
            self.location = data["location"]
        else:
            self.location = None

        self.cancelled = data["cancelled"]
        self.begin_time = datetime.strptime(data["beginTime"], "%H:%M:%S").time()
        h, m, s = map(int, data["duration"].split(":"))
        self.duration = timedelta(hours=h, minutes=m, seconds=s)
