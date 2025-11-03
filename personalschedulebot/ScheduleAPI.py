import json
import os
from datetime import datetime

from personalschedulebot.ElectiveSelectedLessons import ElectiveSelectedLessons
from personalschedulebot.ElectiveSubgroups import ElectiveSubgroups
from personalschedulebot.Lesson import Lesson
from typing import List
import httpx

from personalschedulebot.ElectiveLesson import ElectiveLesson
from personalschedulebot.ElectiveLessonDay import ElectiveLessonDay
from personalschedulebot.UserAlert import UserAlert

SCHEDULE_API = os.environ["SCHEDULE_API"]


async def make_api_get_request(url_path: str, params: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(SCHEDULE_API + url_path, params=params, timeout=35)


async def make_api_post_request(url_path: str, params: dict, content: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            SCHEDULE_API + url_path,
            params=params,
            headers={"Content-Type": "application/json"},
            content=json.dumps(content),
            timeout=35)


async def make_api_patch_request(url_path: str, params: dict, content: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.patch(
            SCHEDULE_API + url_path,
            params=params,
            headers={"Content-Type": "application/json"},
            content=json.dumps(content),
            timeout=35)


async def make_api_delete_request(url_path: str, params: dict) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.delete(SCHEDULE_API + url_path, params=params, timeout=35)


async def get_schedule(
        schedule_date: datetime,
        user_telegram_id: int,
) -> List[Lesson]:
    result: httpx.Response = await make_api_get_request("/schedule", {
        "dateTime": schedule_date.isoformat(),
        "userTelegramId": user_telegram_id,
    })

    return [Lesson(item) for item in result.json()]


async def user_exists(telegram_id: int) -> bool:
    result: httpx.Response = await make_api_get_request("/user/exists", {
        "telegramId": telegram_id,
    })
    return result.json()


async def user_subgroups(telegram_id: int) -> List[int]:
    result: httpx.Response = await make_api_get_request("/group/subgroups", {
        "telegramId": telegram_id,
    })
    return result.json()


async def user_groups(telegram_id: int) -> List[str]:
    result: httpx.Response = await make_api_get_request("/group/user", {
        "telegramId": telegram_id,
    })
    return result.json()


async def create_user(telegram_id: int, group_code: str, subgroup: int = -1) -> int:
    result: httpx.Response = await make_api_post_request("/user", {}, {
        "TelegramId": telegram_id,
        "GroupName": group_code,
        "SubGroup": subgroup,
    })
    match result.status_code:
        case 201:
            return 0
        case 404:
            return 1
        case _:
            return -1


async def change_user_group(telegram_id: int, new_group_code: str, subgroup: int = -1) -> int:
    result: httpx.Response = await make_api_patch_request("/user/group", {}, {
        "TelegramId": telegram_id,
        "GroupName": new_group_code,
        "SubGroup": subgroup,
    })
    match result.status_code:
        case 200:
            return 0
        case 404:
            return 1
        case _:
            return -1


async def get_user_alerts(batch_size: int) -> List[UserAlert]:
    result: httpx.Response = await make_api_get_request("/UserAlerts", {
        "batchSize": batch_size
    })
    return [UserAlert(item) for item in result.json()]


async def get_possible_days(lesson_source_id: int) -> ElectiveLessonDay:
    """GET /elective/days?lessonSourceId=..."""
    result = await make_api_get_request("/elective/days", {
        "lessonSourceId": lesson_source_id,
    })

    if result.status_code == 404:
        raise KeyError(result.text)
    if result.status_code == 400:
        raise ValueError(result.text)

    return ElectiveLessonDay(result.json())


async def get_possible_lessons(partial_name: str) -> List[ElectiveLesson]:
    """GET /elective/lessons?partialLessonName=..."""
    result = await make_api_get_request("/elective/lessons", {
        "partialLessonName": partial_name,
    })

    if result.status_code == 400:
        raise ValueError(result.text)

    return [ElectiveLesson(item) for item in result.json()]


async def get_possible_subgroups(lesson_source_id: int, lesson_type: str) -> ElectiveSubgroups:
    """GET /elective/subgroups?lessonSourceId=...&lessonType=..."""
    result = await make_api_get_request("/elective/subgroups", {
        "lessonSourceId": lesson_source_id,
        "lessonType": lesson_type,
    })

    if result.status_code == 404:
        raise KeyError(result.text)
    if result.status_code == 400:
        raise ValueError(result.text)

    return ElectiveSubgroups(result.json())


async def get_user_elective_lessons(telegram_id: int) -> ElectiveSelectedLessons:
    """GET /elective?telegramId=..."""
    result = await make_api_get_request("/elective", {
        "telegramId": telegram_id,
    })

    if result.status_code == 404:
        raise KeyError(result.text)

    return ElectiveSelectedLessons(result.json())


async def create_user_elective_source(telegram_id: int, lesson_source_id: int, lesson_type: str, subgroup_number: int) -> bool:
    """POST /elective/source?telegramId=...&lessonSourceId=...&lessonType=...&subgroupNumber=..."""
    result = await make_api_post_request("/elective/source", {
        "telegramId": telegram_id,
        "lessonSourceId": lesson_source_id,
        "lessonType": lesson_type,
        "subgroupNumber": subgroup_number,
    }, {})
    return result.status_code == 201


async def delete_user_elective_source(telegram_id: int, lesson_id: int) -> bool:
    """DELETE /elective/source?telegramId=...&lessonId=..."""
    result = await make_api_delete_request("/elective/source", {
        "telegramId": telegram_id,
        "lessonId": lesson_id,
    })
    return result.status_code == 200


async def create_user_elective_entry(telegram_id: int, lesson_source_id: int, lesson_entry: int) -> bool:
    """POST /elective/entry?telegramId=...&lessonSourceId=...&lessonEntry=..."""
    result = await make_api_post_request("/elective/entry", {
        "telegramId": telegram_id,
        "lessonSourceId": lesson_source_id,
        "lessonEntry": lesson_entry,
    }, {})
    return result.status_code == 201


async def delete_user_elective_entry(telegram_id: int, lesson_id: int) -> bool:
    """DELETE /elective/entry?telegramId=...&lessonId=..."""
    result = await make_api_delete_request("/elective/entry", {
        "telegramId": telegram_id,
        "lessonId": lesson_id,
    })
    return result.status_code == 200