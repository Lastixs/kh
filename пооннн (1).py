# khryak_bot.py
# -*- coding: utf-8 -*-

import asyncio
import random
from datetime import datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8369633990:AAFz9W9xw3R4jhXb884eKz1YAJM7ac-NWG0"
CRYPTO_TOKEN = "242332:AACIv79VCLWl0LV4vlrSQW2V9e0mMtXtyNJ"  # из @CryptoBot -> BotFather style token
CRYPTO_API_URL = "https://pay.crypt.bot/api/"
CHECK_INTERVAL = 12                      # как часто проверять счета (сек)
SHOP_PRICE_TON = 0.01                     # цена любого апгрейда в TON
OWNER_ID = 5747423404 #7510524298                # ваш Telegram user_id (админ без КД)

# ================== ИНИЦ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


import aiosqlite

async def init_db():
    async with aiosqlite.connect("khryak.db") as db:
        # Создаем таблицу pigs, если её нет
        await db.execute("""
        CREATE TABLE IF NOT EXISTS pigs (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT,
            weight REAL DEFAULT 10,
            strength INTEGER DEFAULT 10,
            coins INTEGER DEFAULT 0,
            last_train TEXT,
            last_farma TEXT,
            death_at TEXT,
            PRIMARY KEY(user_id, chat_id)
        )
        """)
        await db.commit()

        # Получаем список колонок
        async with db.execute("PRAGMA table_info(pigs)") as cur:
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]

        # Добавляем недостающие колонки, если они вдруг отсутствуют
        missing_columns = {
            "coins": "INTEGER DEFAULT 0",
            "last_train": "TEXT",
            "last_farma": "TEXT",
            "death_at": "TEXT"
        }

        for col, col_type in missing_columns.items():
            if col not in column_names:
                await db.execute(f"ALTER TABLE pigs ADD COLUMN {col} {col_type}")

        await db.commit()

        # Создаем таблицу payments, если её нет
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                invoice_id TEXT PRIMARY KEY,
                user_id    INTEGER,
                chat_id    INTEGER,
                type       TEXT,
                status     TEXT
            )
        """)
        await db.commit()




# ================== УТИЛИТЫ ==================
def can_use_cooldown(last_iso: str | None, user_id: int) -> bool:
    """
    True — можно использовать команду. Обрабатывает None и кривые даты.
    OWNER_ID — без КД.
    """
    if user_id == OWNER_ID:
        return True
    if not last_iso:
        return True
    try:
        last_time = datetime.fromisoformat(last_iso)
    except Exception:
        return True
    return datetime.now() - last_time >= timedelta(hours=24)


def fmt_name(user: types.User) -> str:
    return user.full_name or user.username or "Игрок"


# ================== CRYPTO PAY ==================
async def create_invoice(amount_ton: float, currency: str = "TON", description: str = "Покупка силы/веса"):
    """
    Создать инвойс в Crypto Pay. Возвращает (pay_url, invoice_id) или (None, None).
    """
    async with aiohttp.ClientSession() as session:
        headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
        payload = {
            "amount": amount_ton,
            "currency_type": "crypto",
            "asset": currency,
            "description": description,
        }
        async with session.post(CRYPTO_API_URL + "createInvoice", headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                return data["result"]["pay_url"], data["result"]["invoice_id"]
            else:
                print("Ошибка создания инвойса:", data)
                return None, None


async def check_invoices_loop(ensure_pig=None):
    """
    Фоновая проверка всех 'pending' инвойсов. Начисляет апгрейды после статуса 'paid'.
    """
    await asyncio.sleep(2)  # дать боту стартануть
    while True:
        try:
            async with aiosqlite.connect("khryak.db") as db, aiohttp.ClientSession() as session:
                async with db.execute("SELECT invoice_id, user_id, chat_id, type FROM payments WHERE status='pending'") as cur:
                    rows = await cur.fetchall()

                for invoice_id, user_id, chat_id, ptype in rows:
                    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
                    url = CRYPTO_API_URL + f"getInvoices?invoice_ids={invoice_id}"
                    async with session.get(url, headers=headers) as resp:
                        data = await resp.json()

                    if not data.get("ok"):
                        continue
                    items = data["result"].get("items", [])
                    if not items:
                        continue
                    status = items[0].get("status")  # active | paid | expired
                    if status == "paid":
                        # гарантируем, что запись о хряке есть
                        await ensure_pig(user_id, chat_id, "Игрок")

                        if ptype == "buy_strength":
                            await db.execute(
                                "UPDATE pigs SET strength = COALESCE(strength,0) + 25 WHERE user_id=? AND chat_id=?",
                                (user_id, chat_id),
                            )
                        elif ptype == "buy_weight":
                            await db.execute(
                                "UPDATE pigs SET weight = COALESCE(weight,0) + 50 WHERE user_id=? AND chat_id=?",
                                (user_id, chat_id),
                            )
                        await db.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))
                        await db.commit()
                        try:
                            await bot.send_message(user_id, "✅ Оплата получена! Товар начислен.")
                        except Exception as e:
                            print(f"Не смог отправить сообщение пользователю {user_id}: {e}")

                    elif status == "expired":
                        await aiosqlite.connect("khryak.db")
                        await db.execute("UPDATE payments SET status='expired' WHERE invoice_id=?", (invoice_id,))
                        await db.commit()

        except Exception as e:
            print("Ошибка в чекере инвойсов:", e)

        await asyncio.sleep(CHECK_INTERVAL)


async def send_welcome(chat_id: int, user_id: int, username: str):
    await ensure_pig(user_id, chat_id, username)
    text = (
        "🐷 *Добро пожаловать!*\n\n"
        "Команды:\n"
        "/sway — раз в 24 ч изменить вес и силу хряка (−1 - +3 кг и -1 - +3 силы)\n"
        "/fight — бой (/fight @username)\n"
        "/top — топ 10 хряков в этом чате\n"
        "/global — глобальный топ 10\n"
        "/shop — магазин (TON)\n"
        "/balance — баланс\n"
        "/heal — вылечить свинку\n"
        "/farma — фарм монет\n"
        "/help — помощь\n\n"
        "‼Перед использованием бота прочтите FAQ (можно вызвать командой /faq)"
    )
    await bot.send_message(chat_id, text, parse_mode="Markdown")

# /start только в ЛС
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await send_welcome(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.full_name)

# Приветствие при добавлении бота в группу
@router.message(lambda message: message.new_chat_members is not None)
async def on_new_chat_members(message: types.Message):
    for member in message.new_chat_members:
        if member.id == (await bot.get_me()).id:
            # Это добавление бота
            await send_welcome(message.chat.id, 0, "Игроки")



@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📚 Помощь:\n"
        "/sway — изменить вес и силу (раз в 24ч)\n"
        "/fight — бой хряков (/fight @username)\n"
        "/top — топ 10 по чату\n"
        "/global — глобальный топ 10\n"
        "/balance — баланс\n"
        "/heal — вылечить свинку\n"
        "/farma — фарм монет\n"
        "/shop — магазин улучшений за TON\n\n"
    )

async def ensure_pig(user_id: int, chat_id: int, username: str):
    async with aiosqlite.connect("khryak.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO pigs (user_id, chat_id, username) VALUES (?, ?, ?)",
            (user_id, chat_id, username)
        )
        await db.commit()


def can_use_cooldown(last_time, hours=24):
    if not last_time:
        return True
    return datetime.now() - datetime.fromisoformat(last_time) >= timedelta(hours=hours)


def pig_status(weight, strength):
    coef = strength / max(weight, 1)

    if coef < 0.5:
        return "starving", "⚠️ Истощение — свинка слишком худая."
    elif 0.5 <= coef < 1:
        return "underweight", "🍽 Недобор — свинка слегка худая, стоит подкормить."
    elif 0.95 <= coef <= 1.05:
        return "ideal", "💎 Идеал — баланс веса и силы, свинка в отличной форме."
    elif 1.05 < coef <= 1.8:
        return "good", "🙂 Хорошо — свинка в норме, но уже тяжелеет."
    else:  # coef > 1.8
        return "obese", "⚠️ Ожирение — силы слишком много по сравнению с весом."


def fmt_name(user: types.User):
    return f"@{user.username}" if user.username else user.full_name


# =========================
# /faq
# =========================
@router.message(Command("faq"))
async def cmd_faq(message: types.Message):
    faq_url = "https://telegra.ph/FAQ-Khryak-bot-08-14"
    text = (
        "📜 <b>Инструкция по игре</b>\n\n"
        "Здесь вы найдёте правила, описание команд и советы по уходу за свинкой:\n"
        f"<a href='{faq_url}'>🔗 Открыть инструкцию</a>"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=False)


# =========================
# /sway (тренировка)
# =========================
@router.message(Command("sway"))
async def cmd_sway(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Команда /sway доступна только в групповых чатах.")

    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username or message.from_user.full_name

    await ensure_pig(user_id, chat_id, username)

    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute(
            "SELECT weight, strength, last_train, death_at FROM pigs WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            weight, strength, last_train, death_at = 1, 1, None, None
        else:
            weight, strength, last_train, death_at = row
            weight = weight or 1
            strength = strength or 1

        if user_id != OWNER_ID and not can_use_cooldown(last_train, hours=24):
            return await message.answer("⏳ Команду можно использовать раз в 24 часа.")

        weight_change = random.randint(-1, 3)
        strength_change = random.choice([-1, 2])
        new_weight = max(1, weight + weight_change)
        new_strength = max(1, strength + strength_change)

        status_code, status_text = pig_status(new_weight, new_strength)
        kb = None

        if status_code in ("obese", "starving"):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💊 Лечить свинку (1000💰)", callback_data=f"heal:{user_id}:{chat_id}")]
                ]
            )
            now = datetime.now()
            if not death_at or (death_at and datetime.fromisoformat(death_at) <= now):
                death_date = now + timedelta(days=2)
                await db.execute(
                    "UPDATE pigs SET death_at=? WHERE user_id=? AND chat_id=?",
                    (death_date.isoformat(), user_id, chat_id)
                )

        await db.execute(
            "UPDATE pigs SET weight=?, strength=?, last_train=? WHERE user_id=? AND chat_id=?",
            (new_weight, new_strength, datetime.now().isoformat(), user_id, chat_id)
        )
        await db.commit()

    weight_diff = new_weight - weight
    strength_diff = new_strength - strength
    weight_sign = f"+{weight_diff}" if weight_diff >= 0 else f"{weight_diff}"
    strength_sign = f"+{strength_diff}" if strength_diff >= 0 else f"{strength_diff}"

    await message.answer(
        f"🏋️ {fmt_name(message.from_user)}, тренировка завершена!\n"
        f"⚖️ Вес: {weight} → {new_weight} ({weight_sign})\n"
        f"💪 Сила: {strength} → {new_strength} ({strength_sign})\n"
        f"{status_text}",
        reply_markup=kb
    )


# =========================
# Callback — лечение
# =========================
@router.callback_query(lambda c: c.data.startswith("heal:"))
async def heal_pig_logic(callback: types.CallbackQuery):
    try:
        _, user_id_str, chat_id_str = callback.data.split(":")
        user_id = int(user_id_str)
        chat_id = int(chat_id_str)
    except Exception:
        return await callback.answer("❌ Ошибка данных кнопки", show_alert=True)

    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute("SELECT weight, strength, coins FROM pigs WHERE user_id=? AND chat_id=?", (user_id, chat_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await callback.answer("❌ Свинка не найдена.", show_alert=True)
        weight, strength, coins = row

        if coins < 1000:
            return await callback.answer("💰 Недостаточно монет!", show_alert=True)

        coef = 0.5
        max_strength = max(1, int(weight * coef))
        min_weight = max(1, int(strength / coef))

        if strength > max_strength:
            strength = max_strength
        if weight < min_weight:
            weight = min_weight
        elif weight > strength / coef:
            weight = int(strength / coef)

        await db.execute(
            "UPDATE pigs SET coins=coins-1000, weight=?, strength=?, death_at=NULL WHERE user_id=? AND chat_id=?",
            (weight, strength, user_id, chat_id)
        )
        await db.commit()

    await callback.answer("💊 Свинка вылечена! 🐷", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)


# =========================
# /farma
# =========================
@router.message(Command("farma"))
async def cmd_farma(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username or message.from_user.full_name
    await ensure_pig(user_id, chat_id, username)

    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute("SELECT coins, last_farma FROM pigs WHERE user_id=? AND chat_id=?", (user_id, chat_id)) as cur:
            coins, last_farma = await cur.fetchone()
        if last_farma and not can_use_cooldown(last_farma, hours=4):
            return await message.answer("⏳ Ферма доступна раз в 4 часа.")

        reward = random.randint(100, 300)
        await db.execute("UPDATE pigs SET coins=?, last_farma=? WHERE user_id=? AND chat_id=?",
                         (coins + reward, datetime.now().isoformat(), user_id, chat_id))
        await db.commit()

    await message.answer(f"🌾 Вы поработали на ферме и получили {reward} монет! 💰 Баланс: {coins + reward}")


# =========================
# /balance
# =========================
@router.message(Command("balance"))
async def cmd_balance(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute("SELECT coins FROM pigs WHERE user_id=? AND chat_id=?", (user_id, chat_id)) as cur:
            row = await cur.fetchone()
            if row is None:
                return await message.answer("❌ У вас нет свинки. Создайте новую!")
            coins = row[0]
    await message.answer(f"💰 Ваш баланс: {coins} монет")


# =========================
# /givecoins
# =========================
@router.message(Command("givecoins"))
async def give_coins(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("❌ Только владелец бота может выдавать монеты.")

    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Использование: /givecoins @username количество")

    username = args[1].lstrip("@")
    try:
        amount = int(args[2])
    except ValueError:
        return await message.answer("Количество монет должно быть числом.")

    if amount <= 0:
        return await message.answer("Количество монет должно быть больше нуля.")

    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute(
            "SELECT user_id, coins FROM pigs WHERE username=?",
            (username,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            return await message.answer(f"Пользователь @{username} не найден в базе.")

        user_id, coins = row
        coins = coins or 0
        new_coins = coins + amount

        await db.execute(
            "UPDATE pigs SET coins=? WHERE user_id=?",
            (new_coins, user_id)
        )
        await db.commit()

    await message.answer(f"✅ Пользователю @{username} выдано {amount}💰. Теперь у него {new_coins}💰.")






# Словарь активных боёв {chat_id: {...}}
battles = {}

# =========================
async def ensure_user(db, user: types.User, chat_id: int):
    """Проверяем, есть ли пользователь в базе, если нет — создаём"""
    async with db.execute("SELECT 1 FROM pigs WHERE user_id=? AND chat_id=?", (user.id, chat_id)) as cur:
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO pigs (user_id, chat_id, first_name, strength, wins, losses) VALUES (?, ?, ?, ?, ?, ?)",
                (user.id, chat_id, user.first_name, 1, 0, 0)
            )
            await db.commit()

# =========================
def format_hp(battle):
    return (f"{battle['attacker'].first_name}: {battle['hp'][battle['attacker'].id]} HP\n"
            f"{battle['defender'].first_name}: {battle['hp'][battle['defender'].id]} HP")

@router.message(Command("fight"))
async def cmd_fight(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Команда /fight доступна только в групповых чатах.")

    attacker = message.from_user
    chat_id = message.chat.id

    # Проверяем, что пользователь ответил на сообщение
    if not message.reply_to_message:
        return await message.answer("⚠️ Вы должны ответить на сообщение пользователя, с которым хотите сразиться!")

    defender = message.reply_to_message.from_user

    async with aiosqlite.connect("khryak.db") as db:
        await ensure_user(db, attacker, chat_id)
        await ensure_user(db, defender, chat_id)

    if attacker.id == defender.id:
        return await message.answer("Нельзя сражаться с собой!")

    if chat_id in battles:
        return await message.answer("⚔️ В этом чате уже идёт бой!")

    battles[chat_id] = {
        "attacker": attacker,
        "defender": defender,
        "state": "waiting"
    }

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять бой", callback_data=f"fight_accept:{attacker.id}:{defender.id}")
    kb.button(text="❌ Отказаться", callback_data=f"fight_decline:{attacker.id}:{defender.id}")
    kb.adjust(2)

    await message.answer(
        f"Бой предложен! <a href='tg://user?id={attacker.id}'>{attacker.first_name}</a> против "
        f"<a href='tg://user?id={defender.id}'>{defender.first_name}</a>.\nХодит атакующий.",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


# =========================
@router.callback_query(lambda c: c.data.startswith("fight_"))
async def fight_handler(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if chat_id not in battles:
        return await cb.answer("❌ Бой не найден", show_alert=True)

    battle = battles[chat_id]

    # =========================
    # Принятие или отклонение боя
    if cb.data.startswith(("fight_accept", "fight_decline")):
        parts = cb.data.split(":")
        if len(parts) != 3:
            return await cb.answer("❌ Некорректные данные", show_alert=True)
        action, att_id, def_id = parts
        att_id, def_id = int(att_id), int(def_id)
        if cb.from_user.id != def_id:
            return await cb.answer("Это не ваша кнопка!", show_alert=True)
        if action == "fight_decline":
            del battles[chat_id]
            return await cb.message.edit_text("❌ Бой отклонён.", reply_markup=None)
        if action == "fight_accept":
            battles[chat_id].update({
                "state": "fighting",
                "hp": {att_id: 100, def_id: 100},
                "turn": att_id
            })
            kb = InlineKeyboardBuilder()
            kb.button(text="⚔️ Атаковать", callback_data="fight_attack")
            await cb.message.edit_text(
                f"🥊 Бой начался!\n{battle['attacker'].first_name} против {battle['defender'].first_name}\n"
                f"Ходит <a href='tg://user?id={battle['attacker'].id}'>{battle['attacker'].first_name}</a>.",
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )
        return

    # =========================
    # Ход атаки
    if cb.data == "fight_attack":
        user_id = cb.from_user.id
        if battle["state"] != "fighting":
            return await cb.answer("Бой ещё не начался", show_alert=True)
        if user_id != battle["turn"]:
            return await cb.answer("Сейчас не ваш ход!", show_alert=True)

        async with aiosqlite.connect("khryak.db") as db:
            async with db.execute(
                    "SELECT strength FROM pigs WHERE user_id=? AND chat_id=?",
                    (user_id, chat_id)
            ) as cur:
                row = await cur.fetchone()
                strength = row[0] if row else 1

        damage = random.randint(5, 15) + strength

        target_user = battle["defender"] if user_id == battle["attacker"].id else battle["attacker"]
        battle["hp"][target_user.id] -= damage
        battle["hp"][target_user.id] = max(0, battle["hp"][target_user.id])

        text = f"⚔️ {cb.from_user.first_name} наносит {damage} урона <a href='tg://user?id={target_user.id}'>{target_user.first_name}</a>!\n\n"
        text += format_hp(battle)

        if battle["hp"][target_user.id] <= 0:
            winner = cb.from_user
            loser = target_user
            text += f"\n\n🏆 Победитель: {winner.first_name}!"
            async with aiosqlite.connect("khryak.db") as db:
                await db.execute(
                    "UPDATE pigs SET wins = wins + 1, strength = strength + 1 WHERE user_id=? AND chat_id=?",
                    (winner.id, chat_id)
                )
                await db.execute(
                    """
                    UPDATE pigs
                    SET losses = losses + 1,
                        strength = CASE WHEN strength > 1 THEN strength - 1 ELSE 1 END
                    WHERE user_id=? AND chat_id=?
                    """,
                    (loser.id, chat_id)
                )
                await db.commit()
            del battles[chat_id]
            return await cb.message.edit_text(text, reply_markup=None, parse_mode="HTML")

        battle["turn"] = target_user.id
        kb = InlineKeyboardBuilder()
        kb.button(text="⚔️ Атаковать", callback_data="fight_attack")
        await cb.message.edit_text(
            text + f"\n\nХодит <a href='tg://user?id={target_user.id}'>{target_user.first_name}</a>.",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )

# =========================
# /heal (команда)
# =========================
@router.message(Command("heal"))
async def cmd_heal(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute("SELECT weight, strength, coins FROM pigs WHERE user_id=? AND chat_id=?", (user_id, chat_id)) as cur:
            row = await cur.fetchone()
        if not row:
            return await message.answer("❌ У вас нет свинки.")
        weight, strength, coins = row
        if coins < 1000:
            return await message.answer("💰 Недостаточно монет для лечения!")

        coef = 0.5
        max_strength = max(1, int(weight * coef))
        min_weight = max(1, int(strength / coef))

        if strength > max_strength:
            strength = max_strength
        if weight < min_weight:
            weight = min_weight
        elif weight > strength / coef:
            weight = int(strength / coef)

        await db.execute(
            "UPDATE pigs SET coins=coins-1000, weight=?, strength=?, death_at=NULL WHERE user_id=? AND chat_id=?",
            (weight, strength, user_id, chat_id)
        )
        await db.commit()

    await message.answer("💊 Ваша свинка вылечена! 🐷")


# =========================
# Новая свинка
# =========================
@router.callback_query(lambda c: c.data.startswith("new_pig:"))
async def new_pig(callback: types.CallbackQuery):
    try:
        _, uid_str = callback.data.split(":")
        uid = int(uid_str)
    except:
        return await callback.answer("❌ Ошибка данных", show_alert=True)

    user_id = callback.from_user.id
    if user_id != uid:
        return await callback.answer("❌ Это не ваша свинка!", show_alert=True)

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute(
            "UPDATE pigs SET weight=10, strength=10, coins=0, death_at=NULL WHERE user_id=?",
            (uid,)
        )
        await db.commit()

    await callback.message.edit_reply_markup(None)
    await callback.answer("✅ Вы завели новую свинку! 🐷", show_alert=True)


async def check_pig_life(user_id: int, chat_id: int, bot: Bot):
    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute(
            "SELECT death_at, coins FROM pigs WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return

    death_at, coins = row
    if not death_at:
        return

    now = datetime.now()
    death_time = datetime.fromisoformat(death_at)

    # 🟡 Напоминание: остался 1 день
    if 0 < (death_time - now).total_seconds() <= 86400:  # 1 день
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    "💊 Вылечить свинку (1000💰)",
                    callback_data=f"heal:{user_id}:{chat_id}"
                )
            ]]
        )
        await bot.send_message(
            chat_id,
            f"⚠️ <a href='tg://user?id={user_id}'>Ваша свинка</a> умрёт через 1 день!",
            parse_mode="HTML",
            reply_markup=kb
        )

    # 🔴 Смерть свинки
    elif now >= death_time:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    "🐖 Завести новую свинку",
                    callback_data=f"new_pig:{user_id}"
                )
            ]]
        )
        await bot.send_message(
            chat_id,
            f"💀 <a href='tg://user?id={user_id}'>Ваша свинка умерла!</a>",
            parse_mode="HTML",
            reply_markup=kb
        )


@router.message(Command("top"))
async def cmd_top_chat(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Команда /top доступна только в групповых чатах.")

    chat_id = message.chat.id
    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute(
            """
            SELECT user_id,
                   username,
                   COALESCE(weight,0),
                   COALESCE(strength,0),
                   COALESCE(wins,0),
                   COALESCE(losses,0)
            FROM pigs
            WHERE chat_id=?
            ORDER BY weight DESC, strength DESC, wins DESC
            LIMIT 10
            """,
            (chat_id,),
        ) as cur:
            pigs = await cur.fetchall()

    if not pigs:
        return await message.answer("Нет данных для топа в этом чате.")

    lines = ["🏆 *Топ 10 хряков чата:*\n"]
    for i, (uid, uname, weight, strength, wins, losses) in enumerate(pigs, 1):
        try:
            member = await bot.get_chat_member(chat_id, uid)
            display_name = member.user.username or member.user.full_name or "Игрок"
        except Exception:
            display_name = uname or "Игрок"

        name_link = f"[{display_name}](tg://user?id={uid})"
        lines.append(
            f"{i}. {name_link}\n"
            f"   Вес: {float(weight):.2f} кг | Сила: {int(strength)} | "
            f"Победы: {int(wins)} | Поражения: {int(losses)}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("global"))
async def cmd_top_global(message: types.Message):
    async with aiosqlite.connect("khryak.db") as db:
        async with db.execute(
            """
            SELECT user_id,
                   username,
                   COALESCE(weight,0),
                   COALESCE(strength,0),
                   COALESCE(wins,0),
                   COALESCE(losses,0)
            FROM pigs
            ORDER BY weight DESC, strength DESC, wins DESC
            LIMIT 10
            """
        ) as cur:
            pigs = await cur.fetchall()

    if not pigs:
        return await message.answer("Нет данных для глобального топа.")

    lines = ["🌍 *Глобальный топ 10 хряков:*\n"]
    for i, (uid, uname, weight, strength, wins, losses) in enumerate(pigs, 1):
        try:
            user = await bot.get_chat(uid)  # для глобального — просто инфо о юзере
            display_name = user.username or user.full_name or "Игрок"
        except Exception:
            display_name = uname or "Игрок"

        name_link = f"[{display_name}](tg://user?id={uid})"
        lines.append(
            f"{i}. {name_link}\n"
            f"   Вес: {float(weight):.2f} кг | Сила: {int(strength)} | "
            f"Победы: {int(wins)} | Поражения: {int(losses)}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")



@router.message(Command("shop"))
async def cmd_shop(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("Команда /shop доступна только в групповых чатах.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💪 +25 силы — {SHOP_PRICE_TON} TON", callback_data="buy_strength")],
        [InlineKeyboardButton(text=f"⚖️ +50 кг веса — {SHOP_PRICE_TON} TON", callback_data="buy_weight")],
    ])
    await message.answer("🏪 *Магазин:* выбери товар", reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.in_({"buy_strength", "buy_weight"}))
async def cb_buy(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    item = callback.data  # 'buy_strength' | 'buy_weight'

    pay_url, invoice_id = await create_invoice(SHOP_PRICE_TON, description=f"Покупка {item}")
    if not pay_url:
        await callback.message.answer("❌ Не удалось создать счёт, попробуйте позже.")
        return await callback.answer()

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments (invoice_id, user_id, chat_id, type, status) VALUES (?, ?, ?, ?, 'pending')",
            (invoice_id, user_id, chat_id, item),
        )
        await db.commit()

    await callback.message.answer(f"💳 Оплатите по ссылке:\n{pay_url}\nПосле оплаты товар начислится автоматически.")
    await callback.answer()


# ================== АДМИН-КОМАНДЫ ==================
def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID


@router.message(Command("add_weight"))
async def cmd_add_weight(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ У вас нет прав.")

    args = (command.args or "").split()
    if len(args) != 2:
        return await message.answer("Использование: /add_weight user_id value")

    try:
        uid = int(args[0])
        value = float(args[1])
    except ValueError:
        return await message.answer("user_id должен быть целым, value — числом.")

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute("UPDATE pigs SET weight = COALESCE(weight,0) + ? WHERE user_id = ?", (value, uid))
        await db.commit()
    await message.answer(f"✅ Вес пользователя {uid} увеличен на {value} кг.")


@router.message(Command("remove_weight"))
async def cmd_remove_weight(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ У вас нет прав.")

    args = (command.args or "").split()
    if len(args) != 2:
        return await message.answer("Использование: /remove_weight user_id value")

    try:
        uid = int(args[0])
        value = float(args[1])
    except ValueError:
        return await message.answer("user_id должен быть целым, value — числом.")

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute("UPDATE pigs SET weight = COALESCE(weight,0) - ? WHERE user_id = ?", (value, uid))
        await db.commit()
    await message.answer(f"✅ Вес пользователя {uid} уменьшен на {value} кг.")


@router.message(Command("add_strength"))
async def cmd_add_strength(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ У вас нет прав.")

    args = (command.args or "").split()
    if len(args) != 2:
        return await message.answer("Использование: /add_strength user_id value")

    try:
        uid = int(args[0])
        value = float(args[1])
    except ValueError:
        return await message.answer("user_id должен быть целым, value — числом.")

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute("UPDATE pigs SET strength = COALESCE(strength,0) + ? WHERE user_id = ?", (value, uid))
        await db.commit()
    await message.answer(f"✅ Сила пользователя {uid} увеличена на {value}.")


@router.message(Command("remove_strength"))
async def cmd_remove_strength(message: types.Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ У вас нет прав.")

    args = (command.args or "").split()
    if len(args) != 2:
        return await message.answer("Использование: /remove_strength user_id value")

    try:
        uid = int(args[0])
        value = float(args[1])
    except ValueError:
        return await message.answer("user_id должен быть целым, value — числом.")

    async with aiosqlite.connect("khryak.db") as db:
        await db.execute("UPDATE pigs SET strength = COALESCE(strength,0) - ? WHERE user_id = ?", (value, uid))
        await db.commit()
    await message.answer(f"✅ Сила пользователя {uid} уменьшена на {value}.")


@router.message(Command("reset_all"))
async def cmd_reset_all(message: types.Message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        await message.reply("❌ У вас нет прав для этой команды.")
        return

    try:
        async with aiosqlite.connect("khryak.db") as db:
            await db.execute("DELETE FROM pigs")
            await db.execute("DELETE FROM payments")
            await db.commit()

        await message.reply("✅ Все данные сброшены! Таблицы очищены.")
    except Exception as e:
        await message.reply(f"❌ Ошибка при сбросе: {e}")
        print("Ошибка сброса базы:", e)


# ================== ЗАПУСК ==================
async def main():
    await init_db()
    # запуск бота
    asyncio.create_task(check_invoices_loop(ensure_pig=ensure_pig))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
