import re
from datetime import datetime
from personalschedulebot.Lesson import Lesson
from typing import List


def generate_telegram_message_from_list(lessons: List[Lesson], date: datetime, week_number: int) -> str:
    result: str = f'<b>Пари на {date.strftime("%d.%m")} '
    result += f'({date.strftime("%A").capitalize()} {str(week_number)}):</b>\n\n'
    if not len(lessons):
        return result + f'Протягом дня пари відсутні! 🥳'
    result += '\n'.join([generate_telegram_message(lesson) for lesson in lessons])
    return result

def generate_telegram_message(lesson: Lesson) -> str:
    result: str = f'*️⃣ | {lesson.begin_time.strftime("%H:%M")} - '
    result += (datetime.combine(datetime.now(), lesson.begin_time) + lesson.duration).strftime("%H:%M")
    result += f' | {lesson.title} | {lesson.lesson_type} | {lesson.teacher} | '
    result += f'<a href="{lesson.location}">Посилання</a>' if re.match(r"^https:\\/\\/.*$", lesson.location) else lesson.location

    return result
