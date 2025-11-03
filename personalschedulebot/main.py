import calendar
import logging
import os
import locale
from datetime import datetime, timedelta, date
from typing import List
from zoneinfo import ZoneInfo

from telegram import (
    ForceReply,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

from personalschedulebot.LessonMessageMapper import generate_telegram_message_from_list
from personalschedulebot.ScheduleAPI import (
    get_schedule,
    user_exists,
    create_user,
    change_user_group,
    get_user_alerts,
    get_possible_lessons,
    get_possible_subgroups,
    get_possible_days,
    get_user_elective_lessons,
    create_user_elective_entry,
    create_user_elective_source,
    delete_user_elective_entry,
    delete_user_elective_source, user_subgroups,
)
from personalschedulebot.UserAlert import UserAlert, UserAlertType

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

# Constants
ELECTIVE_PAGE_SIZE = 9

def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

def week_parity(reference_year: int, check_date: date = None) -> int:
    if check_date is None:
        check_date = date.today()
    sept1 = date(reference_year, 9, 1)
    weeks = (check_date - sept1).days // 7
    return 1 if weeks % 2 == 0 else 2


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    welcome_message = 'Привіт!\nЦей бот здатен відображати заняття за розкладом групи та вибіркові дисципліни.\n'

    welcome_message += '\n• /change_group - обрання своєї групи'
    welcome_message += '\n• /elective_add - додавання вибіркових'
    welcome_message += '\n• /elective_list - перегляд своїх вибіркових'
    welcome_message += '\n• /schedule - перегляд розкладу'

    welcome_message += '\n\nУ разі виникнення проблем, звертайся до @kaidigital_bot'

    await update.message.reply_html(welcome_message)


async def display_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def change_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    await ask_for_group(update, context)


async def ask_for_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECTING_MANUAL_GROUP] = True
    text = (
        "Введіть код вашої групи (наприклад: Ба-121-22-4-ПІ).\n\n<i>У разі виникнення проблем, звертайся до</i> @kaidigital_bot"
    )
    await update.message.reply_html(text, reply_markup=ForceReply(selective=True))


async def manual_group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        return

    if context.user_data.get(EXPECTING_MANUAL_GROUP):
        group_code = update.message.text.strip()
        tg_id = update.message.from_user.id

        if await user_exists(tg_id):
            await handle_group_change(update, context, tg_id, group_code, "change")
        else:
            await handle_group_change(update, context, tg_id, group_code, "create")

        context.user_data.pop(EXPECTING_MANUAL_GROUP, None)
        return

    # fallback: maybe user typing elective name
    if context.user_data.get(EXPECTING_ELECTIVE_NAME):
        await handle_elective_partial_name_input(update, context)
        return


async def handle_group_change(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_id: int, group_code: str, action: str) -> None:
    """Handle group creation or change, optionally showing subgroup selection."""
    # Store temporary data
    context.user_data[TEMP_GROUP_CODE] = group_code
    context.user_data[TEMP_GROUP_ACTION] = action

    # Attempt initial group change/creation with default subgroup
    if action == "create":
        result = await create_user(tg_id, group_code)
    else:
        result = await change_user_group(tg_id, group_code)

    if result == 1:
        await update.message.reply_html("Не вірна назва групи.")
        context.user_data.pop(TEMP_GROUP_CODE, None)
        context.user_data.pop(TEMP_GROUP_ACTION, None)
        return
    elif result != 0:
        await update.message.reply_text("Не вдалося оновити групу.\nЗверніться у підтримку @kaidigital_bot.")
        context.user_data.pop(TEMP_GROUP_CODE, None)
        context.user_data.pop(TEMP_GROUP_ACTION, None)
        return

    # Try to fetch available subgroups
    try:
        subgroups = await user_subgroups(tg_id)
        # Filter out -1 (we'll add it explicitly)
        available_subgroups = [sg for sg in subgroups if sg != -1]

        if available_subgroups:
            # Show subgroup selection
            text = f"Оберіть підгрупу для групи <b>{group_code}</b>:"
            kb = [[InlineKeyboardButton("Усі підгрупи", callback_data=f"GROUP_SUBGROUP|{tg_id}|-1")]]
            for sg in sorted(available_subgroups):
                kb.append([InlineKeyboardButton(f"Підгрупа {sg}", callback_data=f"GROUP_SUBGROUP|{tg_id}|{sg}")])
            await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            # No subgroups available, just confirm
            if action == "create":
                await update.message.reply_html(
                    f"Група встановлена: <b>{group_code}</b>.\n\nВикористайте /schedule щоб отримати розклад.\nВикористайте /elective_add для додавання вибіркових дисциплін."
                )
            else:
                await update.message.reply_html(f"Група змінена на <b>{group_code}</b>.")
            context.user_data.pop(TEMP_GROUP_CODE, None)
            context.user_data.pop(TEMP_GROUP_ACTION, None)
    except Exception as e:
        logger.error(f"Error fetching subgroups: {e}")
        # Silently continue without subgroup selection
        if action == "create":
            await update.message.reply_html(
                f"Група встановлена: <b>{group_code}</b>.\n\nВикористайте /schedule щоб отримати розклад.\nВикористайте /elective_add для додавання вибіркових дисциплін."
            )
        else:
            await update.message.reply_html(f"Група змінена на <b>{group_code}</b>.")
        context.user_data.pop(TEMP_GROUP_CODE, None)
        context.user_data.pop(TEMP_GROUP_ACTION, None)


