import asyncio
import logging
import os
import re
import random
import json
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from dotenv import load_dotenv
from openai import AsyncOpenAI
import gspread
from google.oauth2.service_account import Credentials

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

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== FSM СОСТОЯНИЯ ==========
class Dialogue(StatesGroup):
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()

# ========== ГЛАВНОЕ МЕНЮ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу")]
    ],
    resize_keyboard=True
)

# ========== ПРАВИЛЬНОЕ ОПРЕДЕЛЕНИЕ ЗНАКОВ ЗОДИАКА ==========
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

# ========== КРАСИВОЕ ОПИСАНИЕ ЧИСЛА СУДЬБЫ ==========
def calculate_fate_number(birth_date: str) -> tuple:
    try:
        day, month, year = map(int, birth_date.split('.'))
        total = day + month + year
        while total > 9:
            total = sum(int(d) for d in str(total))
        
        detailed_descriptions = {
            1: "🔴 **1 — Число Лидера**\n\nТы — прирождённый первопроходец! Твоя миссия — начинать новое и вдохновлять других. Ты независима, амбициозна и полна идей. \n\n✨ **Твой путь:** самостоятельность и смелость.\n💫 **Твой талант:** ты видишь то, что другие не замечают.\n🌟 **Совет:** доверяй своей интуиции и не бойся быть первой!",
            2: "🟠 **2 — Число Миротворца**\n\nТы — душа любой компании, дипломат и миротворец. Твоя суперсила — создавать гармонию там, где хаос. \n\n✨ **Твой путь:** сотрудничество и понимание.\n💫 **Твой талант:** ты чувствуешь эмоции других.\n🌟 **Совет:** не забывай о своих желаниях, заботясь о других!",
            3: "🟡 **3 — Число Творца**\n\nТы — источник радости и вдохновения! Твоя энергия заражает всех вокруг. \n\n✨ **Твой путь:** самовыражение и творчество.\n💫 **Твой талант:** ты легко находишь слова и идеи.\n🌟 **Совет:** не бойся быть в центре внимания — это твоя стихия!",
            4: "🟢 **4 — Число Строителя**\n\nТы — надёжная опора для всех. Твоя сила в дисциплине и упорстве. \n\n✨ **Твой путь:** создание прочных основ.\n💫 **Твой талант:** ты доводишь дела до конца.\n🌟 **Совет:** иногда позволяй себе отдыхать и не будь слишком строгой к себе!",
            5: "🔵 **5 — Число Свободы**\n\nТы — искатель приключений! Перемены — твой двигатель, рутина — твой враг. \n\n✨ **Твой путь:** свобода и новые впечатления.\n💫 **Твой талант:** ты легко адаптируешься к любому.\n🌟 **Совет:** наслаждайся путешествиями и новыми знакомствами!",
            6: "🔵 **6 — Число Заботы**\n\nТы — сердце семьи и опора для близких. Твоя любовь безусловна. \n\n✨ **Твой путь:** забота и ответственность.\n💫 **Твой талант:** ты создаёшь уют и гармонию.\n🌟 **Совет:** не забывай заботиться и о себе!",
            7: "🟣 **7 — Число Мудрости**\n\nТы — исследователь глубин. Тебе нужно время для размышлений и одиночества. \n\n✨ **Твой путь:** познание и мудрость.\n💫 **Твой талант:** ты видишь то, что скрыто от других.\n🌟 **Совет:** доверяй своей интуиции — она редко ошибается!",
            8: "⚫️ **8 — Число Силы**\n\nТы — рождённая для успеха! Деньги и власть приходят к тебе, когда ты в гармонии с собой. \n\n✨ **Твой путь:** материальная реализация.\n💫 **Твой талант:** ты умеешь зарабатывать и управлять.\n🌟 **Совет:** не забывай о духовном развитии!",
            9: "⚪️ **9 — Число Завершения**\n\nТы — гуманист и учитель. Твоя миссия — помогать другим и завершать старое, открывая путь новому. \n\n✨ **Твой путь:** служение людям.\n💫 **Твой талант:** ты чувствуешь боль других и хочешь помочь.\n🌟 **Совет:** научись прощать и отпускать — это твой ключ к счастью!"
        }
        return (total, detailed_descriptions.get(total, "✨ Уникальная личность с особенным путём!"))
    except:
        return (0, "❌ Ошибка формата даты. Пожалуйста, используй ДД.ММ.ГГГГ")

