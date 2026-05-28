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

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PSYCHOLOGIST_ID = int(os.getenv("PSYCHOLOGIST_ID", 0))
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

# ========== ОСНОВНЫЕ ФУНКЦИИ (КРАСИВЫЕ И ПОДРОБНЫЕ) ==========
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
        return (0, "❌ Ошибка формата даты")

def get_compatibility(date1: str, date2: str, premium: bool = False) -> dict:
    try:
        day1, month1, _ = map(int, date1.split('.'))
        day2, month2, _ = map(int, date2.split('.'))
        sign1 = get_zodiac_sign(day1, month1)
        sign2 = get_zodiac_sign(day2, month2)
        elements = {"Овен": "Огонь 🔥", "Лев": "Огонь 🔥", "Стрелец": "Огонь 🔥",
                    "Телец": "Земля 🌍", "Дева": "Земля 🌍", "Козерог": "Земля 🌍",
                    "Близнецы": "Воздух 💨", "Весы": "Воздух 💨", "Водолей": "Воздух 💨",
                    "Рак": "Вода 💧", "Скорпион": "Вода 💧", "Рыбы": "Вода 💧"}
        elem1 = elements.get(sign1, "")
        elem2 = elements.get(sign2, "")
        if elem1 == elem2:
            compatibility = random.randint(85, 98)
            base_text = f"🌟 **Идеальный союз!** Вы принадлежите к одной стихии {elem1}, поэтому понимаете друг друга с полуслова. Вас ждёт глубокая эмоциональная связь и долгие счастливые отношения 💕"
        elif (elem1 in ["Огонь 🔥", "Воздух 💨"]) and (elem2 in ["Огонь 🔥", "Воздух 💨"]):
            compatibility = random.randint(75, 90)
            base_text = f"💫 **Яркая пара!** {elem1} + {elem2} = взрывная смесь страсти и свободы. Вы будете вдохновлять друг друга на великие дела! 🚀"
        elif (elem1 in ["Земля 🌍", "Вода 💧"]) and (elem2 in ["Земля 🌍", "Вода 💧"]):
            compatibility = random.randint(80, 95)
            base_text = f"🌱 **Гармоничный союз!** {elem1} и {elem2} создают плодородную почву для любви и заботы. Это отношения, в которых хочется строить дом и растить детей 🏡"
        else:
            compatibility = random.randint(50, 70)
            base_text = f"🦋 **Загадочный союз.** Вы очень разные, но именно это делает вашу пару уникальной. Если научитесь ценить различия — откроете новый мир!"
        if premium:
            additional = f"\n\n✨ **Развёрнутый анализ Premium:**\n• Сильные стороны: взаимное вдохновение, страсть, интерес\n• Точки роста: учитесь терпению и принятию различий\n• Кармическая задача: построить крепкий союз на основе взаимного уважения"
        else:
            additional = f"\n\n🔓 **Полный разбор совместимости доступен по подписке Premium** (99 ₽/мес):\n• Сильные и слабые стороны пары\n• Кармическая задача\n• Прогноз развития отношений"
        return {"percent": compatibility, "text": base_text + additional, "sign1": sign1, "sign2": sign2}
    except:
        return {"percent": 0, "text": "❌ Ошибка формата даты"}

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
            "Овен": "🔥 Энергия бьёт ключом! Начни новые дела!",
            "Телец": "💰 Хороший день для финансовых решений.",
            "Близнецы": "💬 День общения и новых знакомств.",
            "Рак": "🏠 День интуиции и семьи.",
            "Лев": "🎭 Творческий день. Покажи себя!",
            "Дева": "📋 День порядка и планирования.",
            "Весы": "⚖️ День гармонии. Избегай конфликтов.",
            "Скорпион": "🦂 День трансформации и глубоких мыслей.",
            "Стрелец": "✈️ День приключений и оптимизма.",
            "Козерог": "🏔️ День достижений. Будь упорна!",
            "Водолей": "💡 День идей и нестандартных решений.",
            "Рыбы": "🎨 День творчества и интуиции."
        }
        horoscope = forecasts.get(sign, "🌟 Гармоничный день. Доверься своей интуиции.")
    else:
        horoscope = "🌟 Гармоничный день. Доверься своей интуиции."
        sign = "—"
    
    cards = {
        "Шут": "🎭 **Шут** — Новое начало! Пора сделать первый шаг!",
        "Маг": "🪄 **Маг** — У тебя есть все ресурсы для исполнения желаний!",
        "Верховная Жрица": "🌙 **Верховная Жрица** — Доверься своей интуиции.",
        "Императрица": "👑 **Императрица** — Время творить и заботиться.",
        "Император": "🏛️ **Император** — Укрепляй свои границы.",
        "Иерофант": "⛪ **Иерофант** — Обратись за советом к старшим.",
        "Влюбленные": "💕 **Влюбленные** — Важный выбор на пути.",
        "Колесница": "⚡ **Колесница** — Управляй своей судьбой!",
        "Сила": "🦁 **Сила** — Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 **Отшельник** — Время побыть наедине.",
        "Колесо Фортуны": "🎡 **Колесо Фортуны** — Жизнь меняется к лучшему.",
        "Справедливость": "⚖️ **Справедливость** — Поступи справедливо.",
        "Повешенный": "🪢 **Повешенный** — Посмотри на ситуацию иначе.",
        "Смерть": "♻️ **Смерть** — Старое уходит, новое приходит.",
        "Умеренность": "⚖️ **Умеренность** — Найди золотую середину.",
        "Дьявол": "😈 **Дьявол** — От чего пора отказаться?",
        "Башня": "🏛️💥 **Башня** — Крах иллюзий, но для нового.",
        "Звезда": "⭐ **Звезда** — Верь в лучшее!",
        "Луна": "🌕 **Луна** — Доверяй интуиции.",
        "Солнце": "☀️ **Солнце** — Всё будет хорошо!",
        "Суд": "🎺 **Суд** — Время подвести итоги.",
        "Мир": "🌍 **Мир** — Ты достигла цели!"
    }
    card_name = random.choice(list(cards.keys()))
    card = cards[card_name]
    
    affirmations = [
        "✨ Я открыта новым возможностям. Вселенная заботится обо мне.",
        "✨ Мои таланты признаны и ценны.",
        "✨ Я привлекаю успех и изобилие.",
        "✨ Моя интуиция ведёт меня правильным путём.",
        "✨ Я люблю и принимаю себя целиком.",
        "✨ Каждый день я становлюсь сильнее.",
        "✨ Я достойна всего самого лучшего.",
        "✨ Мои мечты сбываются в нужное время."
    ]
    affirmation = random.choice(affirmations)
    
    text = f"""
🌅 **Доброе утро, {name}!** 🌅

━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 **Число дня: {day_number}**
{day_descriptions.get(day_number, "Хороший день для новых начинаний!")}

━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ **Гороскоп для {sign}**
{horoscope}

━━━━━━━━━━━━━━━━━━━━━━━━━━

🎴 **Карта дня: {card_name}**
{card}

━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ **Аффирмация дня**
{affirmation}

━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 **Premium-статус активен!**
📄 Полный PDF-отчёт доступен в меню
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
    message = f"🔔 **НОВЫЙ ЗAPOC**\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
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
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    remaining = get_remaining_questions(user_id)
    status = "💎 Premium" if is_premium(user_id) else f"📊 {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов сегодня"
    
    await message.answer(
        f"✨ **Привет, {message.from_user.first_name or 'дорогой друг'}!** ✨\n\n"
        f"🌸 Я {PSYCHOLOGIST_NAME}, твой персональный гид в мире самопознания и магии чисел.\n\n"
        f"📊 Твой статус: {status}\n\n"
        f"💫 Что хочешь узнать?\n"
        f"🔮 **Число судьбы** — по дате рождения\n"
        f"⭐ **Гороскоп** — прогноз на сегодня\n"
        f"♊ **Совместимость** — анализ пары\n"
        f"🎴 **Карта дня Таро** — мудрость древних\n\n"
        f"👇 **Используй кнопки меню или просто напиши мне!**",
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
    await message.answer("🔄 История диалога очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    remaining = get_remaining_questions(message.from_user.id)
    await message.answer(
        f"📖 **Доступные функции:**\n\n"
        f"📊 Осталось вопросов сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
        f"💬 **Просто напиши** — я выслушаю и поддержу\n"
        f"🔮 **Число судьбы** — расчет по дате рождения\n"
        f"⭐ **Гороскоп** — прогноз на сегодня\n"
        f"♊ **Совместимость** — анализ пары\n"
        f"🎴 **Карта дня Таро** — предсказание\n"
        f"📞 **Запись к психологу** — живая консультация\n"
        f"📊 **Демо-отчёт** — пример полного отчёта\n"
        f"📄 **Получить PDF-отчёт** (Premium)\n"
        f"⭐ **Подписка Premium** — безлимитные вопросы + расширенные функции\n\n"
        f"🗑 /reset — очистить диалог\n"
        f"❌ /cancel — отмена",
        reply_markup=menu_keyboard
    )

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
    await message.answer(
        "🔮 **Расчет числа судьбы**\n\n"
        "Введи свою дату рождения в формате:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`, например 15.05.1990", reply_markup=menu_keyboard)
        return
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, description = calculate_fate_number(birth_date, gender)
    await message.answer(
        f"🔮 **Твоё число судьбы — {number}** 🔮\n\n{description}",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer(
        "⭐ **Гороскоп на сегодня**\n\n"
        "Введи свой знак зодиака или дату рождения:\n\n"
        "♈ Овен, ♉ Телец, ♊ Близнецы, ♋ Рак, ♌ Лев, ♍ Дева,\n"
        "♎ Весы, ♏ Скорпион, ♐ Стрелец, ♑ Козерог, ♒ Водолей, ♓ Рыбы\n\n"
        "✨ Или просто отправь дату: `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ **Твой знак: {zodiac_sign}** ♈")
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
            await message.answer("❌ Неизвестный знак. Попробуй еще раз.", reply_markup=menu_keyboard)
            return
    
    forecasts = {
        "Овен": "🔥 Энергия бьет ключом! Начните новые дела, ваша инициатива принесет плоды.",
        "Телец": "💰 Хороший день для финансовых решений. Не торопитесь с тратами.",
        "Близнецы": "💬 День общения и новых знакомств. Полезная информация придет через друзей.",
        "Рак": "🏠 День интуиции и семьи. Займитесь домом, уделите время близким.",
        "Лев": "🎭 Творческий день. Покажите себя, ваши таланты будут замечены.",
        "Дева": "📋 День порядка и планирования. Систематизируйте дела.",
        "Весы": "⚖️ День гармонии. Избегайте конфликтов, ищите компромиссы.",
        "Скорпион": "🦂 День трансформации. Глубокие размышления помогут найти решение.",
        "Стрелец": "✈️ День приключений и оптимизма. Расширяйте горизонты!",
        "Козерог": "🏔️ День достижений. Работайте над целями, будьте упорны.",
        "Водолей": "💡 День идей и нестандартных решений. Делитесь мыслями!",
        "Рыбы": "🎨 День творчества и интуиции. Займитесь искусством."
    }
    forecast = forecasts.get(zodiac_sign, "🌟 Гармоничный день. Доверьтесь своей интуиции.")
    await message.answer(
        f"✨ **Гороскоп для {zodiac_sign}** ✨\n\n"
        f"📅 **Сегодня:** {forecast}\n\n"
        f"💫 Хорошего дня!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "💕 **Расчет совместимости**\n\n"
        "Введи **первую** дату рождения:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer(
        "💕 **Расчет совместимости**\n\n"
        "Введи **вторую** дату рождения:\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
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
        await message.answer(
            f"♊ **Результат совместимости** ♊\n\n"
            f"📅 {date1} → **{result['sign1']}**\n"
            f"📅 {message.text} → **{result['sign2']}**\n\n"
            f"🌟 **Совместимость: {result['percent']}%** 🌟\n\n"
            f"{result['text']}\n\n"
            f"✨ Как тебе результат? Можешь рассказать подробности или спросить совет! ✨",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    taro_cards = {
        "Шут": "🎭 Новое начало, спонтанность. Позволь себе сделать первый шаг!",
        "Маг": "🪄 Сила воли, проявление желаний. У тебя есть все ресурсы!",
        "Верховная Жрица": "🌙 Интуиция, тайны. Доверься своему внутреннему голосу.",
        "Императрица": "👑 Творчество, изобилие. Пришло время творить.",
        "Император": "🏛️ Структура, власть. Укрепляй свои границы.",
        "Иерофант": "⛪ Традиции, обучение. Обратись к опыту старших.",
        "Влюбленные": "💕 Любовь, выбор. Важный выбор на пути.",
        "Колесница": "⚡ Воля, победа. Управляй своей судьбой!",
        "Сила": "🦁 Мужество, сила. Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 Самоанализ, мудрость. Время побыть наедине.",
        "Колесо Фортуны": "🎡 Перемены, удача. Жизнь меняется к лучшему.",
        "Справедливость": "⚖️ Честность, закон. Поступи справедливо.",
        "Повешенный": "🪢 Новая перспектива. Посмотри на ситуацию иначе.",
        "Смерть": "♻️ Трансформация. Старое уходит, новое приходит.",
        "Умеренность": "⚖️ Баланс, терпение. Найди золотую середину.",
        "Дьявол": "😈 Освобождение. От чего пора отказаться?",
        "Башня": "🏛️💥 Внезапные перемены. Крах иллюзий.",
        "Звезда": "⭐ Надежда, исцеление. Верь в лучшее!",
        "Луна": "🌕 Иллюзии, страхи. Доверяй интуиции.",
        "Солнце": "☀️ Радость, успех. Всё будет хорошо!",
        "Суд": "🎺 Пробуждение, прощение. Время подвести итоги.",
        "Мир": "🌍 Завершение, удовлетворение. Ты достигла цели!"
    }
    card_name = random.choice(list(taro_cards.keys()))
    card_meaning = taro_cards[card_name]
    
    await message.answer(
        f"🎴 **Карта дня: {card_name}** 🎴\n\n{card_meaning}\n\n"
        f"✨ Прислушайся к её посланию сегодня.",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "🌸 **Запись на консультацию** 🌸\n\n"
        "Оставь свой контакт (@username или номер телефона), и психолог Дарья свяжется с тобой.\n\n"
        "✨ Всё конфиденциально, ты в безопасности.\n\n"
        "Или нажми /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
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
    
    await message.answer(
        f"✅ **Спасибо!** Психолог {PSYCHOLOGIST_NAME} свяжется с тобой в ближайшее время.\n\n"
        f"Береги себя, и помни — ты не одна! 💕",
        reply_markup=menu_keyboard
    )
    await state.clear()
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message):
    user_id = message.from_user.id
    if is_premium(user_id):
        await message.answer(
            "💎 **Premium уже активна!** 💎\n\n"
            "Спасибо за поддержку проекта! 🙏\n"
            "📬 Каждое утро в 8:00 ты получаешь персональный прогноз!",
            reply_markup=menu_keyboard
        )
    else:
        remaining = get_remaining_questions(user_id)
        await message.answer(
            f"⭐ **Premium-подписка 99 Telegram Stars/мес** ⭐\n\n"
            f"📊 **Твой лимит сегодня:** {remaining}/{FREE_QUESTIONS_PER_DAY} бесплатных вопросов\n\n"
            f"💎 **Что даёт Premium:**\n"
            f"✅ Безлимитные вопросы к ИИ-психологу\n"
            f"✅ Расширенные ответы в разделе «Совместимость»\n"
            f"✅ Полный PDF-отчёт (15+ страниц)\n"
            f"✅ Ежедневный персональный прогноз в 8:00\n"
            f"✅ Приоритетную поддержку\n\n"
            f"✨ Нажми кнопку ниже, чтобы оформить подписку!",
            reply_markup=premium_keyboard
        )

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🔮 **Что даёт Premium-подписка?** 🔮\n\n"
        "1️⃣ **Безлимитные консультации** — без ограничения в 7 вопросов в день\n\n"
        "2️⃣ **Полный разбор совместимости** — детальный анализ пары: сильные стороны, точки роста, кармические задачи\n\n"
        "3️⃣ **Расширенные прогнозы** — гороскоп и число дня с подробными рекомендациями\n\n"
        "4️⃣ **PDF-отчёт** — персональный документ на 15+ страниц для печати\n\n"
        "5️⃣ **Ежедневный прогноз в 8:00** — персональные аффирмации, гороскоп и карта дня\n\n"
        "6️⃣ **Приоритетная поддержка** — ваши вопросы обрабатываются в первую очередь\n\n"
        "💎 **Стоимость:** всего 99 Stars (~99 ₽) в месяц\n\n"
        "✨ Нажми «Оформить подписку» и открой мир магии!"
    )

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="Premium-подписка на месяц", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(
        title="Premium-подписка",
        description="Неограниченные консультации с ИИ-психологом + расширенные функции + PDF-отчёт",
        payload="premium_subscription_30d",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="premium_start"
    )

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    activate_premium(user_id, 30)
    await message.answer(
        "💎 **Поздравляем! Premium-подписка активирована!** 💎\n\n"
        "✨ Теперь тебе доступны:\n"
        "✅ Безлимитные вопросы к ИИ-психологу\n"
        "✅ Полный PDF-отчёт (кнопка в меню)\n"
        "✅ Ежедневный персональный прогноз в 8:00\n"
        "✅ Приоритетная поддержка\n\n"
        "Спасибо, что поддерживаешь проект! 🙏\n\n"
        "📄 Нажми «📄 Получить PDF-отчёт» чтобы скачать свой первый отчёт!",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message):
    user_id = message.from_user.id
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "📊 **Демо-отчёт**\n\n"
            "Сначала укажи свою дату рождения через кнопку «🔮 Число судьбы».\n\n"
            "После этого я смогу показать тебе персонализированный пример отчёта!",
            reply_markup=menu_keyboard
        )
        return
    
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    number, desc = calculate_fate_number(birth_date, gender)
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    
    demo_text = f"""
