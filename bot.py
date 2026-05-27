import asyncio
import logging
import os
import re
import random
from datetime import datetime
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, URLInputFile
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

from numerology import NumerologyCalculator

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
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

if groq_client:
    print("🤖 ИИ-ассистент (Groq) инициализирован")
else:
    print("⚠️ GROQ_API_KEY не найден")

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

# ========== КНОПКИ МЕНЮ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="❌ Отмена"), KeyboardButton(text="🔮 Число судьбы")],
        [KeyboardButton(text="⭐ Гороскоп"), KeyboardButton(text="♊ Совместимость")],
        [KeyboardButton(text="🎴 Карта дня Таро"), KeyboardButton(text="📞 Запись к психологу")]
    ],
    resize_keyboard=True
)

book_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📝 Записаться к Дарье", callback_data="book")],
        [InlineKeyboardButton(text="❌ Пока не готов", callback_data="not_ready")]
    ]
)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_zodiac_sign(day: int, month: int) -> str:
    """Точное определение знака зодиака по дню и месяцу"""
    if (month == 3 and day >= 21) or (month == 4 and day <= 19):
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
    elif (month == 12 and day >= 22) or (month == 1 and day <= 19):
        return "Козерог"
    elif (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "Водолей"
    else:
        return "Рыбы"

# ========== ХРАНИЛИЩЕ ИСТОРИИ ==========
user_history = {}
user_problems = {}

SYSTEM_PROMPT = f"""Ты — эмпатичный помощник-психолог по имени {PSYCHOLOGIST_NAME}.

Твои правила:
1. Внимательно слушай и задавай уточняющие вопросы.
2. Проявляй эмпатию и поддержку.
3. Не ставь диагнозы.
4. При признаках кризиса (суицид, самоповреждение) — дай телефон доверия.
5. Когда соберёшь достаточно информации (обычно после 4-6 обменов), мягко предложи записаться к живому психологу {PSYCHOLOGIST_NAME}.
6. В конце сообщения с предложением записи добавь фразу: "ЗАПИСЬ_ГОТОВА"

Отвечай на русском языке, коротко (2-4 предложения)."""

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

# ========== УВЕДОМЛЕНИЕ ПСИХОЛОГУ ==========
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = (
        f"🔔 **НОВЫЙ ЗАПРОС**\n\n"
        f"👤 {username} (ID: {user_id})\n"
        f"📝 {problem[:300]}\n"
        f"🏷 {direction}\n"
        f"📞 {contact}"
    )
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
            print(f"📤 Уведомление отправлено психологу {PSYCHOLOGIST_ID}")
        except Exception as e:
            logging.error(f"Ошибка отправки: {e}")

# ========== ОБРАБОТЧИКИ ЗАПИСИ ==========
@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📝 **Оставь контакт**\n\nНапиши @username или номер телефона.\n"
        f"Психолог {PSYCHOLOGIST_NAME} свяжется с тобой.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Хорошо, я здесь. Напиши /start когда захочешь поговорить.",
        reply_markup=menu_keyboard
    )

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"✅ Спасибо! Психолог {PSYCHOLOGIST_NAME} свяжется с вами в течение 24 часов.\n\nБерегите себя ❤️",
        reply_markup=menu_keyboard
    )
    await state.clear()

# ========== ОБРАБОТЧИКИ КНОПОК МЕНЮ ==========

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    help_text = (
        "📖 **Доступные функции:**\n\n"
        "💬 **Просто напишите** - я выслушаю и поддержу\n"
        "🔮 **Число судьбы** - расчет по дате рождения\n"
        "⭐ **Гороскоп** - прогноз на сегодня\n"
        "♊ **Совместимость** - анализ пары\n"
        "🎴 **Карта дня Таро** - с эмодзи и значением\n"
        "📞 **Запись к психологу** - живая консультация\n\n"
        "🗑 **Очистить диалог** / ❌ **Отмена**"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🗑 История и состояния очищены.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "❌ Отмена")