# ========== КРАСИВЫЙ РАСЧЕТ СОВМЕСТИМОСТИ ==========
def get_compatibility(date1: str, date2: str) -> dict:
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
        
        # Стихийные сочетания
        if elem1 == elem2:
            compatibility = random.randint(85, 98)
            text = f"🌟 **Идеальный союз!** Вы принадлежите к одной стихии {elem1}, поэтому понимаете друг друга с полуслова. Вас ждёт глубокая эмоциональная связь и долгие счастливые отношения 💕"
        elif (elem1 in ["Огонь 🔥", "Воздух 💨"]) and (elem2 in ["Огонь 🔥", "Воздух 💨"]):
            compatibility = random.randint(75, 90)
            text = f"💫 **Яркая пара!** {elem1} + {elem2} = взрывная смесь страсти и свободы. Вы будете вдохновлять друг друга на великие дела! 🚀"
        elif (elem1 in ["Земля 🌍", "Вода 💧"]) and (elem2 in ["Земля 🌍", "Вода 💧"]):
            compatibility = random.randint(80, 95)
            text = f"🌱 **Гармоничный союз!** {elem1} и {elem2} создают плодородную почву для любви и заботы. Это отношения, в которых хочется строить дом и растить детей 🏡"
        elif (elem1 in ["Огонь 🔥"] and elem2 in ["Земля 🌍"]) or (elem1 in ["Земля 🌍"] and elem2 in ["Огонь 🔥"]):
            compatibility = random.randint(60, 75)
            text = f"⚡ **Страстное притяжение!** {elem1} и {elem2} — это вызов и интерес. Вы можете многое дать друг другу, если научитесь принимать различия."
        else:
            compatibility = random.randint(50, 70)
            text = f"🦋 **Загадочный союз.** Вы очень разные, но именно это делает вашу пару уникальной. Если научитесь ценить差异 — откроете новый мир!"
        
        return {"percent": compatibility, "text": text, "sign1": sign1, "sign2": sign2}
    except:
        return {"percent": 0, "text": "❌ Ошибка формата даты"}

# ========== ХРАНИЛИЩЕ ==========
user_history = {}
user_problems = {}

SYSTEM_PROMPT = f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}.

Правила:
1. Внимательно слушай и задавай вопросы.
2. Проявляй эмпатию.
3. Не ставь диагнозы.
4. При кризисе — дай телефон доверия: 8-800-2000-122.
5. После 4-6 обменов мягко предложи записаться к психологу.
6. В конце сообщения с предложением записи добавь: "ЗАПИСЬ_ГОТОВА"

