class ElectiveSelectedSource:
    __slots__ = ["name", "selected_source_id", "subgroup_number"]
    name: str
    selected_source_id: int
    subgroup_number: int

    def __init__(self, data: dict):
        self.name = data["name"]
        self.selected_source_id = data["selectedSourceId"]
        self.subgroup_number = data["subGroupNumber"]