async def menu_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=menu_keyboard)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer(
        "🔮 **Расчет числа судьбы**\n\nВведите дату рождения:\n`ДД.ММ.ГГГГ`\nНапример: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", parse_mode="Markdown")
        return
    
    number, description = NumerologyCalculator.calculate_fate_number(message.text)
    await message.answer(
        f"🔮 **Ваше число судьбы: {number}**\n\n{description}\n\n"
        f"✨ Это число раскрывает ваши врожденные таланты.",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)
    await message.answer("Можете продолжить диалог или выбрать другую функцию.", reply_markup=menu_keyboard)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer(
        "⭐ **Гороскоп**\n\nВведите ваш знак зодиака или дату рождения:\n"
        "Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы\n\n"
        "Или дату в формате `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    
    # Если ввели дату
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ Ваш знак: **{zodiac_sign}**")
    else:
        # Если ввели название знака
        known = {
            "овен": "Овен", "телец": "Телец", "близнецы": "Близнецы",
            "рак": "Рак", "лев": "Лев", "дева": "Дева",
            "весы": "Весы", "скорпион": "Скорпион", "стрелец": "Стрелец",
            "козерог": "Козерог", "водолей": "Водолей", "рыбы": "Рыбы"
        }
        if text.lower() in known:
            zodiac_sign = known[text.lower()]
        else:
            await message.answer("❌ Неизвестный знак или неверная дата. Попробуйте еще раз.")
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
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "♊ **Расчет совместимости**\n\nВведите **первую** дату рождения:\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", parse_mode="Markdown")
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("Введите **вторую** дату рождения:\n`ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", parse_mode="Markdown")
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начните заново.")
        await state.clear()
        return
    
    result = NumerologyCalculator.get_compatibility(date1, message.text)
    if result['compatibility_percent'] == 0:
        await message.answer("❌ Ошибка расчета. Проверьте даты.")
    else:
        await message.answer(
            f"♊ **Совместимость**\n\n"
            f"📅 Дата 1: {date1} ({result['sign1']}, число {result['number1']})\n"
            f"📅 Дата 2: {message.text} ({result['sign2']}, число {result['number2']})\n\n"
            f"💕 **Совместимость: {result['compatibility_percent']}%**\n{result['text']}",
            parse_mode="Markdown"
        )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    await message.answer("🎴 Вытягиваю карту дня...")
    
    # Словарь карт Таро с эмодзи и значениями
    taro_cards = {
        "0 - Шут": {"emoji": "🎭", "meaning": "Новое начало, невинность, спонтанность. Позвольте себе сделать первый шаг в неизвестность!"},
        "1 - Маг": {"emoji": "🪄", "meaning": "Сила воли, концентрация, проявление желаний. У вас есть все ресурсы для достижения цели!"},
        "2 - Верховная Жрица": {"emoji": "🌙", "meaning": "Интуиция, тайны, подсознание. Доверьтесь своему внутреннему голосу."},
        "3 - Императрица": {"emoji": "👑", "meaning": "Творчество, изобилие, материнство. Пришло время творить и заботиться."},
        "4 - Император": {"emoji": "🏛️", "meaning": "Структура, власть, стабильность. Укрепляйте свои границы."},
        "5 - Иерофант": {"emoji": "⛪", "meaning": "Традиции, обучение, духовность. Обратитесь к опыту старших."},
        "6 - Влюбленные": {"emoji": "💕", "meaning": "Любовь, выбор, гармония. Важный выбор на пути."},
        "7 - Колесница": {"emoji": "⚡", "meaning": "Воля, контроль, победа. Управляйте своей судьбой!"},
        "8 - Сила": {"emoji": "🦁", "meaning": "Мужество, сострадание, внутренняя сила. Вы сильнее, чем кажетесь."},
        "9 - Отшельник": {"emoji": "🏮", "meaning": "Самоанализ, мудрость, поиск истины. Время побыть наедине."},
        "10 - Колесо Фортуны": {"emoji": "🎡", "meaning": "Перемены, судьба, удача. Жизнь меняется к лучшему."},
        "11 - Справедливость": {"emoji": "⚖️", "meaning": "Честность, равновесие, закон. Поступите справедливо."},
        "12 - Повешенный": {"emoji": "🪢", "meaning": "Жертва, новая перспектива. Посмотрите на ситуацию иначе."},
        "13 - Смерть": {"emoji": "♻️", "meaning": "Трансформация, завершение, новое начало. Старое уходит."},
        "14 - Умеренность": {"emoji": "⚖️", "meaning": "Баланс, терпение, гармония. Найдите золотую середину."},
        "15 - Дьявол": {"emoji": "😈", "meaning": "Освобождение от зависимостей. От чего пора отказаться?"},
        "16 - Башня": {"emoji": "🏛️💥", "meaning": "Внезапные перемены. Старое рушится для нового."},
        "17 - Звезда": {"emoji": "⭐", "meaning": "Надежда, вдохновение, исцеление. Верьте в лучшее!"},
        "18 - Луна": {"emoji": "🌕", "meaning": "Иллюзии, страхи, подсознание. Доверяйте интуиции."},
        "19 - Солнце": {"emoji": "☀️", "meaning": "Радость, успех, позитив. Всё будет хорошо!"},
        "20 - Суд": {"emoji": "🎺", "meaning": "Пробуждение, прощение, возрождение. Время подвести итоги."},
        "21 - Мир": {"emoji": "🌍", "meaning": "Завершение, целостность, удовлетворение. Вы достигли цели!"}
    }
    
    card_name = random.choice(list(taro_cards.keys()))
    card = taro_cards[card_name]
    
    await message.answer(
        f"🎴 **Карта дня: {card_name}**\n\n"
        f"{card['emoji']} **Значение:** {card['meaning']}\n\n"
        f"✨ Энергия этой карты будет сопровождать вас сегодня.\n\n"
        f"💫 **Совет:** Прислушайтесь к посланию карты в течение дня.",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 **Запись на консультацию**\n\n"
        "Оставьте ваш контакт (@username или телефон), и психолог свяжется с вами.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

# ========== ОСНОВНОЙ ДИАЛОГ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"👋 **Привет! Я {PSYCHOLOGIST_NAME}, психолог-ассистент.**\n\n"
        f"Расскажи, что тебя беспокоит. Я внимательно выслушаю.\n\n"
        f"Также я могу:\n"
        f"🔮 Рассчитать число судьбы\n"
        f"⭐ Сделать гороскоп\n"
        f"♊ Проверить совместимость\n"
        f"🎴 Вытянуть карту Таро\n\n"
        f"Используй кнопки меню 👇",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=menu_keyboard)

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

@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "❌ Отмена", 
                    "🔮 Число судьбы", "⭐ Гороскоп", "♊ Совместимость", 
                    "🎴 Карта дня Таро", "📞 Запись к психологу"]
    if user_text in menu_buttons:
        return
    
    print(f"📨 Получено: {user_text}")
    
    crisis = ["суицид", "самоубийств", "не хочу жить", "покончить с собой"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer("🚨 Телефон доверия: 8-800-2000-122. Пожалуйста, позвоните ❤️")
        return
    
    if not groq_client:
        await message.answer("🤖 ИИ-ассистент временно недоступен. Используйте кнопки меню.")
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
            temperature=0.8
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
            await message.answer(
                f"💬 **Как ты смотришь на то, чтобы обсудить это с психологом {PSYCHOLOGIST_NAME}?**\n\n"
                f"Это конфиденциально и не обязывает к продолжению.",
                reply_markup=book_keyboard
            )
        else:
            await message.answer(answer)
        
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer("Извините, произошла ошибка. Попробуйте ещё раз или используйте кнопки меню.")

# ========== ЗАПУСК ==========
async def main():
    print("🚀 Бот с ИИ-ассистентом, нумерологией и Таро запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
