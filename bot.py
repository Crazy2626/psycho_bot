import asyncio
import logging
import os
import re
import random
import json
import sqlite3
import io
from datetime import datetime, timedelta
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, LabeledPrice, PreCheckoutQuery,
    BufferedInputFile
)
from dotenv import load_dotenv
from openai import AsyncOpenAI
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT
import pytz

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PSYCHOLOGIST_ID = int(os.getenv("PSYCHOLOGIST_ID", 0))
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

# ========== ИНИЦИАЛИЗАЦИЯ GROQ ==========
groq_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
) if GROQ_API_KEY else None

PSYCHOLOGIST_NAME = "Дарья"
SUBSCRIPTION_PRICE = 99
FREE_QUESTIONS_PER_DAY = 7

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "bot_data.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                gender TEXT,
                name TEXT,
                registered_at TIMESTAMP,
                is_premium BOOLEAN DEFAULT 0,
                premium_until TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_counts (
                user_id INTEGER PRIMARY KEY,
                count INTEGER DEFAULT 0,
                last_reset_date TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_birthdates (
                user_id INTEGER PRIMARY KEY,
                birth_date TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_forecast_sent (
                user_id INTEGER PRIMARY KEY,
                last_sent_date TEXT
            )
        ''')

init_db()

# ========== FSM ==========
class Dialogue(StatesGroup):
    choosing_gender = State()
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()
    waiting_for_partner_date = State()

# ========== КЛАВИАТУРЫ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу"), KeyboardButton(text="⭐ Подписка Premium")],
        [KeyboardButton(text="📊 Демо-отчёт"), KeyboardButton(text="📄 Получить PDF-отчёт")]
    ],
    resize_keyboard=True
)

gender_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="👩 Женский"), KeyboardButton(text="👨 Мужской")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

premium_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить подписку 99 Stars/мес", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔍 Что даёт подписка?", callback_data="what_is_premium")]
    ]
)

partner_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="💕 Да, добавить совместимость", callback_data="pdf_with_partner")],
        [InlineKeyboardButton(text="📄 Только мой отчёт", callback_data="pdf_without_partner")]
    ]
)

# ========== ФУНКЦИИ БАЗЫ ДАННЫХ ==========
def get_user_gender(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "female"

def get_user_name(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else "друг"

def get_user_birthdate(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT birth_date FROM user_birthdates WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def save_user_birthdate(user_id: int, birth_date: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_birthdates (user_id, birth_date) VALUES (?, ?)", (user_id, birth_date))

def set_user_gender(user_id: int, username: str, gender: str, name: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, gender, name, registered_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, gender, name, datetime.now()))

def is_premium(user_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0] and row[1]:
            premium_until = datetime.fromisoformat(row[1])
            if premium_until > datetime.now():
                return True
        return False

def get_all_premium_users() -> list:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE is_premium = 1 AND premium_until > ?", (datetime.now().isoformat(),))
        return [row[0] for row in cursor.fetchall()]

def get_remaining_questions(user_id: int) -> int:
    if is_premium(user_id):
        return 999
    today_str = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count, last_reset_date FROM message_counts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            count, last_reset = row
            if last_reset != today_str:
                count = 0
                cursor.execute("UPDATE message_counts SET count = ?, last_reset_date = ? WHERE user_id = ?",
                               (count, today_str, user_id))
        else:
            count = 0
            cursor.execute("INSERT INTO message_counts (user_id, count, last_reset_date) VALUES (?, ?, ?)",
                           (user_id, count, today_str))
    return FREE_QUESTIONS_PER_DAY - count

def increment_question_count(user_id: int) -> int:
    if is_premium(user_id):
        return FREE_QUESTIONS_PER_DAY
    today_str = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count, last_reset_date FROM message_counts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            count, last_reset = row
            if last_reset != today_str:
                count = 0
            count += 1
            cursor.execute("UPDATE message_counts SET count = ?, last_reset_date = ? WHERE user_id = ?",
                           (count, today_str, user_id))
        else:
            count = 1
            cursor.execute("INSERT INTO message_counts (user_id, count, last_reset_date) VALUES (?, ?, ?)",
                           (user_id, count, today_str))
    return FREE_QUESTIONS_PER_DAY - count

def activate_premium(user_id: int, duration_days: int = 30):
    until = datetime.now() + timedelta(days=duration_days)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
                       (until.isoformat(), user_id))

def was_forecast_sent_today(user_id: int) -> bool:
    today_str = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_sent_date FROM daily_forecast_sent WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row and row[0] == today_str

def mark_forecast_sent(user_id: int):
    today_str = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO daily_forecast_sent (user_id, last_sent_date) VALUES (?, ?)",
                       (user_id, today_str))

# ========== ОСНОВНЫЕ ФУНКЦИИ ==========
def get_zodiac_sign(day: int, month: int) -> str:
    if (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "Водолей"
    elif (month == 2 and day >= 19) or (month == 3 and day <= 20):
        return "Рыбы"
    elif (month == 3 and day >= 21) or (month == 4 and day <= 19):
        return "Овен"
    elif (month == 4 and day >= 20) or (month == 5 and day <= 20):
        return "Телец"
    elif (month == 5 and day >= 21) or (month == 6 and day <= 20):
        return "Близнецы"
    elif (month == 6 and day >= 21) or (month == 7 and day <= 22):
        return "Рак"
    elif (month == 7 and day >= 23) or (month == 8 and day <= 22):
        return "Лев"
    elif (month == 8 and day >= 23) or (month == 9 and day <= 22):
        return "Дева"
    elif (month == 9 and day >= 23) or (month == 10 and day <= 22):
        return "Весы"
    elif (month == 10 and day >= 23) or (month == 11 and day <= 21):
        return "Скорпион"
    elif (month == 11 and day >= 22) or (month == 12 and day <= 21):
        return "Стрелец"
    else:
        return "Козерог"

def calculate_fate_number(birth_date: str, gender: str = "female") -> tuple:
    try:
        day, month, year = map(int, birth_date.split('.'))
        total = day + month + year
        while total > 9:
            total = sum(int(d) for d in str(total))
        descriptions = {
            1: "🔴 *Число Лидера*\n\nТы — прирождённый первопроходец! Твоя миссия — начинать новое и вдохновлять других. Ты независим" + ("а" if gender == "female" else "") + ", амбициозен" + ("на" if gender == "female" else "") + " и полон" + ("а" if gender == "female" else "") + " идей.\n\n✨ *Твой путь:* самостоятельность и смелость.\n💫 *Твой талант:* ты видишь то, что другие не замечают.\n🌟 *Совет:* доверяй своей интуиции и не бойся быть первым" + ("ой" if gender == "female" else "") + "!",
            2: "🟠 *Число Миротворца*\n\nТы — душа любой компании, дипломат и миротворец. Твоя суперсила — создавать гармонию там, где хаос.\n\n✨ *Твой путь:* сотрудничество и понимание.\n💫 *Твой талант:* ты чувствуешь эмоции других.\n🌟 *Совет:* не забывай о своих желаниях, заботясь о других!",
            3: "🟡 *Число Творца*\n\nТы — источник радости и вдохновения! Твоя энергия заражает всех вокруг.\n\n✨ *Твой путь:* самовыражение и творчество.\n💫 *Твой талант:* ты легко находишь слова и идеи.\n🌟 *Совет:* не бойся быть в центре внимания — это твоя стихия!",
            4: "🟢 *Число Строителя*\n\nТы — надёжная опора для всех. Твоя сила в дисциплине и упорстве.\n\n✨ *Твой путь:* создание прочных основ.\n💫 *Твой талант:* ты доводишь дела до конца.\n🌟 *Совет:* иногда позволяй себе отдыхать и не будь слишком строг" + ("ой" if gender == "female" else "им") + " к себе!",
            5: "🔵 *Число Свободы*\n\nТы — искатель приключений! Перемены — твой двигатель, рутина — твой враг.\n\n✨ *Твой путь:* свобода и новые впечатления.\n💫 *Твой талант:* ты легко адаптируешься к любому.\n🌟 *Совет:* наслаждайся путешествиями и новыми знакомствами!",
            6: "🔵 *Число Заботы*\n\nТы — сердце семьи и опора для близких. Твоя любовь безусловна.\n\n✨ *Твой путь:* забота и ответственность.\n💫 *Твой талант:* ты создаёшь уют и гармонию.\n🌟 *Совет:* не забывай заботиться и о себе!",
            7: "🟣 *Число Мудрости*\n\nТы — исследователь глубин. Тебе нужно время для размышлений и одиночества.\n\n✨ *Твой путь:* познание и мудрость.\n💫 *Твой талант:* ты видишь то, что скрыто от других.\n🌟 *Совет:* доверяй своей интуиции — она редко ошибается!",
            8: "⚫️ *Число Силы*\n\nТы — рождённ" + ("ая" if gender == "female" else "ый") + " для успеха! Деньги и власть приходят к тебе, когда ты в гармонии с собой.\n\n✨ *Твой путь:* материальная реализация.\n💫 *Твой талант:* ты умеешь зарабатывать и управлять.\n🌟 *Совет:* не забывай о духовном развитии!",
            9: "⚪️ *Число Завершения*\n\nТы — гуманист и учитель. Твоя миссия — помогать другим и завершать старое, открывая путь новому.\n\n✨ *Твой путь:* служение людям.\n💫 *Твой талант:* ты чувствуешь боль других и хочешь помочь.\n🌟 *Совет:* научись прощать и отпускать — это твой ключ к счастью!"
        }
        return (total, descriptions.get(total, "✨ Уникальная личность с особенным путём!"))
    except:
        return (0, "❌ Ошибка формата даты. Пожалуйста, используй ДД.ММ.ГГГГ")

def get_compatibility(date1: str, date2: str, premium: bool = False) -> dict:
    try:
        day1, month1, _ = map(int, date1.split('.'))
        day2, month2, _ = map(int, date2.split('.'))
        
        sign1 = get_zodiac_sign(day1, month1)
        sign2 = get_zodiac_sign(day2, month2)
        
        # ПРАВИЛЬНЫЕ СТИХИИ
        elements = {
            "Овен": "Огонь 🔥", "Лев": "Огонь 🔥", "Стрелец": "Огонь 🔥",
            "Телец": "Земля 🌍", "Дева": "Земля 🌍", "Козерог": "Земля 🌍",
            "Близнецы": "Воздух 💨", "Весы": "Воздух 💨", "Водолей": "Воздух 💨",
            "Рак": "Вода 💧", "Скорпион": "Вода 💧", "Рыбы": "Вода 💧"
        }
        
        elem1 = elements.get(sign1, "Неизвестно")
        elem2 = elements.get(sign2, "Неизвестно")
        
        # Таблица совместимости по стихиям
        compat_table = {
            ("Огонь 🔥", "Огонь 🔥"): (88, 98, "🌟 *Огонь + Огонь* — страсть зашкаливает! 🔥\n\n💪 *Сильные стороны:*\n• Взаимное вдохновение\n• Яркие и страстные отношения\n• Много энергии для совместных целей\n\n⚠️ *Точки роста:*\n• Конкуренция и борьба за лидерство\n• Склонность к конфликтам\n\n💫 *Совет:* Учитесь уступать друг другу."),
            
            ("Огонь 🔥", "Воздух 💨"): (85, 95, "💫 *Огонь + Воздух* — взрывная смесь! 🔥💨\n\n💪 *Сильные стороны:*\n• Искра между вами никогда не гаснет\n• Лёгкость и свобода в отношениях\n• Взаимное вдохновение\n\n⚠️ *Точки роста:*\n• Нестабильность\n• Отсутствие глубины\n\n💫 *Совет:* Добавьте в отношения немного стабильности."),
            ("Воздух 💨", "Огонь 🔥"): (85, 95, "💫 *Воздух + Огонь* — взрывная смесь! 💨🔥\n\n💪 *Сильные стороны:*\n• Искра между вами никогда не гаснет\n• Лёгкость и свобода в отношениях\n• Взаимное вдохновение\n\n⚠️ *Точки роста:*\n• Нестабильность\n• Отсутствие глубины\n\n💫 *Совет:* Добавьте в отношения немного стабильности."),
            
            ("Земля 🌍", "Земля 🌍"): (90, 98, "🌱 *Земля + Земля* — надёжный союз! 🌍\n\n💪 *Сильные стороны:*\n• Надёжность и стабильность\n• Общие ценности и цели\n• Умение строить будущее\n\n⚠️ *Точки роста:*\n• Скука и рутина\n• Недостаток романтики\n\n💫 *Совет:* Добавьте спонтанности и сюрпризов."),
            
            ("Земля 🌍", "Вода 💧"): (90, 97, "🌸 *Земля + Вода* — идеальная гармония! 🌍💧\n\n💪 *Сильные стороны:*\n• Глубокое взаимопонимание\n• Уют, забота и внимание\n• Совместное созидание\n\n⚠️ *Точки роста:*\n• Зависимость друг от друга\n• Замкнутость в паре\n\n💫 *Совет:* Не теряйте связь с внешним миром."),
            ("Вода 💧", "Земля 🌍"): (90, 97, "🌸 *Вода + Земля* — идеальная гармония! 💧🌍\n\n💪 *Сильные стороны:*\n• Глубокое взаимопонимание\n• Уют, забота и внимание\n• Совместное созидание\n\n⚠️ *Точки роста:*\n• Зависимость друг от друга\n• Замкнутость в паре\n\n💫 *Совет:* Не теряйте связь с внешним миром."),
            
            ("Вода 💧", "Вода 💧"): (85, 98, "🌊 *Вода + Вода* — глубинная связь! 💧\n\n💪 *Сильные стороны:*\n• Эмпатия и взаимопонимание\n• Эмоциональная близость\n• Интуитивная связь\n\n⚠️ *Точки роста:*\n• Эмоциональные качели\n• Обидчивость\n\n💫 *Совет:* Работайте над эмоциональной устойчивостью."),
            
            ("Воздух 💨", "Воздух 💨"): (75, 88, "💬 *Воздух + Воздух* — лёгкость и свобода! 💨\n\n💪 *Сильные стороны:*\n• Интеллектуальное взаимопонимание\n• Много общих тем\n• Лёгкость и свобода\n\n⚠️ *Точки роста:*\n• Недостаток глубины чувств\n• Легкомыслие\n\n💫 *Совет:* Не бойтесь глубины и серьёзных разговоров."),
            
            ("Вода 💧", "Воздух 💨"): (60, 75, "🌊 *Вода + Воздух* — чувства и разум. 💧💨\n\n💪 *Сильные стороны:*\n• Баланс эмоций и логики\n• Умение слушать и слышать\n• Интерес к миру друг друга\n\n⚠️ *Точки роста:*\n• Непонимание\n• Разные темпы жизни\n\n💫 *Совет:* Учитесь принимать различия."),
            ("Воздух 💨", "Вода 💧"): (60, 75, "🌊 *Воздух + Вода* — разум и чувства. 💨💧\n\n💪 *Сильные стороны:*\n• Баланс эмоций и логики\n• Умение слушать и слышать\n• Интерес к миру друг друга\n\n⚠️ *Точки роста:*\n• Непонимание\n• Разные темпы жизни\n\n💫 *Совет:* Учитесь принимать различия."),
            
            ("Огонь 🔥", "Земля 🌍"): (50, 70, "🔥 *Огонь + Земля* — страсть и стабильность. 🔥🌍\n\n💪 *Сильные стороны:*\n• Баланс активности и надёжности\n• Могут многому научиться друг у друга\n\n⚠️ *Точки роста:*\n• Разные ритмы жизни\n• Непонимание потребностей\n\n💫 *Совет:* Уважайте различия, ищите компромиссы."),
            ("Земля 🌍", "Огонь 🔥"): (50, 70, "🔥 *Земля + Огонь* — стабильность и страсть. 🌍🔥\n\n💪 *Сильные стороны:*\n• Баланс активности и надёжности\n• Могут многому научиться друг у друга\n\n⚠️ *Точки роста:*\n• Разные ритмы жизни\n• Непонимание потребностей\n\n💫 *Совет:* Уважайте различия, ищите компромиссы.")
        }
        
        key = (elem1, elem2)
        if key not in compat_table:
            key = (elem2, elem1)
        
        data = compat_table.get(key, (50, 70, "🌟 Интересный союз! Работайте над взаимопониманием."))
        percent_range = data[0]
        compatibility = random.randint(percent_range[0], percent_range[1])
        base_text = data[2]
        
        if premium:
            additional = f"\n\n✨ *Кармическая задача:* Научиться принимать различия и превращать их в силу.\n🔮 *Прогноз развития:* У вас есть все шансы построить крепкий союз, если будете работать над собой.\n💎 *Premium-бонус:* Полный PDF-отчёт с детальным разбором доступен в меню!"
        else:
            additional = f"\n\n🔓 *Полный разбор совместимости (12 страниц) доступен по подписке Premium (99 ₽/мес):*\n• Кармическая задача\n• Совместимость в деньгах, сексе, дружбе\n• Прогноз развития на 1, 3, 5 лет\n\n💎 Нажми «⭐ Подписка Premium» чтобы открыть все возможности!"
        
        return {"percent": compatibility, "text": base_text + additional, "sign1": sign1, "sign2": sign2, "elem1": elem1, "elem2": elem2}
    except Exception as e:
        print(f"Ошибка в get_compatibility: {e}")
        return {"percent": 0, "text": "❌ Ошибка формата даты. Проверьте правильность ввода ДД.ММ.ГГГГ", "sign1": "?", "sign2": "?"}

# ========== PDF-ГЕНЕРАЦИЯ ==========
async def generate_pdf_report(user_id: int, partner_date: str = None) -> io.BytesIO:
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        birth_date = "01.01.1990"
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=24, alignment=TA_CENTER, spaceAfter=20)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading1'], fontSize=18, textColor='#4A148C', spaceAfter=12)
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=11, leading=14, spaceAfter=6)
    story.append(Paragraph(f"<b>Персональный отчёт</b>", title_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"<font size=16>для {name}</font>", title_style))
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(f"📅 Дата рождения: {birth_date}", normal_style))
    story.append(Paragraph(f"📆 Дата составления: {datetime.now().strftime('%d.%m.%Y')}", normal_style))
    story.append(PageBreak())
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    story.append(Paragraph(f"🔮 Число судьбы — {fate_number}", heading_style))
    story.append(Paragraph(fate_desc, normal_style))
    story.append(Spacer(1, 0.5*cm))
    if partner_date:
        comp = get_compatibility(birth_date, partner_date, premium=True)
        story.append(Paragraph(f"💕 Совместимость с партнёром", heading_style))
        story.append(Paragraph(f"📅 {birth_date} → {comp['sign1']}", normal_style))
        story.append(Paragraph(f"📅 {partner_date} → {comp['sign2']}", normal_style))
        story.append(Paragraph(f"🌟 Совместимость: {comp['percent']}%", normal_style))
        story.append(Paragraph(comp['text'], normal_style))
    story.append(Paragraph("🌿 Благодарим за доверие! Берегите себя и будьте счастливы 💕", normal_style))
    doc.build(story)
    buffer.seek(0)
    return buffer

# ========== ЕЖЕДНЕВНЫЕ УТРЕННИЕ ОТЧЁТЫ ==========
async def generate_daily_forecast(user_id: int) -> str:
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    moscow_now = datetime.now(pytz.timezone('Europe/Moscow'))
    day_number = moscow_now.day + moscow_now.month
    while day_number > 9:
        day_number = sum(int(d) for d in str(day_number))
    day_descriptions = {
        1: "🔴 День лидерства! Бери инициативу в свои руки.",
        2: "🟠 День сотрудничества. Работа в команде принесёт успех.",
        3: "🟡 День творчества. Займись чем-то вдохновляющим.",
        4: "🟢 День порядка. Систематизируй дела.",
        5: "🔵 День свободы. Позволь себе новое.",
        6: "🔵 День заботы. Удели время близким.",
        7: "🟣 День мудрости. Прислушайся к интуиции.",
        8: "⚫️ День силы. Действуй решительно.",
        9: "⚪️ День завершения. Закрой старые долги."
    }
    if birth_date:
        day, month, _ = map(int, birth_date.split('.'))
        sign = get_zodiac_sign(day, month)
        forecasts = {
            "Овен": "🔥 Твоя энергия сегодня на пике!", "Телец": "💰 Хороший день для финансов.",
            "Близнецы": "💬 День общения.", "Рак": "🏠 День интуиции.", "Лев": "🎭 Творческий день.",
            "Дева": "📋 День порядка.", "Весы": "⚖️ День гармонии.", "Скорпион": "🦂 День трансформации.",
            "Стрелец": "✈️ День приключений.", "Козерог": "🏔️ День достижений.",
            "Водолей": "💡 День идей.", "Рыбы": "🎨 День творчества."
        }
        horoscope = forecasts.get(sign, "🌟 Гармоничный день.")
    else:
        horoscope = "🌟 Гармоничный день."
        sign = "—"
    cards = {
        "Шут": "🎭 *Шут* — Новое начало! Пора сделать первый шаг!",
        "Маг": "🪄 *Маг* — У тебя есть все ресурсы для исполнения желаний!",
        "Верховная Жрица": "🌙 *Верховная Жрица* — Доверься своей интуиции.",
        "Императрица": "👑 *Императрица* — Время творить и заботиться.",
        "Император": "🏛️ *Император* — Укрепляй свои границы.",
        "Влюбленные": "💕 *Влюбленные* — Важный выбор на пути.",
        "Колесница": "⚡ *Колесница* — Управляй своей судьбой!",
        "Сила": "🦁 *Сила* — Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 *Отшельник* — Время побыть наедине.",
        "Колесо Фортуны": "🎡 *Колесо Фортуны* — Жизнь меняется к лучшему.",
        "Справедливость": "⚖️ *Справедливость* — Поступи справедливо.",
        "Повешенный": "🪢 *Повешенный* — Посмотри на ситуацию иначе.",
        "Смерть": "♻️ *Смерть* — Старое уходит, новое приходит.",
        "Умеренность": "⚖️ *Умеренность* — Найди золотую середину.",
        "Дьявол": "😈 *Дьявол* — От чего пора отказаться?",
        "Башня": "🏛️💥 *Башня* — Крах иллюзий, но для нового.",
        "Звезда": "⭐ *Звезда* — Верь в лучшее!",
        "Луна": "🌕 *Луна* — Доверяй интуиции.",
        "Солнце": "☀️ *Солнце* — Всё будет хорошо!",
        "Мир": "🌍 *Мир* — Ты достигла цели!"
    }
    card_name = random.choice(list(cards.keys()))
    affirmations = [
        "✨ Я открыта новым возможностям. Вселенная заботись обо мне.",
        "✨ Мои таланты признаны и ценны.",
        "✨ Я привлекаю успех и изобилие."
    ]
    affirmation = random.choice(affirmations)
    text = (f"🌅 *Доброе утро, {name}!* 🌅\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔮 *Число дня: {day_number}*\n{day_descriptions.get(day_number, 'Хороший день!')}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⭐ *Гороскоп для {sign}*\n{horoscope}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎴 *Карта дня: {card_name}*\n{cards[card_name]}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✨ *Аффирмация дня*\n{affirmation}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💎 *Premium-статус активен!*\n📄 Полный PDF-отчёт доступен в меню\n━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return text

async def send_daily_premium_forecasts():
    moscow_tz = pytz.timezone('Europe/Moscow')
    while True:
        now = datetime.now(moscow_tz)
        if now.hour == 8 and now.minute == 0:
            users = get_all_premium_users()
            for user_id in users:
                if not was_forecast_sent_today(user_id):
                    try:
                        forecast = await generate_daily_forecast(user_id)
                        await bot.send_message(user_id, forecast, parse_mode="Markdown")
                        mark_forecast_sent(user_id)
                        await asyncio.sleep(1)
                    except Exception as e:
                        print(f"❌ Ошибка отправки {user_id}: {e}")
            await asyncio.sleep(60)
        await asyncio.sleep(30)

# ========== ИСТОРИЯ ДИАЛОГОВ ==========
user_history = {}
user_problems = {}
SYSTEM_PROMPT = f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}.

Твои правила:
1. Внимательно слушай и задавай уточняющие вопросы.
2. Проявляй эмпатию и поддержку.
3. Не ставь диагнозы.
4. При признаках кризиса (суицид, самоповреждение) — дай телефон доверия.
5. После 4-6 обменов мягко предложи записаться к живому психологу {PSYCHOLOGIST_NAME}.
6. В конце сообщения с предложением записи добавь фразу: "ЗАПИСЬ_ГОТОВА"

Отвечай на русском языке, коротко (2-4 предложения), используй эмодзи для эмоциональности."""

def get_history(user_id: int):
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return user_history[user_id]

def detect_direction(text: str) -> str:
    text_lower = text.lower()
    keywords = {
        "тревога": ["тревог", "страх", "паник", "боюсь", "волнуюсь"],
        "отношения": ["отношени", "партнёр", "муж", "жена", "ссор"],
        "выгорание": ["выгоран", "устал", "нет сил", "апати"],
        "самооценка": ["самооценк", "неуверен", "комплекс", "стыд"],
        "дети": ["ребёнк", "дочь", "сын", "родител"]
    }
    for direction, words in keywords.items():
        for word in words:
            if word in text_lower:
                return direction
    return "общая поддержка"

# ========== GOOGLE SHEETS И УВЕДОМЛЕНИЯ ==========
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = f"🔔 *НОВЫЙ ЗАПРОС*\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка: {e}")

def save_to_google_sheets(user_id: int, username: str, problem: str, direction: str, contact: str):
    try:
        if not GOOGLE_CREDENTIALS_JSON or not SHEET_ID:
            return False
        moscow_time = datetime.now(pytz.timezone('Europe/Moscow'))
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        row = [moscow_time.strftime("%Y-%m-%d %H:%M:%S"), user_id, username, problem[:200], direction, contact, "new"]
        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    if row and row[0]:
        await state.clear()
        if user_id in user_history:
            del user_history[user_id]
        if user_id in user_problems:
            del user_problems[user_id]
        remaining = get_remaining_questions(user_id)
        status = "💎 Premium" if is_premium(user_id) else f"📊 {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов"
        await message.answer(f"✨ *С возвращением!* ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}.\nСтатус: {status}\n\n👇 Используй кнопки меню!", reply_markup=menu_keyboard, parse_mode="Markdown")
        await state.set_state(Dialogue.chatting)
        return
    await state.set_state(Dialogue.choosing_gender)
    await message.answer(f"✨ *Привет, {message.from_user.first_name or 'друг'}!* ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}.\n\n👇 Выбери свой пол:", reply_markup=gender_keyboard, parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.choosing_gender))
async def process_gender(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.lower()
    if "жен" in text:
        gender = "female"
        greeting = "👩 Рада знакомству, прекрасная дама!"
    elif "муж" in text:
        gender = "male"
        greeting = "👨 Рада знакомству, благородный рыцарь!"
    else:
        await message.answer("Выбери пол 👇", reply_markup=gender_keyboard)
        return
    set_user_gender(user_id, message.from_user.username or "", gender, message.from_user.first_name or "друг")
    await state.clear()
    remaining = get_remaining_questions(user_id)
    await message.answer(f"{greeting}\n\n🌸 Я {PSYCHOLOGIST_NAME}.\n📊 Лимит: {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов сегодня.\n\n👇 Используй кнопки меню!", reply_markup=menu_keyboard, parse_mode="Markdown")
    await state.set_state(Dialogue.chatting)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🔄 История диалога очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    remaining = get_remaining_questions(message.from_user.id)
    await message.answer(f"📖 *Доступные функции:*\n\n📊 Осталось вопросов: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n💬 Просто напиши\n🔮 Число судьбы\n⭐ Гороскоп\n♊ Совместимость\n🎴 Карта дня Таро\n📞 Запись к психологу\n📊 Демо-отчёт\n📄 PDF-отчёт (Premium)\n⭐ Подписка Premium\n\n🗑 /reset\n❌ /cancel", reply_markup=menu_keyboard, parse_mode="Markdown")

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🧹 История и состояния очищены.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer("🔮 *Расчет числа судьбы*\n\nВведи свою дату рождения в формате:\n`ДД.ММ.ГГГГ`\n\n🌙 Например: 15.05.1990", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`, например 15.05.1990", reply_markup=menu_keyboard, parse_mode="Markdown")
        return
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, description = calculate_fate_number(birth_date, gender)
    await message.answer(f"🔮 *Твоё число судьбы — {number}* 🔮\n\n{description}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer("⭐ *Гороскоп на сегодня*\n\nВведи свой знак зодиака или дату рождения:\n\n♈ Овен, ♉ Телец, ♊ Близнецы, ♋ Рак, ♌ Лев, ♍ Дева,\n♎ Весы, ♏ Скорпион, ♐ Стрелец, ♑ Козерог, ♒ Водолей, ♓ Рыбы\n\n✨ Или просто отправь дату: `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ *Твой знак: {zodiac_sign}* ♈", parse_mode="Markdown")
    else:
        known = {"овен":"Овен","телец":"Телец","близнецы":"Близнецы","рак":"Рак","лев":"Лев","дева":"Дева","весы":"Весы","скорпион":"Скорпион","стрелец":"Стрелец","козерог":"Козерог","водолей":"Водолей","рыбы":"Рыбы"}
        if text.lower() in known:
            zodiac_sign = known[text.lower()]
        else:
            await message.answer("❌ Неизвестный знак. Попробуй ещё раз.", reply_markup=menu_keyboard)
            return
    detailed_forecasts = {
        "Овен": ("🔥 Энергия сегодня зашкаливает!", "💕 Страсть накаляется", "💼 Успех в новых проектах", "🏃‍♀️ Не переутомляйся", "🌟 Направь энергию в мирное русло"),
        "Телец": ("💰 Финансовая удача", "💕 Романтический вечер", "💼 Проси повышение", "🥗 Следи за питанием", "🌟 Позволь себе маленькое удовольствие"),
        "Близнецы": ("💬 Общение — твой козырь", "💕 Флирт привлечёт нужного", "💼 Успешные переговоры", "🧘‍♀️ Медитация успокоит ум", "🌟 Делитесь идеями"),
        "Рак": ("🏠 Стихия — дом и семья", "💕 Скажи о любви", "💼 Работа подождёт", "🛁 Расслабляющая ванна", "🌟 Побалуй себя"),
        "Лев": ("🎭 Ты звезда сегодня", "💕 Романтический сюрприз", "💼 Тебя заметит начальство", "💃 Танцы принесут радость", "🌟 Не скромничай"),
        "Дева": ("📋 Порядок во всём", "💕 Не критикуй", "💼 Деньги через мелочи", "🧹 Займись профилактикой", "🌟 Наведи чистоту"),
        "Весы": ("⚖️ Гармония — твоё оружие", "💕 Романтический ужин", "💼 Уважение коллег", "🎨 Творчество восстановит", "🌟 Ищи красоту в мелочах"),
        "Скорпион": ("🦂 Глубины души активны", "💕 Страсть накаляется", "💼 Интуиция подскажет", "🧠 Психологическая разгрузка", "🌟 Видь суть"),
        "Стрелец": ("✈️ Тянет в путешествия", "💕 Интересные знакомства", "💼 Обучение принесёт пользу", "🚶‍♀️ Ходьба на свежем воздухе", "🌟 Расширяй горизонты"),
        "Козерог": ("🏔️ Карьерные высоты", "💕 Поговори с партнёром", "💼 Бонус или повышение", "💪 Не забывай про спорт", "🌟 Упорство ведёт к цели"),
        "Водолей": ("💡 Идеи витают в воздухе", "💕 Нестандартный подход", "💼 Креатив выделит тебя", "😴 Удели внимание сну", "🌟 Не бойся быть странной"),
        "Рыбы": ("🎨 Творчество на высоте", "💕 Мечты станут реальностью", "💼 Вдохновение поможет", "🌊 Вода лечит", "🌟 Доверяй чувствам")
    }
    f = detailed_forecasts.get(zodiac_sign, detailed_forecasts["Весы"])
    await message.answer(f"✨ *Гороскоп для {zodiac_sign} на сегодня* ✨\n\n🔮 *Общее:* {f[0]}\n\n💕 *Любовь:* {f[1]}\n\n💼 *Карьера:* {f[2]}\n\n🌸 *Здоровье:* {f[3]}\n\n💫 *Совет:* {f[4]}\n\n🌟 Хорошего дня, звёздная! ✨", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer("💕 *Расчет совместимости*\n\nВведи *первую* дату рождения:\n`ДД.ММ.ГГГГ`\n\n🌙 Например: 15.05.1990", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard, parse_mode="Markdown")
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("💕 *Расчет совместимости*\n\nВведи *вторую* дату рождения:\n`ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard, parse_mode="Markdown")
        return
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начните заново.", reply_markup=menu_keyboard)
        await state.clear()
        return
    user_id = message.from_user.id
    premium = is_premium(user_id)
    result = get_compatibility(date1, message.text, premium)
    if result['percent'] == 0:
        await message.answer(f"❌ {result['text']}", reply_markup=menu_keyboard)
    else:
        await message.answer(f"♊ *Результат совместимости* ♊\n\n📅 {date1} → *{result['sign1']}*\n📅 {message.text} → *{result['sign2']}*\n\n🌟 *Совместимость: {result['percent']}%* 🌟\n\n{result['text']}\n\n✨ Как тебе результат? Можешь рассказать подробности или спросить совет! ✨", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    taro_cards = {
        "Шут": {"img": "🎭", "meaning": "Новое начало, невинность, спонтанность, вера в лучшее.", "advice": "Пора сделать первый шаг! Не бойся выглядеть глупо — именно так начинаются великие приключения. 🌈", "detail": "Эта карта говорит о том, что ты стоишь на пороге чего-то нового. Возможно, ты сомневаешься, но Вселенная подталкивает тебя вперёд. Доверься потоку жизни!"},
        "Маг": {"img": "🪄", "meaning": "Сила воли, концентрация, проявление желаний, мастерство.", "advice": "У тебя есть всё необходимое для исполнения мечты! Просто поверь в себя и начни действовать. 🌟", "detail": "Ты обладаешь уникальными талантами, о которых даже не догадываешься. Сегодня — идеальный день, чтобы взять жизнь в свои руки!"},
        "Верховная Жрица": {"img": "🌙", "meaning": "Интуиция, тайны, подсознание, внутренняя мудрость.", "advice": "Прислушайся к своему внутреннему голосу. Ответы уже есть внутри тебя. 🔮", "detail": "Твоя интуиция сейчас на пике. То, что кажется загадкой, скоро раскроется. Доверяй знакам и снам."},
        "Императрица": {"img": "👑", "meaning": "Творчество, изобилие, плодородие, материнская забота.", "advice": "Время творить и созидать! Посей семена — и скоро пожнёшь плоды. 🌸", "detail": "Это карта расцвета. Вокруг тебя появляются возможности для роста. Не упусти момент — действуй с любовью и заботой."},
        "Император": {"img": "🏛️", "meaning": "Структура, власть, стабильность, порядок.", "advice": "Наведи порядок в делах. Твоя сила — в дисциплине и твёрдости. ⚡", "detail": "Пришло время взять ответственность за свою жизнь. Построй прочный фундамент для будущего."},
        "Иерофант": {"img": "⛪", "meaning": "Традиции, обучение, духовность, наставничество.", "advice": "Обратись за советом к тому, кому доверяешь. Или стань наставником для кого-то. 📚", "detail": "Тебе может встретиться мудрый человек, который укажет путь. Не бойся просить помощи."},
        "Влюбленные": {"img": "💕", "meaning": "Любовь, выбор, гармония, отношения.", "advice": "Судьба ставит перед важным решением. Слушай сердце — оно не обманет. 💗", "detail": "В ближайшее время тебя ждёт либо новая любовь, либо важный выбор в существующих отношениях. Будь честна с собой."},
        "Колесница": {"img": "⚡", "meaning": "Воля, контроль, победа, движение вперёд.", "advice": "Преодолей сомнения и двигайся к цели! Победа будет за тобой. 🏆", "detail": "Ты на правильном пути. Все препятствия временны — твоя воля и упорство приведут к успеху."},
        "Сила": {"img": "🦁", "meaning": "Мужество, сострадание, внутренняя сила.", "advice": "Ты сильнее, чем кажешься. Доверься себе и своим чувствам. 💪", "detail": "Внутри тебя скрыта огромная мощь. Сегодня ты сможешь справиться с тем, что раньше казалось невозможным."},
        "Отшельник": {"img": "🏮", "meaning": "Самоанализ, мудрость, поиск истины.", "advice": "Время побыть наедине с собой. Ответы придут, когда ты успокоишь ум. 🕯️", "detail": "Возможно, тебе стоит взять паузу и разобраться в себе. Одиночество сейчас — не наказание, а подарок."},
        "Колесо Фортуны": {"img": "🎡", "meaning": "Перемены, судьба, удача, поворотный момент.", "advice": "Жизнь меняется к лучшему! Жди подарков от Вселенной. ✨", "detail": "Грядут перемены, и они будут к лучшему. То, что казалось проблемой, обернётся удачей."},
        "Справедливость": {"img": "⚖️", "meaning": "Честность, равновесие, закон, истина.", "advice": "Будь честна с собой и другими. Правда восторжествует. 🕊️", "detail": "Карма сейчас активна как никогда. Твои добрые дела вернутся сторицей, а несправедливость будет исправлена."},
        "Повешенный": {"img": "🪢", "meaning": "Жертва, новая перспектива, остановка.", "advice": "Посмотри на ситуацию под другим углом. Возможно, ты увидишь выход там, где раньше не замечала. 👁️", "detail": "Иногда, чтобы двигаться дальше, нужно остановиться и переосмыслить путь. Сделай паузу."},
        "Смерть": {"img": "♻️", "meaning": "Трансформация, завершение, новое начало.", "advice": "Не бойся отпустить прошлое. Закрывая одни двери, ты открываешь другие. 🌱", "detail": "Что-то в твоей жизни подходит к концу. Это не страшно — так освобождается место для нового и лучшего."},
        "Умеренность": {"img": "⚖️", "meaning": "Баланс, терпение, гармония, исцеление.", "advice": "Найди золотую середину во всём. Терпение и умеренность приведут к цели. 🌊", "detail": "Не торопись. Всему своё время. Сейчас важно сохранять спокойствие и равновесие."},
        "Дьявол": {"img": "😈", "meaning": "Зависимости, ограничения, искушение.", "advice": "Пора разорвать цепи, которые тебя сковывают. От чего тебе пора отказаться? 🔗", "detail": "Что-то или кто-то держит тебя в плену. Осознай это — и ты станешь свободной."},
        "Башня": {"img": "🏛️💥", "meaning": "Внезапные перемены, крушение иллюзий.", "advice": "Старое рушится, чтобы освободить место для нового. Не сопротивляйся — так нужно. 🌪️", "detail": "То, на что ты опиралась, может разрушиться. Но это не катастрофа, а шанс построить нечто лучшее."},
        "Звезда": {"img": "⭐", "meaning": "Надежда, вдохновение, исцеление, оптимизм.", "advice": "Загадай желание! Вселенная готовит тебе подарок. Верь в лучшее. 🌟", "detail": "После бури всегда выходит солнце. Сейчас начинается светлая полоса. Мечтай смело!"},
        "Луна": {"img": "🌕", "meaning": "Иллюзии, страхи, подсознание, интуиция.", "advice": "На поверхности не всё так, как кажется. Доверяй своему внутреннему голосу. 🌙", "detail": "Твои страхи могут рисовать ложные картины. Загляни вглубь себя — там ты найдёшь правду."},
        "Солнце": {"img": "☀️", "meaning": "Радость, успех, позитив, жизненная сила.", "advice": "Твой день сияет! Наслаждайся моментом, делись теплом с окружающими. 🌞", "detail": "Счастье уже близко. Ожидай хороших новостей, приятных встреч и улыбок."},
        "Суд": {"img": "🎺", "meaning": "Пробуждение, прощение, возрождение, оценка.", "advice": "Настал час подвести итоги и простить себя и других. Новый цикл начинается! 🕊️", "detail": "Ты готова к перерождению. Отпусти обиды — и станет легче дышать."},
        "Мир": {"img": "🌍", "meaning": "Завершение, целостность, удовлетворение, достижение.", "advice": "Поздравляю! Цикл завершён, ты на финише. Отдыхай и наслаждайся результатом. 🏆", "detail": "Ты достигла того, к чему шла. Теперь можно выдохнуть и праздновать победу!"}
    }
    card_name = random.choice(list(taro_cards.keys()))
    card = taro_cards[card_name]
    await message.answer(f"🎴 *Твоя карта дня — {card_name}* 🎴\n\n{card['img']} *Значение:* {card['meaning']}\n\n✨ *Послание карты:*\n{card['detail']}\n\n💫 *Совет на сегодня:*\n{card['advice']}\n\n🌟 Пусть этот день принесёт тебе волшебство и радость! 🌟", parse_mode="Markdown", reply_markup=menu_keyboard)

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer("🌸 *Запись на консультацию* 🌸\n\nОставь свой контакт (@username или номер телефона), и психолог Дарья свяжется с тобой.\n\n✨ Всё конфиденциально, ты в безопасности.\n\nИли нажми /cancel для отмены.", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await state.set_state(Dialogue.waiting_for_contact)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    save_to_google_sheets(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await message.answer(f"✅ *Спасибо!* Психолог {PSYCHOLOGIST_NAME} свяжется с тобой в ближайшее время.\n\nБереги себя, и помни — ты не одна! 💕", reply_markup=menu_keyboard, parse_mode="Markdown")
    await state.clear()
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message):
    user_id = message.from_user.id
    if is_premium(user_id):
        await message.answer("💎 *Premium уже активна!* 💎\n\nСпасибо за поддержку проекта! 🙏\n📬 Каждое утро в 8:00 ты получаешь персональный прогноз!", reply_markup=menu_keyboard, parse_mode="Markdown")
    else:
        remaining = get_remaining_questions(user_id)
        await message.answer(f"⭐ *Premium-подписка 99 Telegram Stars/мес* ⭐\n\n📊 *Твой лимит сегодня:* {remaining}/{FREE_QUESTIONS_PER_DAY} бесплатных вопросов\n\n💎 *Что даёт Premium:*\n✅ Безлимитные вопросы к ИИ-психологу\n✅ Расширенные ответы в разделе «Совместимость»\n✅ Полный PDF-отчёт (15+ страниц)\n✅ Ежедневный персональный прогноз в 8:00\n✅ Приоритетную поддержку\n\n✨ Нажми кнопку ниже, чтобы оформить подписку!", reply_markup=premium_keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("🔮 *Что даёт Premium-подписка?* 🔮\n\n1️⃣ *Безлимитные консультации* — без ограничения в 7 вопросов в день\n\n2️⃣ *Полный разбор совместимости* — детальный анализ пары: сильные стороны, точки роста, кармические задачи\n\n3️⃣ *Расширенные прогнозы* — гороскоп и число дня с подробными рекомендациями\n\n4️⃣ *PDF-отчёт* — персональный документ на 15+ страниц для печати\n\n5️⃣ *Ежедневный прогноз в 8:00* — персональные аффирмации, гороскоп и карта дня\n\n6️⃣ *Приоритетная поддержка* — ваши вопросы обрабатываются в первую очередь\n\n💎 *Стоимость:* всего 99 Stars (~99 ₽) в месяц\n\n✨ Нажми «Оформить подписку» и открой мир магии!", parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="Premium-подписка на месяц", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(title="Premium-подписка", description="Неограниченные консультации с ИИ-психологом + расширенные функции + PDF-отчёт", payload="premium_subscription_30d", provider_token="", currency="XTR", prices=prices, start_parameter="premium_start")

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    activate_premium(user_id, 30)
    await message.answer("💎 *Поздравляем! Premium-подписка активирована!* 💎\n\n✨ Теперь тебе доступны:\n✅ Безлимитные вопросы к ИИ-психологу\n✅ Полный PDF-отчёт (кнопка в меню)\n✅ Ежедневный персональный прогноз в 8:00\n✅ Приоритетная поддержка\n\nСпасибо, что поддерживаешь проект! 🙏\n\n📄 Нажми «📄 Получить PDF-отчёт» чтобы скачать свой первый отчёт!", reply_markup=menu_keyboard, parse_mode="Markdown")

@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message):
    user_id = message.from_user.id
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer("📊 *Демо-отчёт*\n\n🌸 Сначала укажи свою дату рождения через кнопку «🔮 Число судьбы».\n\n✨ После этого я смогу показать тебе персонализированный пример того, что ты получишь с Premium-подпиской!", parse_mode="Markdown", reply_markup=menu_keyboard)
        return
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    number, desc = calculate_fate_number(birth_date, gender)
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    demo_text = (f"📄 *ПЕРСОНАЛЬНЫЙ ДЕМО-ОТЧЁТ ДЛЯ {name.upper()}* 📄\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"🔮 *ЧИСЛО СУДЬБЫ — {number}*\n\n{desc[:300]}...\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"⭐ *ГОРОСКОП ДЛЯ {sign} НА СЕГОДНЯ*\n\nЗвёзды говорят, что сегодня ты можешь свернуть горы! Твоя энергия на подъёме, а удача улыбается тебе. 🌟\n\n💕 В личной жизни жди приятных сюрпризов.\n💼 В работе возможны неожиданные бонусы.\n🌸 Здоровье требует внимания — выспись.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"💕 *СОВМЕСТИМОСТЬ (ПРИМЕР)*\n\nСовместимость с Весами: 86%\n\n🌟 *Сильные стороны:*\n• Взаимное вдохновение и поддержка\n• Страстные и яркие отношения\n• Много энергии для совместных целей\n\n⚠️ *Точки роста:*\n• Конкуренция и борьба за лидерство\n• Склонность к конфликтам\n\n💫 *Совет:* Учитесь уступать друг другу.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"🎴 *РАСКЛАД ТАРО «ПУТЬ ГОДА»*\n\n1️⃣ *Маг* 🪄 — У тебя есть все ресурсы для исполнения желаний.\n2️⃣ *Колесница* ⚡ — Время действовать и побеждать.\n3️⃣ *Звезда* ⭐ — Верь в лучшее — Вселенная готовит подарок.\n4️⃣ *Солнце* ☀️ — Радость и успех уже близко.\n5️⃣ *Мир* 🌍 — Завершение цикла, достижение цели.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"✨ *ЕЖЕДНЕВНЫЕ АФФИРМАЦИИ*\n\n«Я открыта новым возможностям. Вселенная заботится обо мне»\n«Мои таланты признаны и ценны»\n«Я привлекаю успех и изобилие»\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"✨ *В ПОЛНОМ ОТЧЁТЕ (PREMIUM):*\n\n📄 25+ страниц с персональными прогнозами\n🔮 Разбор 5 сфер: любовь 💕, деньги 💰, карьера 💼, здоровье 🌿, саморазвитие 🌱\n💕 Детальный анализ совместимости (12 страниц)\n🎴 Расклад Таро «Кельтский крест» (10 карт)\n✨ Ежедневные аффирмации на месяц\n🌙 Лунный календарь с ритуалами\n📅 Персональные рекомендации на каждый день\n📎 PDF-файл для печати\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 f"💎 *СТОИМОСТЬ: ВСЕГО 99 Stars/мес (~99 ₽)*\n\n⭐ Нажми «⭐ Подписка Premium», чтобы получить полную версию и ежедневные прогнозы в 8:00!\n\n✨ С любовью, твоя Дарья ✨")
    await message.answer(demo_text, parse_mode="Markdown", reply_markup=menu_keyboard)

@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message):
    user_id = message.from_user.id
    if not is_premium(user_id):
        await message.answer("💎 *PDF-отчёт доступен только Premium-пользователям!* 💎\n\nОформи подписку за 99 Stars/мес, чтобы получить:\n✅ Полный PDF-отчёт на 15+ страниц\n✅ Безлимитные консультации\n✅ Расширенную совместимость\n✅ Ежедневный прогноз в 8:00\n\n👉 Нажми «⭐ Подписка Premium» в меню.", parse_mode="Markdown", reply_markup=menu_keyboard)
        return
    if not get_user_birthdate(user_id):
        await message.answer("🔮 *Сначала укажи дату рождения!* 🔮\n\nНажми кнопку «🔮 Число судьбы» и введи дату в формате ДД.ММ.ГГГГ.\n\nПосле этого я смогу сгенерировать твой персональный отчёт.", parse_mode="Markdown", reply_markup=menu_keyboard)
        return
    await message.answer("💕 *Хочешь добавить анализ совместимости с партнёром?* 💕\n\nЭто сделает отчёт ещё более полным и персонализированным!", reply_markup=partner_keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == "pdf_without_partner")
async def pdf_without_partner(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("📄 Генерирую твой персональный отчёт... Подожди немного ✨")
    pdf = await generate_pdf_report(callback.from_user.id, None)
    await callback.message.answer_document(document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"), caption="✨ *Твой персональный отчёт готов!* ✨\n\nБлагодарим за доверие и поддержку проекта 💕", reply_markup=menu_keyboard, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def pdf_with_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("💕 *Введи дату рождения партнёра* 💕\n\nВ формате `ДД.ММ.ГГГГ`, например: 15.05.1990", parse_mode="Markdown")
    await state.set_state(Dialogue.waiting_for_partner_date)

@dp.message(StateFilter(Dialogue.waiting_for_partner_date))
async def process_partner_date(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", parse_mode="Markdown", reply_markup=menu_keyboard)
        return
    partner_date = message.text
    user_id = message.from_user.id
    await state.clear()
    await message.answer("📄 Генерирую отчёт с анализом совместимости... Подожди немного ✨")
    pdf = await generate_pdf_report(user_id, partner_date)
    await message.answer_document(document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{user_id}.pdf"), caption=f"✨ *Твой отчёт с анализом совместимости готов!* ✨\n\n📅 Дата партнёра: {partner_date}\n\nБлагодарим за доверие! 💕", reply_markup=menu_keyboard, parse_mode="Markdown")

# ========== ОСНОВНОЙ ДИАЛОГ С ИИ ==========
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", "📞 Запись к психологу", "⭐ Подписка Premium", "📊 Демо-отчёт", "📄 Получить PDF-отчёт"]
    if user_text in menu_buttons:
        return
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(f"📊 *Лимит бесплатных вопросов на сегодня исчерпан* ({FREE_QUESTIONS_PER_DAY}).\n\n⭐ Оформи Premium-подписку за 99 Stars/мес, чтобы снять ограничения!\n\n👉 Нажми кнопку «⭐ Подписка Premium» в меню.", reply_markup=menu_keyboard, parse_mode="Markdown")
        return
    if user_id not in user_problems:
        user_problems[user_id] = {"problem": user_text, "direction": detect_direction(user_text)}
    try:
        history = get_history(user_id)
        history.append({"role": "user", "content": user_text})
        response = await groq_client.chat.completions.create(model="llama-3.3-70b-versatile", messages=history, max_tokens=350, temperature=0.9)
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        if len(history) > 15:
            user_history[user_id] = [history[0]] + history[-12:]
        else:
            user_history[user_id] = history
        increment_question_count(user_id)
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            book_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Да, хочу записаться!", callback_data="book")], [InlineKeyboardButton(text="❌ Пока не готов(а)", callback_data="not_ready")]])
            await message.answer(f"💕 *{PSYCHOLOGIST_NAME}* может помочь тебе разобраться в этом глубже.\n\nХочешь обсудить это с живым психологом? Это конфиденциально и не обязывает ни к чему.", reply_markup=book_kb, parse_mode="Markdown")
        else:
            if not is_premium(user_id):
                new_remaining = get_remaining_questions(user_id)
                answer += f"\n\n📊 Осталось вопросов сегодня: {new_remaining}/{FREE_QUESTIONS_PER_DAY}. ⭐ Подписка Premium снимает лимиты!"
            await message.answer(answer)
    except Exception as e:
        await message.answer("🌙 Извини, произошла небольшая ошибка. Попробуй ещё раз или воспользуйся кнопками меню.", reply_markup=menu_keyboard)

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🌸 *Оставь свой контакт* 🌸\n\nНапиши свой Telegram @username или номер телефона.\nПсихолог Дарья свяжется с тобой в ближайшее время.\n\nИли нажми /cancel для отмены.", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("🌿 Хорошо, я понимаю. Если захочешь поговорить — я всегда здесь.\n\nНапиши /start, когда будешь готова 🌸", reply_markup=menu_keyboard, parse_mode="Markdown")
    await state.set_state(Dialogue.chatting)

# ========== ЗАПУСК ==========
async def main():
    print("✨ Бот с полным функционалом и ежедневными уведомлениями в 8:00 по Москве запущен! ✨")
    asyncio.create_task(send_daily_premium_forecasts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
