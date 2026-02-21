from __future__ import annotations

import json
from typing import Any, Optional, Union
from urllib.parse import urlencode

import aiohttp

from .models import (
    LevelReturn,
    LessonDescriptor,
    SelectedElectiveLessonInputOutput,
    UserGroupDto,
    LessonDto,
    OutOfRangeResult,
    UserAlertDto,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScheduleApiError(Exception):
    """Base exception for all API errors."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.body = body


class NotFoundError(ScheduleApiError):
    """Raised on HTTP 404."""


class BadRequestError(ScheduleApiError):
    """Raised on HTTP 400."""


class OutOfRangeDateError(BadRequestError):
    """Raised when the requested date is outside the available timetable range."""

    def __init__(self, out_of_range_result: OutOfRangeResult):
        super().__init__(
            400,
            "Timetable date out of range.",
            out_of_range_result,
        )
        self.out_of_range_result = out_of_range_result


class TooManyElementsError(BadRequestError):
    """Raised when the lesson search returns too many results."""

    def __init__(self):
        super().__init__(400, "Too many elements to return. Try more precise name.")


class ServerError(ScheduleApiError):
    """Raised on HTTP 5xx."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_url(base: str, path: str, params: Optional[dict] = None) -> str:
    """Combine *base*, *path*, and optional query *params* into a URL string."""
    url = base.rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urlencode(filtered)
    return url


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _raise_for_status(status: int, text: str) -> None:
    """Raise the appropriate exception for non-2xx status codes."""
    if 200 <= status < 300:
        return

    body = text.strip()

    if status == 404:
        raise NotFoundError(404, body or "Not found.", body)

    if status == 400:
        # Try to parse OutOfRangeResult first
        try:
            data = json.loads(body)
            if "startDate" in data and "endDate" in data:
                raise OutOfRangeDateError(OutOfRangeResult.from_dict(data))
        except (ValueError, KeyError):
            pass

        if "Too many elements" in body:
            raise TooManyElementsError()

        raise BadRequestError(400, body or "Bad request.", body)

    if status >= 500:
        raise ServerError(status, body or "Internal server error.", body)

    raise ScheduleApiError(status, body or "Unexpected error.", body)


# ---------------------------------------------------------------------------
# Async API client
# ---------------------------------------------------------------------------


class ScheduleApiClient:
    """Async client for the Schedule API backed by a single ``aiohttp.ClientSession``.

    Args:
        base_url: Root URL of the API server, e.g. ``"https://api.example.com"``.
        session:  Optional pre-existing ``aiohttp.ClientSession`` to use.
                  If omitted, one is created automatically on :meth:`open` /
                  ``async with`` entry.
    """

    def __init__(
            self,
            base_url: str,
            session: Optional[aiohttp.ClientSession] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    async def open(self) -> None:
        """Create the underlying ``aiohttp.ClientSession`` (if not supplied)."""
        if self._owns_session and (self._session is None or self._session.closed):
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"}
            )

    async def close(self) -> None:
        """Close the underlying session (only if it was created by this client)."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "ScheduleApiClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Core request helper
    # ------------------------------------------------------------------ #

    async def _request(
            self,
            method: str,
            url: str,
            body: Optional[Any] = None,
    ) -> tuple[int, str]:
        """Send an HTTP request and return ``(status_code, response_text)``."""
        assert self._session is not None, (
            "Session is not open. "
            "Use 'async with ScheduleApiClient(...)' or call await client.open() first."
        )

        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body

        async with self._session.request(method, url, **kwargs) as resp:
            text = await resp.text()
            return resp.status, text

    # ------------------------------------------------------------------ #
    # /api/elective
    # ------------------------------------------------------------------ #

    async def get_elective_levels(self) -> list[LevelReturn]:
        """GET /api/elective/levels

        Returns:
            List of available elective levels.

        Raises:
            ServerError: On HTTP 5xx.
        """
        url = _build_url(self.base_url, "/api/elective/levels")
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return [LevelReturn.from_dict(item) for item in _parse_json(text)]

    async def search_elective_lessons(
            self,
            lesson_name: str,
            source_id: int,
            level_id: Optional[int] = None,
    ) -> list[LessonDescriptor]:
        """GET /api/elective/lessons

        Args:
            lesson_name: Partial or full lesson name to search.
            source_id:   Source identifier to filter by.
            level_id:    Optional elective level id to filter by.

        Returns:
            List of matching lesson descriptors.

        Raises:
            TooManyElementsError: When the query matches too many results (HTTP 400).
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/elective/lessons",
            {"lessonName": lesson_name, "sourceId": source_id, "levelId": level_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        data = _parse_json(text)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [LessonDescriptor.from_dict(item) for item in data]
        return data  # type: ignore[return-value]

    async def get_elective_types(
            self,
            lesson_name: str,
            source_id: int,
    ) -> list[str]:
        """GET /api/elective/types

        Args:
            lesson_name: Lesson name.
            source_id:   Source identifier.

        Returns:
            List of lesson type strings (e.g. ``["Lecture", "Seminar"]``).

        Raises:
            NotFoundError: When the lesson is not found (HTTP 404).
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/elective/types",
            {"lessonName": lesson_name, "sourceId": source_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return _parse_json(text)

    async def get_elective_subgroups(
            self,
            lesson_source_id: int,
            lesson_name: str,
            lesson_type: str,
    ) -> list[Union[int, dict]]:
        """GET /api/elective/subgroups

        Args:
            lesson_source_id: Source id of the lesson.
            lesson_name:      Lesson name.
            lesson_type:      Lesson type (e.g. ``"Lecture"``).

        Returns:
            List of subgroup numbers or subgroup descriptor objects.

        Raises:
            NotFoundError: When not found (HTTP 404).
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/elective/subgroups",
            {
                "lessonSourceId": lesson_source_id,
                "lessonName": lesson_name,
                "lessonType": lesson_type,
            },
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return _parse_json(text)

    # ------------------------------------------------------------------ #
    # /api/group
    # ------------------------------------------------------------------ #

    async def get_user_groups(self, telegram_id: int) -> list[UserGroupDto]:
        """GET /api/group/user

        Args:
            telegram_id: Telegram user ID.

        Returns:
            List of groups associated with the user.

        Raises:
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/group/user",
            {"telegramId": telegram_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        data = _parse_json(text)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [UserGroupDto.from_dict(item) for item in data]
        return data  # type: ignore[return-value]

    async def group_exists(self, group_name: str) -> bool:
        """GET /api/group/exist

        Args:
            group_name: Group name to check.

        Returns:
            ``True`` if the group exists, ``False`` otherwise.

        Raises:
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/group/exist",
            {"groupName": group_name},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        data = _parse_json(text)
        if isinstance(data, bool):
            return data
        if isinstance(data, str):
            return data.lower() == "true"
        return bool(data)

    async def get_group_subgroups(self, group_name: str) -> list:
        """GET /api/group/subgroups by group name

        Args:
            group_name: Name of the group to query subgroups for.

        Returns:
            List of subgroup numbers or descriptors.

        Raises:
            NotFoundError: When the group is not found (HTTP 404).
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/group/subgroups",
            {"groupName": group_name},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return _parse_json(text)

    async def get_user_subgroups(self, telegram_id: int) -> list[Union[int, dict]]:
        """GET /api/group/subgroups

        Args:
            telegram_id: Telegram user ID.

        Returns:
            List of subgroup numbers or descriptors visible for the user.

        Raises:
            NotFoundError: When the user or their group is not found (HTTP 404).
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/group/subgroups",
            {"telegramId": telegram_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return _parse_json(text)

    # ------------------------------------------------------------------ #
    # /api/schedule
    # ------------------------------------------------------------------ #

    async def get_schedule(
            self,
            date_time: str,
            user_telegram_id: int,
    ) -> list[LessonDto]:
        """GET /api/schedule

        Args:
            date_time:        ISO 8601 date-time string, e.g.
                              ``"2026-02-17T00:00:00Z"``.
            user_telegram_id: Telegram user ID.

        Returns:
            List of lessons scheduled for the given date.

        Raises:
            NotFoundError:       When the user is not found (HTTP 404).
            OutOfRangeDateError: When *date_time* is outside the timetable range.
                                 Access ``.out_of_range_result`` for the valid range.
            ServerError:         On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/schedule",
            {"dateTime": date_time, "userTelegramId": user_telegram_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return [LessonDto.from_dict(item) for item in _parse_json(text)]

    # ------------------------------------------------------------------ #
    # /api/useralerts
    # ------------------------------------------------------------------ #

    async def get_user_alerts(self, batch_size: int) -> list[UserAlertDto]:
        """GET /api/useralerts

        Args:
            batch_size: Maximum number of alerts to retrieve.

        Returns:
            List of :class:`UserAlertDto` objects.

        Raises:
            ServerError: On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/useralerts",
            {"batchSize": batch_size},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return [UserAlertDto.from_dict(item) for item in _parse_json(text)]

    async def delete_user_alerts(self, alert_ids: list[int]) -> None:
        """DELETE /api/useralerts

        Args:
            alert_ids: List of alert IDs to remove.

        Raises:
            ServerError: On HTTP 5xx.
        """
        url = _build_url(self.base_url, "/api/useralerts")
        status, text = await self._request("DELETE", url, body=alert_ids)
        _raise_for_status(status, text)

    # ------------------------------------------------------------------ #
    # /api/user
    # ------------------------------------------------------------------ #

    async def create_user(self, telegram_id: int) -> None:
        """POST /api/user

        Creates a new user for the given Telegram ID.

        Args:
            telegram_id: Telegram user ID.

        Raises:
            ServerError: On HTTP 5xx.

        Note:
            The server responds with HTTP 201 (Created) and an empty body.
        """
        url = _build_url(self.base_url, "/api/user", {"telegramId": telegram_id})
        status, text = await self._request("POST", url)
        _raise_for_status(status, text)

    async def update_user_group(
            self,
            telegram_id: int,
            group_name: str,
            subgroup_number: int,
    ) -> None:
        """PUT /api/user/group

        Changes the user's group assignment.

        Args:
            telegram_id:      Telegram user ID.
            group_name:       Name of the new group.
            subgroup_number:  Subgroup number within the group.

        Raises:
            NotFoundError: When the user or group does not exist (HTTP 404).
            ServerError:   On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/user/group",
            {
                "telegramId": telegram_id,
                "groupName": group_name,
                "subgroupNumber": subgroup_number,
            },
        )
        status, text = await self._request("PUT", url)
        _raise_for_status(status, text)

    async def get_user_electives(
            self,
            telegram_id: int,
    ) -> list[SelectedElectiveLessonInputOutput]:
        """GET /api/user/elective

        Returns the user's selected elective lessons.

        Args:
            telegram_id: Telegram user ID.

        Returns:
            List of selected elective lessons (``id`` is the LessonId).

        Raises:
            NotFoundError: When the user does not exist (HTTP 404).
            ServerError:   On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/user/elective",
            {"telegramId": telegram_id},
        )
        status, text = await self._request("GET", url)
        _raise_for_status(status, text)
        return [
            SelectedElectiveLessonInputOutput.from_dict(item)
            for item in _parse_json(text)
        ]

    async def add_user_elective(
            self,
            telegram_id: int,
            elective: SelectedElectiveLessonInputOutput,
    ) -> None:
        """POST /api/user/elective

        Adds a selected elective lesson for the user.

        Args:
            telegram_id: Telegram user ID.
            elective:    Elective lesson to add (``id`` is the SourceId for input).

        Raises:
            NotFoundError: When the user or elective lesson does not exist (HTTP 404).
            ServerError:   On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/user/elective",
            {"telegramId": telegram_id},
        )
        status, text = await self._request("POST", url, body=elective.to_dict())
        _raise_for_status(status, text)

    async def delete_user_elective(
            self,
            telegram_id: int,
            elective_id: int,
    ) -> None:
        """DELETE /api/user/elective

        Removes an elective lesson from the user's selection.

        Args:
            telegram_id:  Telegram user ID.
            elective_id:  ID of the elective lesson to remove.

        Raises:
            NotFoundError: When the user or elective lesson is not found (HTTP 404).
            ServerError:   On HTTP 5xx.
        """
        url = _build_url(
            self.base_url,
            "/api/user/elective",
            {"telegramId": telegram_id, "electiveId": elective_id},
        )
        status, text = await self._request("DELETE", url)
        _raise_for_status(status, text)
