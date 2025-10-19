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
    delete_user_elective_source,
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
            result = await change_user_group(tg_id, group_code)
            if result == 0:
                await update.message.reply_html(f"Група змінена на <b>{group_code}</b>.")
            elif result == 1:
                await update.message.reply_html(f"Не вірна назва групи.")
            else:
                await update.message.reply_text("Не вдалося змінити групу.\nЗверніться у підтримку @kaidigital_bot.")
        else:
            result = await create_user(tg_id, group_code)
            if result == 0:
                await update.message.reply_html(
                    f"Група встановлена: <b>{group_code}</b>.\n\nВикористайте /schedule щоб отримати розклад.\nВикористайте /elective_add для додавання вибіркових дисциплін."
                )
            elif result == 1:
                await update.message.reply_html(f"Не вірна назва групи.")
            else:
                await update.message.reply_text("Не вдалося створити користувача.\nЗверніться у підтримку @kaidigital_bot.")

        context.user_data.pop(EXPECTING_MANUAL_GROUP, None)
        return

    # fallback: maybe user typing elective name
    if context.user_data.get(EXPECTING_ELECTIVE_NAME):
        await handle_elective_partial_name_input(update, context)
        return

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

    if parts[0] == "EL_LIST":  # parts: EL_LIST
        await handle_elective_list_view(query, context)
        return

    if parts[0] == "EL_REMOVE":  # parts: EL_REMOVE|<source|entry>|<id>
        remove_type = parts[1]  # "source" or "entry"
        item_id = int(parts[2])
        await handle_elective_remove(query, context, remove_type, item_id)
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

    await handle_elective_list_view(update, update.message.from_user.id, context)


async def handle_elective_list_view(update_or_query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user's elective lessons with remove options."""
    tg_id = user_id

    try:
        selected_lessons = await get_user_elective_lessons(tg_id)
    except Exception as e:
        msg = "❌ Помилка при отриманні списку вибіркових предметів."
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

    text = "<b>Ваші вибіркові:</b>\n\n"

    kb = []

    # entries (manual adding)
    if selected_lessons.entries:
        text += "<u>Додані вручну:</u>\n"
        for src in selected_lessons.entries:
            text += f"• {src.entry_name}\n"
            kb.append([InlineKeyboardButton(f"❌ {src.entry_name[:50]}", callback_data=f"EL_REMOVE|entry|{src.selected_entry_id}")])

    # sources (subgroup adding)
    if selected_lessons.sources:
        if selected_lessons.sources:
            text += "\n"
        text += "<u>Додані за потоком:</u>\n"
        for entry in selected_lessons.sources:
            text += f"• {entry.name} - потік {entry.subgroup_number}\n"
            kb.append([InlineKeyboardButton(f"❌ {entry.name[:50]}", callback_data=f"EL_REMOVE|source|{entry.selected_source_id}")])

    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_remove(query, context: ContextTypes.DEFAULT_TYPE, remove_type: str, item_id: int) -> None:
    """Remove elective source or entry."""
    tg_id = query.from_user.id

    try:
        if remove_type == "source":
            ok = await delete_user_elective_source(tg_id, item_id)
        else:  # entry
            ok = await delete_user_elective_entry(tg_id, item_id)

        if ok:
            await query.answer("✅ Видалено", show_alert=False)
            # Refresh the list
            await handle_elective_list_view(query, query.from_user.id, context)
        else:
            await query.answer("❌ Не вдалося видалити. Спробуйте пізніше.", show_alert=True)
    except Exception as e:
        await query.answer("❌ Помилка при видаленні.", show_alert=True)
        logger.error(f"Error removing elective: {e}")


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
    result += "Будь ласка, оберіть нову групу командою /change_group."
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку @kaidigital_bot"
    return result


def generate_source_removed_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша вибіркова дисципліна була видалена з розкладу.</b>\n"
    result += f"<b>Предмет:</b> {alert.options['LessonName']}\n"
    result += f"<b>Вид:</b> {alert.options['LessonType']}\n"
    result += f"<b>Потік:</b> {alert.options['SubGroupNumber']}\n"
    result += "Ви можете додати нову вибіркову командою /elective_add."
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку @kaidigital_bot"
    return result


def generate_entry_removed_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша вибіркова дисципліна була видалена з розкладу.</b>\n"
    result += f"<b>Предмет:</b> {alert.options['LessonName']}\n"
    result += f"<b>Вид:</b> {alert.options['LessonType']}\n"
    result += f"<b>Тиждень:</b> {alert.options['LessonWeek']}\n"
    result += f"<b>День:</b> {calendar.day_name[int(alert.options['LessonDay']) % 7].capitalize()}\n"
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