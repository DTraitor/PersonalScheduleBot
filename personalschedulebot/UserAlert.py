from enum import Enum


class UserAlertType(Enum):
    NONE = 0
    GROUP_REMOVED = 1
    SOURCE_REMOVED = 2
    ENTRY_REMOVED = 3
    NEWS = 4


class UserAlert:
    __slots__ = ["id", "telegram_id", "alert_type", "options"]
    id: int
    telegram_id: int
    alert_type: UserAlertType
    options: dict[str, str]

    def __init__(self, data: dict):
        self.id = data["id"]
        self.telegram_id = data["userTelegramId"]
        self.alert_type = UserAlertType(data["alertType"])
        self.options = data["options"]
