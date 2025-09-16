__version__ = "0.8.0"
from Lesson import Lesson
from LessonMessageMapper import generate_telegram_message_from_list, generate_telegram_message
from ScheduleAPI import get_schedule, user_exists, create_user, change_user_group, get_groups, get_faculties
from main import main
