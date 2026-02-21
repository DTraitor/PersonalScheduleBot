import logging
import os
import locale
from datetime import datetime, timedelta, date
from typing import List
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

# New imports
from personalschedulebot.schedule_api import ScheduleApiClient, NotFoundError, TooManyElementsError
from personalschedulebot.lesson_message_mapper import generate_telegram_message_from_list
from personalschedulebot.models import SelectedElectiveLessonInputOutput

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

EXPECTING_MANUAL_GROUP = "expecting_manual_group"

# Elective flow state keys
EXPECTING_ELECTIVE_NAME = "expecting_elective_name"
TEMP_ELECTIVE_LESSON_ID = "temp_elective_lesson_id"
TEMP_ELECTIVE_ADD_METHOD = "temp_elective_add_method"  # "subgroup" or "manual"
TEMP_ELECTIVE_LESSON_TYPE = "temp_elective_lesson_type"
TEMP_ELECTIVE_WEEK = "temp_elective_week"
TEMP_ELECTIVE_DAY = "temp_elective_day"
TEMP_GROUP_CODE = "temp_group_code"
TEMP_GROUP_ACTION = "temp_group_action"  # "create" or "change"
TEMP_ELECTIVE_SEARCH_RESULTS = "temp_elective_search_results"
TEMP_ELECTIVE_LEVEL_ID = "temp_elective_level_id"

# Constants
ELECTIVE_PAGE_SIZE = 9

def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

async def is_command_allowed(telegram_id: int, context) -> bool:
    """Return True when the command is NOT allowed (i.e. user hasn't selected a group).
    Uses the Schedule API to check whether the user has any groups assigned.
    """
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None:
        # If client is not available, conservatively disallow command
        return True
    try:
        groups = await client.get_user_groups(telegram_id)
        return not bool(groups)
    except NotFoundError:
        return True
    except Exception:
        # On error, we don't block the command outright; return False to let API call surface errors
        return False


def week_parity(reference_year: int, check_date: date = None) -> int:
    if check_date is None:
        check_date = date.today()
    sept1 = date(reference_year, 9, 1)
    weeks = (check_date - sept1).days // 7
    return 1 if weeks % 2 == 0 else 2


