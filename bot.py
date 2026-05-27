# ========== ДОБАВЬТЕ НОВЫЕ ИМПОРТЫ В НАЧАЛО ФАЙЛА ==========
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping

# ========== ДОБАВЬТЕ НОВУЮ КНОПКУ В МЕНЮ ==========
# Найдите menu_keyboard и добавьте кнопку
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу"), KeyboardButton(text="⭐ Подписка Premium")],
        [KeyboardButton(text="📊 Демо-отчёт"), KeyboardButton(text="📄 Получить PDF-отчёт")]  # НОВАЯ КНОПКА
    ],
    resize_keyboard=True
)

# ========== ФУНКЦИЯ ГЕНЕРАЦИИ PDF-ОТЧЁТА ==========
async def generate_pdf_report(user_id: int, partner_date: str = None) -> io.BytesIO:
    """Генерирует красивый PDF-отчёт для Premium-пользователя"""
    
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    
    if not birth_date:
        birth_date = "01.01.1990"
    
    # Создаём буфер для PDF
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=24, alignment=TA_CENTER, spaceAfter=20)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading1'], fontSize=18, textColor='#4A148C', spaceAfter=12)
    subheading_style = ParagraphStyle('Subheading', parent=styles['Heading2'], fontSize=14, textColor='#6A1B9A', spaceAfter=8)
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=11, leading=14, spaceAfter=6)
    
    # Титульная страница
    story.append(Paragraph(f"<b>Персональный отчёт</b>", title_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"<font size=16>для {name}</font>", title_style))
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(f"📅 Дата рождения: {birth_date}", normal_style))
    story.append(Paragraph(f"✨ Пол: {'Женский' if gender == 'female' else 'Мужской'}", normal_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"📆 Дата составления: {datetime.now().strftime('%d.%m.%Y')}", normal_style))
    story.append(PageBreak())
    
    # 1. Число судьбы
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    story.append(Paragraph(f"🔮 Число судьбы — {fate_number}", heading_style))
    story.append(Paragraph(fate_desc, normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 2. Гороскоп на месяц
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    story.append(Paragraph(f"⭐ Гороскоп для {sign} на {datetime.now().strftime('%B %Y')}", heading_style))
    forecasts = {
        "Овен": "Ваша энергия на пике! Используйте её для новых начинаний. Середина месяца принесёт приятные сюрпризы в личной жизни.",
        "Телец": "Финансовый месяц. Возможны крупные покупки или инвестиции. В конце месяца — удачные переговоры.",
        "Близнецы": "Месяц общения и новых контактов. Старые друзья напомнят о себе. Возможна короткая поездка.",
        "Рак": "Время семьи и дома. Укрепляйте отношения с близкими. В конце месяца — приятные новости.",
        "Лев": "Творческий подъём. Ваши таланты будут замечены. В середине месяца — важное приглашение.",
        "Дева": "Месяц порядка. Завершите старые дела. В конце месяца — финансовая стабильность.",
        "Весы": "Гармония во всём. Хорошее время для новых знакомств. Возможен романтический интерес.",
        "Скорпион": "Глубокий самоанализ. Ответы придут изнутри. В конце месяца — важное решение.",
        "Стрелец": "Время путешествий и новых впечатлений. Возможно обучение. Конец месяца — удача.",
        "Козерог": "Карьерный рост. Ваши усилия оценят. В середине месяца — прибавка или бонус.",
        "Водолей": "Время идей. Записывайте всё, что приходит в голову. Конец месяца — творческий прорыв.",
        "Рыбы": "Творчество и интуиция. Займитесь искусством. В конце месяца — романтический сюрприз."
    }
    story.append(Paragraph(forecasts.get(sign, "Гармоничный месяц. Доверяйте себе."), normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 3. Совместимость (если запрошена)
    if partner_date:
        comp = get_compatibility(birth_date, partner_date, premium=True)
        story.append(Paragraph(f"💕 Совместимость с партнёром", heading_style))
        story.append(Paragraph(f"📅 Ваша дата: {birth_date} → {comp['sign1']}", normal_style))
        story.append(Paragraph(f"📅 Дата партнёра: {partner_date} → {comp['sign2']}", normal_style))
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(f"🌟 Совместимость: {comp['percent']}%", subheading_style))
        story.append(Paragraph(comp['text'], normal_style))
        story.append(Spacer(1, 0.5*cm))
    
    # 4. Расклад Таро (10 карт)
    story.append(Paragraph(f"🎴 Расклад «Путь года»", heading_style))
    taro_spreads = [
        ("1. Вы сейчас", "Маг 🪄 — «У вас есть всё для нового этапа»"),
        ("2. Что вас ждёт", "Колесница ⚡ — «Время действовать и побеждать»"),
        ("3. Испытания", "Звезда ⭐ — «Надежда поможет преодолеть трудности»"),
        ("4. Помощь", "Сила 🦁 — «Внутренняя мощь поведёт вас»"),
        ("5. Любовь", "Влюблённые 💕 — «Судьбоносная встреча или важный выбор»"),
        ("6. Карьера", "Император 🏛️ — «Укрепление позиций или повышение»"),
        ("7. Финансы", "Десятка Пентаклей 💰 — «Стабильный доход, возможно наследство»"),
        ("8. Здоровье", "Умеренность ⚖️ — «Баланс между работой и отдыхом»"),
        ("9. Духовный рост", "Отшельник 🏮 — «Год глубокого самоанализа»"),
        ("10. Итог года", "Мир 🌍 — «Завершение цикла, достижение цели»")
    ]
    for card, meaning in taro_spreads:
        story.append(Paragraph(f"<b>{card}:</b> {meaning}", normal_style))
        story.append(Spacer(1, 0.2*cm))
    story.append(Spacer(1, 0.5*cm))
    
    # 5. Ежедневные аффирмации
    story.append(Paragraph(f"✨ Аффирмации на {datetime.now().strftime('%B')}", heading_style))
    affirmations = [
        f"1 {datetime.now().strftime('%d.%m')}: «Я открыта новым возможностям. Вселенная заботится обо мне»",
        "2: «Мои таланты признаны и ценны»",
        "3: «Я привлекаю успех и изобилие»",
        "4: «Моя интуиция ведёт меня правильным путём»",
        "5: «Я люблю и принимаю себя целиком»",
        "6: «Каждый день я становлюсь сильнее»",
        "7: «Я достойна всего самого лучшего»",
        "8: «Мои мечты сбываются в нужное время»",
        "9: «Я благодарна за всё, что имею»",
        "10: «Моё сердце открыто для любви»",
        "11: «Я выбираю радость каждый день»",
        "12: «Мои усилия приносят плоды»",
        "13: «Я в гармонии с собой и миром»",
        "14: «Я привлекаю нужных людей»",
        "15: «Моя жизнь наполнена смыслом»",
        "16: «Я верю в свои силы»",
        "17: «Я создаю своё счастливое будущее»",
        "18: «Я достойна успеха и признания»",
        "19: «Я благодарю этот день за радость»",
        "20: «Моя душа растёт и развивается»",
        "21: «Я привлекаю финансовое изобилие»",
        "22: «Мои отношения наполнены любовью»",
        "23: «Я принимаю перемены с радостью»",
        "24: «Моя интуиция — мой компас»",
        "25: «Я вдохновляю себя и других»",
        "26: «Моя жизнь — это чудо»",
        "27: «Я выбираю здоровье и энергию»",
        "28: «Я благодарна за этот месяц»",
        "29: «Я готова к новым свершениям»",
        "30: «Мой потенциал безграничен»"
    ]
    for aff in affirmations[:datetime.now().day + 5]:
        story.append(Paragraph(aff, normal_style))
        story.append(Spacer(1, 0.2*cm))
    story.append(Spacer(1, 0.5*cm))
    
    # 6. Лунный календарь
    story.append(Paragraph(f"🌙 Лунный календарь на {datetime.now().strftime('%B')}", heading_style))
    story.append(Paragraph("• 🌑 Новолуние — время начинать новое", normal_style))
    story.append(Paragraph("• 🌓 Первая четверть — время действовать", normal_style))
    story.append(Paragraph("• 🌕 Полнолуние — время подводить итоги", normal_style))
    story.append(Paragraph("• 🌗 Последняя четверть — время завершать", normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 7. Персональные рекомендации
    story.append(Paragraph(f"💫 Персональные рекомендации", heading_style))
    recommendations = {
        "female": "🌸 Как женщина, вы обладаете особой интуицией и чуткостью. Доверяйте своим чувствам, но не забывайте и о логике. Уделяйте время себе — ваша энергия восстанавливается через творчество и общение с близкими.",
        "male": "💪 Как мужчина, вы обладаете силой и решительностью. Ваша задача — действовать, но не забывайте слушать своё сердце. Уделяйте время отдыху — ваша энергия восстанавливается через спорт и хобби."
    }
    story.append(Paragraph(recommendations.get(gender, recommendations["female"]), normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    story.append(Paragraph("🌿 Благодарим за доверие! Берегите себя и будьте счастливы 💕", normal_style))
    
    # Собираем PDF
    doc.build(story)
    buffer.seek(0)
    return buffer

# ========== ОБРАБОТЧИК КНОПКИ «📄 Получить PDF-отчёт» ==========
@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message):
    user_id = message.from_user.id
    
    # Проверяем, есть ли подписка Premium
    if not is_premium(user_id):
        await message.answer(
            "💎 **Эта функция доступна только Premium-пользователям!**\n\n"
            "Оформите подписку за 99 Stars/мес, чтобы получить:\n"
            "✅ Полный PDF-отчёт на 15+ страниц\n"
            "✅ Безлимитные консультации\n"
            "✅ Расширенную совместимость\n\n"
            "👉 Нажмите «⭐ Подписка Premium» в меню.",
            reply_markup=menu_keyboard
        )
        return
    
    # Проверяем, есть ли дата рождения
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "🔮 **Сначала нужно указать дату рождения!**\n\n"
            "Нажмите кнопку «Число судьбы» и введите дату в формате ДД.ММ.ГГГГ.\n\n"
            "После этого я смогу сгенерировать ваш персональный отчёт.",
            reply_markup=menu_keyboard
        )
        return
    
    await message.answer(
        "📄 **Генерация PDF-отчёта...**\n\n"
        "Пожалуйста, подождите немного. Отчёт будет готов через 10-20 секунд ✨",
        reply_markup=menu_keyboard
    )
    
    # Спрашиваем, нужна ли совместимость с партнёром
    partner_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💕 Да, добавить совместимость", callback_data="pdf_with_partner")],
            [InlineKeyboardButton(text="📄 Только мой отчёт", callback_data="pdf_without_partner")]
        ]
    )
    await message.answer(
        "💕 **Хотите добавить анализ совместимости с партнёром?**\n\n"
        "Это увеличит отчёт ещё на 2-3 страницы с детальным разбором вашей пары.",
        reply_markup=partner_keyboard
    )

@dp.callback_query(lambda c: c.data in ["pdf_with_partner", "pdf_without_partner"])
async def process_pdf_request(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    partner_date = None
    if callback.data == "pdf_with_partner":
        await callback.message.answer(
            "💕 **Введите дату рождения партнёра**\n\n"
            "В формате `ДД.ММ.ГГГГ`, например: 15.05.1990",
            parse_mode="Markdown"
        )
        # Сохраняем состояние ожидания даты партнёра
        await dp.fsm.storage.set_state(user_id, "waiting_for_partner_date")
        return
    
    # Генерируем отчёт без партнёра
    await callback.message.answer("📄 Генерирую ваш персональный отчёт...")
    pdf_buffer = await generate_pdf_report(user_id, None)
    
    await callback.message.answer_document(
        document=types.BufferedInputFile(pdf_buffer.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"),
        caption="✨ **Ваш персональный отчёт готов!** ✨\n\nБлагодарим за доверие и поддержку проекта 💕",
        reply_markup=menu_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def process_pdf_with_partner_date(callback: types.CallbackQuery, state: FSMContext):
    # Этот обработчик будет вызван после ввода даты партнёра
    pass

# Добавьте обработчик для ввода даты партнёра
@dp.message(state="waiting_for_partner_date")
async def process_partner_date(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите дату как `ДД.ММ.ГГГГ`", parse_mode="Markdown")
        return
    
    partner_date = message.text
    user_id = message.from_user.id
    await state.clear()
    
    await message.answer("📄 Генерирую ваш персональный отчёт с совместимостью...")
    pdf_buffer = await generate_pdf_report(user_id, partner_date)
    
    await message.answer_document(
        document=types.BufferedInputFile(pdf_buffer.getvalue(), filename=f"otchet_{message.from_user.id}.pdf"),
        caption=f"✨ **Ваш персональный отчёт с анализом совместимости готов!** ✨\n\n📅 Дата партнёра: {partner_date}\n\nБлагодарим за доверие 💕",
        reply_markup=menu_keyboard
    )
