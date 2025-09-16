import json
import os
from datetime import datetime
from .Lesson import Lesson
from typing import List
import httpx


SCHEDULE_API = os.environ["SCHEDULE_API"]

def make_api_get_request(url_path: str, params: dict) -> httpx.Response:
    return httpx.get(SCHEDULE_API + url_path, params=params)


def make_api_post_request(url_path: str, params: dict, content: dict) -> httpx.Response:
    return httpx.post(
        SCHEDULE_API + url_path,
        params=params,
        headers={"Content-Type": "application/json"},
        content=json.dumps(content))


def make_api_patch_request(url_path: str, params: dict, content: dict) -> httpx.Response:
    return httpx.patch(
        SCHEDULE_API + url_path,
        params=params,
        headers={"Content-Type": "application/json"},
        content=json.dumps(content))


def get_schedule(
        schedule_date: datetime,
        user_telegram_id: int,
) -> List[Lesson]:
    result: httpx.Response = make_api_get_request("/schedule", {
        "dateTime": schedule_date.isoformat(),
        "userTelegramId": user_telegram_id,
    })

    return [Lesson(item) for item in result.json()]


def get_faculties() -> List[str]:
    return make_api_get_request("/group/faculties", {}).json()


def get_groups(faculty: str, bachelor: bool) -> List[str]:
    return make_api_get_request("/group/degree", {
        "facultyName": faculty,
        "bachelor": bachelor,
    }).json()


def user_exists(telegram_id: int) -> bool:
    result: httpx.Response = make_api_get_request("/user/exists", {
        "telegramId": telegram_id,
    })
    return result.json()


def create_user(telegram_id: int, group_code: str) -> bool:
    result: httpx.Response = make_api_post_request("/user", {}, {
        "TelegramId": telegram_id,
        "GroupName": group_code,
    })
    return result.status_code == 201


def change_user_group(telegram_id: int, new_group_code: str) -> bool:
    result: httpx.Response = make_api_patch_request("/user", {}, {
        "TelegramId": telegram_id,
        "GroupName": new_group_code,
    })
    return result.status_code == 200