async def start(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    welcome_message = 'Привіт!\nЦей бот здатен відображати заняття за розкладом групи та вибіркові дисципліни.\n'

    welcome_message += '\n• /change_group - обрання своєї групи'
    welcome_message += '\n• /elective_add - додавання вибіркових'
    welcome_message += '\n• /electives - перегляд своїх вибіркових'
    welcome_message += '\n• /schedule - перегляд розкладу'

    welcome_message += '\n\nУ разі виникнення проблем, звертайся до @kaidigital_bot'

    await update.message.reply_html(welcome_message)


async def display_week(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    week_message = f'📗 Триває {str(week_parity(datetime.now().year, datetime.now().date()))}-й тиждень.\n'

    week_message += '\n⏰ Початок та кінець пар:'
    week_message += '\n• 1 пара - 8.00 - 9.35'
    week_message += '\n• 2 пара - 9.50 - 11.25'
    week_message += '\n• 3 пара - 11.40 - 13.15'
    week_message += '\n• 4 пара - 13.30 - 15.05'
    week_message += '\n• 5 пара - 15.20 - 16.55'
    week_message += '\n• 6 пара - 17.10 - 18.45'
    week_message += '\n• 7 пара - 19.00 - 20.35'

    week_message += '\n\n• • • • • • • • • • • • • • • • • • •\n🤖 Надіслано ботом @schedulekai_bot'

    await update.message.reply_html(week_message)


def build_schedule_nav_keyboard(target_date: datetime) -> List[List[InlineKeyboardButton]]:
    cur_date = target_date.strftime("%Y-%m-%d")
    return [[
        InlineKeyboardButton("◀️ Попередній", callback_data=f"SCH_NAV|{cur_date}|PREV"),
        InlineKeyboardButton("Наступний ▶️", callback_data=f"SCH_NAV|{cur_date}|NEXT"),
    ]]


async def get_schedule(target_date: datetime, telegram_id: int, context) -> list:
    """Call Schedule API and return list of LessonDto for the given date and user.

    The API expects an ISO-8601 UTC datetime string (e.g. 2026-02-17T00:00:00Z). We convert the
    provided target_date (assumed in Europe/Kyiv) to UTC midnight for the given day.
    """
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None:
        raise RuntimeError("Schedule API client is not initialized")

    user_tz = ZoneInfo("Europe/Kyiv")
    # Normalize target_date: use local date at midnight
    if target_date.tzinfo is None:
        local_midnight = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
    else:
        local_midnight = target_date.astimezone(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)

    utc_dt = local_midnight.astimezone(ZoneInfo("UTC"))
    iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    lessons = await client.get_schedule(iso, telegram_id)
    return lessons


async def render_schedule(update: Update, context, target_date: datetime = None, from_callback: bool = False):
    """General function to get schedule, apply timezone, render, and send UTC datetime.

    The function accepts either a message Update or a callback Update. When handling a
    callback, pass from_callback=True and the function will use update.callback_query to
    edit/answer messages. For normal commands, pass from_callback=False and update.message
    will be used.
    """
    # Determine telegram user id from the update
    tg_id = None
    if getattr(update, 'effective_user', None):
        tg_id = update.effective_user.id
    else:
        try:
            if getattr(update, 'message', None) and getattr(update.message, 'from_user', None):
                tg_id = update.message.from_user.id
            elif getattr(update, 'callback_query', None) and getattr(update.callback_query, 'from_user', None):
                tg_id = update.callback_query.from_user.id
        except Exception:
            tg_id = None

    if await is_command_allowed(tg_id, context):
        # Inform user they need to choose a group
        if from_callback and getattr(update, 'callback_query', None):
            await update.callback_query.answer("Ви ще не вибрали групу.", show_alert=True)
        elif getattr(update, 'message', None):
            await update.message.reply_html("Ви ще не вибрали групу.")
        return

    # Use current date if not provided
    if target_date is None:
        user_tz = ZoneInfo("Europe/Kyiv")
        target_date = datetime.now(tz=user_tz)

    # Fetch lessons (API expects UTC ISO datetime)
    lessons = await get_schedule(target_date, tg_id, context)
    try:
        # some API responses may already be LessonDto objects
        lessons.sort(key=lambda l: l.begin_time)
    except Exception:
        pass

    text = generate_telegram_message_from_list(lessons, target_date, week_parity(target_date.year, target_date.date()))
    kb = build_schedule_nav_keyboard(target_date)

    if from_callback and getattr(update, 'callback_query', None):
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            # ignore edit failures
            pass
    elif getattr(update, 'message', None):
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def schedule_command(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    # Allow /schedule [DD.MM]
    if context.args:
        try:
            user_zone = ZoneInfo("Europe/Kyiv")
            given_date = datetime.strptime(context.args[0], "%d.%m").replace(year=datetime.now().year, tzinfo=user_zone)
        except ValueError:
            await update.message.reply_html("Невірний формат дати. Використовуйте <code>ДД.MM</code> (наприклад, 20.09).")
            return
    else:
        given_date = None

    await render_schedule(update, context, target_date=given_date, from_callback=False)


async def tomorrow_command(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    user_zone = ZoneInfo("Europe/Kyiv")
    target = datetime.now(tz=user_zone) + timedelta(days=1)
    await render_schedule(update, context, target_date=target, from_callback=False)


async def ask_for_group(update_or_query, context) -> None:
    """Notify user that they need to choose a group and suggest /change_group."""
    try:
        # If this is a callback query, answer it with an alert
        if getattr(update_or_query, 'callback_query', None):
            await update_or_query.callback_query.answer("Ви ще не вибрали групу. Використайте /change_group щоб обрати групу.", show_alert=True)
            return
    except Exception:
        pass

    # Otherwise, reply to the message if present
    if getattr(update_or_query, 'message', None):
        await update_or_query.message.reply_html("Ви ще не вибрали групу. Використайте /change_group щоб обрати групу.")


async def schedule_nav_callback(update: Update, context) -> None:
    """Handle navigation callback buttons (SCH_NAV|YYYY-MM-DD|PREV/NEXT)."""
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 3:
        await cq.answer()
        return
    # parts: ["SCH_NAV", "YYYY-MM-DD", "PREV"|"NEXT"]
    _, date_str, action = parts[0], parts[1], parts[2]
    try:
        user_tz = ZoneInfo("Europe/Kyiv")
        base_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=user_tz)
    except Exception:
        await cq.answer()
        return

    if action == "PREV":
        new_date = base_date - timedelta(days=1)
    else:
        new_date = base_date + timedelta(days=1)

    try:
        await cq.answer()
    except Exception:
        pass

    await render_schedule(update, context, target_date=new_date, from_callback=True)


# Keys in context.user_data
CHANGING_GROUP_KEY = "changing_group"
TEMP_GROUP_NAME_KEY = "temp_group_name"


def normalize_group_name(name: str) -> str:
    """Replace Latin letters with visually equivalent Ukrainian letters for group codes."""
    # Mapping from ASCII Latin to Cyrillic Ukrainian characters (best-effort)
    mapping = {
        'A': 'А', 'a': 'а',
        'B': 'В', 'b': 'в',
        'E': 'Е', 'e': 'е',
        'K': 'К', 'k': 'к',
        'M': 'М', 'm': 'м',
        'H': 'Н', 'h': 'н',
        'O': 'О', 'o': 'о',
        'P': 'Р', 'p': 'р',
        'C': 'С', 'c': 'с',
        'T': 'Т', 't': 'т',
        'Y': 'У', 'y': 'у',
        'X': 'Х', 'x': 'х',
        'I': 'І', 'i': 'і',
        'V': 'В', 'v': 'в',
        'S': 'С', 's': 'с',
        'N': 'Н', 'n': 'н',
        'R': 'Р', 'r': 'р'
    }
    return ''.join(mapping.get(ch, ch) for ch in name)


async def change_group_command(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    """Start change group flow: ask user to input group name and show current group."""
    tg_id = update.effective_user.id if update.effective_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")

    current = "(не встановлена)"
    try:
        if client and tg_id:
            groups = await client.get_user_groups(tg_id)
            if groups:
                # show first assigned group
                current = groups[0]
    except Exception:
        # ignore errors showing current group
        pass

    text = f"Введіть код вашої групи. Поточна група: {current}\n\nНаприклад: КН-01"
    kb = [[InlineKeyboardButton("Скасувати", callback_data="CHANGE_GROUP_CANCEL")]]

    # mark state
    context.user_data[CHANGING_GROUP_KEY] = True
    # clear any previous temp
    context.user_data.pop(TEMP_GROUP_NAME_KEY, None)

    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def cancel_change_flow(update_or_query, context) -> None:
    """Cancel the ongoing change_group flow (message or callback)."""
    # clear state
    group = context.user_data.pop(CHANGING_GROUP_KEY, None)
    other_group = context.user_data.pop(TEMP_GROUP_NAME_KEY, None)
    if group is None and other_group is None:
        return

    if getattr(update_or_query, 'callback_query', None):
        try:
            await update_or_query.callback_query.answer("Зміна групи скасована.")
            await update_or_query.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    elif getattr(update_or_query, 'message', None):
        await update_or_query.message.reply_html("Зміна групи скасована.")


async def handle_group_text(update: Update, context) -> None:
    """Handle text input when user is in change group flow."""
    # If user is in elective-name entry flow, delegate to elective handler
    if context.user_data.get(EXPECTING_ELECTIVE_NAME):
        await handle_elective_name_text(update, context)
        return

    if not context.user_data.get(CHANGING_GROUP_KEY):
        return

    text = update.message.text.strip()
    norm = normalize_group_name(text)

    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    tg_id = update.effective_user.id if update.effective_user else None
    if client is None or tg_id is None:
        await update.message.reply_html("Виникла помилка сервера. Спробуйте пізніше.")
        context.user_data.pop(CHANGING_GROUP_KEY, None)
        return

    try:
        exists = await client.group_exists(norm)
    except Exception:
        await update.message.reply_html("Не вдалося перевірити групу. Спробуйте ще раз.")
        return

    if not exists:
        await update.message.reply_html("Групи з такою назвою не знайдено. Введіть назву ще раз.")
        return

    # group exists; query subgroups by group name
    try:
        subgroups = await client.get_group_subgroups(norm)
    except NotFoundError:
        subgroups = []
    except Exception:
        subgroups = []

    # store temp name
    context.user_data[TEMP_GROUP_NAME_KEY] = norm

    if len(subgroups) > 1:
        subgroups.remove(-1)
        # build buttons for subgroups and 'all'
        kb = []
        row = []
        for sg in subgroups:
            # sg may be int or dict; show appropriate label
            label = str(sg) if not isinstance(sg, dict) else str(sg.get('number', sg))
            row.append(InlineKeyboardButton(label, callback_data=f"CHANGE_GROUP_SUB|{norm}|{label}"))
            if len(row) >= 4:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        # 'all' button
        kb.append([InlineKeyboardButton("Всі підгрупи", callback_data=f"CHANGE_GROUP_SUB|{norm}|-1")])

        await update.message.reply_html("Оберіть підгрупу:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        # no subgroups -> set subgroup -1
        try:
            await client.update_user_group(tg_id, norm, -1)
            await update.message.reply_html(f"Групу змінено на: {norm}")
        except Exception:
            await update.message.reply_html("Не вдалося змінити групу. Спробуйте ще раз пізніше.")


async def change_group_cancel_callback(update: Update, context) -> None:
    cq = update.callback_query
    if cq:
        await cancel_change_flow(cq, context)


async def change_group_sub_callback(update: Update, context) -> None:
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 3:
        await cq.answer()
        return
    _, group_name, subgroup = parts[0], parts[1], parts[2]
    try:
        subgroup_number = int(subgroup)
    except Exception:
        try:
            subgroup_number = int(subgroup)
        except Exception:
            subgroup_number = 0

    tg_id = cq.from_user.id if cq.from_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await cq.answer()
        return

    try:
        await client.update_user_group(tg_id, group_name, subgroup_number)
    except Exception:
        await cq.answer("Не вдалося змінити групу. Спробуйте пізніше.")
        return

    # clear state and inform user
    context.user_data.pop(CHANGING_GROUP_KEY, None)
    context.user_data.pop(TEMP_GROUP_NAME_KEY, None)
    try:
        text = f"Групу змінено на: {group_name}"
        if subgroup_number != -1:
            text += f" (підгрупа: {subgroup_number})"
        await cq.edit_message_text(text)
    except Exception:
        try:
            await cq.answer("Групу змінено.")
        except Exception:
            pass

# ------------------------------------------------------------------
# Elective viewing and management
# ------------------------------------------------------------------

async def _build_electives_keyboard(electives: list, page: int) -> List[List[InlineKeyboardButton]]:
    """Return inline keyboard rows for a page of electives.

    Each elective is a separate row; navigation buttons are placed at the bottom.
    """
    kb: List[List[InlineKeyboardButton]] = []
    total = len(electives)
    if total == 0:
        return kb

    pages = (total + ELECTIVE_PAGE_SIZE - 1) // ELECTIVE_PAGE_SIZE
    if page < 0:
        page = 0
    if page >= pages:
        page = pages - 1

    start = page * ELECTIVE_PAGE_SIZE
    end = min(start + ELECTIVE_PAGE_SIZE, total)

    for idx, el in enumerate(electives[start:end], start=start + 1):
        label = f"{idx}. {el.lesson_name}"
        if el.lesson_type:
            label += f" — {el.lesson_type}"
        if el.subgroup_number is not None:
            label += f" (підгрупа {el.subgroup_number})"
        kb.append([InlineKeyboardButton(label, callback_data=f"EL_ITEM|{el.id}|{page}")])

    # navigation
    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Попередній", callback_data=f"EL_LIST|{page}|PREV"))
    if page < pages - 1:
        nav_row.append(InlineKeyboardButton("Наступний ▶️", callback_data=f"EL_LIST|{page}|NEXT"))
    if nav_row:
        kb.append(nav_row)

    return kb


async def _render_electives_text(electives: list, page: int) -> str:
    total = len(electives)
    if total == 0:
        return "У вас немає доданих вибіркових дисциплін."
    pages = (total + ELECTIVE_PAGE_SIZE - 1) // ELECTIVE_PAGE_SIZE
    if page < 0:
        page = 0
    if page >= pages:
        page = pages - 1
    start = page * ELECTIVE_PAGE_SIZE
    end = min(start + ELECTIVE_PAGE_SIZE, total)
    lines = [f"Ваші вибіркові (сторінка {page+1}/{pages}):\n"]
    for idx, el in enumerate(electives[start:end], start=start + 1):
        typ = el.lesson_type or "—"
        lines.append(f"{idx}. {el.lesson_name} — {typ} (підгрупа: {el.subgroup_number})")
    return "\n".join(lines)


async def elective_list_command(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    """Handler for /electives command - show first page of user's electives."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.effective_user.id if update.effective_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await update.message.reply_html("Виникла помилка сервера. Спробуйте пізніше.")
        return

    try:
        electives = await client.get_user_electives(tg_id)
    except NotFoundError:
        electives = []
    except Exception:
        await update.message.reply_html("Не вдалося отримати ваші вибіркові. Спробуйте пізніше.")
        return

    page = 0
    text = await _render_electives_text(electives, page)
    kb = await _build_electives_keyboard(electives, page)
    if kb:
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_html(text)


async def elective_list_callback(update: Update, context) -> None:
    """Callback handler for paginated elective list navigation (EL_LIST)."""
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return

    try:
        cur_page = int(parts[1])
    except Exception:
        cur_page = 0

    action = parts[2] if len(parts) >= 3 else None
    if action == "PREV":
        new_page = max(cur_page - 1, 0)
    elif action == "NEXT":
        new_page = cur_page + 1
    else:
        new_page = cur_page

    tg_id = cq.from_user.id if cq.from_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await cq.answer()
        return

    try:
        electives = await client.get_user_electives(tg_id)
    except NotFoundError:
        electives = []
    except Exception:
        await cq.answer("Не вдалося отримати вибіркові. Спробуйте пізніше.")
        return

    text = await _render_electives_text(electives, new_page)
    kb = await _build_electives_keyboard(electives, new_page)
    try:
        if kb:
            await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:
        try:
            await cq.answer()
        except Exception:
            pass


async def elective_item_callback(update: Update, context) -> None:
    """Callback handler to show details for a single elective (EL_ITEM|<id>|<page>)."""
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return

    try:
        elective_id = int(parts[1])
    except Exception:
        await cq.answer()
        return

    page = int(parts[2]) if len(parts) >= 3 else 0

    tg_id = cq.from_user.id if cq.from_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await cq.answer()
        return

    try:
        electives = await client.get_user_electives(tg_id)
    except NotFoundError:
        electives = []
    except Exception:
        await cq.answer("Не вдалося отримати деталі. Спробуйте пізніше.")
        return

    found = None
    for el in electives:
        if getattr(el, "id", None) == elective_id:
            found = el
            break

    if found is None:
        await cq.answer("Вибіркова дисципліна не знайдена.")
        return

    typ = found.lesson_type or "—"
    text = (
        f"<b>{found.lesson_name}</b>\n"
        f"Тип: {typ}\n"
        f"Підгрупа: {found.subgroup_number}\n"
        f"ID: {found.id}"
    )

    kb = [
        [InlineKeyboardButton("Видалити", callback_data=f"EL_DEL|{found.id}|{page}" )],
        [InlineKeyboardButton("Назад", callback_data=f"EL_LIST|{page}")],
    ]

    try:
        await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        try:
            await cq.answer()
        except Exception:
            pass


async def elective_delete_callback(update: Update, context) -> None:
    """Callback handler to delete an elective (EL_DEL|<id>|<page>)."""
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return

    try:
        elective_id = int(parts[1])
    except Exception:
        await cq.answer()
        return

    page = int(parts[2]) if len(parts) >= 3 else 0

    tg_id = cq.from_user.id if cq.from_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await cq.answer()
        return

    try:
        await client.delete_user_elective(tg_id, elective_id)
    except NotFoundError:
        await cq.answer("Вибіркова дисципліна не знайдена.")
        return
    except Exception:
        await cq.answer("Не вдалося видалити вибіркову. Спробуйте пізніше.")
        return

    # After deletion, refresh list and show the same page
    try:
        electives = await client.get_user_electives(tg_id)
    except Exception:
        electives = []

    text = await _render_electives_text(electives, page)
    kb = await _build_electives_keyboard(electives, page)

    try:
        if kb:
            await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:
        try:
            await cq.answer("Видалено.")
        except Exception:
            pass


async def elective_add_command(update: Update, context) -> None:
    await cancel_change_flow(update, context)
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    # clear any previous elective-related state
    context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
    context.user_data.pop(TEMP_ELECTIVE_SEARCH_RESULTS, None)
    context.user_data.pop(TEMP_ELECTIVE_LESSON_ID, None)
    context.user_data.pop(TEMP_ELECTIVE_LESSON_TYPE, None)
    context.user_data.pop(TEMP_ELECTIVE_LEVEL_ID, None)

    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None:
        await update.message.reply_html("Сервіс тимчасово недоступний.")
        return

    # ask user to choose elective level first
    try:
        levels = await client.get_elective_levels()
    except Exception:
        await update.message.reply_html("Не вдалося отримати рівні вибіркових. Продовжте, ввівши назву.")
        # fallback to asking for name
        context.user_data[EXPECTING_ELECTIVE_NAME] = True
        kb = [[InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")]]
        await update.message.reply_html("Введіть частину назви дисципліни, яку ви хочете додати:", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb = []
    row = []
    for lvl in levels:
        label = getattr(lvl, 'name', str(lvl))
        row.append(InlineKeyboardButton(label, callback_data=f"EL_LEVEL|{getattr(lvl, 'id', lvl)}"))
        if len(row) >= 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    # allow choosing all levels
    kb.append([InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")])

    await update.message.reply_html("Оберіть рівень вибіркових:", reply_markup=InlineKeyboardMarkup(kb))


async def elective_level_callback(update: Update, context) -> None:
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return
    try:
        level_id = int(parts[1])
    except Exception:
        level_id = None

    # store level (None or -1 means no filter)
    if level_id == -1:
        context.user_data[TEMP_ELECTIVE_LEVEL_ID] = None
    else:
        context.user_data[TEMP_ELECTIVE_LEVEL_ID] = level_id

    # prompt for lesson name
    try:
        await cq.edit_message_text("Введіть частину назви дисципліни, яку ви хочете додати:")
    except Exception:
        try:
            await cq.answer()
        except Exception:
            pass

    # mark expecting name
    context.user_data[EXPECTING_ELECTIVE_NAME] = True
    # give cancel option as separate message
    try:
        await cq.message.reply_html("Якщо потрібно, натисніть Скасувати:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")]]))
    except Exception:
        pass


async def handle_elective_name_text(update: Update, context) -> None:
    """Process user's text when expecting elective name: search and present results."""
    text = update.message.text.strip()
    tg_id = update.effective_user.id if update.effective_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await update.message.reply_html("Виникла помилка сервера. Спробуйте пізніше.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        return

    # Need user's source id (take first group)
    try:
        groups = await client.get_user_groups(tg_id)
        if not groups:
            await update.message.reply_html("Ви ще не вибрали групу. Використайте /change_group щоб обрати групу.")
            context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
            return
        source_id = groups[0].source_id if hasattr(groups[0], 'source_id') else getattr(groups[0], 'sourceId', None)
    except Exception:
        await update.message.reply_html("Не вдалося отримати інформацію про вашу групу. Спробуйте пізніше.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        return

    try:
        level_id = context.user_data.get(TEMP_ELECTIVE_LEVEL_ID)
        results = await client.search_elective_lessons(text, source_id, level_id)
    except TooManyElementsError:
        await update.message.reply_html("Забагато результатів. Введіть більш точну назву.")
        return
    except NotFoundError:
        await update.message.reply_html("Не знайдено дисциплінів за запитом.")
        return
    except Exception:
        await update.message.reply_html("Помилка при пошуку. Спробуйте пізніше.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        return

    if not results:
        await update.message.reply_html("Не знайдено дисциплін за запитом. Спробуйте іншу назву.")
        return

    # store results for selection
    context.user_data[TEMP_ELECTIVE_SEARCH_RESULTS] = results

    # build keyboard of matches
    kb = []
    for idx, r in enumerate(results):
        label = getattr(r, 'name', None) or getattr(r, 'lesson_name', str(r))
        kb.append([InlineKeyboardButton(label, callback_data=f"EL_SEL|{idx}")])

    kb.append([InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")])
    try:
        await update.message.reply_html("Оберіть дисципліну:", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await update.message.reply_text("Оберіть дисципліну:")


async def cancel_elective_add_callback(update: Update, context) -> None:
     cq = update.callback_query
     if cq:
         # clear elective-related state
         context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
         context.user_data.pop(TEMP_ELECTIVE_SEARCH_RESULTS, None)
         context.user_data.pop(TEMP_ELECTIVE_LESSON_ID, None)
         context.user_data.pop(TEMP_ELECTIVE_LESSON_TYPE, None)
         context.user_data.pop(TEMP_ELECTIVE_LEVEL_ID, None)
         try:
             await cq.answer("Додавання вибіркової скасовано.")
             await cq.edit_message_reply_markup(reply_markup=None)
         except Exception:
             pass


async def elective_select_callback(update: Update, context) -> None:
    """Handle selection from search results (EL_SEL|<index>)."""
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return
    try:
        idx = int(parts[1])
    except Exception:
        await cq.answer()
        return

    results = context.user_data.get(TEMP_ELECTIVE_SEARCH_RESULTS) or []
    if idx < 0 or idx >= len(results):
        await cq.answer("Невірний вибір.")
        return

    selected = results[idx]
    # store selected source id and name
    source_id = getattr(selected, 'source_id', None) or getattr(selected, 'sourceId', None) or getattr(selected, 'id', None)
    lesson_name = getattr(selected, 'name', None) or getattr(selected, 'lesson_name', None) or str(selected)
    context.user_data[TEMP_ELECTIVE_LESSON_ID] = source_id
    context.user_data[TEMP_ELECTIVE_LESSON_TYPE] = None

    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None:
        await cq.answer("Сервіс тимчасово недоступний.")
        return

    # fetch types
    try:
        types = await client.get_elective_types(lesson_name, source_id)
    except NotFoundError:
        types = []
    except Exception:
        await cq.answer("Не вдалося отримати типи. Спробуйте пізніше.")
        return

    if not types:
        # proceed to subgroup selection with no type
        context.user_data[TEMP_ELECTIVE_LESSON_TYPE] = None
        await cq.answer()
        await _ask_for_elective_subgroup(cq, context, source_id, lesson_name, None)
        return

    if len(types) == 1:
        context.user_data[TEMP_ELECTIVE_LESSON_TYPE] = types[0]
        await cq.answer()
        await _ask_for_elective_subgroup(cq, context, source_id, lesson_name, types[0])
        return

    # multiple types -> present buttons
    kb = []
    for t in types:
        kb.append([InlineKeyboardButton(t, callback_data=f"EL_TYPE|{t}")])
    kb.append([InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")])
    try:
        await cq.edit_message_text(f"Оберіть тип для <b>{lesson_name}</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        try:
            await cq.answer()
        except Exception:
            pass


async def elective_type_callback(update: Update, context) -> None:
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return
    chosen_type = parts[1]
    source_id = context.user_data.get(TEMP_ELECTIVE_LESSON_ID)
    results = context.user_data.get(TEMP_ELECTIVE_SEARCH_RESULTS) or []
    lesson_name = None
    if results:
        # try to find name from previously stored selection
        for r in results:
            if getattr(r, 'source_id', None) == source_id or getattr(r, 'sourceId', None) == source_id:
                lesson_name = getattr(r, 'name', None) or getattr(r, 'lesson_name', None)
                break
    context.user_data[TEMP_ELECTIVE_LESSON_TYPE] = chosen_type
    await cq.answer()
    await _ask_for_elective_subgroup(cq, context, source_id, lesson_name, chosen_type)


async def _ask_for_elective_subgroup(cq, context, lesson_source_id, lesson_name, lesson_type):
    """Query API for available subgroups and present buttons."""
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None:
        try:
            await cq.answer("Сервіс тимчасово недоступний.")
        except Exception:
            pass
        return

    try:
        subgroups = await client.get_elective_subgroups(lesson_source_id, lesson_name, lesson_type)
    except NotFoundError:
        subgroups = []
    except Exception:
        try:
            await cq.answer("Не вдалося отримати підгрупи. Спробуйте пізніше.")
        except Exception:
            pass
        return

    kb = []
    row = []
    for sg in subgroups:
        label = str(sg) if not isinstance(sg, dict) else str(sg.get('number', sg))
        row.append(InlineKeyboardButton(label, callback_data=f"EL_SUB|{label}"))
        if len(row) >= 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("Всі підгрупи", callback_data=f"EL_SUB|-1")])
    kb.append([InlineKeyboardButton("Скасувати", callback_data="EL_ADD_CANCEL")])

    try:
        await cq.edit_message_text(f"Оберіть підгрупу для <b>{lesson_name}</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        try:
            await cq.answer()
        except Exception:
            pass


async def elective_subgroup_callback(update: Update, context) -> None:
    cq = update.callback_query
    if cq is None or cq.data is None:
        return
    parts = cq.data.split("|")
    if len(parts) < 2:
        await cq.answer()
        return
    try:
        subgroup = int(parts[1])
    except Exception:
        await cq.answer()
        return

    source_id = context.user_data.get(TEMP_ELECTIVE_LESSON_ID)
    lesson_type = context.user_data.get(TEMP_ELECTIVE_LESSON_TYPE)
    results = context.user_data.get(TEMP_ELECTIVE_SEARCH_RESULTS) or []
    lesson_name = None
    for r in results:
        if getattr(r, 'source_id', None) == source_id or getattr(r, 'sourceId', None) == source_id:
            lesson_name = getattr(r, 'name', None) or getattr(r, 'lesson_name', None)
            break

    tg_id = cq.from_user.id if cq.from_user else None
    client: ScheduleApiClient = context.application.bot_data.get("schedule_api_client")
    if client is None or tg_id is None:
        await cq.answer("Сервіс тимчасово недоступний.")
        return

    elective = SelectedElectiveLessonInputOutput(
        id=source_id,
        lesson_name=lesson_name or "",
        subgroup_number=subgroup,
        lesson_type=lesson_type,
    )

    try:
        await client.add_user_elective(tg_id, elective)
    except NotFoundError:
        await cq.answer("Дисципліна або користувач не знайдені.")
        return
    except Exception:
        await cq.answer("Не вдалося додати вибіркову. Спробуйте пізніше.")
        return

    # clear state and confirm
    context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
    context.user_data.pop(TEMP_ELECTIVE_SEARCH_RESULTS, None)
    context.user_data.pop(TEMP_ELECTIVE_LESSON_ID, None)
    context.user_data.pop(TEMP_ELECTIVE_LESSON_TYPE, None)
    context.user_data.pop(TEMP_ELECTIVE_LEVEL_ID, None)

    try:
        await cq.edit_message_text(f"Вибіркову <b>{lesson_name}</b> додано (підгрупа: {subgroup}).", parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await cq.answer("Додано.")
        except Exception:
            pass


def main() -> None:
    locale.setlocale(locale.LC_ALL, 'uk_UA.UTF-8')

    token = os.environ["BOT_TOKEN"]
    application = Application.builder().token(token).build()

    # Initialize Schedule API client and store on application for reuse
    schedule_api_base = os.environ.get("SCHEDULE_API_BASE", "http://localhost:5110/")
    application.bot_data["schedule_api_client"] = ScheduleApiClient(schedule_api_base)

    # Existing handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("change_group", change_group_command))
    application.add_handler(CommandHandler(["schedule", "te"], schedule_command))
    application.add_handler(CommandHandler(["tomorrow", "te_t"], tomorrow_command))
    application.add_handler(CommandHandler("week", display_week))
    application.add_handler(CommandHandler("electives", elective_list_command))
    application.add_handler(CommandHandler("elective_add", elective_add_command))
    # change group handlers: cancel on any command, text input, callbacks

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_text))
    application.add_handler(CallbackQueryHandler(change_group_cancel_callback, pattern=r"^CHANGE_GROUP_CANCEL$"))
    application.add_handler(CallbackQueryHandler(change_group_sub_callback, pattern=r"^CHANGE_GROUP_SUB\|"))
    application.add_handler(CallbackQueryHandler(schedule_nav_callback, pattern=r"^SCH_NAV\|"))

    # Elective handlers
    application.add_handler(CallbackQueryHandler(elective_list_callback, pattern=r"^EL_LIST\|"))
    application.add_handler(CallbackQueryHandler(elective_item_callback, pattern=r"^EL_ITEM\|"))
    application.add_handler(CallbackQueryHandler(elective_delete_callback, pattern=r"^EL_DEL\|"))
    application.add_handler(CallbackQueryHandler(cancel_elective_add_callback, pattern=r"^EL_ADD_CANCEL$"))
    application.add_handler(CallbackQueryHandler(elective_select_callback, pattern=r"^EL_SEL\|"))
    application.add_handler(CallbackQueryHandler(elective_type_callback, pattern=r"^EL_TYPE\|"))
    application.add_handler(CallbackQueryHandler(elective_subgroup_callback, pattern=r"^EL_SUB\|"))
    application.add_handler(CallbackQueryHandler(elective_level_callback, pattern=r"^EL_LEVEL\|"))

    async def post_init(app: Application):
        # Open API client session
        client: ScheduleApiClient = app.bot_data.get("schedule_api_client")
        if client:
            await client.open()

        await app.bot.set_my_commands([
            BotCommand("start", "Почати роботу з ботом"),
            BotCommand("schedule", "Розклад на сьогодні або дату"),
            BotCommand("tomorrow", "Розклад на завтра"),
            BotCommand("week", "Відобразити навчальний тиждень"),
            BotCommand("help", "Список команд"),
        ])

    application.post_init = post_init
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