📄 **ПЕРСОНАЛЬНЫЙ ДЕМО-ОТЧЁТ** 📄
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 **ЧИСЛО СУДЬБЫ — {number}**
{desc[:200]}...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ **ГОРОСКОП ДЛЯ {sign}**

Звёзды говорят, что сегодня отличный день для новых начинаний. Твоя энергия на подъёме!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💕 **СОВМЕСТИМОСТЬ (ПРИМЕР)**

Совместимость с Весами: 86%
Сильные стороны: взаимное вдохновение, страсть, интерес

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎴 **РАСКЛАД ТАРО «ПУТЬ ГОДА»**

1. Маг 🪄 — У тебя есть все ресурсы!
2. Колесница ⚡ — Время действовать!
3. Звезда ⭐ — Верь в лучшее!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ **В ПОЛНОМ ОТЧЁТЕ (PREMIUM):**
• 15+ страниц с персональными прогнозами
• Разбор 5 сфер: любовь, деньги, карьера, здоровье, саморазвитие
• Детальный анализ совместимости (12 страниц)
• Ежедневные аффирмации на месяц
• Лунный календарь
• PDF-файл для печати

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎 **Всего за 99 Stars/мес!**

👉 Нажми «⭐ Подписка Premium» чтобы получить полную версию!
"""
    await message.answer(demo_text, parse_mode="Markdown", reply_markup=menu_keyboard)

@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message):
    user_id = message.from_user.id
    
    if not is_premium(user_id):
        await message.answer(
            "💎 **PDF-отчёт доступен только Premium-пользователям!** 💎\n\n"
            "Оформи подписку за 99 Stars/мес, чтобы получить:\n"
            "✅ Полный PDF-отчёт на 15+ страниц\n"
            "✅ Безлимитные консультации\n"
            "✅ Расширенную совместимость\n"
            "✅ Ежедневный прогноз в 8:00\n\n"
            "👉 Нажми «⭐ Подписка Premium» в меню.",
            reply_markup=menu_keyboard
        )
        return
    
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "🔮 **Сначала укажи дату рождения!** 🔮\n\n"
            "Нажми кнопку «🔮 Число судьбы» и введи дату в формате ДД.ММ.ГГГГ.\n\n"
            "После этого я смогу сгенерировать твой персональный отчёт.",
            reply_markup=menu_keyboard
        )
        return
    
    await message.answer(
        "💕 **Хочешь добавить анализ совместимости с партнёром?** 💕\n\n"
        "Это сделает отчёт ещё более полным и персонализированным!",
        reply_markup=partner_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_without_partner")
async def pdf_without_partner(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("📄 Генерирую твой персональный отчёт... Подожди немного ✨")
    pdf = await generate_pdf_report(callback.from_user.id, None)
    await callback.message.answer_document(
        document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"),
        caption="✨ **Твой персональный отчёт готов!** ✨\n\nБлагодарим за доверие и поддержку проекта 💕",
        reply_markup=menu_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def pdf_with_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💕 **Введи дату рождения партнёра** 💕\n\n"
        "В формате `ДД.ММ.ГГГГ`, например: 15.05.1990",
        parse_mode="Markdown"
    )
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
    await message.answer_document(
        document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{user_id}.pdf"),
        caption=f"✨ **Твой отчёт с анализом совместимости готов!** ✨\n\n📅 Дата партнёра: {partner_date}\n\nБлагодарим за доверие! 💕",
        reply_markup=menu_keyboard
    )

# ========== ОСНОВНОЙ ДИАЛОГ С ИИ ==========
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
            "🕊️ **Мне очень жаль, что тебе так тяжело** 🕊️\n\n"
            "Пожалуйста, обратись за помощью прямо сейчас:\n"
            "📞 **Телефон доверия:** 8-800-2000-122 (круглосуточно, анонимно)\n"
            "🚑 **МЧС России:** 112\n\n"
            "Ты не один. Пожалуйста, позвони ❤️"
        )
        return
    
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(
            f"📊 **Лимит бесплатных вопросов на сегодня исчерпан** ({FREE_QUESTIONS_PER_DAY}).\n\n"
            f"⭐ Оформи Premium-подписку за 99 Stars/мес, чтобы снять ограничения!\n\n"
            f"👉 Нажми кнопку «⭐ Подписка Premium» в меню.",
            reply_markup=menu_keyboard
        )
        return
    
    if user_id not in user_problems:
        user_problems[user_id] = {"problem": user_text, "direction": detect_direction(user_text)}
    
    try:
        history = get_history(user_id)
        history.append({"role": "user", "content": user_text})
        
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=history,
            max_tokens=350,
            temperature=0.9
        )
        
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
            
            book_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Да, хочу записаться!", callback_data="book")],
                    [InlineKeyboardButton(text="❌ Пока не готов(а)", callback_data="not_ready")]
                ]
            )
            await message.answer(
                f"💕 **{PSYCHOLOGIST_NAME}** может помочь тебе разобраться в этом глубже.\n\n"
                f"Хочешь обсудить это с живым психологом? Это конфиденциально и не обязывает ни к чему.",
                reply_markup=book_keyboard
            )
        else:
            if not is_premium(user_id):
                new_remaining = get_remaining_questions(user_id)
                answer += f"\n\n📊 Осталось вопросов сегодня: {new_remaining}/{FREE_QUESTIONS_PER_DAY}. ⭐ Подписка Premium снимает лимиты!"
            await message.answer(answer)
        
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer(
            "🌙 Извини, произошла небольшая ошибка. Попробуй ещё раз или воспользуйся кнопками меню.",
            reply_markup=menu_keyboard
        )

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🌸 **Оставь свой контакт** 🌸\n\n"
        "Напиши свой Telegram @username или номер телефона.\n"
        "Психолог Дарья свяжется с тобой в ближайшее время.\n\n"
        "Или нажми /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "🌿 Хорошо, я понимаю. Если захочешь поговорить — я всегда здесь.\n\n"
        "Напиши /start, когда будешь готова 🌸",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

# ========== ЗАПУСК ==========
async def main():
    print("✨ Бот с полным функционалом и ежедневными уведомлениями в 8:00 запущен! ✨")
    asyncio.create_task(send_daily_premium_forecasts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