Отвечай коротко (2-4 предложения) на русском."""

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

# ========== УВЕДОМЛЕНИЕ ПСИХОЛОГУ ==========
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = f"🔔 **НОВЫЙ ЗАПРОС**\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка: {e}")

# ========== СОХРАНЕНИЕ В GOOGLE SHEETS ==========
def save_to_google_sheets(user_id: int, username: str, problem: str, direction: str, contact: str):
    try:
        if not GOOGLE_CREDENTIALS_JSON or not SHEET_ID:
            print("⚠️ GOOGLE_CREDENTIALS_JSON или SHEET_ID не найдены")
            return False
        
        moscow_time = datetime.now() + timedelta(hours=3)
        
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        
        row = [
            moscow_time.strftime("%Y-%m-%d %H:%M:%S"),
            user_id,
            username,
            problem[:200],
            direction,
            contact,
            "new"
        ]
        sheet.append_row(row)
        print(f"✅ Заявка сохранена: {username}")
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"✨ **Добро пожаловать, {message.from_user.first_name or 'дорогой друг'}!** ✨\n\n"
        f"🌸 Я {PSYCHOLOGIST_NAME}, твой персональный гид в мире самопознания и магии чисел.\n\n"
        f"💫 Я умею:\n"
        f"🔮 **Число судьбы** — раскрою твои таланты и жизненный путь\n"
        f"⭐ **Гороскоп** — подскажу, что приготовили звёзды на сегодня\n"
        f"♊ **Совместимость** — расскажу, как сложатся отношения\n"
        f"🎴 **Карта дня Таро** — дам мудрый совет\n\n"
        f"💬 А ещё я всегда готова просто поговорить и поддержать.\n\n"
        f"👇 **Начни с кнопки меню или просто напиши мне!**",
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
    await message.answer("✨ История нашего разговора очищена. Начинаем с чистого листа!", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

# ========== КНОПКИ МЕНЮ ==========
@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    await message.answer(
        "📖 **Что я умею?**\n\n"
        "💬 **Просто напиши** — я выслушаю и поддержу\n"
        "🔮 **Число судьбы** — введи дату рождения, узнаешь свои таланты\n"
        "⭐ **Гороскоп** — выбери знак или отправь дату рождения\n"
        "♊ **Совместимость** — введи две даты, узнай будущее пары\n"
        "🎴 **Карта дня Таро** — мудрый совет на сегодня\n"
        "📞 **Запись к психологу** — оставь контакт, я передам Дарье\n\n"
        "🗑 /reset — начать диалог заново\n"
        "❌ /cancel — отменить текущее действие",
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
    await message.answer("🧹 История очищена. Мы начинаем с чистого листа!", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer(
        "🔮 **Расчёт числа судьбы**\n\n"
        "Введи свою дату рождения в формате:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

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

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "💕 **Расчёт совместимости**\n\n"
        "Введи **первую** дату рождения:\n`ДД.ММ.ГГГГ`\n\n"
        "🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    taro_cards = {
        "Шут": "🎭 **Шут** — Новое начало!\n\nТы стоишь на пороге приключения. Пора сделать первый шаг туда, куда давно боялась заглянуть. Доверься Вселенной — она поддержит!",
        "Маг": "🪄 **Маг** — Сила внутри!\n\nУ тебя есть всё необходимое для исполнения желаний. Просто поверь в себя и действуй! Твои ресурсы безграничны.",
        "Верховная Жрица": "🌙 **Верховная Жрица** — Тайны откроются.\n\nПрислушайся к своей интуиции сегодня. Ответы уже внутри тебя — просто загляни вглубь себя.",
        "Императрица": "👑 **Императрица** — Твори и созидай!\n\nЭто время любви, заботы и творчества. Посей семена — и скоро пожнёшь плоды.",
        "Император": "🏛️ **Император** — Структура и порядок.\n\nПришло время навести порядок в делах. Твоя сила в дисциплине и твёрдости.",
        "Иерофант": "⛪ **Иерофант** — Мудрый наставник.\n\nОбратись за советом к тому, кому доверяешь. Или стань наставником для кого-то.",
        "Влюбленные": "💕 **Влюблённые** — Сердечный выбор!\n\nСудьба ставит перед важным решением. Слушай сердце — оно не обманет.",
        "Колесница": "⚡ **Колесница** — Победа будет!\n\nПреодолей сомнения и двигайся вперёд. Успех уже близко!",
        "Сила": "🦁 **Сила** — Ты справишься!\n\nВнутри тебя скрыта огромная мощь. Не бойся проявлять её с любовью и терпением.",
        "Отшельник": "🏮 **Отшельник** — Время тишины.\n\nПобудь наедине с собой. Ответы придут, когда ты успокоишь ум.",
        "Колесо Фортуны": "🎡 **Колесо Фортуны** — Жизнь меняется!\n\nЖди перемен! Что-то старое уходит, уступая место новому и лучшему.",
        "Справедливость": "⚖️ **Справедливость** — Честность превыше всего.\n\nСегодня важно быть честной с собой и другими. Правда восторжествует.",
        "Повешенный": "🪢 **Повешенный** — Новый взгляд.\n\nПопробуй посмотреть на ситуацию под другим углом. Возможно, ты увидишь выход там, где раньше не замечала.",
        "Смерть": "♻️ **Смерть** — Трансформация.\n\nНе бойся отпустить прошлое. Закрывая одни двери, ты открываешь новые.",
        "Умеренность": "⚖️ **Умеренность** — Баланс и гармония.\n\nНайди золотую середину во всём. Терпение и умеренность приведут к цели.",
        "Дьявол": "😈 **Дьявол** — Освобождение.\n\nПора разорвать цепи, которые тебя сковывают. От чего тебе пора отказаться?",
        "Башня": "🏛️💥 **Башня** — Внезапные перемены.\n\nЧто-то рушится, чтобы освободить место для нового. Не сопротивляйся — так нужно.",
        "Звезда": "⭐ **Звезда** — Надежда сбывается!\n\nЗагадай желание! Вселенная готовит тебе подарок. Верь в лучшее.",
        "Луна": "🌕 **Луна** — Слушай интуицию.\n\nНа поверхности не всё так, как кажется. Доверяй своему внутреннему голосу.",
        "Солнце": "☀️ **Солнце** — Радость и успех!\n\nТвой день сияет! Наслаждайся моментом, делись теплом с окружающими.",
        "Суд": "🎺 **Суд** — Пробуждение.\n\nНастал час подвести итоги и простить себя и других. Новый цикл начинается!",
        "Мир": "🌍 **Мир** — Ты достигла цели!\n\nПоздравляю! Цикл завершён, ты на финише. Отдыхай и наслаждайся результатом."
    }
    
    card_name = random.choice(list(taro_cards.keys()))
    card_meaning = taro_cards[card_name]
    
    await message.answer(
        f"🎴 **Твоя карта дня — {card_name}** 🎴\n\n{card_meaning}\n\n"
        f"✨ Пусть этот день принесёт тебе волшебство! ✨",
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

# ========== ВВОД ДАННЫХ ДЛЯ РАСЧЕТОВ ==========
@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`, например 15.05.1990", reply_markup=menu_keyboard)
        return
    
    number, description = calculate_fate_number(message.text)
    await message.answer(
        f"🔮 **Твоё число судьбы — {number}** 🔮\n\n{description}\n\n"
        f"✨ Это число — ключ к пониманию твоих врождённых талантов! ✨",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

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
            await message.answer("❌ Неизвестный знак. Попробуй ещё раз.", reply_markup=menu_keyboard)
            return
    
    detailed_forecasts = {
        "Овен": "🔥 **Овен** — твоя энергия сегодня зашкаливает! Отличный день для начала новых проектов и спортивных достижений. Вечером жди приятных сюрпризов от близких.",
        "Телец": "💰 **Телец** — сегодня твоя стихия — финансы. Удачный день для крупных покупок и инвестиций. Не упусти возможность!",
        "Близнецы": "💬 **Близнецы** — звёзды советуют больше общаться. Жди новостей от старых друзей, возможна неожиданная встреча.",
        "Рак": "🏠 **Рак** — лучший день для семьи и дома. Уют и забота наполнят тебя энергией на неделю вперёд.",
        "Лев": "🎭 **Лев** — сегодня ты в центре внимания! Покажи свои таланты, не стесняйся. Вечером возможен романтический сюрприз.",
        "Дева": "📋 **Дева** — день порядка и планирования. Разбери завалы — и почувствуешь невероятное облегчение.",
        "Весы": "⚖️ **Весы** — гармония во всём! Хороший день для культурного досуга, похода в театр или музей.",
        "Скорпион": "🦂 **Скорпион** — погрузись в свои глубины. Уединись и подумай о важном. Ответы придут сами.",
        "Стрелец": "✈️ **Стрелец** — звёзды зовут в путешествия! Даже небольшая прогулка принесёт вдохновение.",
        "Козерог": "🏔️ **Козерог** — день карьерных побед. Будь упорна — и достигнешь цели. Твой труд оценят.",
        "Водолей": "💡 **Водолей** — идеи витают в воздухе! Записывай всё, что приходит в голову. Гениальное рядом.",
        "Рыбы": "🎨 **Рыбы** — погрузись в творчество или медитацию. Это восстановит твои силы лучше любого отдыха."
    }
    
    forecast = detailed_forecasts.get(zodiac_sign, "🌟 Звёзды шепчут: сегодня отличный день для тебя!")
    await message.answer(
        f"⭐ **Гороскоп для {zodiac_sign} на сегодня** ⭐\n\n{forecast}\n\n✨ Хорошего дня, звёздная! ✨",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("💕 Теперь введи **вторую** дату рождения:\n`ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Давай начнём заново.", reply_markup=menu_keyboard)
        await state.clear()
        return
    
    result = get_compatibility(date1, message.text)
    if result['percent'] == 0:
        await message.answer(f"❌ {result['text']}", reply_markup=menu_keyboard)
    else:
        await message.answer(
            f"💕 **Результат совместимости** 💕\n\n"
            f"📅 {date1} → **{result['sign1']}**\n"
            f"📅 {message.text} → **{result['sign2']}**\n\n"
            f"🌟 **Совместимость: {result['percent']}%** 🌟\n\n"
            f"{result['text']}\n\n"
            f"✨ Как тебе результат? Можешь рассказать подробности или спросить совет! ✨",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
    await state.set_state(Dialogue.chatting)

# ========== ОБРАБОТКА ЗАПИСИ К ПСИХОЛОГУ ==========
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
        f"🌸 **Спасибо, {message.from_user.first_name or 'дорогой друг'}!** 🌸\n\n"
        f"Я передала твой контакт психологу {PSYCHOLOGIST_NAME}.\n\n"
        f"✨ Она свяжется с тобой в ближайшее время ✨\n\n"
        f"Береги себя. Ты не одна! 💕",
        reply_markup=menu_keyboard
    )
    await state.clear()
    await state.set_state(Dialogue.chatting)

# ========== ОСНОВНОЙ ДИАЛОГ С ИИ ==========
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", 
                    "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", 
                    "📞 Запись к психологу"]
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
            "Ты не одна. Пожалуйста, позвони ❤️"
        )
        return
    
    if not groq_client:
        await message.answer(
            "🤖 ИИ-ассистент временно недоступен. Пожалуйста, используй кнопки меню — их работа не зависит от интернета.",
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
        
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            
            book_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Да, хочу записаться!", callback_data="book")],
                    [InlineKeyboardButton(text="❌ Пока не готова", callback_data="not_ready")]
                ]
            )
            await message.answer(
                f"💕 **{PSYCHOLOGIST_NAME}** может помочь тебе разобраться в этом глубже.\n\n"
                f"Хочешь обсудить это с живым психологом? Это конфиденциально и не обязывает ни к чему.",
                reply_markup=book_keyboard
            )
        else:
            await message.answer(answer)
        
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer(
            "🌙 Извини, произошла небольшая ошибка. Попробуй ещё раз или воспользуйся кнопками меню.",
            reply_markup=menu_keyboard
        )

# ========== КОЛБЭКИ ДЛЯ ИНЛАЙН-КНОПОК ==========
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
    print("✨ Бот с живыми ответами запущен! ✨")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
