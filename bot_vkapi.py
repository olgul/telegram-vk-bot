# -*- coding: utf-8 -*-

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import requests
import sqlite3
import time

# ========== КОНФИГУРАЦИЯ ==========
import os
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    print("❌ Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

SMMLABA_SERVICE_CODE = "vklikebest3"
SMMLABA_API_URL = "https://smmlaba.com/vkapi/v1/"
SMMLABA_COUNT = 23

VK_API_VERSION = "5.131"
DB_PATH = "vk_posts.db"


# ========== БАЗА ДАННЫХ ==========

def init_database():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Таблица ВК-аккаунтов
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS vk_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        vk_input TEXT NOT NULL,                  -- что пользователь ввёл (id123, shortname, club123)
        owner_id INTEGER NOT NULL,               -- owner_id для VK API (user >0, group <0)
        vk_token TEXT NOT NULL,                  -- личный токен для этого аккаунта
        page_name TEXT DEFAULT 'Неименованная',
        last_post_url TEXT,
        last_post_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, vk_input)
    )
    """)

    # Таблица учётных данных smmlaba
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_smmlaba_credentials (
        user_id INTEGER PRIMARY KEY,
        email TEXT NOT NULL,
        api_key TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


# ========== VK API ФУНКЦИИ ==========

def vk_api_call(method: str, params: dict, access_token: str):
    """
    Универсальный вызов VK API.

    Args:
        method: название метода (wall.get, utils.resolveScreenName и т.д.)
        params: параметры запроса
        access_token: пользовательский токен VK (vk1.a...)

    Returns:
        (response_data, error_dict) или (None, error_dict)
    """
    url = f"https://api.vk.com/method/{method}"

    p = dict(params)
    p["access_token"] = access_token
    p["v"] = VK_API_VERSION

    try:
        r = requests.get(url, params=p, timeout=10)
        r.encoding = "utf-8"
        data = r.json()

        if "error" in data:
            return None, data["error"]

        return data.get("response"), None
    except Exception as e:
        return None, {"error_msg": str(e)}


def resolve_owner_id(vk_input: str, access_token: str):
     """
     Превращаем ввод пользователя в owner_id для VK API.

     Примеры:
         id123456789 -> 123456789 (user, >0)
         club123 -> -123 (group, <0)
         shortname -> resolveScreenName -> ±number

     Args:
         vk_input: что ввёл пользователь
         access_token: токен VK для запроса resolveScreenName

     Returns:
         (owner_id, error_msg) или (None, error_msg)
    """
     vk_input = vk_input.strip().lower()

     # Формат: id123456789
     if vk_input.startswith("id") and vk_input[2:].isdigit():
         return int(vk_input[2:]), None

     # Формат: club123 или public123
     if vk_input.startswith("club") and vk_input[4:].isdigit():
         return -int(vk_input[4:]), None
     if vk_input.startswith("public") and vk_input[6:].isdigit():
         return -int(vk_input[6:]), None

     # Иначе пробуем как screen_name (shortname)
     resp, err = vk_api_call("utils.resolveScreenName", {"screen_name": vk_input}, access_token)
     if err:
         return None, err.get("error_msg", "Ошибка при разрешении shortname")

     if not resp:
         return None, "Не удалось распознать ID/shortname"

     obj_type = resp.get("type")
     obj_id = resp.get("object_id")

     if obj_type == "user":
         return int(obj_id), None
     elif obj_type in ("group", "page"):
         return -int(obj_id), None
     else:
         return None, f"Неизвестный тип объекта: {obj_type}"


def get_last_vk_post(owner_id: int, access_token: str):
    """
    Возвращает: post_url, post_id, skip_send, error
    skip_send=True если репостов >=1 (не отправлять в smmlaba)
    """
    resp, err = vk_api_call(
        "wall.get",
        {
            "owner_id": owner_id,
            "count": 10,
            "filter": "owner",
        },
        access_token
    )

    if err:
        return None, None, False, err.get("error_msg", "Ошибка VK API")

    if not isinstance(resp, dict):
        return None, None, False, "Неожиданный формат ответа VK API"

    items = resp.get("items", [])
    if not items:
        return None, None, False, None  # постов нет

    # Ищем первый нормальный пост (не закреп, не реклама)
    chosen_post = None
    for post in items:
        if post.get("is_pinned") == 1:
            continue
        if post.get("marked_as_ads") == 1:
            continue
        chosen_post = post
        break

    if chosen_post is None:
        chosen_post = items[0]

    post_id = chosen_post.get("id")
    if not post_id:
        return None, None, False, "Нет ID поста"

    post_url = f"https://vk.com/wall{owner_id}_{post_id}"

    # Проверяем репосты
    reposts_info = chosen_post.get("reposts", {}) or {}
    reposts_count = reposts_info.get("count", 0) or 0
    skip_send = reposts_count >= 1  # если репостов 1+ — не отправляем

    return post_url, str(post_id), skip_send, None


# ========== SMMLABA API ФУНКЦИИ ==========
def smmlaba_request(data: dict):
    """
    Универсальная функция для запросов к SMMLaba.
    Возвращает (json_dict, None) или (None, текст_ошибки).
    """

    # Заголовки для запроса.
    # Accept просит сервер отвечать JSON (если он умеет).
    # User-Agent часто помогает, чтобы сервер не считал запрос "ботом".
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (TelegramBot; +https://t.me/)"
    }

    try:
        # Отправляем POST-запрос на SMMLaba.
        # data=... означает "отправить как form-urlencoded" (обычный формат для SMM API).
        r = requests.post(SMMLABA_API_URL, data=data, headers=headers, timeout=15)

        # Берем ответ как текст, чтобы в случае ошибки показать первые символы.
        text = (r.text or "").strip()

        # Если сервер вернул не 200 — это уже проблема.
        # Часто тут бывает 403/404/502 и вместо JSON приходит HTML.
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}. Ответ: {text[:250]}"

        # Пробуем разобрать ответ как JSON.
        try:
            return r.json(), None
        except ValueError:
            # Если это не JSON — вернём кусок ответа, чтобы понять, что пришло.
            return None, f"Ответ API не JSON. Ответ: {text[:250]}"

    except Exception as e:
        # Любая сетевая ошибка: нет интернета, таймаут, DNS и т.д.
        return None, f"Ошибка соединения: {e}"




def check_smmlaba_balance(email: str, api_key: str):
    """
    Проверяет баланс на smmlaba по их API-инструкции.
    Возвращает (balance, None) или (None, error_msg).
    """

    # Готовим данные для POST-запроса.
    # username — email пользователя в smmlaba
    # apikey  — ключ API из личного кабинета
    # action  — какую функцию вызываем (balance)
    data = {
        "username": email,
        "apikey": api_key,
        "action": "balance",
    }

    # Заголовки. Accept просит JSON, User-Agent делает запрос "похожим на браузер".
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        # Отправляем POST на правильный URL smmlaba.
        r = requests.post(SMMLABA_API_URL, data=data, headers=headers, timeout=15)
        r.encoding = "utf-8"

        # Если сервер вернул не 200 — это ошибка уровня HTTP.
        if r.status_code != 200:
            return None, f"HTTP ошибка: {r.status_code}. Ответ: {(r.text or '')[:200]}"

        # Пытаемся распарсить JSON.
        try:
            result = r.json()
        except ValueError:
            return None, f"Ответ API не JSON. Ответ: {(r.text or '')[:200]}"

        # По инструкции: result = success/error
        if result.get("result") != "success":
            return None, result.get("error", "Неизвестная ошибка API")

        # При success полезные данные лежат в поле message
        message = result.get("message", {})

        # В message для balance должно быть поле balance
        try:
            balance = float(message.get("balance", 0))
            return balance, None
        except (TypeError, ValueError):
            return None, f"Не удалось прочитать balance из ответа: {message}"

    except Exception as e:
        return None, f"Ошибка запроса: {e}"

def send_to_smmlaba(post_url: str, email: str, api_key: str):
    """
    Создаёт заказ на smmlaba по их API-инструкции.
    Возвращает (True, message) или (False, error_msg).
    """

    data = {
        "username": email,
        "apikey": api_key,
        "action": "add",
        "service": SMMLABA_SERVICE_CODE,
        "url": post_url,
        "count": SMMLABA_COUNT,
    }

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        r = requests.post(SMMLABA_API_URL, data=data, headers=headers, timeout=15)
        r.encoding = "utf-8"

        if r.status_code != 200:
            return False, f"HTTP ошибка: {r.status_code}. Ответ: {(r.text or '')[:200]}"

        try:
            result = r.json()
        except ValueError:
            return False, f"Ответ API не JSON. Ответ: {(r.text or '')[:200]}"

        if result.get("result") == "success":
            return True, result.get("message", "Заказ принят")

        return False, result.get("error", "Неизвестная ошибка API")

    except Exception as e:
        return False, f"Ошибка запроса: {e}"

# ========== КЛАВИАТУРЫ ==========

def get_main_menu_keyboard():
    """Главное меню"""
    keyboard = [
        ["✅ Проверить все посты", "📋 Мои аккаунты"],
        ["⚙️ Настройка", "📚 Справка"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_settings_menu_keyboard():
    """Меню настроек"""
    keyboard = [
        ["🔐 Smmlaba", "📱 Добавить ВК токен"],
        ["💰 Баланс", "🗑️ Удалить аккаунт"],  # ← Добавляем новую кнопку
        ["🏠 Назад"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ========== TELEGRAM КОМАНДЫ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и главное меню"""
    user_name = update.effective_user.first_name
    text = (
        f"👋 Привет, {user_name}!\n\n"
        f"Я бот для отслеживания постов ВК через официальный VK API и автозагрузки на smmlaba 🚀\n\n"
        f"⚡ Возможности:\n"
        f"✅ Поддержка до 10 ВК-аккаунтов\n"
        f"✅ Стабильная работа через VK API\n"
        f"✅ Безопасность: токены в БД\n"
        f"✅ Автоматическая загрузка на smmlaba\n\n"
        f"📖 Начните с /help для справки"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справка по командам и как получить токен"""
    text = (
        "📚 СПРАВКА\n\n"
        "1️⃣ УЧЁТНЫЕ ДАННЫЕ SMMLABA (обязательно!):\n"
        "/set_smmlaba EMAIL API_KEY\n"
        "Пример: /set_smmlaba test@example.com abc123xyz\n\n"
        "2️⃣ ДОБАВИТЬ ВК АККАУНТ:\n"
        "/add_vk VK_ID VK_TOKEN\n"
        "Пример: /add_vk id123456789 vk1.a...\n"
        "Максимум: 10 аккаунтов на пользователя\n\n"
        "3️⃣ ПРОВЕРИТЬ НОВЫЕ ПОСТЫ:\n"
        "/check\n"
        "Проверяет все добавленные аккаунты и загружает новые посты\n\n"
        "4️⃣ ПОКАЗАТЬ СПИСОК АККАУНТОВ:\n"
        "/list\n\n"
        "🔐 КАК ПОЛУЧИТЬ USER TOKEN VK:\n"
        "1. Откройте URL: https://oauth.vk.com/authorize?client_id=2685278&scope=wall,groups,offline&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token&v=5.131\n"
        "2. Нажмите 'Разрешить'\n"
        "3. Скопируйте всю адресную строку появившейсся страницы\n"
        "4. Вставте скопированную строку в команду /add_vk. Пример: /add_vk ID_VK Скопированная_адресная_строка\n\n"
        "⚠️ ВАЖНО:\n"
        "• Токен — это секрет, не публикуйте его\n"
        "• Бот автоматически удаляет сообщение с токеном"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())


async def set_smmlaba_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет учётные данные smmlaba"""
    user_id = update.effective_user.id

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Неправильный формат!\n"
            "Используйте: /set_smmlaba EMAIL API_KEY\n"
            "Пример: /set_smmlaba test@example.com abc123xyz"
        )
        return

    email = context.args[0].strip()
    api_key = context.args[1].strip()

    msg = await update.message.reply_text("⏳ Проверяю учётные данные...")

    # Проверяем данные через API smmlaba
    balance, error = check_smmlaba_balance(email, api_key)
    if error:
        await msg.edit_text(f"❌ Ошибка при проверке:\n{error}\n\nУбедитесь, что email и API ключ верны.")
        return

    # Сохраняем в БД
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM user_smmlaba_credentials WHERE user_id=?", (user_id,))
    exists = cursor.fetchone()

    if exists:
        cursor.execute(
            "UPDATE user_smmlaba_credentials SET email=?, api_key=? WHERE user_id=?",
            (email, api_key, user_id)
        )
    else:
        cursor.execute(
            "INSERT INTO user_smmlaba_credentials (user_id, email, api_key) VALUES (?, ?, ?)",
            (user_id, email, api_key)
        )

    conn.commit()
    conn.close()

    await msg.edit_text(
        f"✅ Учётные данные smmlaba сохранены!\n\n"
        f"📧 Email: {email}\n"
        f"💰 Баланс: {balance} руб.\n\n"
        f"Теперь используйте: /add_vk VK_ID VK_TOKEN"
    )


