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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import pytz

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PSYCHOLOGIST_ID = int(os.getenv("PSYCHOLOGIST_ID", 0))
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

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

class Dialogue(StatesGroup):
    choosing_gender = State()
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()
    waiting_for_partner_date = State()

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
        if row and row[0]:
            return row[0]
        return "друг"

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

def activate_premium(user_id: int, duration_days: int = 30):
    """Активирует Premium-подписку для пользователя (по московскому времени)"""
    moscow_tz = pytz.timezone('Europe/Moscow')
    until = datetime.now(moscow_tz) + timedelta(days=duration_days)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
                           (until.isoformat(), user_id))
        else:
            cursor.execute("INSERT INTO users (user_id, is_premium, premium_until) VALUES (?, 1, ?)",
                           (user_id, until.isoformat()))
        conn.commit()
        print(f"✅ Premium активирован для {user_id} до {until} (MSK)")

def is_premium(user_id: int) -> bool:
    """Проверяет, активна ли Premium-подписка у пользователя (по московскому времени)"""
    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            if row[1]:
                premium_until = datetime.fromisoformat(row[1])
                if premium_until > now:
                    return True
            else:
                return True
        return False

def get_all_premium_users() -> list:
    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE is_premium = 1 AND premium_until > ?", (now.isoformat(),))
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
            1: "🔴 **1 — Число Лидера**\n\nТы — прирождённый первопроходец! Твоя миссия — начинать новое и вдохновлять других. Ты независим" + ("а" if gender == "female" else "") + ", амбициозен" + ("на" if gender == "female" else "") + " и полон" + ("а" if gender == "female" else "") + " идей. \n\n✨ **Твой путь:** самостоятельность и смелость.\n💫 **Твой талант:** ты видишь то, что другие не замечают.\n🌟 **Совет:** доверяй своей интуиции и не бойся быть первым" + ("ой" if gender == "female" else "") + "!",
            2: "🟠 **2 — Число Миротворца**\n\nТы — душа любой компании, дипломат и миротворец. Твоя суперсила — создавать гармонию там, где хаос. \n\n✨ **Твой путь:** сотрудничество и понимание.\n💫 **Твой талант:** ты чувствуешь эмоции других.\n🌟 **Совет:** не забывай о своих желаниях, заботясь о других!",
            3: "🟡 **3 — Число Творца**\n\nТы — источник радости и вдохновения! Твоя энергия заражает всех вокруг. \n\n✨ **Твой путь:** самовыражение и творчество.\n💫 **Твой талант:** ты легко находишь слова и идеи.\n🌟 **Совет:** не бойся быть в центре внимания — это твоя стихия!",
            4: "🟢 **4 — Число Строителя**\n\nТы — надёжная опора для всех. Твоя сила в дисциплине и упорстве. \n\n✨ **Твой путь:** создание прочных основ.\n💫 **Твой талант:** ты доводишь дела до конца.\n🌟 **Совет:** иногда позволяй себе отдыхать и не будь слишком строг" + ("ой" if gender == "female" else "им") + " к себе!",
            5: "🔵 **5 — Число Свободы**\n\nТы — искатель приключений! Перемены — твой двигатель, рутина — твой враг. \n\n✨ **Твой путь:** свобода и новые впечатления.\n💫 **Твой талант:** ты легко адаптируешься к любому.\n🌟 **Совет:** наслаждайся путешествиями и новыми знакомствами!",
            6: "🔵 **6 — Число Заботы**\n\nТы — сердце семьи и опора для близких. Твоя любовь безусловна. \n\n✨ **Твой путь:** забота и ответственность.\n💫 **Твой талант:** ты создаёшь уют и гармонию.\n🌟 **Совет:** не забывай заботиться и о себе!",
            7: "🟣 **7 — Число Мудрости**\n\nТы — исследователь глубин. Тебе нужно время для размышлений и одиночества. \n\n✨ **Твой путь:** познание и мудрость.\n💫 **Твой талант:** ты видишь то, что скрыто от других.\n🌟 **Совет:** доверяй своей интуиции — она редко ошибается!",
            8: "⚫️ **8 — Число Силы**\n\nТы — рождённ" + ("ая" if gender == "female" else "ый") + " для успеха! Деньги и власть приходят к тебе, когда ты в гармонии с собой. \n\n✨ **Твой путь:** материальная реализация.\n💫 **Твой талант:** ты умеешь зарабатывать и управлять.\n🌟 **Совет:** не забывай о духовном развитии!",
            9: "⚪️ **9 — Число Завершения**\n\nТы — гуманист и учитель. Твоя миссия — помогать другим и завершать старое, открывая путь новому. \n\n✨ **Твой путь:** служение людям.\n💫 **Твой талант:** ты чувствуешь боль других и хочешь помочь.\n🌟 **Совет:** научись прощать и отпускать — это твой ключ к счастью!"
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

        elements = {
            "Овен": "Огонь 🔥", "Лев": "Огонь 🔥", "Стрелец": "Огонь 🔥",
            "Телец": "Земля 🌍", "Дева": "Земля 🌍", "Козерог": "Земля 🌍",
            "Близнецы": "Воздух 💨", "Весы": "Воздух 💨", "Водолей": "Воздух 💨",
            "Рак": "Вода 💧", "Скорпион": "Вода 💧", "Рыбы": "Вода 💧"
        }
        elem1 = elements.get(sign1, "")
        elem2 = elements.get(sign2, "")

        if elem1 == elem2:
            compatibility = random.randint(85, 98)
            base_text = "🌟 *Идеальный союз!* Вы принадлежите к одной стихии. 💕\n\n💪 *Сильные стороны:*\n• Взаимное вдохновение\n• Глубокая связь\n• Общие ценности\n\n⚠️ *Точки роста:*\n• Может быть скучно\n• Нужно разнообразие\n\n💫 *Совет:* Добавьте новизну в отношения."
        elif (elem1 == "Огонь 🔥" and elem2 == "Воздух 💨") or (elem1 == "Воздух 💨" and elem2 == "Огонь 🔥"):
            compatibility = random.randint(75, 90)
            base_text = "💫 *Яркая пара!* Огонь и Воздух создают страсть и свободу. 🚀\n\n💪 *Сильные стороны:*\n• Взаимное вдохновение\n• Лёгкость и драйв\n• Много общих интересов\n\n⚠️ *Точки роста:*\n• Нестабильность\n• Нехватка глубины\n\n💫 *Совет:* Добавьте больше романтики."
        elif (elem1 == "Земля 🌍" and elem2 == "Вода 💧") or (elem1 == "Вода 💧" and elem2 == "Земля 🌍"):
            compatibility = random.randint(80, 95)
            base_text = "🌸 *Гармоничный союз!* Земля и Вода питают друг друга. 💧\n\n💪 *Сильные стороны:*\n• Взаимопонимание\n• Уют и забота\n• Совместное созидание\n\n⚠️ *Точки роста:*\n• Зависимость друг от друга\n• Изоляция\n\n💫 *Совет:* Не забывайте о друзьях и хобби."
        elif elem1 == "Земля 🌍" and elem2 == "Земля 🌍":
            compatibility = random.randint(85, 95)
            base_text = "🏡 *Стабильный союз!* Две Земли создают крепкий фундамент. 🏗️\n\n💪 *Сильные стороны:*\n• Надёжность\n• Общие цели\n• Практичность\n\n⚠️ *Точки роста:*\n• Скука\n• Отсутствие романтики\n\n💫 *Совет:* Устраивайте сюрпризы."
        elif elem1 == "Воздух 💨" and elem2 == "Воздух 💨":
            compatibility = random.randint(70, 85)
            base_text = "💨 *Свободный союз!* Воздух + Воздух — легкость и общение. 🦋\n\n💪 *Сильные стороны:*\n• Интеллектуальная близость\n• Много разговоров\n• Независимость\n\n⚠️ *Точки роста:*\n• Недостаток глубины\n• Необязательность\n\n💫 *Совет:* Углубляйте чувства."
        elif elem1 == "Вода 💧" and elem2 == "Вода 💧":
            compatibility = random.randint(85, 98)
            base_text = "🌊 *Эмоциональный союз!* Две Воды — глубокая связь душ. 💕\n\n💪 *Сильные стороны:*\n• Эмпатия\n• Интуиция\n• Безусловная любовь\n\n⚠️ *Точки роста:*\n• Эмоциональные качели\n• Обидчивость\n\n💫 *Совет:* Работайте над устойчивостью."
        else:
            compatibility = random.randint(50, 70)
            base_text = "🦋 *Интересный союз!* Вы очень разные, но это ваша сила. 🌈\n\n💪 *Сильные стороны:*\n• Взаимное притяжение\n• Учитесь друг у друга\n• Новые горизонты\n\n⚠️ *Точки роста:*\n• Непонимание\n• Конфликты\n\n💫 *Совет:* Цените различия."

        if premium:
            additional = f"\n\n✨ *Кармическая задача пары:* Научиться принимать различия.\n🔮 *Прогноз развития:* Перспективы отличные при взаимной работе.\n💎 *Premium-бонус:* Полный PDF-отчёт доступен в меню!"
        else:
            additional = f"\n\n🔓 *Полный разбор совместимости (12 страниц) доступен по подписке Premium (99 ₽/мес):*\n• Кармическая задача\n• Совместимость в деньгах, сексе, дружбе\n• Прогноз развития на 1, 3, 5 лет\n\n💎 Нажми «⭐ Подписка Premium» чтобы открыть все возможности!"

        return {"percent": compatibility, "text": base_text + additional, "sign1": sign1, "sign2": sign2}

    except Exception as e:
        print(f"Ошибка в get_compatibility: {e}")
        return {"percent": 0, "text": f"❌ Ошибка при расчёте: {e}"}

