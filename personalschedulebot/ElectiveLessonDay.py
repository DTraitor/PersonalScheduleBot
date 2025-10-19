from typing import List

from personalschedulebot.ElectiveLessonDaySpecific import ElectiveLessonDaySpecific


class ElectiveLessonDay:
    __slots__ = ["source_id", "lesson_days"]
    source_id: int
    lesson_days: List[ElectiveLessonDaySpecific]

    def __init__(self, data: dict):
        self.source_id = data["sourceId"]
        self.lesson_days = [ElectiveLessonDaySpecific(ld) for ld in data["lessonDays"]]
