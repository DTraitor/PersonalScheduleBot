# main.py (updated with elective flows)
import calendar
import logging
import os
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
    get_faculties,
    get_groups,
    create_user,
    change_user_group,
    # elective API functions
    get_possible_days,
    get_possible_lessons,
    get_user_elective_lessons,
    create_user_elective_lesson,
    delete_user_elective_lessons, get_user_alerts,
)
from personalschedulebot.ElectiveLesson import ElectiveLesson
from personalschedulebot.ElectiveLessonDay import ElectiveLessonDay
from personalschedulebot.UserAlert import UserAlert, UserAlertType

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

EXPECTING_MANUAL_GROUP = "expecting_manual_group"  # flag in user_data

# Elective flow state keys
EXPECTING_ELECTIVE_NAME = "expecting_elective_name"  # waiting for user to type partial lesson name
TEMP_ELECTIVE_DAY_ID = "temp_elective_day_id"  # selected ElectiveLessonDay.id
TEMP_ELECTIVE_WEEK = "temp_elective_week"
TEMP_ELECTIVE_DAY = "temp_elective_day"
ELECTIVE_PAGE = "elective_page"  # current page in list view

# Constants
ELECTIVE_PAGE_SIZE = 9

def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

def week_parity(reference_year: int, check_date: date = None) -> int:
    if check_date is None:
        check_date = date.today()
    sept1 = date(reference_year, 9, 1)
    if check_date < sept1:
        raise ValueError("check_date must not be before September 1 of the given year")
    weeks = (check_date - sept1).days // 7
    return 1 if weeks % 2 == 0 else 2


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.message.from_user.id
    if user_exists(tg_id):
        await update.message.reply_html(
            "Ваша група вже встановлена. Використайте команду /schedule щоб отримати розклад на сьогодні або /change_group щоб змінити групу."
        )
        return

    await ask_for_group(update, context)


async def change_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    await ask_for_group(update, context)


async def ask_for_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[EXPECTING_MANUAL_GROUP] = True
    text = (
        "Введіть код вашої групи (наприклад: Ба-121-22-4-ПІ).\n\n<i>У разі виникнення проблем, звертайтесь до</i> @kaidigital_bot"
    )
    await update.message.reply_html(text, reply_markup=ForceReply(selective=True))


async def manual_group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    if context.user_data.get(EXPECTING_MANUAL_GROUP):
        group_code = update.message.text.strip()
        tg_id = update.message.from_user.id

        if user_exists(tg_id):
            result = change_user_group(tg_id, group_code)
            if result == 0:
                await update.message.reply_html(f"Група змінена на <b>{group_code}</b>.")
            elif result == 1:
                await update.message.reply_html(f"Не вірна назва групи.")
            else:
                await update.message.reply_text("Не вдалося змінити групу.\nЗверніться у підтримку @kaidigital_bot.")
        else:
            result = create_user(tg_id, group_code)
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

    # fallback: maybe user typing partial elective name
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

    if not user_exists(tg_id):
        if not from_callback:
            await update_or_query.message.reply_html("Ви ще не вибрали групу.")
            await ask_for_group(update_or_query, context)
        return

    # Use current date if not provided
    if target_date is None:
        user_tz = ZoneInfo("Europe/Kyiv")  # Replace with user-specific TZ if available
        target_date = datetime.now(tz=user_tz)

    # Fetch lessons (API expects naive datetime)
    lessons = get_schedule(target_date, tg_id)
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
    if parts[0] == "EL_WEEK":  # parts: EL_WEEK|<weekNumber>
        week_num = int(parts[1])
        await handle_elective_week_selected(query, context, week_num)
        return

    if parts[0] == "EL_DAY":  # parts: EL_DAY|<weekNumber>|<dayOfWeek>
        week_num = int(parts[1])
        day_of_week = int(parts[2])
        await handle_elective_day_selected(query, context, week_num, day_of_week)
        return

    if parts[0] == "EL_TIME":  # parts: EL_TIME|<electiveDayId>
        elective_day_id = int(parts[1])
        await handle_elective_time_selected(query, context, elective_day_id)
        return

    if parts[0] == "EL_CHOICE":  # parts: EL_CHOICE|<lessonId>
        lesson_id = int(parts[1])
        await handle_elective_choice_selected(query, context, lesson_id)
        return

    if parts[0] == "EL_LISTPAGE":  # parts: EL_LISTPAGE|<page>
        page = int(parts[1])
        await handle_elective_list_page(query, context, page)
        return

    if parts[0] == "EL_VIEW":  # parts: EL_VIEW|<lessonId>|<page>
        lesson_id = int(parts[1])
        page = int(parts[2])
        await handle_elective_view(query, context, lesson_id, page)
        return

    if parts[0] == "EL_REMOVE":  # parts: EL_REMOVE|<lessonId>
        lesson_id = int(parts[1])
        await handle_elective_remove(query, context, lesson_id)
        return

    # Unknown callback: ignore
    return