async def generate_pdf_report(user_id: int, partner_date: str = None) -> io.BytesIO:
    """Генерирует подробный PDF-отчёт (10-15 страниц)"""
    
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    if not name:
        name = "друг"
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        birth_date = "01.01.1990"
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=A4, 
        topMargin=2*cm, 
        bottomMargin=2*cm,
        leftMargin=2*cm,
        rightMargin=2*cm
    )
    story = []
    
    try:
        pdfmetrics.registerFont(TTFont('Roboto', 'Roboto.ttf'))
        pdfmetrics.registerFont(TTFont('Roboto-Bold', 'Roboto.ttf'))
        font_name = 'Roboto'
    except:
        font_name = 'Helvetica'
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'Title', parent=styles['Title'],
        fontName=font_name, fontSize=28, alignment=TA_CENTER, spaceAfter=30,
        textColor='#4A148C'
    )
    heading1_style = ParagraphStyle(
        'Heading1', parent=styles['Heading1'],
        fontName=font_name, fontSize=20, alignment=TA_LEFT, spaceAfter=15,
        textColor='#6A1B9A'
    )
    heading2_style = ParagraphStyle(
        'Heading2', parent=styles['Heading2'],
        fontName=font_name, fontSize=16, alignment=TA_LEFT, spaceAfter=10,
        textColor='#8E24AA'
    )
    heading3_style = ParagraphStyle(
        'Heading3', parent=styles['Heading3'],
        fontName=font_name, fontSize=13, alignment=TA_LEFT, spaceAfter=8,
        textColor='#AD1457'
    )
    normal_style = ParagraphStyle(
        'Normal', parent=styles['Normal'],
        fontName=font_name, fontSize=11, leading=16, spaceAfter=8
    )
    center_style = ParagraphStyle(
        'Center', parent=styles['Normal'],
        fontName=font_name, fontSize=11, alignment=TA_CENTER, spaceAfter=8
    )
    
    # ---- ТИТУЛЬНАЯ СТРАНИЦА ----
    story.append(Paragraph("✨ ПЕРСОНАЛЬНЫЙ НУМЕРОЛОГИЧЕСКИЙ ОТЧЁТ ✨", title_style))
    story.append(Spacer(1, 1*cm))
    safe_name = name if name else "друг"
    story.append(Paragraph(f"<font size=20>для {safe_name.upper()}</font>", title_style))
    story.append(Spacer(1, 2*cm))
    story.append(Paragraph(f"📅 <b>Дата рождения:</b> {birth_date}", normal_style))
    story.append(Paragraph(f"👤 <b>Пол:</b> {'Женский' if gender == 'female' else 'Мужской'}", normal_style))
    story.append(Paragraph(f"📆 <b>Дата составления:</b> {datetime.now().strftime('%d.%m.%Y')}", normal_style))
    story.append(Spacer(1, 2*cm))
    story.append(Paragraph("✨ Открывая себя — открываешь мир ✨", center_style))
    story.append(PageBreak())
    
    # ---- 1. ЧИСЛО СУДЬБЫ ----
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    story.append(Paragraph(f"🔮 1. ЧИСЛО СУДЬБЫ — {fate_number}", heading1_style))
    story.append(Paragraph(fate_desc, normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    fate_details = {
        1: "Вы — прирождённый лидер. Вам суждено вести за собой, начинать новое и вдохновлять.",
        2: "Вы — дипломат и миротворец. Ваша суперсила — находить общий язык с любым человеком.",
        3: "Вы — творец и вдохновитель. Ваша энергия радости заражает всех вокруг.",
        4: "Вы — строитель и опора. Ваша сила в дисциплине и упорстве.",
        5: "Вы — искатель свободы и приключений. Рутина — ваш враг.",
        6: "Вы — сердце семьи и опора для близких. Ваша любовь безусловна.",
        7: "Вы — исследователь глубин. Вам нужно время для размышлений и одиночества.",
        8: "Вы — рождены для успеха. Деньги и власть приходят к вам.",
        9: "Вы — гуманист и учитель. Ваша миссия — помогать другим."
    }
    story.append(Paragraph(f"✨ <b>Глубинная расшифровка:</b> {fate_details.get(fate_number, 'Уникальная личность с особенным путём.')}", normal_style))
    story.append(Spacer(1, 0.8*cm))
    
    # ---- 2. ПО ЦИФРАМ ДНЯ РОЖДЕНИЯ ----
    day, month, year = map(int, birth_date.split('.'))
    story.append(Paragraph("🔢 2. ГЛУБИННАЯ РАСШИФРОВКА ДАТЫ РОЖДЕНИЯ", heading1_style))
    
    day_desc = {
        1: "Лидерство, независимость, новаторство. Вы любите начинать новое.",
        2: "Дипломатичность, сотрудничество, гармония. Вы — командный игрок.",
        3: "Творчество, коммуникация, оптимизм. Ваша стихия — самовыражение.",
        4: "Стабильность, порядок, трудолюбие. Вы — надёжная опора.",
        5: "Свобода, перемены, приключения. Рутина — ваш враг.",
        6: "Забота, ответственность, семья. Дом и близкие — ваше всё.",
        7: "Анализ, мудрость, духовность. Вы — исследователь глубин.",
        8: "Успех, власть, материализация. Вы созданы для больших свершений.",
        9: "Гуманизм, завершение, прощение. Ваша миссия — помогать."
    }
    month_desc = {
        1: "Январь — начало цикла. Вы любите планировать и начинать новое.",
        2: "Февраль — время терпения. Вы умеете ждать и договариваться.",
        3: "Март — энергия роста. Вы активны и любознательны.",
        4: "Апрель — стабильность. Вы цените порядок и структуру.",
        5: "Май — свобода. Вы стремитесь к независимости.",
        6: "Июнь — гармония. Вы создаёте уют вокруг себя.",
        7: "Июль — мудрость. Вы любите анализировать и размышлять.",
        8: "Август — сила. Вы амбициозны и целеустремлённы.",
        9: "Сентябрь — завершение. Вы умеете прощать и отпускать.",
        10: "Октябрь — трансформация. Вы легко адаптируетесь к переменам.",
        11: "Ноябрь — духовность. Вы ищете глубинный смысл.",
        12: "Декабрь — итоги. Вы подводите черту и готовитесь к новому."
    }
    year_total = sum(int(d) for d in str(year))
    while year_total > 9:
        year_total = sum(int(d) for d in str(year_total))
    year_desc = {
        1: "Год начинаний — время ставить цели и действовать.",
        2: "Год сотрудничества — время искать союзников и договариваться.",
        3: "Год творчества — время самовыражения и радости.",
        4: "Год труда — время строить основы и работать над планами.",
        5: "Год перемен — время путешествий и новых впечатлений.",
        6: "Год семьи — время заботы о близких и укрепления связей.",
        7: "Год мудрости — время учиться, анализировать и расти духовно.",
        8: "Год силы — время карьерных достижений и материального роста.",
        9: "Год завершения — время подводить итоги и освобождать место новому."
    }
    
    story.append(Paragraph(f"📌 <b>День рождения ({day}):</b> {day_desc.get(day, 'Уникальная личность.')}", normal_style))
    story.append(Paragraph(f"📌 <b>Месяц рождения ({month}):</b> {month_desc.get(month, 'Ваш месяц несёт особую энергию.')}", normal_style))
    story.append(Paragraph(f"📌 <b>Год рождения ({year} → {year_total}):</b> {year_desc.get(year_total, 'Ваш год рождения определяет жизненный путь.')}", normal_style))
    story.append(Spacer(1, 0.8*cm))
    
    # ---- 3. ПРОГНОЗ НА 2026 ГОД ----
    story.append(Paragraph("⭐ 3. ПРОГНОЗ НА 2026 ГОД", heading1_style))
    year_2026 = 2026
    total_2026 = day + month + year_2026
    while total_2026 > 9:
        total_2026 = sum(int(d) for d in str(total_2026))
    forecasts_2026 = {
        1: "🔥 ГОД ЛИДЕРСТВА — новые начинания и возможности проявить себя.",
        2: "🤝 ГОД СОТРУДНИЧЕСТВА — удачные партнёрства и гармония.",
        3: "🎨 ГОД ТВОРЧЕСТВА — время самовыражения и вдохновения.",
        4: "🏗️ ГОД СТРОИТЕЛЬСТВА — закладывайте фундамент для будущего.",
        5: "✈️ ГОД ПЕРЕМЕН — путешествия и новые впечатления.",
        6: "🏡 ГОД СЕМЬИ — время укрепить связи с близкими.",
        7: "📚 ГОД МУДРОСТИ — обучение, самоанализ и духовный рост.",
        8: "💼 ГОД СИЛЫ — карьерный рост и финансовый успех.",
        9: "🌅 ГОД ЗАВЕРШЕНИЯ — отпустите прошлое, подведите итоги."
    }
    story.append(Paragraph(f"✨ <b>Число 2026 года для вас: {total_2026}</b>", heading2_style))
    story.append(Paragraph(forecasts_2026.get(total_2026, "Год перемен и новых возможностей."), normal_style))
    story.append(Spacer(1, 0.3*cm))
    
    # Прогноз по сферам
    story.append(Paragraph("<b>🔮 Детальный прогноз по сферам жизни на 2026 год:</b>", heading3_style))
    love_text = "💕 <b>Любовь и отношения:</b> " + {
        1: "Год активного поиска. Вас ждут яркие знакомства.",
        2: "Год гармонии. Существующие отношения укрепятся.",
        3: "Год флирта и лёгкости. Не торопитесь с обязательствами.",
        4: "Год стабильности. Отношения требуют работы.",
        5: "Год перемен. Возможны новые романы.",
        6: "Год семьи. Время укреплять связи с близкими.",
        7: "Год одиночества и самоанализа.",
        8: "Год страсти. Яркие романы и сильные чувства.",
        9: "Год завершения. Отпустите старые связи."
    }.get(total_2026, "Год приятных сюрпризов в личной жизни.")
    story.append(Paragraph(love_text, normal_style))
    story.append(Spacer(1, 0.2*cm))
    
    career_text = "💼 <b>Карьера и финансы:</b> " + {
        1: "Год новых проектов и стартов.",
        2: "Год партнёрств. Работа в команде принесёт плоды.",
        3: "Год творчества. Ищите нестандартные решения.",
        4: "Год упорного труда. Закладывайте фундамент.",
        5: "Год перемен. Не бойтесь менять работу.",
        6: "Год стабильности. Укрепляйте позиции.",
        7: "Год обучения. Инвестируйте в знания.",
        8: "Год успеха и признания.",
        9: "Год завершения проектов."
    }.get(total_2026, "Год карьерного роста.")
    story.append(Paragraph(career_text, normal_style))
    story.append(Spacer(1, 0.2*cm))
    
    health_text = "🌸 <b>Здоровье:</b> " + {
        1: "Будьте активны. Спорт и движение — ваше всё.",
        2: "Уделяйте внимание психологическому состоянию.",
        3: "Заботьтесь о нервной системе. Избегайте перегрузок.",
        4: "Обратите внимание на питание и режим.",
        5: "Путешествия пойдут на пользу.",
        6: "Уделяйте время семье и дому.",
        7: "Восстанавливайтесь через знания и духовные практики.",
        8: "Следите за спиной и суставами.",
        9: "Завершите курсы лечения."
    }.get(total_2026, "Год для укрепления здоровья.")
    story.append(Paragraph(health_text, normal_style))
    story.append(Spacer(1, 0.8*cm))
    
    # ---- 4. ПОМЕСЯЧНЫЙ ПРОГНОЗ ----
    story.append(Paragraph("📅 4. ПОМЕСЯЧНЫЙ ПРОГНОЗ НА 2026 ГОД", heading1_style))
    monthly_details = [
        ("Январь", "🌟 Месяц новых начинаний. Ставьте цели и действуйте смело!"),
        ("Февраль", "💕 Месяц любви и гармонии. Укрепляйте отношения с близкими."),
        ("Март", "🎨 Месяц творчества. Займитесь тем, что приносит радость."),
        ("Апрель", "🏗️ Месяц труда. Работайте над долгосрочными проектами."),
        ("Май", "✈️ Месяц перемен. Путешествия и новые впечатления."),
        ("Июнь", "🏡 Месяц семьи. Время заботы о доме и близких."),
        ("Июль", "📚 Месяц мудрости. Учитесь и анализируйте."),
        ("Август", "💼 Месяц силы. Карьерные успехи и финансовая удача."),
        ("Сентябрь", "🌅 Месяц завершения. Подводите итоги."),
        ("Октябрь", "🌟 Новый цикл. Новые возможности."),
        ("Ноябрь", "🤝 Месяц партнёрства. Ищите союзников."),
        ("Декабрь", "🎄 Месяц радости. Наслаждайтесь праздниками и итогами года.")
    ]
    for month_name, month_text in monthly_details:
        story.append(Paragraph(f"<b>{month_name}:</b> {month_text}", normal_style))
        story.append(Spacer(1, 0.2*cm))
    story.append(Spacer(1, 0.5*cm))
    
    # ---- 5. СОВМЕСТИМОСТЬ ----
    if partner_date:
        comp = get_compatibility(birth_date, partner_date, premium=True)
        story.append(Paragraph("💕 5. ДЕТАЛЬНЫЙ АНАЛИЗ СОВМЕСТИМОСТИ", heading1_style))
        story.append(Paragraph(f"📅 <b>Ваша дата:</b> {birth_date} → {comp['sign1']}", normal_style))
        story.append(Paragraph(f"📅 <b>Дата партнёра:</b> {partner_date} → {comp['sign2']}", normal_style))
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph(f"🌟 <b>Совместимость: {comp['percent']}%</b>", heading2_style))
        clean_text = comp['text'].replace("*", "").replace("_", "")
        story.append(Paragraph(clean_text[:500], normal_style))
        story.append(Spacer(1, 0.5*cm))
    else:
        story.append(Paragraph("💕 5. АНАЛИЗ СОВМЕСТИМОСТИ", heading1_style))
        story.append(Paragraph("🔓 Полный анализ совместимости доступен по подписке Premium.", normal_style))
        story.append(Spacer(1, 0.5*cm))
    
    # ---- 6. РАСКЛАД ТАРО ----
    story.append(Paragraph("🎴 6. РАСКЛАД ТАРО «КЕЛЬТСКИЙ КРЕСТ»", heading1_style))
    story.append(Paragraph("Этот древний расклад покажет ваш путь на ближайшее время.", normal_style))
    story.append(Spacer(1, 0.3*cm))
    
    taro_spreads = [
        ("1. Вы сейчас", "Маг 🪄 — «У вас есть все ресурсы для достижения цели. Действуйте!»"),
        ("2. Что вас ждёт", "Колесница ⚡ — «Время двигаться вперёд, преодолевая препятствия.»"),
        ("3. Испытания", "Звезда ⭐ — «Верьте в лучшее. Вселенная готовит вам подарок.»"),
        ("4. Помощь", "Сила 🦁 — «Внутренняя мощь поведёт вас.»"),
        ("5. Любовь", "Влюблённые 💕 — «Судьбоносная встреча или важный выбор.»"),
        ("6. Карьера", "Император 🏛️ — «Укрепление позиций или повышение.»"),
        ("7. Финансы", "Десятка Пентаклей 💰 — «Стабильный доход, возможно наследство.»"),
        ("8. Здоровье", "Умеренность ⚖️ — «Баланс между работой и отдыхом.»"),
        ("9. Духовный рост", "Отшельник 🏮 — «Год глубокого самоанализа.»"),
        ("10. Итог года", "Мир 🌍 — «Завершение цикла, достижение цели.»")
    ]
    for card, meaning in taro_spreads:
        story.append(Paragraph(f"<b>{card}:</b> {meaning}", normal_style))
        story.append(Spacer(1, 0.2*cm))
    story.append(Spacer(1, 0.5*cm))
    
    # ---- 7. АФФИРМАЦИИ ----
    story.append(Paragraph("✨ 7. ЕЖЕДНЕВНЫЕ АФФИРМАЦИИ НА МЕСЯЦ", heading1_style))
    affirmations = [
        "Я открыта новым возможностям. Вселенная заботится обо мне.",
        "Мои таланты признаны и ценны. Я достойна успеха.",
        "Я привлекаю успех и изобилие. Деньги приходят ко мне легко.",
        "Моя интуиция ведёт меня правильным путём. Я доверяю себе.",
        "Я люблю и принимаю себя целиком. Я совершенна.",
        "Каждый день я становлюсь сильнее и мудрее.",
        "Я достойна всего самого лучшего. Я выбираю счастье.",
        "Мои мечты сбываются в нужное время. Я благодарна."
    ]
    for i, aff in enumerate(affirmations, 1):
        story.append(Paragraph(f"<b>{i}:</b> «{aff}»", normal_style))
        story.append(Spacer(1, 0.2*cm))
    story.append(Spacer(1, 0.5*cm))
    
    # ---- 8. ЛУННЫЙ КАЛЕНДАРЬ ----
    story.append(Paragraph("🌙 8. ЛУННЫЙ КАЛЕНДАРЬ И РИТУАЛЫ", heading1_style))
    story.append(Paragraph("🌑 <b>Новолуние</b> — время начинать новое, загадывать желания.", normal_style))
    story.append(Paragraph("🌓 <b>Первая четверть</b> — время действовать, принимать решения.", normal_style))
    story.append(Paragraph("🌕 <b>Полнолуние</b> — время подводить итоги, отпускать лишнее.", normal_style))
    story.append(Paragraph("🌗 <b>Последняя четверть</b> — время завершать дела, избавляться от ненужного.", normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # ---- 9. ЗАКЛЮЧЕНИЕ ----
    story.append(Paragraph("💫 9. ПЕРСОНАЛЬНЫЕ РЕКОМЕНДАЦИИ", heading1_style))
    story.append(Paragraph("✨ <b>Доверяйте своей интуиции</b> — она редко ошибается.", normal_style))
    story.append(Paragraph("✨ <b>Уделяйте время отдыху</b> — ваша энергия главный ресурс.", normal_style))
    story.append(Paragraph("✨ <b>Не бойтесь просить о помощи</b> — вы не одиноки.", normal_style))
    story.append(Paragraph("✨ <b>Благодарите себя и других</b> — благодарность привлекает чудеса.", normal_style))
    story.append(Spacer(1, 0.8*cm))
    
    story.append(Paragraph("🌿 <b>Благодарим за доверие! Берегите себя и будьте счастливы.</b> 💕", center_style))
    story.append(Paragraph("✨ По вопросам консультации: @AskPsyioBot ✨", center_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer

async def generate_daily_forecast(user_id: int) -> str:
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    
    moscow_now = datetime.now(pytz.timezone('Europe/Moscow'))
    day_number = moscow_now.day + moscow_now.month
    while day_number > 9:
        day_number = sum(int(d) for d in str(day_number))
    
    day_descriptions = {
        1: "🔴 *День лидерства!* Бери инициативу в свои руки. 💪",
        2: "🟠 *День сотрудничества.* Работа в команде принесёт успех. 🤝",
        3: "🟡 *День творчества.* Займись чем-то вдохновляющим. 🎨",
        4: "🟢 *День порядка.* Систематизируй дела. 📋",
        5: "🔵 *День свободы.* Позволь себе новое. 🦋",
        6: "🔵 *День заботы.* Удели время близким. 💕",
        7: "🟣 *День мудрости.* Прислушайся к интуиции. 🔮",
        8: "⚫️ *День силы.* Действуй решительно. ⚡",
        9: "⚪️ *День завершения.* Закрой старые долги. 🌅"
    }
    
    if birth_date:
        day, month, _ = map(int, birth_date.split('.'))
        sign = get_zodiac_sign(day, month)
        forecasts = {
            "Овен": "🔥 *Овен* — твоя энергия сегодня зашкаливает!",
            "Телец": "💰 *Телец* — сегодня твоя стихия — финансы.",
            "Близнецы": "💬 *Близнецы* — звёзды советуют больше общаться.",
            "Рак": "🏠 *Рак* — лучший день для семьи и дома.",
            "Лев": "🎭 *Лев* — сегодня ты в центре внимания!",
            "Дева": "📋 *Дева* — день порядка и планирования.",
            "Весы": "⚖️ *Весы* — гармония во всём!",
            "Скорпион": "🦂 *Скорпион* — погрузись в свои глубины.",
            "Стрелец": "✈️ *Стрелец* — звёзды зовут в путешествия!",
            "Козерог": "🏔️ *Козерог* — день карьерных побед.",
            "Водолей": "💡 *Водолей* — идеи витают в воздухе!",
            "Рыбы": "🎨 *Рыбы* — погрузись в творчество или медитацию."
        }
        horoscope = forecasts.get(sign, "🌟 Звёзды шепчут: сегодня отличный день для тебя!")
    else:
        horoscope = "🌟 Звёзды шепчут: сегодня отличный день для тебя!"
        sign = "—"
    
    cards = {
        "Шут": "🎭 *Шут* — Новое начало!",
        "Маг": "🪄 *Маг* — У тебя есть всё необходимое!",
        "Верховная Жрица": "🌙 *Верховная Жрица* — Доверься интуиции.",
        "Императрица": "👑 *Императрица* — Время творить!",
        "Император": "🏛️ *Император* — Укрепляй границы.",
        "Иерофант": "⛪ *Иерофант* — Обратись за советом.",
        "Влюбленные": "💕 *Влюбленные* — Важный выбор на пути.",
        "Колесница": "⚡ *Колесница* — Управляй судьбой!",
        "Сила": "🦁 *Сила* — Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 *Отшельник* — Время тишины.",
        "Колесо Фортуны": "🎡 *Колесо Фортуны* — Перемены к лучшему.",
        "Справедливость": "⚖️ *Справедливость* — Поступи справедливо.",
        "Повешенный": "🪢 *Повешенный* — Новый взгляд.",
        "Смерть": "♻️ *Смерть* — Старое уходит.",
        "Умеренность": "⚖️ *Умеренность* — Найди баланс.",
        "Дьявол": "😈 *Дьявол* — Освободись.",
        "Башня": "🏛️💥 *Башня* — Крах иллюзий.",
        "Звезда": "⭐ *Звезда* — Верь в лучшее!",
        "Луна": "🌕 *Луна* — Доверяй интуиции.",
        "Солнце": "☀️ *Солнце* — Всё будет хорошо!",
        "Суд": "🎺 *Суд* — Время подвести итоги.",
        "Мир": "🌍 *Мир* — Ты достигла цели!"
    }
    card_name = random.choice(list(cards.keys()))
    card = cards[card_name]
    
    affirmations = [
        "✨ Я открыта новым возможностям.",
        "✨ Мои таланты признаны и ценны.",
        "✨ Я привлекаю успех и изобилие.",
        "✨ Моя интуиция ведёт меня.",
        "✨ Я люблю и принимаю себя.",
        "✨ Каждый день я становлюсь сильнее."
    ]
    affirmation = random.choice(affirmations)
    
    text = f"""
🌅 *Доброе утро, {name}!* 🌅

━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 *Число дня: {day_number}*
{day_descriptions.get(day_number, "Хороший день!")}

━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ *Гороскоп для {sign}*
{horoscope}

━━━━━━━━━━━━━━━━━━━━━━━━━━

🎴 *Карта дня: {card_name}*
{card}

━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ *Аффирмация дня*
{affirmation}

━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 *Premium-статус активен!*
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
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
                        print(f"📬 Утренний отчёт отправлен {user_id}")
                        await asyncio.sleep(1)
                    except Exception as e:
                        print(f"❌ Ошибка отправки {user_id}: {e}")
            await asyncio.sleep(60)
        await asyncio.sleep(30)

user_history = {}
user_problems = {}

SYSTEM_PROMPT = f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}.

Твои правила:
1. Внимательно слушай и задавай уточняющие вопросы.
2. Проявляй эмпатию и поддержку. Используй эмодзи.
3. Не ставь диагнозы.
4. При кризисе — дай телефон доверия: 8-800-2000-122.
5. После 4-6 обменов предложи записаться к психологу.
6. В конце сообщения с предложением записи добавь: "ЗАПИСЬ_ГОТОВА"

Отвечай на русском, коротко (2-4 предложения), с душой."""

def get_history(user_id: int):
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return user_history[user_id]

def detect_direction(text: str) -> str:
    text_lower = text.lower()
    keywords = {
        "тревога": ["тревог", "страх", "паник", "боюсь"],
        "отношения": ["отношени", "партнёр", "муж", "жена", "ссор"],
        "выгорание": ["выгоран", "устал", "нет сил", "апати"],
        "самооценка": ["самооценк", "неуверен", "комплекс"],
        "дети": ["ребёнк", "дочь", "сын", "родител"]
    }
    for direction, words in keywords.items():
        for word in words:
            if word in text_lower:
                return direction
    return "общая поддержка"

async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = f"🔔 **НОВЫЙ ЗАПРОС НА КОНСУЛЬТАЦИЮ**\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
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

# ---------------------- ОБРАБОТЧИКИ ----------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    remaining = get_remaining_questions(user_id)
    status = "💎 Premium" if is_premium(user_id) else f"📊 {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов сегодня"
    
    await message.answer(
        f"✨ *Добро пожаловать, {message.from_user.first_name or 'дорогой друг'}!* ✨\n\n"
        f"🌸 Я {PSYCHOLOGIST_NAME}, твой персональный гид.\n\n"
        f"📊 Твой статус: {status}\n\n"
        f"💫 *Что я умею:*\n"
        f"🔮 **Число судьбы**\n"
        f"⭐ **Гороскоп**\n"
        f"♊ **Совместимость**\n"
        f"🎴 **Карта дня Таро**\n"
        f"📞 **Запись к психологу**\n"
        f"📊 **Демо-отчёт**\n"
        f"📄 **PDF-отчёт** (Premium)\n"
        f"⭐ **Подписка Premium**\n\n"
        f"👇 *Напиши мне или используй кнопки меню!*",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
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
    await message.answer("🔄 История очищена. Начинаем заново!", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(Command("activate_premium"))
async def force_activate_premium(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Только администратор может использовать эту команду.")
        await state.set_state(Dialogue.chatting)
        return
    user_id = message.from_user.id
    activate_premium(user_id, 30)
    await message.answer(
        "✅ *Premium активирован вручную!* ✅\n\n"
        "✨ Теперь тебе доступны:\n"
        "✅ Безлимитные вопросы\n"
        "✅ Полный PDF-отчёт\n"
        "✅ Ежедневный прогноз в 8:00\n\n"
        "📄 Нажми «📄 Получить PDF-отчёт» чтобы скачать свой первый отчёт!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message, state: FSMContext):
    remaining = get_remaining_questions(message.from_user.id)
    await message.answer(
        f"📖 *Мои возможности:*\n\n"
        f"📊 Осталось вопросов сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
        f"💬 *Просто напиши* — поддержка\n"
        f"🔮 *Число судьбы* — введи дату\n"
        f"⭐ *Гороскоп* — выбери знак\n"
        f"♊ *Совместимость* — введи две даты\n"
        f"🎴 *Карта дня Таро* — совет\n"
        f"📞 *Запись к психологу* — консультация\n"
        f"📊 *Демо-отчёт* — пример\n"
        f"📄 *PDF-отчёт* — только Premium\n"
        f"⭐ *Подписка Premium* — безлимит\n\n"
        f"🗑 /reset — начать заново\n"
        f"❌ /cancel — отменить",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🧹 История диалога очищена. Начинаем заново!", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer(
        "🔮 *Расчёт числа судьбы*\n\n"
        "Введи дату рождения в формате:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 *Пример:* 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard, parse_mode="Markdown")
        await state.set_state(Dialogue.chatting)
        return
    
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, description = calculate_fate_number(birth_date, gender)
    await message.answer(
        f"🔮 *Твоё число судьбы — {number}* 🔮\n\n{description}",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer(
        "⭐ *Гороскоп на сегодня*\n\n"
        "Введи знак или дату рождения:\n\n"
        "♈ Овен, ♉ Телец, ♊ Близнецы, ♋ Рак, ♌ Лев, ♍ Дева,\n"
        "♎ Весы, ♏ Скорпион, ♐ Стрелец, ♑ Козерог, ♒ Водолей, ♓ Рыбы\n\n"
        "✨ Или отправь дату: `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ *Твой знак: {zodiac_sign}* ♈", parse_mode="Markdown")
    else:
        known = {
            "овен": "Овен", "телец": "Телец", "близнецы": "Близнецы",
            "рак": "Рак", "лев": "Лев", "дева": "Дева",
            "весы": "Весы", "скорпион": "Скорпион", "стрелец": "Стрелец",
            "козерог": "Козерог", "водолей": "Водолей", "рыбы": "Рыбы"
        }
        if text.lower() in known:
            zodiac_sign = known[text.lower()]
        else:
            await message.answer("❌ Неизвестный знак. Попробуй ещё раз.", reply_markup=menu_keyboard)
            await state.set_state(Dialogue.chatting)
            return
    
    detailed_forecasts = {
        "Овен": {
            "general": "🔥 Энергия зашкаливает! Ты — огненный ураган.",
            "love": "💕 В личных отношениях возможна вспышка страсти.",
            "career": "💼 Отличный день для новых проектов.",
            "health": "🏃‍♀️ Будь осторожна с переутомлением.",
            "advice": "🌟 Направь энергию в мирное русло."
        },
        "Телец": {
            "general": "💰 Деньги любят тебя сегодня!",
            "love": "💕 Романтический вечер укрепит отношения.",
            "career": "💼 Не бойся просить повышения.",
            "health": "🥗 Обрати внимание на питание.",
            "advice": "🌟 Позволь себе небольшое удовольствие."
        },
        "Близнецы": {
            "general": "💬 Общение — твой главный козырь.",
            "love": "💕 Флирт и лёгкость привлекут нужного человека.",
            "career": "💼 Переговоры пройдут успешно.",
            "health": "🧘‍♀️ Медитация поможет успокоить ум.",
            "advice": "🌟 Делитесь идеями."
        },
        "Рак": {
            "general": "🏠 Сегодня твоя стихия — дом и семья.",
            "love": "💕 Скажи близким о своей любви.",
            "career": "💼 Работа подождёт.",
            "health": "🛁 Прими расслабляющую ванну.",
            "advice": "🌟 Побалуй себя чем-то вкусным."
        },
        "Лев": {
            "general": "🎭 Сегодня ты звезда!",
            "love": "💕 Романтический сюрприз поднимет настроение.",
            "career": "💼 Тебя заметит начальство.",
            "health": "💃 Танцы принесут радость.",
            "advice": "🌟 Покажи миру, на что ты способна!"
        },
        "Дева": {
            "general": "📋 Порядок во всём — твой девиз.",
            "love": "💕 Не критикуй по пустякам.",
            "career": "💼 Деньги придут через мелкие дела.",
            "health": "🧹 Займись профилактикой.",
            "advice": "🌟 Наведи чистоту."
        },
        "Весы": {
            "general": "⚖️ Гармония — твоё главное оружие.",
            "love": "💕 Романтический ужин укрепит отношения.",
            "career": "💼 Посредничество принесёт уважение.",
            "health": "🎨 Творчество восстановит равновесие.",
            "advice": "🌟 Ищи красоту в мелочах."
        },
        "Скорпион": {
            "general": "🦂 Глубины твоей души особенно активны.",
            "love": "💕 Страсть накаляется.",
            "career": "💼 Интуиция подскажет решение.",
            "health": "🧠 Удели время разгрузке.",
            "advice": "🌟 Твоя сила — в умении видеть суть."
        },
        "Стрелец": {
            "general": "✈️ Тянет в путешествия!",
            "love": "💕 Новые знакомства обещают быть интересными.",
            "career": "💼 Командировка принесёт пользу.",
            "health": "🚶‍♀️ Ходьба восстановит силы.",
            "advice": "🌟 Расширяй горизонты."
        },
        "Козерог": {
            "general": "🏔️ Карьерные высоты манят.",
            "love": "💕 Поговори с партнёром о чувствах.",
            "career": "💼 Твой труд оценят.",
            "health": "💪 Не забывай про активность.",
            "advice": "🌟 Упорство приведёт к цели."
        },
        "Водолей": {
            "general": "💡 Идеи витают в воздухе!",
            "love": "💕 Нестандартный подход принесёт свежесть.",
            "career": "💼 Креатив поможет выделиться.",
            "health": "😴 Удели внимание сну.",
            "advice": "🌟 Не бойся быть странной."
        },
        "Рыбы": {
            "general": "🎨 Творчество и интуиция на высоте.",
            "love": "💕 Мечты о любви могут стать реальностью.",
            "career": "💼 Вдохновение поможет справиться.",
            "health": "🌊 Вода лечит.",
            "advice": "🌟 Доверяй своим чувствам."
        }
    }
    
    f = detailed_forecasts.get(zodiac_sign, detailed_forecasts["Весы"])
    
    await message.answer(
        f"✨ *Гороскоп для {zodiac_sign} на сегодня* ✨\n\n"
        f"🔮 {f['general']}\n\n"
        f"💕 {f['love']}\n\n"
        f"💼 {f['career']}\n\n"
        f"🌸 {f['health']}\n\n"
        f"💫 {f['advice']}\n\n"
        f"🌟 Хорошего дня! ✨",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "💕 *Расчёт совместимости*\n\n"
        "Введи *первую* дату рождения:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 *Пример:* 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard, parse_mode="Markdown")
        await state.set_state(Dialogue.chatting)
        return
    
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer(
        "💕 *Расчёт совместимости*\n\n"
        "Теперь введи *вторую* дату рождения:\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard, parse_mode="Markdown")
        await state.set_state(Dialogue.chatting)
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начни заново.", reply_markup=menu_keyboard)
        await state.set_state(Dialogue.chatting)
        await state.clear()
        return
    
    user_id = message.from_user.id
    premium = is_premium(user_id)
    result = get_compatibility(date1, message.text, premium)
    
    if result['percent'] == 0:
        await message.answer(f"❌ {result['text']}", reply_markup=menu_keyboard)
    else:
        await message.answer(
            f"♊ *Результат совместимости* ♊\n\n"
            f"📅 {date1} → *{result['sign1']}*\n"
            f"📅 {message.text} → *{result['sign2']}*\n\n"
            f"🌟 *Совместимость: {result['percent']}%* 🌟\n\n"
            f"{result['text']}",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
    await state.clear()
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message, state: FSMContext):
    taro_cards = {
        "Шут": {
            "img": "🎭",
            "meaning": "Новое начало, спонтанность, вера в лучшее.",
            "advice": "Пора сделать первый шаг! 🌈",
            "detail": "Ты стоишь на пороге чего-то нового. Доверься потоку жизни!"
        },
        "Маг": {
            "img": "🪄",
            "meaning": "Сила воли, проявление желаний, мастерство.",
            "advice": "У тебя есть всё необходимое! 🌟",
            "detail": "Ты обладаешь уникальными талантами. Действуй!"
        },
        "Верховная Жрица": {
            "img": "🌙",
            "meaning": "Интуиция, тайны, подсознание.",
            "advice": "Прислушайся к своему внутреннему голосу. 🔮",
            "detail": "Твоя интуиция сейчас на пике. Доверяй знакам."
        },
        "Императрица": {
            "img": "👑",
            "meaning": "Творчество, изобилие, забота.",
            "advice": "Время творить и созидать! 🌸",
            "detail": "Вокруг тебя появляются возможности для роста."
        },
        "Император": {
            "img": "🏛️",
            "meaning": "Структура, власть, стабильность.",
            "advice": "Наведи порядок в делах. ⚡",
            "detail": "Пришло время взять ответственность за свою жизнь."
        },
        "Иерофант": {
            "img": "⛪",
            "meaning": "Традиции, обучение, наставничество.",
            "advice": "Обратись за советом к тому, кому доверяешь. 📚",
            "detail": "Тебе может встретиться мудрый человек."
        },
        "Влюбленные": {
            "img": "💕",
            "meaning": "Любовь, выбор, гармония.",
            "advice": "Слушай сердце — оно не обманет. 💗",
            "detail": "Тебя ждёт важный выбор в отношениях."
        },
        "Колесница": {
            "img": "⚡",
            "meaning": "Воля, контроль, победа.",
            "advice": "Преодолей сомнения и двигайся к цели! 🏆",
            "detail": "Ты на правильном пути."
        },
        "Сила": {
            "img": "🦁",
            "meaning": "Мужество, внутренняя сила.",
            "advice": "Ты сильнее, чем кажешься. 💪",
            "detail": "Внутри тебя скрыта огромная мощь."
        },
        "Отшельник": {
            "img": "🏮",
            "meaning": "Самоанализ, мудрость.",
            "advice": "Время побыть наедине с собой. 🕯️",
            "detail": "Ответы придут, когда ты успокоишь ум."
        },
        "Колесо Фортуны": {
            "img": "🎡",
            "meaning": "Перемены, удача.",
            "advice": "Жизнь меняется к лучшему! ✨",
            "detail": "Грядут перемены, и они будут к лучшему."
        },
        "Справедливость": {
            "img": "⚖️",
            "meaning": "Честность, равновесие.",
            "advice": "Будь честна с собой. 🕊️",
            "detail": "Карма сейчас активна как никогда."
        },
        "Повешенный": {
            "img": "🪢",
            "meaning": "Новая перспектива.",
            "advice": "Посмотри на ситуацию иначе. 👁️",
            "detail": "Сделай паузу и переосмысли путь."
        },
        "Смерть": {
            "img": "♻️",
            "meaning": "Трансформация, новое начало.",
            "advice": "Не бойся отпустить прошлое. 🌱",
            "detail": "Что-то в твоей жизни подходит к концу."
        },
        "Умеренность": {
            "img": "⚖️",
            "meaning": "Баланс, терпение.",
            "advice": "Найди золотую середину. 🌊",
            "detail": "Не торопись. Всему своё время."
        },
        "Дьявол": {
            "img": "😈",
            "meaning": "Зависимости, искушение.",
            "advice": "Пора разорвать цепи. 🔗",
            "detail": "Что-то или кто-то держит тебя в плену."
        },
        "Башня": {
            "img": "🏛️💥",
            "meaning": "Внезапные перемены.",
            "advice": "Не сопротивляйся — так нужно. 🌪️",
            "detail": "То, на что ты опиралась, может разрушиться."
        },
        "Звезда": {
            "img": "⭐",
            "meaning": "Надежда, исцеление.",
            "advice": "Загадай желание! 🌟",
            "detail": "После бури всегда выходит солнце."
        },
        "Луна": {
            "img": "🌕",
            "meaning": "Иллюзии, страхи.",
            "advice": "Доверяй интуиции. 🌙",
            "detail": "Твои страхи могут рисовать ложные картины."
        },
        "Солнце": {
            "img": "☀️",
            "meaning": "Радость, успех.",
            "advice": "Твой день сияет! 🌞",
            "detail": "Счастье уже близко."
        },
        "Суд": {
            "img": "🎺",
            "meaning": "Пробуждение, прощение.",
            "advice": "Подведи итоги. 🕊️",
            "detail": "Ты готова к перерождению."
        },
        "Мир": {
            "img": "🌍",
            "meaning": "Завершение, удовлетворение.",
            "advice": "Поздравляю! 🏆",
            "detail": "Ты достигла того, к чему шла."
        }
    }
    
    card_name = random.choice(list(taro_cards.keys()))
    card = taro_cards[card_name]
    
    await message.answer(
        f"🎴 *Твоя карта дня — {card_name}* 🎴\n\n"
        f"{card['img']} **Значение:** {card['meaning']}\n\n"
        f"✨ **Послание:**\n{card['detail']}\n\n"
        f"💫 **Совет:**\n{card['advice']}\n\n"
        f"🌟 Пусть этот день принесёт тебе волшебство! 🌟",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "🌸 *Запись на консультацию* 🌸\n\n"
        "Оставь свой контакт (@username или номер телефона), и психолог Дарья свяжется с тобой.\n\n"
        "✨ Всё конфиденциально.\n\n"
        "Нажми /cancel, чтобы отменить запись.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    if message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("❌ Запись отменена.", reply_markup=menu_keyboard)
        await state.set_state(Dialogue.chatting)
        return

    contact = message.text.strip()
    is_valid = False
    if contact.startswith("@"):
        is_valid = True
    elif re.match(r'^[\+\d][\d\s\-\(\)]{5,20}$', contact):
        is_valid = True
    elif contact.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").isdigit():
        is_valid = True
    
    if not is_valid:
        await message.answer(
            "⚠️ Я не распознал контакт. Пожалуйста, отправь @username или номер телефона.\n\n"
            "Или нажми /cancel.",
            reply_markup=menu_keyboard
        )
        await state.set_state(Dialogue.chatting)
        return

    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    
    save_to_google_sheets(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"✅ *Спасибо, {message.from_user.first_name or 'дорогой друг'}!* ✅\n\n"
        f"Психолог {PSYCHOLOGIST_NAME} свяжется с тобой в ближайшее время.\n\n"
        f"✨ Береги себя, и помни — ты не одна! 💕",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.clear()
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_premium(user_id):
        await message.answer(
            "💎 *Premium уже активна!* 💎\n\n"
            "Спасибо за поддержку проекта! 🙏\n"
            "📬 Каждое утро в 8:00 ты получаешь персональный прогноз!",
            reply_markup=menu_keyboard,
            parse_mode="Markdown"
        )
    else:
        remaining = get_remaining_questions(user_id)
        await message.answer(
            f"⭐ *Premium-подписка 99 Telegram Stars/мес* ⭐\n\n"
            f"📊 *Твой лимит сегодня:* {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
            f"💎 *Что даёт Premium:*\n"
            f"✅ Безлимитные вопросы\n"
            f"✅ Расширенная совместимость\n"
            f"✅ Полный PDF-отчёт\n"
            f"✅ Ежедневный прогноз в 8:00\n\n"
            f"✨ Нажми кнопку ниже, чтобы оформить подписку!",
            reply_markup=premium_keyboard,
            parse_mode="Markdown"
        )
    await state.set_state(Dialogue.chatting)

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🔮 *Что даёт Premium-подписка?* 🔮\n\n"
        "1️⃣ *Безлимитные консультации*\n"
        "2️⃣ *Полный разбор совместимости*\n"
        "3️⃣ *Расширенные прогнозы*\n"
        "4️⃣ *PDF-отчёт* (15+ страниц)\n"
        "5️⃣ *Ежедневный прогноз в 8:00*\n"
        "6️⃣ *Приоритетная поддержка*\n\n"
        "💎 *Стоимость:* 99 Stars/мес\n\n"
        "✨ Нажми «Оформить подписку»!",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    prices = [LabeledPrice(label="Premium-подписка на месяц", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(
        title="Premium-подписка",
        description="Безлимитные консультации + PDF-отчёт + ежедневные прогнозы",
        payload="premium_subscription_30d",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="premium_start"
    )
    await state.set_state(Dialogue.chatting)

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery, state: FSMContext):
    await query.answer(ok=True)
    await state.set_state(Dialogue.chatting)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    activate_premium(user_id, 30)
    await message.answer(
        "💎 *Поздравляем! Premium-подписка активирована!* 💎\n\n"
        "✨ Теперь тебе доступны:\n"
        "✅ Безлимитные вопросы\n"
        "✅ Полный PDF-отчёт\n"
        "✅ Ежедневный прогноз в 8:00\n\n"
        "📄 Нажми «📄 Получить PDF-отчёт» чтобы скачать свой первый отчёт!",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "📊 *Демо-отчёт*\n\n"
            "🔮 Сначала укажи дату рождения через кнопку «🔮 Число судьбы».",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
        await state.set_state(Dialogue.chatting)
        return
    
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    number, desc = calculate_fate_number(birth_date, gender)
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    
    demo_text = f"""
📄 *ПЕРСОНАЛЬНЫЙ ДЕМО-ОТЧЁТ ДЛЯ {name.upper()}* 📄
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 *ЧИСЛО СУДЬБЫ — {number}*

{desc[:200]}...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ *ГОРОСКОП ДЛЯ {sign}*

Звёзды говорят, что сегодня отличный день!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💕 *СОВМЕСТИМОСТЬ (ПРИМЕР)*

Совместимость с Весами: 86%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎴 *РАСКЛАД ТАРО «ПУТЬ ГОДА»*

1️⃣ *Маг* 🪄 — Ресурсы есть
2️⃣ *Колесница* ⚡ — Действуй!
3️⃣ *Звезда* ⭐ — Верь в лучшее
4️⃣ *Солнце* ☀️ — Радость близко
5️⃣ *Мир* 🌍 — Цель достигнута

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ *В ПОЛНОМ ОТЧЁТЕ (PREMIUM):*

📄 15+ страниц с прогнозами
🔮 Разбор 5 сфер жизни
💕 Анализ совместимости
🎴 Расклад Таро (10 карт)
✨ Аффирмации на месяц
🌙 Лунный календарь
📎 PDF-файл для печати

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎 *СТОИМОСТЬ: 99 Stars/мес*

⭐ Нажми «⭐ Подписка Premium»!
"""
    await message.answer(demo_text, parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if not is_premium(user_id):
        await message.answer(
            "💎 *PDF-отчёт доступен только Premium-пользователям!* 💎\n\n"
            "Оформи подписку за 99 Stars/мес.\n\n"
            "👉 Нажми «⭐ Подписка Premium» в меню.",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
        await state.set_state(Dialogue.chatting)
        return
    
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "🔮 *Сначала укажи дату рождения!* 🔮\n\n"
            "Нажми кнопку «🔮 Число судьбы» и введи дату.",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
        await state.set_state(Dialogue.chatting)
        return
    
    await message.answer(
        "💕 *Хочешь добавить анализ совместимости с партнёром?* 💕\n\n"
        "Это сделает отчёт ещё более полным!",
        reply_markup=partner_keyboard
    )
    await state.set_state(Dialogue.waiting_for_partner_date)

@dp.callback_query(lambda c: c.data == "pdf_without_partner")
async def pdf_without_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    waiting_msg = await callback.message.answer("📄 Генерирую отчёт... Подожди немного ✨")
    
    try:
        pdf = await generate_pdf_report(callback.from_user.id, None)
        await waiting_msg.delete()
        await callback.message.answer_document(
            document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"),
            caption="✨ *Твой персональный отчёт готов!* ✨\n\nБлагодарим за доверие! 💕",
            reply_markup=menu_keyboard
        )
    except Exception as e:
        await waiting_msg.edit_text("❌ Ошибка при генерации отчёта. Попробуй ещё раз.")
        print(f"Ошибка PDF: {e}")
    finally:
        await state.set_state(Dialogue.chatting)

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def pdf_with_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💕 *Введи дату рождения партнёра* 💕\n\n"
        "В формате `ДД.ММ.ГГГГ`, например: 15.05.1990",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_partner_date)

@dp.message(StateFilter(Dialogue.waiting_for_partner_date))
async def process_partner_date(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", parse_mode="Markdown", reply_markup=menu_keyboard)
        await state.set_state(Dialogue.chatting)
        return
    
    partner_date = message.text
    user_id = message.from_user.id
    
    waiting_msg = await message.answer("📄 Генерирую отчёт с анализом совместимости... Подожди немного ✨")
    
    try:
        pdf = await generate_pdf_report(user_id, partner_date)
        await waiting_msg.delete()
        await message.answer_document(
            document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{user_id}.pdf"),
            caption=f"✨ *Твой отчёт с анализом совместимости готов!* ✨\n\n📅 Дата партнёра: {partner_date}\n\nБлагодарим за доверие! 💕",
            reply_markup=menu_keyboard
        )
    except Exception as e:
        await waiting_msg.edit_text("❌ Ошибка при генерации отчёта. Попробуй ещё раз.")
        print(f"Ошибка PDF: {e}")
    finally:
        await state.clear()
        await state.set_state(Dialogue.chatting)

# ---------------------- ОСНОВНОЙ ДИАЛОГ ----------------------
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", 
                    "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", 
                    "📞 Запись к психологу", "⭐ Подписка Premium", 
                    "📊 Демо-отчёт", "📄 Получить PDF-отчёт"]
    if user_text in menu_buttons:
        return
    
    print(f"📨 Получено: {user_text}")
    
    crisis = ["суицид", "самоубийств", "не хочу жить", "покончить с собой", "умру"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer(
            "🕊️ *Мне очень жаль, что тебе так тяжело* 🕊️\n\n"
            "📞 *Телефон доверия:* 8-800-2000-122\n"
            "🚑 *МЧС России:* 112\n\n"
            "Ты не один. Пожалуйста, позвони ❤️",
            parse_mode="Markdown"
        )
        await state.set_state(Dialogue.chatting)
        return
    
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(
            f"📊 *Лимит бесплатных вопросов на сегодня исчерпан* ({FREE_QUESTIONS_PER_DAY}).\n\n"
            f"⭐ Оформи Premium-подписку за 99 Stars/мес!\n\n"
            f"👉 Нажми кнопку «⭐ Подписка Premium» в меню.",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
        await state.set_state(Dialogue.chatting)
        return
    
    if user_id not in user_problems:
        user_problems[user_id] = {"problem": user_text, "direction": detect_direction(user_text)}
    
    try:
        history = get_history(user_id)
        history.append({"role": "user", "content": user_text})
        
        if not groq_client:
            answer = "🌙 *ИИ-ассистент временно недоступен.* Используй кнопки меню. ✨"
        else:
            response = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=history,
                max_tokens=350,
                temperature=0.9,
                timeout=30.0
            )
            answer = response.choices[0].message.content
        
        history.append({"role": "assistant", "content": answer})
        
        if len(history) > 15:
            user_history[user_id] = [history[0]] + history[-12:]
        else:
            user_history[user_id] = history
        
        increment_question_count(user_id)
        new_remaining = get_remaining_questions(user_id)
        
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            
            book_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Да, хочу записаться!", callback_data="book")],
                    [InlineKeyboardButton(text="❌ Пока не готова", callback_data="not_ready")]
                ]
            )
            await message.answer(
                f"💕 *{PSYCHOLOGIST_NAME}* может помочь тебе разобраться в этом глубже.\n\n"
                f"Хочешь обсудить это с живым психологом? Это конфиденциально.",
                reply_markup=book_kb,
                parse_mode="Markdown"
            )
        else:
            if not is_premium(user_id):
                answer += f"\n\n📊 Осталось вопросов сегодня: {new_remaining}/{FREE_QUESTIONS_PER_DAY}"
            await message.answer(answer)
        
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer(
            "🌙 *Извини, произошла ошибка.* Попробуй ещё раз или используй кнопки меню.",
            reply_markup=menu_keyboard,
            parse_mode="Markdown"
        )
    finally:
        await state.set_state(Dialogue.chatting)

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🌸 *Оставь свой контакт* 🌸\n\n"
        "Напиши свой Telegram @username или номер телефона.\n"
        f"Психолог {PSYCHOLOGIST_NAME} свяжется с тобой.\n\n"
        "✨ Всё конфиденциально.\n\n"
        "Или нажми /cancel для отмены.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "🌿 *Хорошо, я понимаю.* Если захочешь поговорить — я всегда здесь.\n\n"
        "Напиши /start, когда будешь готова 🌸",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

async def main():
    print("✨ Бот с полным функционалом запущен! ✨")
    asyncio.create_task(send_daily_premium_forecasts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