async def handle_group_subgroup_selected(query, context: ContextTypes.DEFAULT_TYPE, tg_id: int, subgroup: int) -> None:
    """User selected a subgroup — update user with the selected subgroup."""
    group_code = context.user_data.get(TEMP_GROUP_CODE)
    action = context.user_data.get(TEMP_GROUP_ACTION)

    if not group_code or not action:
        await query.edit_message_text("Помилка: інформація про групу не знайдена.")
        return

    # Update user with selected subgroup
    if action == "create":
        result = await create_user(tg_id, group_code, subgroup)
    else:
        result = await change_user_group(tg_id, group_code, subgroup)

    if result == 0:
        if subgroup == -1:
            await query.edit_message_text(f"✅ Підгрупа встановлена на <b>Усі підгрупи</b>.", parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(f"✅ Підгрупа встановлена на <b>Підгрупа {subgroup}</b>.", parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text("❌ Не вдалося встановити підгрупу. Спробуйте пізніше.")

    # Cleanup
    context.user_data.pop(TEMP_GROUP_CODE, None)
    context.user_data.pop(TEMP_GROUP_ACTION, None)


def build_schedule_nav_keyboard(target_date: datetime) -> List[List[InlineKeyboardButton]]:
    cur_date = target_date.strftime("%Y-%m-%d")
    return [[
        InlineKeyboardButton("◀️ Попередній", callback_data=f"SCH_NAV|{cur_date}|PREV"),
        InlineKeyboardButton("Наступний ▶️", callback_data=f"SCH_NAV|{cur_date}|NEXT"),
    ]]


async def render_schedule(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_date: datetime = None, from_callback: bool = False):
    """General function to get schedule, apply timezone, render, and send UTC datetime."""
    tg_id = update_or_query.from_user.id if from_callback else update_or_query.message.from_user.id

    if not await user_exists(tg_id):
        if not from_callback:
            await update_or_query.message.reply_html("Ви ще не вибрали групу.")
            await ask_for_group(update_or_query, context)
        return

    # Use current date if not provided
    if target_date is None:
        user_tz = ZoneInfo("Europe/Kyiv")
        target_date = datetime.now(tz=user_tz)

    # Fetch lessons (API expects naive datetime)
    lessons = await get_schedule(target_date, tg_id)
    lessons.sort(key=lambda l: l.begin_time)

    text = generate_telegram_message_from_list(lessons, target_date, week_parity(target_date.year, target_date.date()))
    kb = build_schedule_nav_keyboard(target_date)

    if from_callback:
        try:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
    else:
        await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            user_zone = ZoneInfo("Europe/Kyiv")
            given_date = datetime.strptime(context.args[0], "%d.%m").replace(year=datetime.now().year, tzinfo=user_zone)
        except ValueError:
            await update.message.reply_html("Невірний формат дати. Використовуйте <code>ДД.MM</code> (наприклад, 20.09).")
            return
    else:
        given_date = None

    await render_schedule(update, context, target_date=given_date)


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_zone = ZoneInfo("Europe/Kyiv")
    target = datetime.now(tz=user_zone) + timedelta(days=1)
    await render_schedule(update, context, target_date=target)


async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if not parts:
        return

    # Handle group subgroup selection
    if parts[0] == "GROUP_SUBGROUP":  # parts: GROUP_SUBGROUP|<telegramId>|<subgroup>
        if len(parts) != 3:
            return
        tg_id = int(parts[1])
        subgroup = int(parts[2])
        await handle_group_subgroup_selected(query, context, tg_id, subgroup)
        return

    # Schedule navigation
    if parts[0] == "SCH_NAV":
        if len(parts) != 3:
            return
        try:
            current_date = datetime.strptime(parts[1], "%Y-%m-%d")
            current_date = current_date.replace(tzinfo=ZoneInfo("Europe/Kyiv"))
        except ValueError:
            return
        new_date = current_date + timedelta(days=-1 if parts[2] == "PREV" else 1 if parts[2] == "NEXT" else 0)
        await render_schedule(query, context, target_date=new_date, from_callback=True)
        return

    # Elective callbacks start with EL_
    if parts[0] == "EL_LESSON":  # parts: EL_LESSON|<lessonId>
        lesson_id = int(parts[1])
        await handle_elective_lesson_selected(query, context, lesson_id)
        return

    if parts[0] == "EL_METHOD":  # parts: EL_METHOD|<lessonId>|<method>
        lesson_id = int(parts[1])
        method = parts[2]  # "subgroup" or "manual"
        await handle_elective_method_selected(query, context, lesson_id, method)
        return

    if parts[0] == "EL_SUBGROUP_TYPE":  # parts: EL_SUBGROUP_TYPE|<lessonId>|<type>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        await handle_elective_subgroup_type_selected(query, context, lesson_id, lesson_type)
        return

    if parts[0] == "EL_SUBGROUP":  # parts: EL_SUBGROUP|<lessonId>|<type>|<subgroup>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        subgroup = int(parts[3])
        await handle_elective_subgroup_selected(query, context, lesson_id, lesson_type, subgroup)
        return

    if parts[0] == "EL_MANUAL_TYPE":  # parts: EL_MANUAL_TYPE|<lessonId>|<type>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        await handle_elective_manual_type_selected(query, context, lesson_id, lesson_type)
        return

    if parts[0] == "EL_MANUAL_WEEK":  # parts: EL_MANUAL_WEEK|<lessonId>|<type>|<week>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        week = parts[3] == 'True'
        await handle_elective_manual_week_selected(query, context, lesson_id, lesson_type, week)
        return

    if parts[0] == "EL_MANUAL_DAY":  # parts: EL_MANUAL_DAY|<lessonId>|<type>|<week>|<day>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        week = parts[3] == 'True'
        day = int(parts[4])
        await handle_elective_manual_day_selected(query, context, lesson_id, lesson_type, week, day)
        return

    if parts[0] == "EL_MANUAL_TIME":  # parts: EL_MANUAL_TIME|<lessonId>|<type>|<week>|<day>|<time>
        lesson_id = int(parts[1])
        lesson_type = parts[2]
        week = parts[3] == 'True'
        day = int(parts[4])
        entry_id = int(parts[5])
        await handle_elective_manual_time_selected(query, context, lesson_id, lesson_type, week, day, entry_id)
        return

    if parts[0] == "EL_LIST_MAIN":  # parts: EL_LIST_MAIN
        await handle_elective_list_main_view(query, context)
        return

    if parts[0] == "EL_LIST_LESSON":  # parts: EL_LIST_LESSON|<lessonIndex>
        lesson_index = int(parts[1])
        await handle_elective_list_lesson_view(query, context, lesson_index)
        return

    if parts[0] == "EL_LIST_SOURCE":  # parts: EL_LIST_SOURCE|<sourceId>
        source_id = int(parts[1])
        await handle_elective_list_source_view(query, context, source_id)
        return

    if parts[0] == "EL_DELETE_SOURCE":  # parts: EL_DELETE_SOURCE|<sourceId>
        source_id = int(parts[1])
        await handle_elective_delete_source(query, context, source_id)
        return

    if parts[0] == "EL_DELETE_ENTRY":  # parts: EL_DELETE_ENTRY|<entryId>
        entry_id = int(parts[1])
        await handle_elective_delete_entry(query, context, entry_id)
        return

    if parts[0] == "EL_LIST_BACK":  # Navigate back in hierarchy
        back_to = parts[1] if len(parts) > 1 else "main"
        if back_to == "main":
            await handle_elective_list_main_view(query, context)
        elif back_to.startswith("lesson_"):
            lesson_index = int(back_to.split("_")[1])
            await handle_elective_list_lesson_view(query, context, lesson_index)
        return

    # Unknown callback: ignore
    return


# ---------- Elective flow handlers ----------

async def elective_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start adding elective: ask user to input lesson name."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    tg_id = update.message.from_user.id
    if not await user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу. Використайте /start щоб встановити групу.")
        return

    context.user_data[EXPECTING_ELECTIVE_NAME] = True
    text = "Введіть назву або частину назви вибіркового предмета:"
    await update.message.reply_html(text, reply_markup=ForceReply(selective=True))


async def handle_elective_partial_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User typed lesson name — query API and show matching results."""
    if not context.user_data.get(EXPECTING_ELECTIVE_NAME):
        return

    partial = update.message.text.strip()
    context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)

    try:
        results = await get_possible_lessons(partial)
    except ValueError:
        await update.message.reply_text("❌ Невірна назва. Будь ласка, введіть більш точну назву предмета:")
        context.user_data[EXPECTING_ELECTIVE_NAME] = True
        return
    except Exception as e:
        await update.message.reply_text("Помилка при зверненні до API. Спробуйте пізніше.")
        logger.error(f"Error fetching lessons: {e}")
        return

    if not results:
        await update.message.reply_text("За вказаною назвою нічого не знайдено. Спробуйте інший запит:")
        context.user_data[EXPECTING_ELECTIVE_NAME] = True
        return

    # Store lessons in temp data and show results as inline buttons
    context.user_data["temp_elective_lessons"] = {lesson.source_id: lesson for lesson in results}
    kb = []
    for lesson in results:
        label = f"{lesson.title}"
        kb.append([InlineKeyboardButton(label[:64], callback_data=f"EL_LESSON|{lesson.source_id}")])

    await update.message.reply_text("Оберіть предмет зі списку:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_lesson_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int) -> None:
    """User selected a lesson — check if subgroup adding is possible."""
    context.user_data[TEMP_ELECTIVE_LESSON_ID] = lesson_id

    # Get lesson from cached results
    lessons = context.user_data.get("temp_elective_lessons", {})
    lesson = lessons.get(lesson_id)

    if not lesson:
        await query.edit_message_text("❌ Інформація про предмет не знайдена.")
        return

    # Store lesson types for later use
    context.user_data["temp_elective_lesson_types"] = lesson.types

    has_subgroup = False
    # Check if any type has subgroups other than -1
    for lesson_type in lesson.types:
        try:
            subgroups = await get_possible_subgroups(lesson_id, lesson_type)
            if any(sg != -1 for sg in subgroups.possible_subgroups):
                has_subgroup = True
                break
        except Exception:
            pass

    if not has_subgroup:
        # Only manual adding is possible
        await query.edit_message_text(
            "❌ Цей предмет можна додати тільки вручну.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Додати вручну", callback_data=f"EL_METHOD|{lesson_id}|manual")
            ]])
        )
    else:
        # Show both options
        kb = [[
            InlineKeyboardButton("За потоком (рекомендується)", callback_data=f"EL_METHOD|{lesson_id}|subgroup")
        ], [
            InlineKeyboardButton("Вручну", callback_data=f"EL_METHOD|{lesson_id}|manual")
        ]]
        await query.edit_message_text("Як додати предмет?", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_method_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, method: str) -> None:
    """User selected adding method (subgroup or manual)."""
    context.user_data[TEMP_ELECTIVE_ADD_METHOD] = method

    if method == "subgroup":
        await handle_elective_subgroup_flow_start(query, context, lesson_id)
    else:
        await handle_elective_manual_flow_start(query, context, lesson_id)


async def handle_elective_subgroup_flow_start(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int) -> None:
    """Start subgroup adding flow — ask user to select lesson type."""
    # Get lesson types from stored data
    lesson_types = context.user_data.get("temp_elective_lesson_types", [])

    if not lesson_types:
        await query.edit_message_text("❌ Не вдалося отримати типи заняття.")
        return

    kb = [[InlineKeyboardButton(t.capitalize(), callback_data=f"EL_SUBGROUP_TYPE|{lesson_id}|{t}")] for t in lesson_types]
    await query.edit_message_text("Оберіть тип заняття:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_subgroup_type_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str) -> None:
    """User selected lesson type for subgroup adding — show available subgroups."""
    try:
        subgroups_obj = await get_possible_subgroups(lesson_id, lesson_type)
        subgroups = [sg for sg in subgroups_obj.possible_subgroups if sg != -1]
    except Exception as e:
        await query.edit_message_text("❌ Для цього типу заняття немає доступних потоків.")
        logger.error(f"Error fetching subgroups: {e}")
        return

    if not subgroups:
        await query.edit_message_text("❌ Для цього типу заняття немає доступних потоків.")
        return

    kb = [[InlineKeyboardButton(f"Потік {sg}", callback_data=f"EL_SUBGROUP|{lesson_id}|{lesson_type}|{sg}")] for sg in sorted(subgroups)]
    await query.edit_message_text("Оберіть потік:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_subgroup_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str, subgroup: int) -> None:
    """User selected subgroup — create elective entry."""
    tg_id = query.from_user.id

    try:
        ok = await create_user_elective_source(tg_id, lesson_id, lesson_type, subgroup)
        if ok:
            await query.edit_message_text("✅ Предмет успішно додано до ваших вибіркових пар.")
        else:
            await query.edit_message_text("❌ Не вдалося додати предмет. Спробуйте пізніше.")
    except Exception as e:
        await query.edit_message_text("❌ Помилка при додаванні предмета.")
        logger.error(f"Error creating elective entry: {e}")

    # cleanup
    context.user_data.pop(TEMP_ELECTIVE_LESSON_ID, None)
    context.user_data.pop(TEMP_ELECTIVE_ADD_METHOD, None)


async def handle_elective_manual_flow_start(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int) -> None:
    """Start manual adding flow — ask user to select lesson type."""
    lesson_types = context.user_data.get("temp_elective_lesson_types", [])

    if not lesson_types:
        await query.edit_message_text("❌ Не вдалося отримати типи заняття.")
        return

    kb = [[InlineKeyboardButton(t.capitalize(), callback_data=f"EL_MANUAL_TYPE|{lesson_id}|{t}")] for t in lesson_types]
    await query.edit_message_text("Оберіть тип заняття:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_manual_type_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str) -> None:
    """User selected lesson type for manual adding — show available weeks."""
    context.user_data[TEMP_ELECTIVE_LESSON_TYPE] = lesson_type

    try:
        days_obj = await get_possible_days(lesson_id)
        weeks = sorted(set(day.week_number for day in days_obj.lesson_days if day.type == lesson_type))
    except Exception as e:
        await query.edit_message_text("❌ Не вдалося отримати доступні тижні.")
        logger.error(f"Error fetching days: {e}")
        return

    if not weeks:
        await query.edit_message_text("❌ Для цього типу заняття немає доступних тижнів.")
        return

    kb = [[InlineKeyboardButton(f"Тиждень {w + 1}", callback_data=f"EL_MANUAL_WEEK|{lesson_id}|{lesson_type}|{w}")] for w in weeks]
    await query.edit_message_text("Оберіть тиждень:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_manual_week_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str, week: bool) -> None:
    """User selected week — show available days."""
    context.user_data[TEMP_ELECTIVE_WEEK] = week

    try:
        days_obj = await get_possible_days(lesson_id)
        days = sorted(
            set(day.day_of_week for day in days_obj.lesson_days if day.type == lesson_type and day.week_number == week)
        )
    except Exception as e:
        await query.edit_message_text("❌ Не вдалося отримати доступні дні.")
        logger.error(f"Error fetching days: {e}")
        return

    if not days:
        await query.edit_message_text("❌ Для цього тижня немає доступних днів.")
        return

    kb = []
    for day in days:
        day_name = calendar.day_name[day - 1].capitalize() if 1 <= day <= 7 else str(day)
        kb.append([InlineKeyboardButton(day_name, callback_data=f"EL_MANUAL_DAY|{lesson_id}|{lesson_type}|{week}|{day}")])

    await query.edit_message_text("Оберіть день тижня:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_manual_day_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str, week: bool, day: int) -> None:
    """User selected day — show available lesson times to choose from."""
    context.user_data[TEMP_ELECTIVE_WEEK] = week
    context.user_data[TEMP_ELECTIVE_DAY] = day

    try:
        days_obj = await get_possible_days(lesson_id)
        # Filter by lesson type, week, and day
        matching_days = [
            d for d in days_obj.lesson_days
            if d.type == lesson_type and d.week_number == week and d.day_of_week == day
        ]
    except Exception as e:
        await query.edit_message_text("❌ Не вдалося отримати доступні часи.")
        logger.error(f"Error fetching days: {e}")
        return

    if not matching_days:
        await query.edit_message_text("❌ Для цього дня немає доступних часів.")
        return

    kb = []
    for day_slot in sorted(matching_days, key=lambda x: x.start_time):
        label = f"{day_slot.start_time.strftime('%H:%M')}"
        kb.append([InlineKeyboardButton(label, callback_data=f"EL_MANUAL_TIME|{lesson_id}|{lesson_type}|{week}|{day}|{day_slot.id}")])

    await query.edit_message_text("Оберіть час заняття:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_manual_time_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, lesson_type: str, week: int, day: int, entry_id: int) -> None:
    """User selected time — create elective entry."""
    tg_id = query.from_user.id

    try:
        ok = await create_user_elective_entry(tg_id, lesson_id, entry_id)
        if ok:
            await query.edit_message_text("✅ Предмет успішно додано до ваших вибіркових пар.")
        else:
            await query.edit_message_text("❌ Не вдалося додати предмет. Спробуйте пізніше.")
    except Exception as e:
        await query.edit_message_text("❌ Помилка при додаванні предмета.")
        logger.error(f"Error creating elective entry: {e}")

    # cleanup
    context.user_data.pop(TEMP_ELECTIVE_LESSON_ID, None)
    context.user_data.pop(TEMP_ELECTIVE_ADD_METHOD, None)
    context.user_data.pop(TEMP_ELECTIVE_LESSON_TYPE, None)
    context.user_data.pop(TEMP_ELECTIVE_WEEK, None)
    context.user_data.pop(TEMP_ELECTIVE_DAY, None)
    context.user_data.pop("temp_elective_lesson_types", None)
    context.user_data.pop("temp_elective_lessons", None)


async def elective_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's elective lessons."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    tg_id = update.message.from_user.id
    if not await user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу. Використайте /start щоб встановити групу.")
        return

    await handle_elective_list_main_view(update, context)


async def handle_elective_list_main_view(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display list of unique lessons with sources/entries grouped."""
    if isinstance(update_or_query, Update):
        tg_id = update_or_query.message.from_user.id
    else:
        tg_id = update_or_query.from_user.id

    try:
        selected_lessons = await get_user_elective_lessons(tg_id)
    except Exception as e:
        msg = "Помилка при отриманні списку вибіркових предметів."
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        logger.error(f"Error fetching user electives: {e}")
        return

    if not selected_lessons.sources and not selected_lessons.entries:
        msg = "У вас ще немає доданих вибіркових пар."
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return

    # Group lessons by name
    lessons_dict = {}

    # Add sources
    for src in selected_lessons.sources:
        if src.name not in lessons_dict:
            lessons_dict[src.name] = {"sources": [], "entries": []}
        lessons_dict[src.name]["sources"].append(src)

    # Add entries
    for entry in selected_lessons.entries:
        if entry.entry_name not in lessons_dict:
            lessons_dict[entry.entry_name] = {"sources": [], "entries": []}
        lessons_dict[entry.entry_name]["entries"].append(entry)

    # Store in context for navigation
    context.user_data["elective_lessons_dict"] = lessons_dict
    context.user_data["elective_lessons_list"] = list(lessons_dict.keys())

    text = "<b>Ваші вибіркові:</b>\n\n"
    for i, lesson_name in enumerate(context.user_data["elective_lessons_list"]):
        text += f"{i + 1}. {lesson_name}\n"

    kb = []
    for i, lesson_name in enumerate(context.user_data["elective_lessons_list"]):
        kb.append([InlineKeyboardButton(lesson_name[:50], callback_data=f"EL_LIST_LESSON|{i}")])

    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_list_lesson_view(update_or_query, context: ContextTypes.DEFAULT_TYPE, lesson_index: int) -> None:
    """Display all sources and entries for a specific lesson."""
    lessons_dict = context.user_data.get("elective_lessons_dict", {})
    lessons_list = context.user_data.get("elective_lessons_list", [])

    if lesson_index >= len(lessons_list):
        await update_or_query.edit_message_text("Помилка: урок не знайдений.")
        return

    lesson_name = lessons_list[lesson_index]
    lesson_data = lessons_dict[lesson_name]

    text = f"<b>{lesson_name}</b>\n\n"

    kb = []

    # Add sources
    if lesson_data["sources"]:
        text += "<u>За потоком:</u>\n"
        for src in lesson_data["sources"]:
            text += f"• Потік {src.subgroup_number}\n"
            kb.append([InlineKeyboardButton(f"Потік {src.subgroup_number}", callback_data=f"EL_LIST_SOURCE|{src.selected_source_id}")])

    # Add entries
    if lesson_data["entries"]:
        if lesson_data["sources"]:
            text += "\n"
        text += "<u>Додані вручну:</u>\n"
        for entry in lesson_data["entries"]:
            day_name = calendar.day_name[entry.day_of_week - 1].capitalize() if 1 <= entry.day_of_week <= 7 else str(entry.day_of_week)
            week_str = "2" if entry.week_number else "1"
            text += f"• Тиждень {week_str}, {day_name}, {entry.start_time.strftime('%H:%M')}\n"
            kb.append([InlineKeyboardButton(
                f"Тиждень {week_str}, {day_name}, {entry.start_time.strftime('%H:%M')}",
                callback_data=f"EL_LIST_SOURCE|{entry.selected_entry_id}"
            )])

    # Back button
    kb.append([InlineKeyboardButton("Назад", callback_data="EL_LIST_BACK|main")])

    await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_list_source_view(update_or_query, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    """Display details about a specific source or entry."""
    lessons_dict = context.user_data.get("elective_lessons_dict", {})

    source_obj = None
    entry_obj = None
    lesson_name = None
    lesson_index = None

    # Find the source or entry
    for idx, (name, data) in enumerate(zip(context.user_data.get("elective_lessons_list", []), [lessons_dict.get(l) for l in context.user_data.get("elective_lessons_list", [])])):
        for src in data["sources"]:
            if src.selected_source_id == source_id:
                source_obj = src
                lesson_name = name
                lesson_index = idx
                break
        for entry in data["entries"]:
            if entry.selected_entry_id == source_id:
                entry_obj = entry
                lesson_name = name
                lesson_index = idx
                break
        if source_obj or entry_obj:
            break

    if not source_obj and not entry_obj:
        await update_or_query.edit_message_text("Помилка: елемент не знайдений.")
        return

    if source_obj:
        text = f"<b>{lesson_name}</b>\n\n"
        text += f"<b>Тип:</b> За потоком\n"
        text += f"<b>Потік:</b> {source_obj.subgroup_number}\n"
        kb = [
            [InlineKeyboardButton("Видалити", callback_data=f"EL_DELETE_SOURCE|{source_obj.selected_source_id}")],
            [InlineKeyboardButton("Назад", callback_data=f"EL_LIST_BACK|lesson_{lesson_index}")]
        ]
    else:
        day_name = calendar.day_name[entry_obj.day_of_week - 1].capitalize() if 1 <= entry_obj.day_of_week <= 7 else str(entry_obj.day_of_week)
        week_str = "2" if entry_obj.week_number else "1"
        text = f"<b>{lesson_name}</b>\n\n"
        text += f"<b>Тип:</b> Додано вручну\n"
        if entry_obj.type:
            text += f"<b>Вид:</b> {entry_obj.type}\n"
        text += f"<b>Тиждень:</b> {week_str}\n"
        text += f"<b>День:</b> {day_name}\n"
        text += f"<b>Час:</b> {entry_obj.start_time.strftime('%H:%M')}\n"
        kb = [
            [InlineKeyboardButton("Видалити", callback_data=f"EL_DELETE_ENTRY|{entry_obj.selected_entry_id}")],
            [InlineKeyboardButton("Назад", callback_data=f"EL_LIST_BACK|lesson_{lesson_index}")]
        ]

    await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_delete_source(update_or_query, context: ContextTypes.DEFAULT_TYPE, source_id: int) -> None:
    """Delete a source and refresh the view."""
    tg_id = update_or_query.from_user.id

    try:
        ok = await delete_user_elective_source(tg_id, source_id)
        if ok:
            await update_or_query.answer("Видалено", show_alert=False)
            # Refresh to main view
            await handle_elective_list_main_view(update_or_query, context)
        else:
            await update_or_query.answer("Не вдалося видалити. Спробуйте пізніше.", show_alert=True)
    except Exception as e:
        await update_or_query.answer("Помилка при видаленні.", show_alert=True)
        logger.error(f"Error deleting elective source: {e}")


async def handle_elective_delete_entry(update_or_query, context: ContextTypes.DEFAULT_TYPE, entry_id: int) -> None:
    """Delete an entry and refresh the view."""
    tg_id = update_or_query.from_user.id

    try:
        ok = await delete_user_elective_entry(tg_id, entry_id)
        if ok:
            await update_or_query.answer("Видалено", show_alert=False)
            # Refresh to main view
            await handle_elective_list_main_view(update_or_query, context)
        else:
            await update_or_query.answer("Не вдалося видалити. Спробуйте пізніше.", show_alert=True)
    except Exception as e:
        await update_or_query.answer("Помилка при видаленні.", show_alert=True)
        logger.error(f"Error deleting elective entry: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "• /start - почати\n"
        "• /change_group - змінити групу\n"
        "• /schedule [<code>DD.MM</code>] - розклад на сьогодні або дату\n"
        "• /tomorrow - розклад на завтра\n"
        "• /elective_add - додати вибіркове заняття\n"
        "• /elective_list - показати ваші вибіркові заняття\n"
        "• /week - відобразити навчальний тиждень\n"
        "• /help - список команд\n"
        "\n<i>У разі виникнення проблем, звертайся до</i> @kaidigital_bot"
    )


async def alert_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    alert: UserAlert
    for alert in await get_user_alerts(100):
        try:
            match alert.alert_type:
                case UserAlertType.GROUP_REMOVED:
                    msg = generate_group_removed_message(alert)
                    await context.bot.send_message(chat_id=alert.telegram_id, text=msg, parse_mode=ParseMode.HTML)
                case UserAlertType.SOURCE_REMOVED:
                    msg = generate_source_removed_message(alert)
                    await context.bot.send_message(chat_id=alert.telegram_id, text=msg, parse_mode=ParseMode.HTML)
                case UserAlertType.ENTRY_REMOVED:
                    msg = generate_entry_removed_message(alert)
                    await context.bot.send_message(chat_id=alert.telegram_id, text=msg, parse_mode=ParseMode.HTML)
                case UserAlertType.NEWS:
                    await context.bot.send_message(chat_id=alert.telegram_id, text=alert.options['NewsText'], parse_mode=ParseMode.HTML)
                case _:
                    continue
        except:
            pass


def generate_group_removed_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша група '{alert.options['LessonName']}',  була видалена з розкладу.</b>\n"
    result += "Будь ласка, оберіть нову групу командою /change_group.\n"
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку @kaidigital_bot"
    return result


def generate_source_removed_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша вибіркова дисципліна була видалена з розкладу.</b>\n"
    result += f"<b>Предмет:</b> {alert.options['LessonName']}\n"
    result += f"<b>Вид:</b> {alert.options['LessonType']}\n"
    result += f"<b>Потік:</b> {alert.options['SubGroupNumber']}\n"
    result += "Ви можете додати нову вибіркову командою /elective_add.\n"
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку @kaidigital_bot"
    return result


def generate_entry_removed_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша вибіркова дисципліна була видалена з розкладу.</b>\n"
    result += f"<b>Предмет:</b> {alert.options['LessonName']}\n"
    result += f"<b>Вид:</b> {alert.options['LessonType']}\n"
    result += f"<b>Тиждень:</b> {'2' if alert.options['LessonWeek'] == 'True' else '1'}\n"
    result += f"<b>День:</b> {calendar.day_name[(int(alert.options['LessonDay']) - 1) % 7].capitalize()}\n"
    result += f"<b>Час:</b> {alert.options['LessonStartTime']}\n\n"
    result += "Аби додати іншу вибіркову скористайтесь /elective_add.\n"
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку @kaidigital_bot"
    return result


def main() -> None:
    locale.setlocale(locale.LC_ALL, 'uk_UA.UTF-8')

    token = os.environ["BOT_TOKEN"]
    application = Application.builder().token(token).build()

    # Existing handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("change_group", change_group))
    application.add_handler(CommandHandler(["schedule", "te"], schedule_command))
    application.add_handler(CommandHandler(["tomorrow", "te_t"], tomorrow_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("week", display_week))

    # Elective handlers
    application.add_handler(CommandHandler("elective_add", elective_add_command))
    application.add_handler(CommandHandler("elective_list", elective_list_command))

    application.add_handler(CallbackQueryHandler(callback_query_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_group_message_handler))

    async def post_init(app: Application):
        await app.bot.set_my_commands([
            BotCommand("start", "Почати роботу з ботом"),
            BotCommand("change_group", "Змінити групу"),
            BotCommand("schedule", "Розклад на сьогодні або дату"),
            BotCommand("tomorrow", "Розклад на завтра"),
            BotCommand("elective_add", "Додати вибіркове заняття"),
            BotCommand("elective_list", "Переглянути ваші вибіркові заняття"),
            BotCommand("week", "Відобразити навчальний тиждень"),
            BotCommand("help", "Список команд"),
        ])

    application.job_queue.run_repeating(alert_users, interval=30, first=30)
    application.post_init = post_init
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()