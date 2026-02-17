from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LevelReturn:
    """Represents an elective level (e.g. Undergraduate, Graduate)."""
    name: str
    id: int

    @classmethod
    def from_dict(cls, data: dict) -> "LevelReturn":
        return cls(name=data["name"], id=data["id"])


@dataclass
class LessonDescriptor:
    """Represents a searchable elective lesson descriptor."""
    id: int
    name: str
    source_id: int
    available_types: list[str] = field(default_factory=list)
    subgroups: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "LessonDescriptor":
        return cls(
            id=data["id"],
            name=data["name"],
            source_id=data["sourceId"],
            available_types=data.get("availableTypes", []),
            subgroups=data.get("subgroups", []),
        )


@dataclass
class SelectedElectiveLessonInputOutput:
    """Input/output model for a user's selected elective lesson.

    For output: ``id`` is the LessonId.
    For input:  ``id`` is the SourceId.
    """
    id: int
    lesson_name: str
    subgroup_number: int
    lesson_type: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "SelectedElectiveLessonInputOutput":
        return cls(
            id=data["id"],
            lesson_name=data["lessonName"],
            lesson_type=data.get("lessonType"),
            subgroup_number=data["subgroupNumber"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lessonName": self.lesson_name,
            "lessonType": self.lesson_type,
            "subgroupNumber": self.subgroup_number,
        }


@dataclass
class UserGroupDto:
    """Represents a group associated with a user."""
    group_name: str
    subgroup: int
    source_id: int

    @classmethod
    def from_dict(cls, data: dict) -> "UserGroupDto":
        return cls(
            group_name=data["groupName"],
            subgroup=data["subgroup"],
            source_id=data["sourceId"],
        )


@dataclass
class LessonDto:
    """A scheduled lesson returned by GET /api/schedule."""
    title: str
    teacher: list[str]
    cancelled: bool
    begin_time: str   # "HH:mm:ss"
    duration: str     # "HH:mm:ss"
    time_zone_id: str
    lesson_type: Optional[str] = None
    location: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "LessonDto":
        return cls(
            title=data["title"],
            lesson_type=data.get("lessonType"),
            teacher=data.get("teacher", []),
            location=data.get("location"),
            cancelled=data["cancelled"],
            begin_time=data["beginTime"],
            duration=data["duration"],
            time_zone_id=data["timeZoneId"],
        )


@dataclass
class OutOfRangeResult:
    """Returned with HTTP 400 when the requested date is outside the timetable range."""
    start_date: str   # ISO 8601
    end_date: str     # ISO 8601

    @classmethod
    def from_dict(cls, data: dict) -> "OutOfRangeResult":
        return cls(
            start_date=data["startDate"],
            end_date=data["endDate"],
        )


@dataclass
class UserAlertDto:
    """A user alert to be processed."""
    id: int
    user_telegram_id: int
    alert_type: str   # enum name or numeric value, serialized as-is
    options: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "UserAlertDto":
        return cls(
            id=data["id"],
            user_telegram_id=data["userTelegramId"],
            alert_type=data["alertType"],
            options=data.get("options", {}),
        )