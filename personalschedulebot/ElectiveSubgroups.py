from typing import List


class ElectiveSubgroups:
    __slots__ = ["lesson_source_id", "possible_subgroups"]
    lesson_source_id: int
    possible_subgroups: List[int]

    def __init__(self, data: dict):
        self.lesson_source_id = data["lessonSourceId"]
        self.possible_subgroups = data["possibleSubgroups"]