# ---------- Elective flow handlers ----------

async def elective_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start adding elective: present weeks available."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    tg_id = update.message.from_user.id
    if not user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу. Використайте /start щоб встановити групу.")
        return

    possible_days = get_possible_days()  # List[ElectiveLessonDay]
    if not possible_days:
        await update.message.reply_text("Немає доступних дат для вибору вибіркових пар.")
        return

    weeks = sorted({d.week_number for d in possible_days})
    kb = [[InlineKeyboardButton(f"Тиждень {w+1}", callback_data=f"EL_WEEK|{w}") ] for w in weeks]
    await update.message.reply_text("Оберіть номер тижня:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_week_selected(query, context: ContextTypes.DEFAULT_TYPE, week_num: int) -> None:
    """After week selection show days of week available for that week."""
    # get days filtered by week
    days = [d for d in get_possible_days() if d.week_number == week_num]
    if not days:
        await query.edit_message_text("Немає днів для цього тижня.")
        return

    # unique day_of_week values with representative begin_time(s)
    day_numbers = sorted({d.day_of_week for d in days})
    kb = []
    for dn in day_numbers:
        # show day number
        rep = next((d for d in days if d.day_of_week == dn), None)
        label = f"{calendar.day_name[dn-1]}" if rep else str(dn)
        kb.append([InlineKeyboardButton(label, callback_data=f"EL_DAY|{week_num}|{dn}")])
    await query.edit_message_text(f"Тиждень {week_num+1} — оберіть день тижня:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_day_selected(query, context: ContextTypes.DEFAULT_TYPE, week_num: int, day_of_week: int) -> None:
    """After day selection show available times (ElectiveLessonDay.begin_time) for chosen week/day."""
    possible = [d for d in get_possible_days() if d.week_number == week_num and d.day_of_week == day_of_week]
    if not possible:
        await query.edit_message_text("Немає доступних часів для цього дня.")
        return

    kb = []
    for d in sorted(possible, key=lambda x: x.begin_time):
        kb.append([InlineKeyboardButton(d.begin_time.strftime("%H:%M"), callback_data=f"EL_TIME|{d.id}")])

    # keep chosen week/day in user_data for convenience
    context.user_data[TEMP_ELECTIVE_WEEK] = week_num
    context.user_data[TEMP_ELECTIVE_DAY] = day_of_week

    await query.edit_message_text("Оберіть час:", reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_time_selected(query, context: ContextTypes.DEFAULT_TYPE, elective_day_id: int) -> None:
    """After time selected: store elective_day_id and ask user to type partial lesson name."""
    # verify elective_day_id exists
    possible = [d for d in get_possible_days() if d.id == elective_day_id]
    if not possible:
        await query.edit_message_text("Обраний час недоступний.")
        return

    context.user_data[TEMP_ELECTIVE_DAY_ID] = elective_day_id
    context.user_data[EXPECTING_ELECTIVE_NAME] = True

    # ask user to type partial lesson name using ForceReply so they can type it
    await query.edit_message_text("Введіть частину назви предмета (наприклад, 'матем'):", reply_markup=None)


async def handle_elective_partial_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User typed partial lesson name — query API and show matching results (or ask to refine)."""
    if not context.user_data.get(EXPECTING_ELECTIVE_NAME):
        return
    partial = update.message.text.strip()
    elective_day_id = context.user_data.get(TEMP_ELECTIVE_DAY_ID)
    if elective_day_id is None:
        await update.message.reply_text("Не вибрано дату/час. Розпочніть спочатку командою /elective_add.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        return

    results = get_possible_lessons(elective_day_id, partial)  # List[ElectiveLesson]
    if results is None:
        await update.message.reply_text("Помилка при зверненні до API. Спробуйте пізніше.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        return

    if len(results) == 0:
        await update.message.reply_text("За вказаною частиною нічого не знайдено. Спробуйте інший запит.")
        return

    if len(results) > 10:
        await update.message.reply_text("Знайдено більш ніж 10 збігів — введіть більш конкретну частину назви.")
        return

    # show results as column of inline buttons: "{LessonName} | {LessonType}"
    kb = []
    for r in results:
        label_type = r.lesson_type if r.lesson_type else "-"
        label = f"{label_type} | {r.title}"
        kb.append([InlineKeyboardButton(label[:64], callback_data=f"EL_CHOICE|{r.id}")])  # truncate label if too long

    await update.message.reply_text("Оберіть предмет зі списку:", reply_markup=InlineKeyboardMarkup(kb))
    # leave EXPECTING_ELECTIVE_NAME until user chooses (or timeout/other action clears it)


async def handle_elective_choice_selected(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int) -> None:
    """User selected one of the returned elective lessons — create user elective, replace buttons with confirmation."""
    tg_id = query.from_user.id
    # call API to create
    ok = create_user_elective_lesson(tg_id, lesson_id)
    if not ok:
        await query.edit_message_text("Не вдалося додати предмет. Спробуйте пізніше.")
        context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
        context.user_data.pop(TEMP_ELECTIVE_DAY_ID, None)
        return

    # success — show confirmation with a single button to remove it (so user can remove immediately if accidental)
    await query.edit_message_text("✅ Предмет успішно додано до ваших вибіркових пар.", reply_markup=None)
    # cleanup
    context.user_data.pop(EXPECTING_ELECTIVE_NAME, None)
    context.user_data.pop(TEMP_ELECTIVE_DAY_ID, None)
    context.user_data.pop(TEMP_ELECTIVE_WEEK, None)
    context.user_data.pop(TEMP_ELECTIVE_DAY, None)


# ---------- Viewing and removing user's electives ----------

async def elective_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paginated list of user's elective lessons (9 per page)."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    tg_id = update.message.from_user.id
    if not user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу. Використайте /start щоб встановити групу.")
        return

    lessons = get_user_elective_lessons(tg_id)  # List[ElectiveLesson]
    if not lessons:
        await update.message.reply_text("У вас ще немає доданих вибіркових пар.")
        return

    # store lessons in user_data temporarily for paging (small list)
    context.user_data["__elective_cached"] = lessons
    context.user_data[ELECTIVE_PAGE] = 0
    await display_elective_page(update, context, 0)


async def display_elective_page(update_or_query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    lessons: List[ElectiveLesson] = context.user_data.get("__elective_cached", [])
    total = len(lessons)
    pages = (total + ELECTIVE_PAGE_SIZE - 1) // ELECTIVE_PAGE_SIZE
    if page < 0 or page >= pages:
        page = 0

    start = page * ELECTIVE_PAGE_SIZE
    end = min(start + ELECTIVE_PAGE_SIZE, total)
    chunk = lessons[start:end]

    kb = []
    for l in chunk:
        label = f"{l.week_number + 1} | {l.day_of_week + 1} | {l.title} | {l.lesson_type or '-'}"
        kb.append([InlineKeyboardButton(label[:64], callback_data=f"EL_VIEW|{l.id}|{page}")])

    # nav buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"EL_LISTPAGE|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"EL_LISTPAGE|{page}"))
    if page < pages-1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"EL_LISTPAGE|{page+1}"))
    kb.append(nav)

    text_lines = [f"<b>Ваші вибіркові (сторінка {page+1}/{pages}):</b>\n"]
    text = "\n".join(text_lines)

    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        try:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            # fallback: send new message
            await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_list_page(query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    context.user_data[ELECTIVE_PAGE] = page
    await display_elective_page(query, context, page)


async def handle_elective_view(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int, page: int) -> None:
    """Show detailed info about a single elective and provide remove option."""
    lessons: List[ElectiveLesson] = context.user_data.get("__elective_cached", [])
    chosen = None
    # try find in cache; if not found, fetch all again
    for l in lessons:
        if l.id == lesson_id:
            chosen = l
            break
    if not chosen:
        # reload
        tg_id = query.from_user.id
        lessons = get_user_elective_lessons(tg_id)
        context.user_data["__elective_cached"] = lessons
        for l in lessons:
            if l.id == lesson_id:
                chosen = l
                break

    if not chosen:
        await query.edit_message_text("Інформація про предмет не знайдена.")
        return

    # build descriptive text
    teacher = chosen.teacher[0] if len(chosen.teacher) > 0 else "-"
    location = chosen.location or "-"
    length_str = (datetime.combine(datetime.now(), chosen.begin_time) + chosen.duration).strftime("%H:%M")
    text = (
        f"<b>{chosen.title}</b>\n"
        f"Тип: {chosen.lesson_type or '-'}\n"
        f"Тиждень: {chosen.week_number + 1}\n"
        f"День: {chosen.day_of_week + 1}\n"
        f"Початок: {chosen.begin_time.strftime('%H:%M')}\n"
        f"Кінець: {length_str}\n"
        f"Викладач: {teacher}\n"
        f"Місце: {location}\n"
    )

    kb = [
        [InlineKeyboardButton("❌ Видалити", callback_data=f"EL_REMOVE|{chosen.id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"EL_LISTPAGE|{page}")]
    ]

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        # fallback: send message
        await query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def handle_elective_remove(query, context: ContextTypes.DEFAULT_TYPE, lesson_id: int) -> None:
    tg_id = query.from_user.id
    ok = delete_user_elective_lessons(tg_id, lesson_id)
    if not ok:
        await query.answer("Не вдалося видалити. Спробуйте пізніше.", show_alert=True)
        return

    # refresh cache
    lessons = get_user_elective_lessons(tg_id)
    context.user_data["__elective_cached"] = lessons

    await query.edit_message_text("✅ Предмет видалено з ваших вибіркових пар.")
    # Optionally: after deletion offer to show list again
    # await query.message.reply_text("Використайте /elective_list щоб переглянути оновлений список.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "/start - почати\n"
        "/change_group - змінити групу\n"
        "/schedule [<code>DD.MM</code>] - розклад на сьогодні або дату\n"
        "/tomorrow - розклад на завтра\n"
        "/elective_add - додати вибіркову пару\n"
        "/elective_list - показати ваші вибіркові пари\n"
        "/help - список команд\n"
        "\n<i>У разі виникнення проблем, звертайтесь до</i> @kaidigital_bot"
    )


async def alert_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    alert: UserAlert
    for alert in get_user_alerts(100):
        match alert.alert_type:
            case UserAlertType.GROUP_REMOVED:
                msg = generate_group_deleted_message(alert)
                await context.bot.send_message(chat_id=alert.telegram_id, text=msg, parse_mode=ParseMode.HTML)
            case UserAlertType.ELECTIVE_LESSON_REMOVED:
                msg = generate_elective_deleted_message(alert)
                await context.bot.send_message(chat_id=alert.telegram_id, text=msg, parse_mode=ParseMode.HTML)
            case _:
                continue


def generate_group_deleted_message(alert: UserAlert) -> str:
    result = f"⚠️ <b>Ваша група '{alert.options['GroupName']}' була видалена з розкладу.</b>\n"
    result += "Будь ласка, оберіть нову групу командою /change_group."
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку бота."
    return result


def generate_elective_deleted_message(alert: UserAlert) -> str:
    lesson_start_times = {
        "1": "8:00",
        "2": "9:50",
        "3": "11:40",
        "4": "13:30",
        "5": "15:20",
        "6": "17:10",
        "7": "19:00",
    }
    result = f"⚠️ <b>Ваша вибірково пара була видалена з розкладу.</b>\n"
    result += f"<b>Предмет:</b> {alert.options['LessonName']}\n"
    result += f"<b>Вид:</b> {alert.options['LessonType']}\n"
    result += f"<b>Тиждень:</b> {(int(alert.options['LessonDay']) // 7) + 1}\n"
    result += f"<b>День:</b> {calendar.day_name[int(alert.options['LessonDay']) % 7]}\n"
    result += f"<b>Час:</b> {lesson_start_times[alert.options['LessonStartTime']]}\n\n"
    result += "Аби додати іншу вибіркову скористайтесь /elective_add.\n"
    result += "Якщо вважаєте, що сталася помилка - зверніться у підтримку бота."
    return result


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    application = Application.builder().token(token).build()

    # existing handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("change_group", change_group))
    application.add_handler(CommandHandler(["schedule", "te"], schedule_command))
    application.add_handler(CommandHandler(["tomorrow", "te_t"], tomorrow_command))
    application.add_handler(CommandHandler("help", help_command))

    # elective handlers
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
            BotCommand("elective_add", "Додати вибіркову пару"),
            BotCommand("elective_list", "Переглянути ваші вибіркові пари"),
            BotCommand("help", "Список команд"),
        ])

    application.job_queue.run_repeating(alert_users, interval=5, first=5)
    application.post_init = post_init
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
