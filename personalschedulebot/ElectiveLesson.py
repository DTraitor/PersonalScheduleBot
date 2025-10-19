from typing import List


class ElectiveLesson:
    __slots__ = ["title", "source_id", "types"]
    title: str
    source_id: int
    types: List[str]

    def __init__(self, data: dict):
        self.title = data["title"]
        self.source_id = data["sourceId"]
        self.types = data["types"]