async def show_smmlaba_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий баланс и учётные данные"""
    user_id = update.effective_user.id

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT email, api_key FROM user_smmlaba_credentials WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(
            "❌ Вы не сохранили учётные данные smmlaba!\n"
            "Используйте: /set_smmlaba EMAIL API_KEY"
        )
        return

    email, api_key = row
    msg = await update.message.reply_text("⏳ Получаю информацию о балансе...")

    balance, error = check_smmlaba_balance(email, api_key)

    if error:
        await msg.edit_text(f"❌ Ошибка: {error}")
    else:
        status = "✅ Достаточно средств" if balance > 0 else "⚠️ Баланс исчерпан"
        await msg.edit_text(
            f"📊 Информация smmlaba:\n\n"
            f"📧 Email: {email}\n"
            f"💰 Баланс: {balance} руб.\n"
            f"{status}"
        )


async def add_vk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Добавляет ВК-аккаунт пользователя (максимум 10).

    Формат теперь такой:
    /add_vk VK_ID ПОЛНАЯ_ССЫЛКА_ИЗ_БРАУЗЕРА

    Пример:
    /add_vk id123456789 https://oauth.vk.com/blank.html#access_token=vk1.a...

    Бот сам вытащит access_token=... из строки.
    """
    user_id = update.effective_user.id

    # 1. Проверяем, что передано хотя бы 2 аргумента
    if len(context.args) < 2:
        help_text = (
            "❌ Неправильный формат!\n\n"
            "Теперь команда выглядит так:\n"
            "/add_vk VK_ID ПОЛНАЯ_ССЫЛКА_ИЗ_АДРЕСНОЙ_СТРОКИ\n\n"
            "🔹 VK_ID — это ID или короткое имя страницы/группы ВК:\n"
            "   • id123456789 — для личной страницы\n"
            "   • club123456789 — для группы\n\n"
            "🔹 ПОЛНАЯ_ССЫЛКА — это адрес из браузера после выдачи токена.\n"
            "Как получить ссылку:\n"
            "1) Откройте ссылку из /help в браузере.\n"
            "2) Нажмите «Разрешить».\n"
            "3) Скопируйте ВСЮ строку из адресной строки браузера\n"
            "   (она начинается с https://oauth.vk.com/blank.html#access_token=...)\n\n"
            "Пример команды:\n"
            "/add_vk id123456789 https://oauth.vk.com/blank.html#access_token=vk1.a...."
        )
        await update.message.reply_text(help_text)
        return

    # 2. Первый аргумент — это VK_ID, всё остальное склеиваем обратно в одну строку URL
    vk_input = context.args[0].strip()
    full_url = " ".join(context.args[1:]).strip()

    # 3. Аккуратно достаем access_token из полной ссылки
    #    Ищем подстроку "access_token=" и обрезаем до следующего '&' или до конца строки
    token_marker = "access_token="
    if token_marker not in full_url:
        await update.message.reply_text(
            "❌ Не нашёл 'access_token=' в ссылке.\n\n"
            "Убедитесь, что вы скопировали ВСЮ строку из адресной строки браузера "
            "после нажатия «Разрешить».\n\n"
            "Строка должна начинаться примерно так:\n"
            "https://oauth.vk.com/blank.html#access_token=vk1.a..."
        )
        return

    token_part = full_url.split(token_marker, 1)[1]
    # Если в строке есть '&', то токен заканчивается перед ним
    if "&" in token_part:
        vk_token = token_part.split("&", 1)[0]
    else:
        vk_token = token_part

    vk_token = vk_token.strip()

    if not vk_token:
        await update.message.reply_text(
            "❌ Не удалось вытащить токен из ссылки.\n"
            "Попробуйте ещё раз скопировать ссылку из адресной строки полностью."
        )
        return

    # 4. Пытаемся удалить сообщение с токеном (безопасность)
    try:
        await update.message.delete()
    except Exception:
        pass

    status = await update.effective_chat.send_message(
        f"⏳ Добавляю ВК аккаунт {vk_input}...\n"
        f"Проверяю токен и доступ к стене..."
    )

    # 5. Проверяем лимит 10 аккаунтов
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM vk_accounts WHERE user_id=?", (user_id,))
    count = cursor.fetchone()[0]

    if count >= 10:
        await status.edit_text("❌ Лимит достигнут! Максимум 10 аккаунтов на пользователя.")
        conn.close()
        return

    # 6. Получаем owner_id из vk_input (id123, club123, короткое имя и т.п.)
    owner_id, err = resolve_owner_id(vk_input, vk_token)
    if err:
        await status.edit_text(f"❌ Ошибка при распознавании VK ID:\n{err}")
        conn.close()
        return

    # 7. Проверяем доступ к стене — берём последний пост
    last_post_url, last_post_id, err = get_last_vk_post(owner_id, vk_token)
    if err:
        await status.edit_text(f"❌ Ошибка VK API:\n{err}")
        conn.close()
        return

    if last_post_url is None:
        await status.edit_text(
            "❌ Не удалось получить посты со стены.\n"
            "Возможные причины:\n"
            "• Стена пустая (нет ни одного поста)\n"
            "• Стена закрыта или доступны не все записи\n"
            "• У токена нет прав для доступа к этой стене"
        )
        conn.close()
        return

    # 8. Сохраняем в базу
    try:
        cursor.execute(
            """
            INSERT INTO vk_accounts (user_id, vk_input, owner_id, vk_token, last_post_url, last_post_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, vk_input, owner_id, vk_token, last_post_url, last_post_id),
        )
        conn.commit()

        await status.edit_text(
            "✅ ВК аккаунт успешно добавлен!\n\n"
            f"Аккаунт: {vk_input}\n"
            f"owner_id: {owner_id}\n"
            f"Последний пост: {last_post_url}\n\n"
            "Теперь бот будет отслеживать новые посты на этой стене.\n"
            "Чтобы запустить проверку, используйте команду: /check"
        )

    except sqlite3.IntegrityError:
        await status.edit_text("⚠️ Этот аккаунт уже добавлен для вашего Telegram-профиля.")
    except Exception as e:
        await status.edit_text(f"❌ Ошибка при сохранении в базу:\n{e}")
    finally:
        conn.close()
# ========== УДАЛЕНИЕ ВК АККАУНТА ==========

async def delete_vk_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Удаляет ВК аккаунт пользователя.
    Формат: /delete_vk VK_ID
    Пример: /delete_vk id123456789
    """
    user_id = update.effective_user.id
    
    # Проверяем, что передан аргумент (VK_ID)
    if len(context.args) < 1:
        help_text = (
            "❌ Неправильный формат!\\n\\n"
            "Используйте: /delete_vk VK_ID\\n\\n"
            "VK_ID — это ID или короткое имя страницы/группы ВК:\\n"
            " • id123456789 — для личной страницы\\n"
            " • club123456789 — для группы\\n\\n"
            "Пример команды:\\n"
            "/delete_vk id123456789"
        )
        await update.message.reply_text(help_text)
        return
    
    # Берём первый аргумент как VK_ID
    vk_input = context.args[0].strip().lower()
    
    # Подключаемся к базе данных
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Ищем такой аккаунт у этого пользователя
    cursor.execute(
        "SELECT id, vk_input FROM vk_accounts WHERE user_id=? AND vk_input=?",
        (user_id, vk_input)
    )
    
    account = cursor.fetchone()
    
    if not account:
        conn.close()
        await update.message.reply_text(
            f"❌ Аккаунт '{vk_input}' не найден!\\n\\n"
            f"Используйте /list чтобы посмотреть все аккаунты"
        )
        return
    
    # Удаляем аккаунт из базы данных
    try:
        cursor.execute("DELETE FROM vk_accounts WHERE id=?", (account[0],))
        conn.commit()
        
        await update.message.reply_text(
            f"✅ Аккаунт '{vk_input}' успешно удалён!\\n\\n"
            f"Вы всё ещё можете добавить до 10 аккаунтов.\\n"
            f"Используйте: /add_vk VK_ID VK_TOKEN"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при удалении:\\n{e}")
    finally:
        conn.close()

async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список добавленных ВК-аккаунтов"""
    user_id = update.effective_user.id

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT vk_input, owner_id, last_post_url FROM vk_accounts WHERE user_id=? ORDER BY id",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "❌ Нет добавленных ВК аккаунтов.\n"
            "Добавьте аккаунт: /add_vk VK_ID VK_TOKEN"
        )
        return

    text = "📋 Ваши ВК аккаунты:\n\n"
    for i, (vk_input, owner_id, last_post_url) in enumerate(rows, 1):
        text += f"{i}. {vk_input} (owner_id={owner_id})\n"
    text += f"\n📊 Всего: {len(rows)}/10 (макс 10)"
    
    await update.message.reply_text(text)


async def check_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет все ВК-аккаунты на новые посты и загружает их на smmlaba"""
    user_id = update.effective_user.id

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Получаем учётные данные smmlaba
    cursor.execute("SELECT email, api_key FROM user_smmlaba_credentials WHERE user_id=?", (user_id,))
    smm = cursor.fetchone()

    if not smm:
        conn.close()
        await update.message.reply_text(
            "❌ Сначала сохраните учётные данные smmlaba!\n"
            "Используйте: /set_smmlaba EMAIL API_KEY"
        )
        return

    email, api_key = smm

    # Проверяем баланс
    balance, error = check_smmlaba_balance(email, api_key)
    if error or balance <= 0:
        conn.close()
        await update.message.reply_text(
            f"❌ Проблема с балансом!\n"
            f"Ошибка: {error if error else 'Баланс = 0'}\n\n"
            f"Пополните баланс на https://smmlaba.com/"
        )
        return

    # Получаем все ВК-аккаунты пользователя
    cursor.execute(
        "SELECT id, vk_input, owner_id, vk_token, last_post_id FROM vk_accounts WHERE user_id=?",
        (user_id,)
    )
    accounts = cursor.fetchall()

    if not accounts:
        conn.close()
        await update.message.reply_text(
            "❌ Нет добавленных ВК аккаунтов!\n"
            "Добавьте: /add_vk VK_ID VK_TOKEN"
        )
        return

    msg = await update.message.reply_text(f"⏳ Проверяю посты...\n💰 Баланс: {balance} руб.")

    checked = 0
    updated = 0
    ok_pages = []

    # Проверяем каждый аккаунт
    for acc_id, vk_input, owner_id, vk_token, last_post_id in accounts:
        time.sleep(0.4)  # Пауза между запросами к VK API (чтобы не превышать лимит)

        post_url, post_id, skip_send, err = get_last_vk_post(owner_id, vk_token)

        if err or post_url is None:
            continue

        checked += 1

        if post_id != last_post_id:
            # 1) Всегда обновляем БД (даже если skip_send=True)
            cursor.execute(
                "UPDATE vk_accounts SET last_post_url=?, last_post_id=? WHERE id=?",
                (post_url, post_id, acc_id)
            )
            conn.commit()

            # 2) Если репостов 1+ — НЕ отправляем в smmlaba
            if skip_send:
                continue

            # 3) Иначе отправляем
            success, msg_text = send_to_smmlaba(post_url, email, api_key)
            if success:
                updated += 1
                ok_pages.append(vk_input)

    conn.close()

    # Формируем итоговое сообщение
    result = (
        f"✅ Проверка завершена!\n\n"
        f"📊 Результаты:\n"
        f"• Всего проверено аккаунтов: {checked}\n"
        f"• Загружено новых постов: {updated}\n"
        f"• Баланс: {balance} руб.\n"
    )
    
    if ok_pages:
        result += "\n✅ Загруженные аккаунты:\n" + "\n".join(f"  • {page}" for page in ok_pages)
    else:
        result += "\n📌 Новых постов не найдено"

    await msg.edit_text(result)


# ========== ОБРАБОТКА СООБЩЕНИЙ ==========

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений (кнопки меню)"""
    text = update.message.text

    if text == "✅ Проверить все посты":
        await check_posts(update, context)
    elif text == "📋 Мои аккаунты":
        await list_accounts(update, context)
    elif text == "⚙️ Настройка":
        await update.message.reply_text("⚙️ Выберите действие:", reply_markup=get_settings_menu_keyboard())
    elif text == "📚 Справка":
        await help_command(update, context)
    elif text == "🔐 Smmlaba":
        await update.message.reply_text(
            "🔐 УЧЁТНЫЕ ДАННЫЕ SMMLABA\n\n"
            "Используйте команду:\n"
            "/set_smmlaba EMAIL API_KEY\n\n"
            "Пример:\n"
            "/set_smmlaba test@example.com abc123xyz"
        )
    elif text == "📱 Добавить ВК токен":
        await update.message.reply_text(
            "📱 ДОБАВИТЬ ВК АККАУНТ\n\n"
            "Используйте команду:\n"
            "/add_vk VK_ID VK_TOKEN\n\n"
            "Примеры:\n"
            "/add_vk id123456789 vk1.a...\n"
            "/add_vk club12345678 vk1.a...\n\n"
            "Как получить токен: /help"
        )
    elif text == "💰 Баланс":
        await show_smmlaba_info(update, context)
    elif text == "🏠 Назад":
        await start(update, context)
    elif text == "🗑️ Удалить аккаунт":
    await update.message.reply_text(
        "🗑️ УДАЛИТЬ ВК АККАУНТ\\n\\n"
        "Используйте команду:\\n"
        "/delete_vk VK_ID\\n\\n"
        "Примеры:\\n"
        "/delete_vk id123456789\\n"
        "/delete_vk club12345678\\n\\n"
        "Используйте /list чтобы посмотреть все ваши аккаунты"
    )
    else:
        await update.message.reply_text(
            "👋 Пожалуйста, используйте кнопки меню или команды.",
            reply_markup=get_main_menu_keyboard()
        )


# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    """Инициализирует и запускает бота"""
    init_database()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("set_smmlaba", set_smmlaba_credentials))
    app.add_handler(CommandHandler("my_smmlaba", show_smmlaba_info))
    app.add_handler(CommandHandler("add_vk", add_vk))
    app.add_handler(CommandHandler("delete_vk", delete_vk_account))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("check", check_posts))

    # Обработчик текстовых сообщений (кнопки)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("🚀 Бот запущен!")
    print("📌 Нажмите Ctrl+C для остановки")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
