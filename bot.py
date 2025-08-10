import telebot
from telebot import types
from math import floor
from datetime import datetime, timedelta
import time
import random
import config
import telebot.types as types
import db
import crypto_pay
import json
from uuid import uuid4
from requests.exceptions import RequestException
import requests
import sqlite3
import db as db_module
import logging
import threading
import schedule
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_db
from db import add_user, get_db  
import re
from telebot.apihelper import ApiTelegramException
import io
from telebot.handler_backends import State, StatesGroup
from threading import Lock
import uuid


bot = telebot.TeleBot(config.BOT_TOKEN)


treasury_lock = threading.Lock()
active_treasury_admins = {}

def auto_confirm_number(number, user_id, code):
    with db.get_db() as conn:
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Устанавливаем статус "активен" и TAKE_DATE
        cursor.execute('''
            UPDATE numbers 
            SET status = "активен", 
                hold_start_time = NULL, 
                VERIFICATION_CODE = NULL, 
                TAKE_DATE = ? 
            WHERE number = ?
        ''', (current_time, number))
        conn.commit()
        print(f"[DEBUG] Номер {number} автоматически подтверждён в {current_time}")

    # Уведомляем пользователя
    safe_send_message(user_id, f"✅ Номер {number} автоматически помечен как 'встал' в {current_time}.")

    # Обновляем сообщение в группе
    if number in code_messages:
        message_data = code_messages[number]
        chat_id = message_data["chat_id"]
        message_id = message_data["message_id"]
        tg_number = message_data["tg_number"]
        try:
            bot.edit_message_text(
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"⏰ Номер {number} автоматически помечен как 'встал' в {current_time}.",
                chat_id,
                message_id,
                parse_mode='HTML'
            )
            print(f"[DEBUG] Сообщение в группе {chat_id} обновлено для номера {number}")
        except Exception as e:
            print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
        del code_messages[number]

    for mod_id in config.MODERATOR_IDS:
        safe_send_message(mod_id, f"⏰ Номер {number} автоматически помечен как 'встал' в {current_time}.")

class Database:
    def get_db(self):
        return sqlite3.connect('database.db')

    def is_moderator(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            return cursor.fetchone() is not None

    def update_balance(self, user_id, amount):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, user_id))
            conn.commit()

    def get_group_name(self, group_id):
        return db_module.get_group_name(group_id)


    def update_last_activity(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            current_time = datetime.now()

            # Проверяем, есть ли пользователь
            cursor.execute('SELECT LAST_ACTIVITY FROM users WHERE ID = ?', (user_id,))
            result = cursor.fetchone()

            if not result:
                # Новый пользователь
                cursor.execute(
                    'INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, AFK_TYPE, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?, ?)',
                    (user_id, 0.0, current_time.strftime("%Y-%m-%d %H:%M:%S"), 0, 0, current_time.strftime("%Y-%m-%d %H:%M:%S"))
                )
                print(f"[DEBUG] Новый пользователь {user_id} добавлен и время активности установлено")

            # Только обновляем время активности, не трогая IS_AFK
            cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', 
                        (current_time.strftime("%Y-%m-%d %H:%M:%S"), user_id))
            conn.commit()
            print(f"[DEBUG] Обновлено время активности для пользователя {user_id}: {current_time}")

db = Database()


# ----------------------------
#  ХЕЛПЕРЫ (номера, баланс)
# ----------------------------

def is_russian_number(phone_number):
    phone_number = phone_number.strip()
    phone_number = re.sub(r'[\s\-()]+', '', phone_number)
    if phone_number.startswith('7') or phone_number.startswith('8'):
        phone_number = '+7' + phone_number[1:]
    elif phone_number.startswith('9') and len(phone_number) == 10:
        phone_number = '+7' + phone_number
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number
    pattern = r'^\+7\d{10}$'
    return phone_number if bool(re.match(pattern, phone_number)) else None

def check_balance_and_fix(user_id):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] < 0:
            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
            conn.commit()

# ----------------------------
#  СТИЛЕВОЕ ОФОРМЛЕНИЕ
# ----------------------------
DIV = "━━━━━━━━━━━━━━━"
def header(title_emoji, title_text):
    return f"{title_emoji} <b>{title_text}</b>\n{DIV}\n"

def success_text(txt):
    return f"✅ <b>{txt}</b"

def error_text(txt):
    return f"❌ <b>{txt}</b>"


# ----------------------------
#  КОМАНДЫ / Меню
# ----------------------------

@bot.message_handler(commands=['help'])
def help_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    db.update_last_activity(user_id)  # Обновляем время активности

    # Проверяем статус пользователя
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, BLOCKED FROM requests WHERE ID = ?', (user_id,))
        request = cursor.fetchone()

        # Если пользователь заблокирован
        if request and request[1] == 1:
            bot.send_message(chat_id, "🚫 Вы заблокированы в боте. Обратитесь к поддержке: @{config.PAYOUT_MANAGER}", parse_mode='HTML')
            return

        # Если пользователь не одобрен
        if not request or request[0] != 'approved':
            bot.send_message(chat_id, "👋 Ваша заявка на вступление ещё не одобрена. Ожидайте подтверждения администратора.", parse_mode='HTML')
            return

    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    if is_admin:
        help_text = (
            "👑 <b>Справка для администратора</b>\n\n"
            "Вы имеете полный доступ к управлению ботом. Доступные команды и действия:\n\n"
            "⚙️ <b>Админ-панель</b> (кнопка в меню)\n"
            "   - Управление заявками на вступление\n"
            "   - Настройка АФК пользователей\n"
            "   - Изменение цен для пользователей\n"
            "   - Уменьшение баланса пользователей\n"
            "   - Отправка чеков (всем или отдельным пользователям)\n\n"
            "📱 <b>Работа с номерами</b>\n"
            "   - Команда в группе: <code>тг1</code> (от 1 до 70) — взять номер в обработку\n"
            "   - Команда: <code>слет +79991234567</code> — пометить номер как слетевший\n\n"
            "💰 <b>Управление выплатами</b>\n"
            "   - Просмотр и обработка заявок на вывод\n"
            "   - Создание и отправка чеков через CryptoBot\n\n"
            "📊 <b>Статистика</b>\n"
            "   - Доступна в профиле: общее количество пользователей и номеров\n\n"
            "📞 <b>Поддержка</b>\n"
            f"   - Связь с менеджером: @{config.PAYOUT_MANAGER}"
        )
    elif is_moderator:
        help_text = (
            "🛡 <b>Справка для модератора</b>\n\n"
            "Вы можете обрабатывать номера в рабочих группах. Доступные команды и действия:\n\n"
            "📱 <b>Работа с номерами</b>\n"
            "   - Команда в группе: <code>тг1</code> (от 1 до 70) — взять номер в обработку\n"
            "   - Команда: <code>слет +79991234567</code> — пометить номер как слетевший  \n"
            "   - Подтверждение или отклонение номеров через кнопки\n\n"
            "🔙 <b>Возврат в меню</b>\n"
            "   - Используйте кнопку ниже или команду /start\n\n"
            "📞 <b>Поддержка</b>\n"
            f"   - Связь с менеджером: @{config.PAYOUT_MANAGER}"
        )
    else:
        # Получаем текущие настройки
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
            result = cursor.fetchone()
            price, hold_time = result if result else (2.0, 5)

        help_text = (
            f"<b>📢 Справка для пользователя {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• Вы сдаёте номер, мы выплачиваем вам деньги после проверки.\n"
            f"• Моментальные выплаты через CryptoBot после {hold_time} минут работы номера.\n\n"
            "<b>💰 Тарифы:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n\n"
            "<b>📱 Доступные действия:</b>\n"
            "1. <b>Сдать номер</b> — через кнопку в меню\n"
            "2. <b>Удалить номер</b> — если хотите убрать свой номер\n"
            "3. <b>Изменить номер</b> — заменить один номер на другой\n"
            "4. <b>Мой профиль</b> — просмотр баланса, активных и успешных номеров\n"
            "5. <b>Вывести деньги</b> — запрос вывода средств\n"
            "6. <b>АФК-режим</b> — скрыть номера на время отсутствия\n\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
            "✅ Прозрачные условия\n"
            "✅ Выгодные тарифы и быстрые выплаты\n"
            "✅ Поддержка 24/7\n\n"
            "<b>📞 Поддержка:</b>\n"
            f"Связь с менеджером: @{config.PAYOUT_MANAGER}\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )

    bot.send_message(chat_id, help_text, parse_mode='HTML', reply_markup=markup)
    
cooldowns = {}

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username
    db_module.add_user(user_id=user_id, username=username)
    print(f"[DEBUG] Username для user_id {user_id}: {username}")  # Отладочный вывод
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Проверяем текущий статус АФК
    was_afk = db_module.get_afk_status(user_id)
    db_module.update_last_activity(user_id)  # Обновляем время активности и сбрасываем АФК
    
    chat_type = bot.get_chat(message.chat.id).type
    is_group = chat_type in ["group", "supergroup"]
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BLOCKED FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 1:
            bot.send_message(
                message.chat.id,
                "🚫 Вас заблокировали в боте!",
                disable_notification=True
            )
            return
    
    is_moderator = db_module.is_moderator(user_id)
    is_admin = user_id in config.ADMINS_ID

    # Уведомление о выходе из АФК

    if is_group and is_moderator and not is_admin:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            group_id = cursor.fetchone()
            group_name = db_module.get_group_name(group_id[0]) if group_id else "Неизвестная группа"
        
        moderator_text = (
            f"Здравствуйте 🤝\n"
            f"Вы назначены модератором в группе: <b>{group_name}</b>\n\n"
            "Вот что вы можете:\n\n"
            "1. Брать номера в обработку и работать с ними\n\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "Не злоупотребляйте этим в юмористических целях!\n\n"
            "<b>Доступные вам команды в чате:</b>\n"
            "1. <b>Запросить номер</b>\n"
            "Запрос номера производится вводом таких символов как «тг1» и отправлением его в рабочий чат\n"
            "Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "<b>Важно!</b> Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "Пример: <code>слет +79991112345</code>\n"
            "После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел"
        )
        bot.send_message(
            message.chat.id,
            moderator_text,
            parse_mode='HTML',
            disable_notification=True
        )
        return
    
    if user_id in config.ADMINS_ID:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                          (user_id, current_date, 'approved', 0, 1))
            conn.commit()
        if is_group:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
            )
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
            is_afk = db_module.get_afk_status(user_id)
            afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
            bot.send_message(
                message.chat.id,
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                "<b>💼 Как это работает?</b>\n"
                "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после 5 минут работы.</b>\n\n"
                "<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ <code>2.0$</code> за номер (холд 5 минут)\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                "✅ <i>Прозрачные условия сотрудничества</i>\n"
                "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                "✅ <i>Оперативная поддержка 24/7</i>\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                reply_markup=markup,
                parse_mode='HTML',
                disable_notification=True
            )
        else:
            # Send a temporary message to get message_id
            temp_message = bot.send_message(
                chat_id,
                "Загрузка меню...",
                parse_mode='HTML',
                disable_notification=True
            )
            show_main_menu(chat_id, temp_message.message_id, user_id)
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT LAST_REQUEST, STATUS FROM requests WHERE ID = ?', (user_id,))
        request = cursor.fetchone()
        if request and request[1] == 'approved':
            if is_group:
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                    types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
                )
                is_afk = db_module.get_afk_status(user_id)
                afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
                markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
                bot.send_message(
                    message.chat.id,
                    f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                    f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                    "<b>💼 Как это работает?</b>\n"
                    "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                    f"• <i>Моментальные выплаты</i> – <b>после 5 минут работы.</b>\n\n"
                    "<b>💰 Тарифы на сдачу номеров:</b>\n"
                    f"▪️ <code>2.0$</code> за номер (холд 5 минут)\n"
                    f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
                    "✅ <i>Прозрачные условия сотрудничества</i>\n"
                    "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                    "✅ <i>Оперативная поддержка 24/7</i>\n\n"
                    "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                    reply_markup=markup,
                    parse_mode='HTML',
                    disable_notification=True
                )
            else:
                # Send a temporary message to get message_id
                temp_message = bot.send_message(
                    chat_id,
                    "Загрузка меню...",
                    parse_mode='HTML',
                    disable_notification=True
                )
                show_main_menu(chat_id, temp_message.message_id, user_id)
            return
        if request:
            last_request_time = datetime.strptime(request[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_request_time < timedelta(minutes=15):
                time_left = 15 - ((datetime.now() - last_request_time).seconds // 60)
                bot.send_message(
                    message.chat.id, 
                    f"⏳ Ожидайте подтверждения. Вы сможете отправить новый запрос через {time_left} минут.",
                    disable_notification=True
                )
                return
        cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                      (user_id, current_date, 'pending', 0, 1))
        conn.commit()
        bot.send_message(
            message.chat.id, 
            "👋 Здравствуйте! Ожидайте, пока вас впустит администратор.",
            disable_notification=True
        )
        # Notify admins with approval buttons for non-admin/moderator pending users
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            admin_ids = config.ADMINS_ID
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?)) AND ID != ?'
            params = ('pending', *admin_ids, 'moderator', user_id)
            cursor.execute(query, params)
            pending_users = cursor.fetchall()
    if pending_users:
        admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
        markup = types.InlineKeyboardMarkup()
        
        for pending_user_id, reg_date in pending_users:
            try:
                user = bot.get_chat_member(pending_user_id, pending_user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
                username_link = f"<a href=\"tg://user?id={pending_user_id}\">{username}</a>" if user.username else "Нет username"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username для user_id {pending_user_id}: {e}")
                username_link = "Неизвестный пользователь"

            admin_text += (
                f"👤 Пользователь ID: <a href=\"https://t.me/@id{pending_user_id}\">{pending_user_id}</a> (Зарегистрирован: {reg_date})\n"
                f"👤 Username: {username_link}\n\n"
            )

            approve_button = types.InlineKeyboardButton(f"✅ Одобрить {pending_user_id}", callback_data=f"approve_user_{pending_user_id}")
            reject_button = types.InlineKeyboardButton(f"❌ Отклонить {pending_user_id}", callback_data=f"reject_user_{pending_user_id}")
            markup.row(approve_button, reject_button)

        try:
            for admin_id in config.ADMINS_ID:
                bot.send_message(
                    admin_id,
                    admin_text,
                    parse_mode='HTML',
                    reply_markup=markup,
                    disable_notification=True
                )
        except Exception as e:
            print(f"[ERROR] Не удалось отправить уведомление админам: {e}")

def show_main_menu(chat_id, message_id, user_id):
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            db_module.add_user(user_id)
            is_afk = False
            afk_locked = False
        else:
            is_afk, afk_locked = result

    is_moderator = db_module.is_moderator(user_id)

    if is_moderator:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            group_id = cursor.fetchone()
            group_name = db_module.get_group_name(group_id[0]) if group_id else "Неизвестная группа"

        moderator_text = (
            f"Здравствуйте 🤝\n"
            f"Вы назначены модератором в группе: <b>{group_name}</b>\n\n"
            "Вот что вы можете:\n"
            "1. Брать номера в обработку и работать с ними\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "   Не злоупотребляйте этим в юмористических целях!\n\n"
            "Доступные вам команды в чате:\n"
            "1. Запросить номер\n"
            "   Запрос номера производится вводом таких символов как «тг1» и отправлением его в рабочий чат\n"
            "   Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "   Важно! Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "   Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "   Пример: <code>слет +79991112345</code>\n"
            "   После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел"
        )
        try:
            bot.edit_message_text(
                moderator_text,
                chat_id,
                message_id,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(
                    chat_id,
                    moderator_text,
                    parse_mode='HTML',
                    disable_notification=True
                )
    else:
        price = db_module.get_user_price(user_id)
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5

        welcome_text = (
            f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
            f"• <i>Моментальные выплаты</i> – <b>после {hold_time} минут работы.</b>\n\n"
            "<b>💰 Тарифы на сдачу номеров:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
            "✅ <i>Прозрачные условия сотрудничества</i>\n"
            "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
            "✅ <i>Оперативная поддержка 24/7</i>\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )

        is_admin = user_id in config.ADMINS_ID
        if not is_admin and not is_moderator:
            markup.add(types.InlineKeyboardButton("🗑️ Удалить номер", callback_data="delete_number"))
            markup.add(types.InlineKeyboardButton("✏️ Изменить номер", callback_data="change_number"))
            markup.add(types.InlineKeyboardButton("🔐 2FA", callback_data="manage_2fa"))
            markup.add(types.InlineKeyboardButton("📩 Апелляция номера", callback_data="appeal_number"))
            markup.add(types.InlineKeyboardButton("🔓 Сбросить 2FA", callback_data="reset_2fa"))

        if is_admin:
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))

        afk_button_text = "🔴 Выключить АФК" if is_afk and not afk_locked else "🟢 Включить АФК"
        if afk_locked:
            markup.add(types.InlineKeyboardButton(f"🔒 АФК заблокирован (админ)", callback_data="afk_locked_info"))
        else:
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))

        try:
            bot.edit_message_text(
                welcome_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(
                    chat_id,
                    welcome_text,
                    parse_mode='HTML',
                    reply_markup=markup,
                    disable_notification=True
                )

        if is_afk and afk_locked:
            bot.send_message(
                chat_id,
                "🔔 Вы в режиме АФК, заблокированном администратором. Номера скрыты.",
                parse_mode='HTML',
                disable_notification=True
            )

# ==========================
# 📌 Обработчик кнопки "АФК заблокирован (админ)"
# ==========================

@bot.callback_query_handler(func=lambda call: call.data == "afk_locked_info")
def afk_locked_info(call):
    bot.answer_callback_query(
        call.id,
        "🚫 Режим АФК заблокирован администратором.\n📩 Обратитесь в поддержку.",
        show_alert=True  # Чтобы текст показался в отдельном окне
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    print(f"[DEBUG] Обработчик back_to_main вызван для user_id={user_id}, chat_id={chat_id}")
    
    # Очищаем состояние пользователя
    clear_state(user_id)
    
    try:
        bot.answer_callback_query(call.id, "↩ Возврат в главное меню")
        temp_message = bot.send_message(
            chat_id,
            "Загрузка меню...",
            parse_mode='HTML',
            disable_notification=True
        )
        show_main_menu(chat_id, temp_message.message_id, user_id)
    except Exception as e:
        print(f"[ERROR] Ошибка в back_to_main: {e}")
        bot.send_message(
            chat_id,
            "❌ Произошла ошибка при возврате в меню. Попробуйте снова.",
            parse_mode='HTML'
        )

# ==========================
# 📌 Система состояний (для ввода и кнопки "Назад")
# ==========================
user_states = {}  # {user_id: {"state": str, "data": {...}}}

def set_state(user_id, state, data=None):
    user_states[user_id] = {"state": state, "data": data or {}}

def clear_state(user_id):
    user_states.pop(user_id, None)

# ==========================
# 🔙 Универсальный обработчик кнопки "Назад"
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "go_back")
def go_back(call):
    clear_state(call.from_user.id)
    bot.answer_callback_query(call.id, "↩ Возврат в меню")
    show_main_menu(call.message.chat.id, call.message.message_id, call.from_user.id)


# ==========================
# ✅ ОДОБРЕНИЕ ЗАЯВКИ
# ==========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_user_"))
def approve_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет прав!")
        return

    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET STATUS = ? WHERE ID = ?', ('approved', user_id))
        conn.commit()

    try:
        bot.send_message(
            user_id,
            "✅ <b>Заявка одобрена!</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "🎉 Добро пожаловать! Теперь у вас полный доступ к боту.\n"
            "Введите <code>/start</code> для начала.",
            parse_mode='HTML'
        )
        text = (
            "✅ <b>Заявка одобрена</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"👤 Пользователь: <a href='tg://user?id={user_id}'>Профиль</a>\n"
            "Получил полный доступ."
        )
    except:
        text = (
            "✅ <b>Заявка одобрена</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"👤 Пользователь: <a href='tg://user?id={user_id}'>Профиль</a>\n"
            "Доступ выдан.\n"
            "⚠️ Уведомление не доставлено."
        )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 К списку заявок", callback_data="pending_requests"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

# ==========================
# ❌ ОТКЛОНЕНИЕ ЗАЯВКИ
# ==========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_user_"))
def reject_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет прав!")
        return

    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE requests SET STATUS = ?, LAST_REQUEST = ? WHERE ID = ?',
            ('rejected', datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id)
        )
        conn.commit()

    try:
        bot.send_message(
            user_id,
            "❌ <b>Доступ отклонён</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "⏳ Повторно подать заявку можно через <b>15 минут</b>.",
            parse_mode='HTML'
        )
        text = (
            "❌ <b>Заявка отклонена</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"👤 Пользователь: <a href='tg://user?id={user_id}'>Профиль</a>\n"
            "Может подать повторно через 15 минут."
        )
    except:
        text = (
            "❌ <b>Заявка отклонена</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"👤 Пользователь: <a href='tg://user?id={user_id}'>Профиль</a>\n"
            "⚠️ Уведомление не доставлено."
        )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 К списку заявок", callback_data="pending_requests"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)



# ==========================
# 🔔 ЗАЯВКИ НА ВСТУПЛЕНИЕ
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "pending_requests")
def pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет прав!")
        return

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        admin_ids = config.ADMINS_ID
        if not admin_ids:
            query = 'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM personal WHERE TYPE = ?)'
            params = ('pending', 'moderator')
        else:
            placeholders = ','.join('?' for _ in admin_ids)
            query = f'''
                SELECT ID, LAST_REQUEST FROM requests
                WHERE STATUS = ?
                AND ID NOT IN (
                    SELECT ID FROM requests WHERE ID IN ({placeholders})
                    OR ID IN (SELECT ID FROM personal WHERE TYPE = ?)
                )
            '''
            params = ('pending', *admin_ids, 'moderator')
        cursor.execute(query, params)
        pending_users = cursor.fetchall()

    text = "🔔 <b>Заявки на вступление</b>\n━━━━━━━━━━━━━━━\n"
    markup = types.InlineKeyboardMarkup()

    if pending_users:
        for uid, date in pending_users:
            try:
                user = bot.get_chat_member(uid, uid).user
                uname = f"@{user.username}" if user.username else "Без username"
                link = f"<a href='tg://user?id={uid}'>{uname}</a>"
            except:
                link = "Неизвестный пользователь"

            text += f"👤 {link}\n🆔 <code>{uid}</code>\n📅 Заявка: {date}\n━━━━━━━━━━━━━━━\n"
            markup.row(
                types.InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_user_{uid}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_user_{uid}")
            )
    else:
        text += "📭 Новых заявок нет."

    markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)




# ==========================
# 📨 АПЕЛЛЯЦИЯ НОМЕРА
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "appeal_number")
def start_appeal_number(call):
    set_state(call.from_user.id, "appeal_number")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="go_back"))
    bot.send_message(
        call.message.chat.id,
        "✏ <b>Апелляция номера</b>\n━━━━━━━━━━━━━━━\nВведите номер для апелляции:",
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "appeal_number")
def process_appeal_number(message):
    user_id = message.from_user.id
    number = message.text.strip()
    clear_state(user_id)

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
        row = cursor.fetchone()

    if not row:
        bot.send_message(message.chat.id, f"❌ <b>Ошибка</b>\n📱 {number} — не найден.", parse_mode='HTML')
        return

    if row[0] != "невалид":
        bot.send_message(message.chat.id, f"⚠️ <b>Отказ</b>\n📱 {number} — апелляция невозможна.")
        return

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO appeals (NUMBER, USER_ID, STATUS) VALUES (?, ?, "pending")', (number, user_id))
        conn.commit()

    bot.send_message(message.chat.id, f"📨 <b>Апелляция отправлена</b>\n📱 {number} — ожидает рассмотрения.")



# ==========================
# 📞 УДАЛЕНИЕ НОМЕРА
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "delete_number")
def handle_delete_number(call):
    user_id = call.from_user.id
    if user_id in config.ADMINS_ID or db_module.is_moderator(user_id):
        bot.answer_callback_query(call.id, "❌ Эта функция только для пользователей")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="go_back"))

    set_state(user_id, "delete_number")
    bot.send_message(
        call.message.chat.id,
        "🗑 <b>Удаление номеров</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "Введите номера для удаления (по одному в строке):\n"
        "📌 Пример:\n+79991234567\n79091234567\n9021234567",
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "delete_number")
def process_delete_number(message):
    user_id = message.from_user.id
    text = message.text.strip()
    if not text:
        bot.send_message(message.chat.id, "⚠️ Ввод пустой. Операция отменена.")
        clear_state(user_id)
        return

    nums = [n.strip() for n in text.split("\n") if n.strip()]
    deleted, not_found, invalid = 0, 0, 0

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        for num in nums:
            norm = is_russian_number(num)
            if not norm:
                invalid += 1
                continue
            cursor.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, norm))
            if not cursor.fetchone():
                not_found += 1
                continue
            cursor.execute('DELETE FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, norm))
            conn.commit()
            deleted += 1

    bot.send_message(
        message.chat.id,
        f"📊 <b>Итог:</b>\n"
        f"🗑 Удалено: {deleted}\n"
        f"⚠️ Не найдено: {not_found}\n"
        f"❌ Некорректных: {invalid}",
        parse_mode='HTML'
    )
    clear_state(user_id)
    start(message)


# ==========================
# 🔄 СМЕНА НОМЕРА
# ==========================

@bot.callback_query_handler(func=lambda call: call.data == "change_number")
def handle_change_number(call):
    if call.from_user.id in config.ADMINS_ID or db_module.is_moderator(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Только для обычных пользователей!")
        return
    set_state(call.from_user.id, "change_number_old")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="go_back"))
    bot.send_message(
        call.message.chat.id,
        "🔄 <b>Изменение номера</b>\n━━━━━━━━━━━━━━━\nВведите старый номер:",
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "change_number_old")
def process_old_number(message):
    num = is_russian_number(message.text.strip())
    if not num:
        bot.send_message(message.chat.id, "❌ Некорректный номер!")
        clear_state(message.from_user.id)
        return
    with db_module.get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (message.from_user.id, num))
        if not c.fetchone():
            bot.send_message(message.chat.id, "⚠️ Номер не найден.")
            clear_state(message.from_user.id)
            return
    set_state(message.from_user.id, "change_number_new", {"old": num})
    bot.send_message(message.chat.id, f"✏ Введите новый номер для замены {num}:")

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "change_number_new")
def process_new_number(message):
    old = user_states[message.from_user.id]["data"]["old"]
    num = is_russian_number(message.text.strip())
    if not num:
        bot.send_message(message.chat.id, "❌ Некорректный новый номер!")
        clear_state(message.from_user.id)
        return
    with db_module.get_db() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM numbers WHERE NUMBER = ? AND ID_OWNER != ?', (num, message.from_user.id))
        if c.fetchone():
            bot.send_message(message.chat.id, "❌ Этот номер уже используется другим пользователем!")
            clear_state(message.from_user.id)
            return
        c.execute('UPDATE numbers SET NUMBER = ? WHERE ID_OWNER = ? AND NUMBER = ?', (num, message.from_user.id, old))
        conn.commit()
    bot.send_message(message.chat.id, f"✅ Номер изменён\n📴 Был: {old}\n📱 Стал: {num}")
    clear_state(message.from_user.id)



# ==========================
# 🔐 2FA
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "manage_2fa")
def manage_2fa(call):
    set_state(call.from_user.id, "set_2fa")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="go_back"))
    bot.send_message(call.message.chat.id, "🔐 <b>Установка 2FA</b>\n━━━━━━━━━━━━━━━\nВведите ваш пароль:", parse_mode='HTML', reply_markup=markup)

@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "set_2fa")
def process_2fa_input(message):
    fa = message.text.strip()
    if not fa:
        bot.send_message(message.chat.id, "❌ Пароль не может быть пустым!")
        return
    with db_module.get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET fa = ? WHERE ID = ?', (fa, message.from_user.id))
        c.execute('UPDATE numbers SET fa = ? WHERE ID_OWNER = ?', (fa, message.from_user.id))
        conn.commit()
    bot.send_message(message.chat.id, f"✅ 2FA сохранён\n🔑 Пароль: {fa}")
    clear_state(message.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "reset_2fa")
def reset_2fa(call):
    with db_module.get_db() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET fa = NULL WHERE ID = ?', (call.from_user.id,))
        c.execute('UPDATE numbers SET fa = NULL WHERE ID_OWNER = ?', (call.from_user.id,))
        conn.commit()
    bot.send_message(call.message.chat.id, "🔓 2FA сброшен")

#===========================================================================
#======================ПРОФИЛЬ=====================ПРОФИЛЬ==================
#===========================================================================




@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    check_balance_and_fix(user_id)
    bot.answer_callback_query(
        call.id, "👤Ваш профиль")

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()

        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)

        if user:
            cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND SHUTDOWN_DATE = "0"', (user_id,))
            active_numbers = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "отстоял"', (user_id,))
            successful_numbers = cursor.fetchone()[0]

            roles = []
            if user_id in config.ADMINS_ID:
                roles.append("👑 Администратор")
            if db.is_moderator(user_id):
                roles.append("🛡 Модератор")
            if not roles:
                roles.append("👤 Пользователь")

            # Получаем индивидуальную цену пользователя
            price = db_module.get_user_price(user_id)

            profile_text = (
                f"🪪 <b>Ваш профиль</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🔗 <b>ID ссылкой:</b> <code>https://t.me/@id{user_id}</code>\n"
                f"🆔 <b>ID:</b> <code>{user[0]}</code>\n"
                f"💰 <b>Баланс:</b> {user[1]} $\n"
                f"📱 <b>Активных номеров:</b> {active_numbers}\n"
                f"✅ <b>Успешных номеров:</b> {successful_numbers}\n"
                f"🎭 <b>Роль:</b> {' | '.join(roles)}\n"
                f"📅 <b>Дата регистрации:</b> {user[2]}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"<i>💵 Тариф: {price}$ | ⏱ Холд: {hold_time} минут</i>"
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("💳 Вывести", callback_data="withdraw"),
                types.InlineKeyboardButton("📱 Мои номера", callback_data="my_numbers")
            )

            if user_id in config.ADMINS_ID:
                cursor.execute('SELECT COUNT(*) FROM users')
                total_users = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE SHUTDOWN_DATE = "0"')
                active_total = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers')
                total_numbers = cursor.fetchone()[0]

                profile_text += (
                    f"\n\n📊 <b>Статистика бота</b>\n"
                    f"👥 Пользователей: {total_users}\n"
                    f"📱 Активных номеров: {active_total}\n"
                    f"📞 Всего номеров: {total_numbers}"
                )

            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))

            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass

            bot.send_message(
                call.message.chat.id,
                profile_text,
                reply_markup=markup,
                parse_mode='HTML'
            )



@bot.callback_query_handler(func=lambda call: call.data == "withdraw")
def start_withdrawal_request(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance > 0:
            msg = bot.edit_message_text(f"💰 Ваш баланс: {balance}$\n💳 Введите сумму для вывода или нажмите 'Да' для вывода всего баланса:",
                                      call.message.chat.id,
                                      call.message.message_id)
            bot.register_next_step_handler(msg, handle_withdrawal_request, balance)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👤 Связаться с менеджером", url=f"https://t.me/{config.PAYOUT_MANAGER}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.edit_message_text(f"❌ На вашем балансе недостаточно средств для вывода.\n\n"
                               f"Если вы считаете, что произошла ошибка или у вас есть вопросы по выводу, "
                               f"свяжитесь с ответственным за выплаты: @{config.PAYOUT_MANAGER}",
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup)


def handle_withdrawal_request(message, amount):
    user_id = message.from_user.id
    chat_id = message.chat.id  # Используется для get_chat_member

    # Получаем информацию о пользователе для username
    try:
        user_info = bot.get_chat_member(chat_id, user_id).user
        username = f"@{user_info.username}" if user_info.username else "Нет username"
        username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user_info.username else "Нет username"
    except Exception as e:
        print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
        username_link = "Неизвестный пользователь"

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()

        if not user or user[0] <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ У вас нет средств на балансе для вывода.", reply_markup=markup)
            return
        withdrawal_amount = user[0]
        
        try:
            if message.text != "Да" and message.text != "да":
                try:
                    requested_amount = float(message.text)
                    if requested_amount <= 0:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, "❖ Введите положительное число.", reply_markup=markup)
                        return
                        
                    if requested_amount > withdrawal_amount:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, 
                                      f"❌ Запрошенная сумма ({requested_amount}$) превышает ваш баланс ({withdrawal_amount}$).", 
                                      reply_markup=markup)
                        return
                        
                    withdrawal_amount = requested_amount
                except ValueError:
                    pass
            
            processing_message = bot.send_message(message.chat.id, 
                                        f"⏳ <b>Обработка запроса на вывод {withdrawal_amount}$...</b>\n\n"
                                        f"Пожалуйста, подождите, мы формируем ваш чек.",
                                        parse_mode='HTML')
            
            # Получаем актуальный баланс казны из API CryptoBot
            treasury_balance = db_module.get_treasury_balance()
            logging.info(f"[DEBUG] Treasury balance: {treasury_balance}, Withdrawal amount: {withdrawal_amount}")
            
            if withdrawal_amount > treasury_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.edit_message_text(
                    f"❌ <b>В данный момент вывод недоступен</b>\n\n"
                    f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"⚠️ <b>Попытка вывода при недостаточных средствах</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Запрошенная сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_balance}$\n\n"
                    f"⛔️ Вывод был заблокирован из-за нехватки средств в казне."
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
                return
            
            auto_input_status = db_module.get_auto_input_status()
            
            if not auto_input_status:
                cursor.execute('INSERT INTO withdraws (ID, AMOUNT, DATE, STATUS) VALUES (?, ?, ?, ?)', 
                             (user_id, withdrawal_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"))
                conn.commit()
                new_balance = user[0] - withdrawal_amount
                cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                conn.commit()
                # Вычисляем новый баланс казны для других целей (например, логирования)
                treasury_new_balance = treasury_balance - withdrawal_amount
                # Обновляем базу, если требуется
                db_module.update_treasury_balance(-withdrawal_amount)
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"✅ <b>Запрос на вывод средств принят!</b>\n\n"
                    f"Сумма: <code>{withdrawal_amount}$</code>\n"
                    f"Новой баланс: <code>{new_balance}$</code>\n\n"
                    f"⚠️ Авто-вывод отключен. Средства будут выведены вручную администратором.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"💰 <b>Новая заявка на выплату</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_balance}$"  # Используем старый баланс
                )
                admin_markup = types.InlineKeyboardMarkup()
                admin_markup.add(
                    types.InlineKeyboardButton("✅ Отправить чек", callback_data=f"send_check_{user_id}_{withdrawal_amount}"),
                    types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_withdraw_{user_id}_{withdrawal_amount}")
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, reply_markup=admin_markup, parse_mode='HTML')
                    except:
                        continue
                return
            
            try:
                crypto_api = crypto_pay.CryptoPay()
                cheque_result = crypto_api.create_check(
                    amount=withdrawal_amount,
                    asset="USDT",
                    description=f"Выплата для пользователя {user_id}"
                )
                
                if cheque_result.get("ok", False):
                    cheque = cheque_result.get("result", {})
                    cheque_link = cheque.get("bot_check_url", "")
                    
                    if cheque_link:
                        new_balance = user[0] - withdrawal_amount
                        cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                        conn.commit()
                        # Вычисляем новый баланс казны для сообщения
                        treasury_new_balance = treasury_balance - withdrawal_amount
                        # Обновляем базу, если требуется
                        db_module.update_treasury_balance(-withdrawal_amount)
                        db_module.log_treasury_operation("Автоматический вывод", withdrawal_amount, treasury_new_balance)
                        
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                        markup.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"✅ <b>Ваш вывод средств обработан!</b>\n\n"
                            f"Сумма: <code>{withdrawal_amount}$</code>\n"
                            f"Новый баланс: <code>{new_balance}$</code>\n\n"
                            f"Нажмите на кнопку ниже, чтобы активировать чек:",
                            message.chat.id, 
                            processing_message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        
                        log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] | Автоматический вывод | Пользователь {user_id} | Сумма {withdrawal_amount}$"
                        with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                            log_file.write(log_entry + "\n")
                        
                        admin_message = (
                            f"💸 <b>Автоматический вывод выполнен</b>\n\n"
                            f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                            f"👤 Username: {username_link}\n"
                            f"💵 Сумма: {withdrawal_amount}$\n"
                            f"💰 Баланс казны: {treasury_new_balance}$\n\n"
                            f"🔗 Чек: {cheque_link}"
                        )
                        for admin_id in config.ADMINS_ID:
                            try:
                                bot.send_message(admin_id, admin_message, parse_mode='HTML')
                            except:
                                continue
                    else:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.edit_message_text(
                            f"❌ <b>Не удалось создать чек для вывода</b>\n\n"
                            f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                            message.chat.id, 
                            processing_message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                else:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    bot.edit_message_text(
                        f"❌ <b>Не удалось создать чек для вывода</b>\n\n"
                        f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                        message.chat.id, 
                        processing_message.message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
            except Exception as e:
                print(f"[ERROR] Ошибка при создании чека для user_id {user_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.edit_message_text(
                    f"❌ <b>Произошла ошибка при обработке вывода</b>\n\n"
                    f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                admin_message = (
                    f"⚠️ <b>Ошибка при автоматическом выводе</b>\n\n"
                    f"👤 ID: <a href=\"https://t.me/@id{user_id}\">{user_id}</a>\n"
                    f"👤 Username: {username_link}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"❌ Ошибка: {str(e)}"
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
        except Exception as e:
            print(f"[ERROR] Общая ошибка в handle_withdrawal_request для user_id {user_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, 
                           f"❌ Произошла ошибка при обработке запроса. Пожалуйста, попробуйте позже.", 
                           reply_markup=markup)
            
@bot.callback_query_handler(func=lambda call: call.data.startswith("send_check_"))
def send_check_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return

    try:
        parts = call.data.split("_")
        user_id = int(parts[2])
        amount = float(parts[3])

        # Получаем информацию о пользователе для username
        try:
            user_info = bot.get_chat_member(user_id, user_id).user
            username = f"@{user_info.username}" if user_info.username else "Нет username"
            username_link = f"<a href=\"tg://user?id={user_id}\">{username}</a>" if user_info.username else "Нет username"
        except Exception as e:
            print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
            username_link = "Неизвестный пользователь"

        # Проверяем баланс пользователя
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
            user = cursor.fetchone()
            if not user or user[0] < amount:
                bot.answer_callback_query(call.id, f"❌ Недостаточно средств на балансе пользователя {user_id}!")
                bot.edit_message_text(
                    f"❌ Не удалось отправить чек на {amount}$ пользователю {user_id}: недостаточно средств на балансе.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML'
                )
                return

        # Создаём чек через CryptoBot API
        crypto_api = crypto_pay.CryptoPay()
        cheque_result = crypto_api.create_check(
            amount=amount,
            asset="USDT",
            description=f"Выплата для пользователя {user_id}"
        )

        if cheque_result.get("ok", False):
            cheque = cheque_result.get("result", {})
            cheque_link = cheque.get("bot_check_url", "")

            if cheque_link:
                # Уменьшаем баланс пользователя
                with db.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('UPDATE users SET BALANCE = BALANCE - ? WHERE ID = ?', (amount, user_id))
                    cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
                    new_balance = cursor.fetchone()[0]
                    conn.commit()
                    print(f"[DEBUG] Баланс пользователя {user_id} уменьшен на {amount}$, новый баланс: {new_balance}")

                # Обновляем баланс казны
                db_module.update_treasury_balance(-amount)

                # Уведомляем пользователя
                markup_user = types.InlineKeyboardMarkup()
                markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                safe_send_message(
                    user_id,
                    f"✅ Вам отправлен чек на {amount}$!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )

                # Уведомляем администратора
                markup_admin = types.InlineKeyboardMarkup()
                markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
                bot.edit_message_text(
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {user_id} ({username_link}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )

                # Логируем операцию
                db_module.log_treasury_operation("Вывод (чек)", -amount, db_module.get_treasury_balance())
            else:
                bot.edit_message_text(
                    f"❌ Не удалось создать чек для пользователя {user_id}.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML'
                )
        else:
            bot.edit_message_text(
                f"❌ Ошибка при создании чека: {cheque_result.get('error', 'Неизвестная ошибка')}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML'
            )

        bot.answer_callback_query(call.id, f"Чек на {amount}$ отправлен пользователю {user_id}.")
    except Exception as e:
        print(f"[ERROR] Ошибка в send_check_callback: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при отправке чека!")
        bot.edit_message_text(
            f"❌ Произошла ошибка при отправке чека пользователю {user_id}.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("manual_check_"))
def manual_check_request(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        msg = bot.edit_message_text(
            f"📤 Введите ссылку на чек для пользователя {user_id} на сумму {amount}$:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_check_link, user_id, amount)

def process_check_link_success(call, user_id, amount, check_link):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
        conn.commit()
    
    markup_admin = types.InlineKeyboardMarkup()
    markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(
        f"✅ Чек на сумму {amount}$ успешно создан и отправлен пользователю {user_id}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup_admin
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.send_message(int(user_id),
                       f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                       reply_markup=markup)
    except Exception as e:
        print(f"Error sending message to user {user_id}: {e}")

def process_check_link(message, user_id, amount):
    if message.from_user.id in config.ADMINS_ID:
        check_link = message.text.strip()
        
        if not check_link.startswith("https://") or "t.me/" not in check_link:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"manual_check_{user_id}_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, 
                           "❌ Неверный формат ссылки на чек. Пожалуйста, убедитесь, что вы скопировали полную ссылку.",
                           reply_markup=markup)
            return
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
            conn.commit()
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id,
                       f"✅ Чек на сумму {amount}$ успешно отправлен пользователю {user_id}",
                       reply_markup=markup_admin)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                           reply_markup=markup)
        except Exception as e:
            print(f"Error sending message to user {user_id}: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_withdraw_"))
def reject_withdraw(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        amount = float(amount)
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, int(user_id)))
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), amount))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Попробовать снова", callback_data="withdraw"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"❌ Ваша заявка на вывод {amount}$ отклонена\n💰 Средства возвращены на баланс",
                           reply_markup=markup)
        except:
            pass
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text("✅ Выплата отклонена, средства возвращены",
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=markup_admin)


#===========================================================================
#=======================КАЗНА====================КАЗНА======================
#===========================================================================

 

@bot.callback_query_handler(func=lambda call: call.data == "treasury")
def show_treasury(call):
    if call.from_user.id not in config.dostup:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    auto_input_status = db_module.get_auto_input_status()
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{crypto_balance}</code> USDT"
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nОшибка при получении баланса: <code>{str(e)}</code>"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
    markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
    auto_input_text = "🔴 Включить авто-ввод" if not auto_input_status else "🟢 Выключить авто-ввод"
    markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(
        treasury_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_withdraw")
def treasury_withdraw_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(
            f"📤 <b>Вывод средств из казны</b>\n\nТекущий баланс: <code>{crypto_balance}</code> USDT\n\nВведите сумму для вывода:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        
        bot.register_next_step_handler(msg, process_treasury_withdraw)
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_treasury_withdraw(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "⛔ У вас нет доступа к этой функции.", parse_mode='HTML')
        return
    
    admin_id = message.from_user.id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        amount = float(message.text)
        
        if amount <= 0:
            bot.send_message(
                message.chat.id,
                "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        with treasury_lock:
            crypto_api = crypto_pay.CryptoPay()
            balance_result = crypto_api.get_balance()
            
            if not balance_result.get("ok", False):
                raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
            
            crypto_balance = 0
            for currency in balance_result.get("result", []):
                if currency.get("currency_code") == "USDT":
                    crypto_balance = float(currency.get("available", "0"))
                    break
            
            print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
            
            if amount > crypto_balance:
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>Недостаточно средств на балансе CryptoBot!</b>\nТекущий баланс: <code>{crypto_balance}</code> USDT",
                    parse_mode='HTML',
                    reply_markup=markup
                )
                return
            
            amount_to_send = calculate_amount_to_send(amount)
            
            check_result = crypto_api.create_check(
                amount=amount_to_send,
                asset="USDT",
                description=f"Вывод из казны от {admin_id}"
            )
            
            if check_result.get("ok", False):
                check = check_result.get("result", {})
                check_link = check.get("bot_check_url", "")
                
                if check_link:
                    new_balance = db_module.update_treasury_balance(-amount)
                    db_module.log_treasury_operation("Автовывод через чек", amount, new_balance)
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.send_message(
                        message.chat.id,
                        f"✅ <b>Средства успешно выведены с помощью чека!</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n"
                        f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                        f"Для получения средств активируйте чек по кнопке ниже:",
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    return
            else:
                error_details = check_result.get("error_details", "Неизвестная ошибка")
                raise Exception(f"Ошибка при создании чека: {error_details}")
    
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ <b>Ошибка!</b> Введите числовое значение.",
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"Ошибка при выводе через CryptoBot: {e}")
        bot.send_message(
            message.chat.id,
            f"⚠️ <b>Ошибка при автовыводе средств:</b> {str(e)}",
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_deposit")
def treasury_deposit_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(
            f"📥 <b>Пополнение казны</b>\n\nТекущий баланс: <code>{crypto_balance}</code> USDT\n\nВведите сумму для пополнения:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        
        bot.register_next_step_handler(msg, process_treasury_deposit)
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_treasury_deposit(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "⛔ У вас нет доступа к этой функции.", parse_mode='HTML')
        return
    
    admin_id = message.from_user.id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        amount = float(message.text)
        
        if amount <= 0:
            bot.send_message(
                message.chat.id,
                "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).",
                parse_mode='HTML',
                reply_markup=markup
            )
            return
        
        markup_crypto = types.InlineKeyboardMarkup()
        markup_crypto.add(types.InlineKeyboardButton("💳 Пополнить через CryptoBot", callback_data=f"treasury_deposit_crypto_{amount}"))
        markup_crypto.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup_crypto.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.send_message(
            message.chat.id,
            f"💰 <b>Пополнение казны на {amount}$</b>\n\n"
            f"Нажмите кнопку ниже для пополнения через CryptoBot:",
            parse_mode='HTML',
            reply_markup=markup_crypto
        )
    
    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ <b>Ошибка!</b> Введите числовое значение.",
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_deposit_crypto_"))
def treasury_deposit_crypto(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    amount = float(call.data.split("_")[-1])
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        amount_with_fee = calculate_amount_to_send(amount)
        
        invoice_result = crypto_api.create_invoice(
            amount=amount_with_fee,
            asset="USDT",
            description=f"Пополнение казны от {admin_id}",
            hidden_message="Спасибо за пополнение казны!",
            paid_btn_name="callback",
            paid_btn_url=f"https://t.me/{bot.get_me().username}",
            expires_in=300
        )
        
        if invoice_result.get("ok", False):
            invoice = invoice_result.get("result", {})
            invoice_link = invoice.get("pay_url", "")
            invoice_id = invoice.get("invoice_id")
            
            if invoice_link and invoice_id:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoice_link))
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                message = bot.edit_message_text(
                    f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                    f"Сумма: <code>{amount}</code> USDT\n\n"
                    f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                    f"2. Оплатите созданный инвойс\n\n"
                    f"⚠️ <i>Инвойс действует 5 минут</i>\n\n"
                    f"⏳ <b>Ожидание оплаты...</b>",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                check_payment_thread = threading.Thread(
                    target=check_invoice_payment,
                    args=(invoice_id, amount, admin_id, call.message.chat.id, call.message.message_id)
                )
                check_payment_thread.daemon = True
                check_payment_thread.start()
                return
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        error_message = invoice_result.get("error", {}).get("message", "Неизвестная ошибка")
        bot.edit_message_text(
            f"❌ <b>Ошибка при создании инвойса</b>\n\n"
            f"Не удалось создать инвойс через CryptoBot.\n"
            f"Ошибка: {error_message}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Error creating invoice for treasury deposit: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"❌ <b>Ошибка при работе с CryptoBot</b>\n\n"
            f"Произошла ошибка: {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

def check_invoice_payment(invoice_id, amount, admin_id, chat_id, message_id):
    crypto_api = crypto_pay.CryptoPay()
    start_time = datetime.now()
    timeout = timedelta(minutes=5)
    check_interval = 5
    check_counter = 0
    
    try:
        while datetime.now() - start_time < timeout:
            print(f"Checking invoice {invoice_id} (attempt {check_counter + 1})...")
            invoices_result = crypto_api.get_invoices(invoice_ids=[invoice_id])
            print(f"Invoice API response: {invoices_result}")
            
            if invoices_result.get("ok", False):
                invoices = invoices_result.get("result", {}).get("items", [])
                
                if not invoices:
                    print(f"No invoices found for ID {invoice_id}")
                    time.sleep(check_interval)
                    check_counter += 1
                    continue
                
                status = invoices[0].get("status", "")
                print(f"Invoice {invoice_id} status: {status}")
                
                if status in ["paid", "completed"]:
                    print(f"Invoice {invoice_id} paid successfully!")
                    try:
                        with treasury_lock:
                            new_balance = db_module.update_treasury_balance(amount)
                            print(f"Updated treasury balance: {new_balance}")
                            db_module.log_treasury_operation("Пополнение через Crypto Pay", amount, new_balance)
                            print(f"Logged treasury operation: amount={amount}, new_balance={new_balance}")
                        
                        balance_result = crypto_api.get_balance()
                        crypto_balance = 0
                        if balance_result.get("ok", False):
                            for currency in balance_result.get("result", []):
                                if currency.get("currency_code") == "USDT":
                                    crypto_balance = float(currency.get("available", "0"))
                                    break
                        print(f"Баланс CryptoBot после оплаты: {crypto_balance} USDT")
                        
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"✅ <b>Казна успешно пополнена!</b>\n\n"
                            f"Сумма: <code>{amount}</code> USDT\n"
                            f"Текущий баланс казны: <code>{new_balance}</code> USDT\n"
                            f"Баланс CryptoBot: <code>{crypto_balance}</code> USDT",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        print(f"Payment confirmation message updated for invoice {invoice_id}")
                        return
                    
                    except Exception as db_error:
                        print(f"Error updating treasury balance or logging operation: {db_error}")
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"⚠️ <b>Ошибка при обновлении казны:</b> {str(db_error)}\n"
                            f"Пополнение на сумму <code>{amount}</code> USDT выполнено, но казна не обновлена.",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        return
                
                elif status == "expired":
                    print(f"Invoice {invoice_id} expired.")
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                        f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                        f"Вы можете создать новый инвойс.",
                        chat_id,
                        message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    return
                
                check_counter += 1
                if check_counter % 5 == 0:
                    elapsed = datetime.now() - start_time
                    remaining_seconds = int(timeout.total_seconds() - elapsed.total_seconds())
                    minutes = remaining_seconds // 60
                    seconds = remaining_seconds % 60
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoices[0].get("pay_url", "")))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n\n"
                        f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                        f"2. Оплатите созданный инвойс\n\n"
                        f"⏱ <b>Оставшееся время:</b> {minutes}:{seconds:02d}\n"
                        f"⏳ <b>Ожидание оплаты...</b>",
                        chat_id,
                        message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    print(f"Waiting message updated: {minutes}:{seconds:02d} remaining")
            
            else:
                print(f"API request failed: {invoices_result}")
            
            time.sleep(check_interval)
        
        print(f"Invoice {invoice_id} not paid after timeout.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
            f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
            f"Вы можете создать новый инвойс.",
            chat_id,
            message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Error in check_invoice_payment thread: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"❌ <b>Ошибка при проверке оплаты</b>\n\n"
            f"Произошла ошибка: {str(e)}\n"
            f"Пожалуйста, попробуйте снова.",
            chat_id,
            message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_toggle_auto")
def treasury_toggle_auto_input(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    
    new_status = db_module.toggle_auto_input()
    
    try:
        crypto_api = crypto_pay.CryptoPay()
        balance_result = crypto_api.get_balance()
        
        if not balance_result.get("ok", False):
            raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
        
        crypto_balance = 0
        for currency in balance_result.get("result", []):
            if currency.get("currency_code") == "USDT":
                crypto_balance = float(currency.get("available", "0"))
                break
        
        print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
        
        status_text = "включен" if new_status else "выключен"
        operation = f"Авто-ввод {status_text}"
        db_module.log_treasury_operation(operation, 0, crypto_balance)
        
        status_emoji = "🟢" if new_status else "🔴"
        auto_message = f"{status_emoji} <b>Авто-ввод {status_text}!</b>\n"
        if new_status:
            auto_message += "Средства будут автоматически поступать в казну."
        else:
            auto_message += "Средства больше не будут автоматически поступать в казну."
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{crypto_balance}</code> USDT\n\n{auto_message}"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
        markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
        
        auto_input_text = "🔴 Включить авто-ввод" if not new_status else "🟢 Выключить авто-ввод"
        markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
        
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            treasury_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    except Exception as e:
        print(f"Ошибка при получении баланса CryptoBot: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"⚠️ <b>Ошибка при получении баланса:</b> {str(e)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_withdraw_all_"))
def treasury_withdraw_all(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "⛔ У вас нет доступа к этой функции.", show_alert=True)
        return
    
    admin_id = call.from_user.id
    amount = float(call.data.split("_")[-1])
    
    if amount <= 0:
        bot.answer_callback_query(call.id, "⚠️ Баланс казны пуст. Нечего выводить.", show_alert=True)
        return
    
    with treasury_lock:
        operation_success = False
        
        try:
            crypto_api = crypto_pay.CryptoPay()
            balance_result = crypto_api.get_balance()
            
            if not balance_result.get("ok", False):
                raise Exception(f"Ошибка API CryptoBot: {balance_result.get('error', 'Неизвестная ошибка')}")
            
            crypto_balance = 0
            for currency in balance_result.get("result", []):
                if currency.get("currency_code") == "USDT":
                    crypto_balance = float(currency.get("available", "0"))
                    break
            
            print(f"Текущий баланс CryptoBot: {crypto_balance} USDT")
            
            if crypto_balance < amount:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"❌ <b>Недостаточно средств на балансе CryptoBot!</b>\n"
                    f"Баланс: <code>{crypto_balance}</code> USDT, требуется: <code>{amount}</code> USDT.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                return
            
            amount_to_send = calculate_amount_to_send(amount)
            
            check_result = crypto_api.create_check(
                amount=amount_to_send,
                asset="USDT",
                description=f"Вывод всей казны от {admin_id}"
            )
            
            if check_result.get("ok", False):
                check = check_result.get("result", {})
                check_link = check.get("bot_check_url", "")
                
                if check_link:
                    new_balance = db_module.update_treasury_balance(-amount)
                    db_module.log_treasury_operation("Вывод всей казны через чек", amount, new_balance)
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    bot.edit_message_text(
                        f"✅ <b>Все средства успешно выведены с помощью чека!</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n"
                        f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                        f"Для получения средств активируйте чек по кнопке ниже:",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    operation_success = True
                    return
                else:
                    error_details = check_result.get("error_details", "Неизвестная ошибка")
                    raise Exception(f"Ошибка при создании чека: {error_details}")
        
        except Exception as e:
            print(f"Ошибка при выводе через CryptoBot: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.edit_message_text(
                f"⚠️ <b>Ошибка при работе с CryptoBot:</b> {str(e)}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        
        if not operation_success:
            new_balance = db_module.update_treasury_balance(-amount)
            db_module.log_treasury_operation("Вывод всей казны", amount, new_balance)
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.edit_message_text(
                f"✅ <b>Все средства успешно выведены!</b>\n\n"
                f"Сумма: <code>{amount}</code> USDT\n"
                f"Остаток в казне: <code>{new_balance}</code> USDT",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )

def calculate_amount_to_send(target_amount):
    """
    Рассчитывает сумму для отправки с учётом комиссии CryptoBot (3%).
    Возвращает сумму, которую нужно отправить, чтобы после комиссии получить target_amount.
    """
    commission_rate = 0.03  # Комиссия 3%
    amount_with_fee = target_amount / (1 - commission_rate) 
    rounded_amount = round(amount_with_fee, 2)  
    
    received_amount = rounded_amount * (1 - commission_rate)
    if received_amount < target_amount:
        rounded_amount += 0.01  
    
    return round(rounded_amount, 2)



# ╔══════════════════════════════════════╗
# ║        📢 БЛОК: РАССЫЛКА             ║
# ╚══════════════════════════════════════╝

broadcast_state = {}

@bot.callback_query_handler(func=lambda call: call.data == "broadcast")
def request_broadcast_message(call):
    """Запрос на ввод текста для рассылки"""
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для рассылки!")
        return

    broadcast_state[call.from_user.id] = {"active": True}

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    markup.add(InlineKeyboardButton("📋 Админ-панель", callback_data="admin_panel"))

    msg = bot.edit_message_text(
        "📢 <b>Введите текст для рассылки:</b>\n\n"
        "ℹ️ Сообщение будет отправлено всем пользователям (кроме модераторов и админов).",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, process_broadcast_message)


def process_broadcast_message(message):
    """Обработка текста для рассылки"""
    user_id = message.from_user.id

    if user_id not in broadcast_state or not broadcast_state[user_id].get("active", False):
        return
    if user_id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для рассылки!")
        return

    broadcast_text = message.text
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.ID
                FROM users u
                LEFT JOIN personal p ON u.ID = p.ID
                WHERE p.TYPE IS NULL OR p.TYPE NOT IN ('moder', 'ADMIN')
            ''')
            users = cursor.fetchall()

        success, failed = 0, 0
        for user in users:
            try:
                bot.send_message(user[0], broadcast_text)
                success += 1
                time.sleep(0.05)
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
                failed += 1

        stats_text = (
            "📊 <b>Статистика рассылки</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"✅ Успешно: <b>{success}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"👥 Всего получателей: <b>{len(users)}</b>"
        )

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Новая рассылка", callback_data="broadcast"))
        markup.add(InlineKeyboardButton("📋 Админ-панель", callback_data="admin_panel"))
        markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

        bot.send_message(message.chat.id, stats_text, reply_markup=markup, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Ошибка при рассылке: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при рассылке.")
    finally:
        broadcast_state.pop(user_id, None)


# ----------------------------
#  НАСТРОЙКИ -----------------
# ----------------------------
@bot.callback_query_handler(func=lambda call: call.data == "settings")
def show_settings(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        r = cursor.fetchone()
        price, hold_time = r if r else (2.0, 5)
    text = header("⚙️", "Настройки оплаты") + f"💵 <b>Ставка:</b> <code>{price}$</code>\n⏱ <b>Холд:</b> <code>{hold_time} мин</code>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💰 Изменить сумму", callback_data="change_amount"))
    markup.add(types.InlineKeyboardButton("⏱ Изменить холд", callback_data="change_hold_time"))
    markup.add(types.InlineKeyboardButton("📋 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "change_amount")
def change_amount_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    msg = bot.edit_message_text("💰 <b>Введите новую сумму (в $):</b>\n" + DIV, call.message.chat.id, call.message.message_id, parse_mode='HTML')
    bot.register_next_step_handler(msg, process_change_amount)

def process_change_amount(message):
    if message.from_user.id not in config.ADMINS_ID:
        return
    try:
        v = float(message.text.strip())
        if v <= 0:
            raise ValueError
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE settings SET PRICE = ?', (v,))
            conn.commit()
        bot.send_message(message.chat.id, f"✅ <b>Новая ставка:</b> <code>{v}$</code>", parse_mode='HTML',
                         reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")))
    except Exception:
        bot.send_message(message.chat.id, "❌ Введите корректное положительное число.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")))

@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    msg = bot.edit_message_text("⏱ <b>Введите новое время холда (мин):</b>\n" + DIV, call.message.chat.id, call.message.message_id, parse_mode='HTML')
    bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id not in config.ADMINS_ID:
        return
    try:
        v = int(message.text.strip())
        if v <= 0:
            raise ValueError
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE settings SET HOLD_TIME = ?', (v,))
            conn.commit()
        bot.send_message(message.chat.id, f"✅ <b>Новый холд:</b> <code>{v} мин</code>", parse_mode='HTML',
                         reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")))
    except Exception:
        bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")))

# ----------------------------
#  МОДЕРАТОРЫ — СПИСОК / ДОБАВИТЬ / УДАЛИТЬ
# ----------------------------
@bot.callback_query_handler(func=lambda call: call.data == "moderators")
def moderators_menu(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Добавить", callback_data="add_moder"))
    markup.add(types.InlineKeyboardButton("➖ Удалить", callback_data="remove_moder"))
    markup.add(types.InlineKeyboardButton("🗂 Удалить через кнопку", callback_data="delete_moderator"))
    markup.add(types.InlineKeyboardButton("👥 Список модераторов", callback_data="all_moderators_1"))
    markup.add(types.InlineKeyboardButton("📋 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, header("👥", "Управление модераторами"), parse_mode='HTML', reply_markup=markup)




@bot.callback_query_handler(func=lambda call: call.data == "add_moder")
def add_moder_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
    msg = bot.send_message(call.message.chat.id, "👤 <b>Введите ID пользователя для назначения модератором:</b>", parse_mode='HTML', reply_markup=markup)
    bot.register_next_step_handler(msg, process_add_moder, msg.message_id)

def process_add_moder(message, initial_message_id):
    try:
        new_moder_id = int(message.text.strip())
    except Exception:
        bot.send_message(message.chat.id, "❌ Введите корректный числовой ID!", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (new_moder_id, 'moder'))
        if cursor.fetchone() is not None:
            bot.send_message(message.chat.id, "⚠️ Этот пользователь уже модератор!", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
            return
        cursor.execute('SELECT COUNT(*) FROM groups')
        if cursor.fetchone()[0] == 0:
            bot.send_message(message.chat.id, "❌ Нет групп. Создайте группу прежде чем назначать модератора.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("👥 Группы", callback_data="groups")))
            return

    try:
        bot.delete_message(message.chat.id, message.message_id)
        bot.delete_message(message.chat.id, initial_message_id)
    except Exception:
        pass

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
    msg = bot.send_message(message.chat.id, f"👤 ID: <code>{new_moder_id}</code>\n📝 Введите название группы для назначения:", parse_mode='HTML', reply_markup=markup)
    bot.register_next_step_handler(msg, process_assign_group, new_moder_id, msg.message_id)


def process_assign_group(message, new_moder_id, group_message_id):
    group_name = message.text.strip()
    if not group_name:
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE NAME = ?', (group_name,))
        row = cursor.fetchone()
        if not row:
            bot.send_message(message.chat.id, f"❌ Группа '{group_name}' не найдена.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("👥 Группы", callback_data="groups")))
            return
        group_id = row[0]
        try:
            cursor.execute('INSERT INTO personal (ID, TYPE, GROUP_ID) VALUES (?, ?, ?)', (new_moder_id, 'moder', group_id))
            conn.commit()
        except Exception as e:
            logging.error(f"[MODER] {e}")
            bot.send_message(message.chat.id, "❌ Ошибка при назначении модератора.")
            return

    try:
        moder_msg = bot.send_message(new_moder_id, f"🎉 Вам выданы права модератора в группе '{group_name}'! Напишите /start")
        threading.Timer(30.0, lambda: bot.delete_message(new_moder_id, moder_msg.message_id)).start()
    except Exception:
        pass

    bot.send_message(message.chat.id, f"✅ Пользователь <code>{new_moder_id}</code> назначен модератором группы <b>{group_name}</b>.", parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))



@bot.callback_query_handler(func=lambda call: call.data == "remove_moder")
def remove_moder_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    msg = bot.send_message(call.message.chat.id, "👤 <b>Введите ID модератора для удаления:</b>", parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
    bot.register_next_step_handler(msg, process_remove_moder)

def process_remove_moder(message):
    try:
        moder_id = int(message.text.strip())
    except Exception:
        bot.send_message(message.chat.id, "❌ Введите корректный ID.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM personal WHERE ID = ? AND TYPE = ?', (moder_id, 'moder'))
        conn.commit()
        affected = cursor.rowcount

    if affected > 0:
        try:
            msg = bot.send_message(moder_id, "⚠️ Вам отозвали права модератора.")
            threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
        except Exception:
            pass
        bot.send_message(message.chat.id, f"✅ Модератор <code>{moder_id}</code> удалён.", parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
    else:
        bot.send_message(message.chat.id, "⚠️ Данный пользователь не был модератором.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))


@bot.callback_query_handler(func=lambda call: call.data == "delete_moderator")
def delete_moderator_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ID FROM personal WHERE TYPE = 'moder'")
        moderators = cursor.fetchall()
    if not moderators:
        bot.send_message(call.message.chat.id, "📭 Нет модераторов для удаления.", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators")))
        return
    text = "👥 <b>Выберите модератора для удаления:</b>\n" + DIV + "\n"
    markup = types.InlineKeyboardMarkup()
    for m in moderators:
        mid = m[0]
        text += f"• <code>{mid}</code>\n"
        markup.add(types.InlineKeyboardButton(f"Удалить {mid}", callback_data=f"confirm_delete_moder_{mid}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_moder_"))
def confirm_delete_moderator(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "🚫 У вас нет доступа!")
        return
    try:
        mid = int(call.data.split("_")[3])
    except Exception:
        bot.answer_callback_query(call.id, "❌ Ошибка данных!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM personal WHERE ID = ? AND TYPE = 'moder'", (mid,))
        affected = cursor.rowcount
        conn.commit()
    if affected > 0:
        try:
            mmsg = bot.send_message(mid, "⚠️ Вам отозваны права модератора.")
            threading.Timer(30.0, lambda: bot.delete_message(mid, mmsg.message_id)).start()
        except Exception:
            pass
        bot.send_message(call.message.chat.id, f"✅ Модератор <code>{mid}</code> удалён.", parse_mode='HTML')
    else:
        bot.send_message(call.message.chat.id, f"❌ Модератор <code>{mid}</code> не найден.", parse_mode='HTML')



@bot.callback_query_handler(func=lambda call: call.data.startswith("all_moderators_"))
def all_moderators_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра списка модераторов!")
        return
    
    try:
        page = int(call.data.split("_")[2])
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        page = 1
    
    with get_db() as conn:
        cursor = conn.cursor()
        # Получаем всех модераторов и их группы (без USERNAME)
        cursor.execute('''
            SELECT p.ID, g.NAME
            FROM personal p
            LEFT JOIN groups g ON p.GROUP_ID = g.ID
            WHERE p.TYPE = 'moder'
            ORDER BY p.ID
        ''')
        moderators = cursor.fetchall()
    
    if not moderators:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Назад", callback_data="moderators"))
        bot.edit_message_text(
            "📭 Нет модераторов.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return
    
    items_per_page = 10
    total_pages = (len(moderators) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_moderators = moderators[start_idx:end_idx]
    
    text = f"<b>👥 Список модераторов (страница {page}/{total_pages}):</b>\n\n"
    with get_db() as conn:
        cursor = conn.cursor()
        for idx, (moder_id, group_name) in enumerate(page_moderators, start=start_idx + 1):
            # Подсчёт успешных номеров для модератора
            cursor.execute('''
                SELECT COUNT(*) 
                FROM numbers 
                WHERE CONFIRMED_BY_MODERATOR_ID = ? AND STATUS = 'отстоял'
            ''', (moder_id,))
            accepted_numbers = cursor.fetchone()[0]
            
            # Получаем username через Telegram API
            try:
                user = bot.get_chat(moder_id)
                username = user.username if user.username else "Нет username"
            except Exception as e:
                logging.error(f"Ошибка при получении username для user_id {moder_id}: {e}")
                username = "Ошибка получения"
            
            group_display = group_name if group_name else "Без группы"
            # Форматируем UserID как ссылку
            text += f"{idx}. 🆔UserID: <a href=\"tg://user?id={moder_id}\">{moder_id}</a>\n"
            text += f"Username: @{username}\n"
            text += f"🏠 Группа: {group_display}\n"
            text += f"📱 Принято номеров: {accepted_numbers}\n"
            text += "────────────────────\n"
    
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Слишком много данных, используйте пагинацию)"
    
    markup = InlineKeyboardMarkup()
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"all_moderators_{page-1}"))
        row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_moderators_{page+1}"))
        markup.row(*row)
    
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения all_moderators: {e}")
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    bot.answer_callback_query(call.id)




#=======================================================================================
#=======================================================================================
#===================================ГРУППЫ==============================================
#=======================================================================================
#=======================================================================================
#=======================================================================================




# ╔════════════════════════════════════╗
# ║   👥 УПРАВЛЕНИЕ ГРУППАМИ - АДМИН    ║
# ╚════════════════════════════════════╝

@bot.callback_query_handler(func=lambda call: call.data == "groups")
def groups_menu(call):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления группами!")
        return

    text = (
        "📂 <b>Управление группами</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Здесь вы можете добавлять, удалять группы, а также смотреть их статистику.\n"
        "Выберите нужное действие из меню ниже 👇"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("➕ Добавить группу", callback_data="add_group"),
        types.InlineKeyboardButton("➖ Удалить группу", callback_data="remove_group")
    )
    markup.add(types.InlineKeyboardButton("📊 Статистика групп", callback_data="group_statistics"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main"))

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

# ╔════════════════════════════════════╗
# ║   🆕 СОЗДАНИЕ ГРУППЫ                ║
# ╚════════════════════════════════════╝

@bot.callback_query_handler(func=lambda call: call.data == "create_group")
def create_group_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для создания группы!")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    msg = bot.send_message(
        call.message.chat.id,
        "🆕 <b>Создание новой группы</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 Пожалуйста, введите <u>название</u> для новой группы:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_create_group, msg.message_id)

def process_create_group(message, initial_message_id):
    if message.from_user.id not in config.ADMINS_ID:
        return

    group_name = message.text.strip()
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        bot.send_message(message.chat.id, "⚠️ Название группы не может быть пустым!", reply_markup=markup)
        return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO groups (NAME) VALUES (?)', (group_name,))
            conn.commit()

        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except:
            pass

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👥 Вернуться к группам", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main"))

        bot.send_message(
            message.chat.id,
            f"✅ Группа <b>{group_name}</b> успешно создана!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Теперь вы можете назначать в неё модераторов 📋",
            parse_mode="HTML",
            reply_markup=markup
        )

    except sqlite3.IntegrityError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        bot.send_message(
            message.chat.id,
            f"⚠️ Группа с названием <b>{group_name}</b> уже существует!",
            parse_mode="HTML",
            reply_markup=markup
        )

# ╔════════════════════════════════════╗
# ║   ❌ УДАЛЕНИЕ ГРУППЫ                ║
# ╚════════════════════════════════════╝

@bot.callback_query_handler(func=lambda call: call.data == "delete_group")
def delete_group_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для удаления группы!")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    msg = bot.send_message(
        call.message.chat.id,
        "🗑 <b>Удаление группы</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Введите <u>название</u> группы, которую хотите удалить:",
        parse_mode="HTML",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_delete_group)

def process_delete_group(message):
    if message.from_user.id not in config.ADMINS_ID:
        return

    group_name = message.text.strip()
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        bot.send_message(message.chat.id, "⚠️ Название группы не может быть пустым!", reply_markup=markup)
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE NAME = ?', (group_name,))
        group = cursor.fetchone()

        if group:
            group_id = group[0]
            cursor.execute('UPDATE personal SET GROUP_ID = NULL WHERE GROUP_ID = ?', (group_id,))
            cursor.execute('DELETE FROM groups WHERE ID = ?', (group_id,))
            conn.commit()

            bot.send_message(
                message.chat.id,
                f"✅ Группа <b>{group_name}</b> успешно удалена!",
                parse_mode="HTML"
            )
        else:
            bot.send_message(
                message.chat.id,
                f"⚠️ Группа с названием <b>{group_name}</b> не найдена!",
                parse_mode="HTML"
            )


@bot.callback_query_handler(func=lambda call: call.data.startswith("view_group_stats_"))
def view_group_stats(call):
    user_id = call.from_user.id
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    try:
        _, _, group_id, page = call.data.split("_")
        group_id = int(group_id)
        page = int(page)
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        # Подсчитываем участников (модераторов) группы
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = "moder"', (group_id,))
        member_count = cursor.fetchone()[0]

        # Получаем номера с статусом "отстоял" для конкретной группы
        cursor.execute('''
            SELECT n.NUMBER, n.TAKE_DATE, n.SHUTDOWN_DATE
            FROM numbers n
            LEFT JOIN personal p ON p.ID = n.ID_OWNER
            WHERE n.STATUS = 'отстоял'
            AND (p.GROUP_ID = ? OR p.GROUP_ID IS NULL)
            ORDER BY n.SHUTDOWN_DATE DESC
        ''', (group_id,))
        numbers = cursor.fetchall()

    # Пагинация
    items_per_page = 20
    total_pages = max(1, (len(numbers) + items_per_page - 1) // items_per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_numbers = numbers[start_idx:end_idx]

    # Формируем текст статистики
    text = (
        f"<b>📊 Статистика группы {group_id}:</b>\n\n"
        f"📱 Успешных номеров: {len(numbers)}\n"
        f"────────────────────\n"
        f"<b>📱 Список номеров (страница {page}/{total_pages}):</b>\n\n"
    )

    if not page_numbers:
        text += "📭 Нет успешных номеров в этой группе."
    else:
        for number, take_date, shutdown_date in page_numbers:
            text += f"Номер: {number}\n"
            text += f"🟢 Встал: {take_date}\n"
            text += f"🟢 Отстоял: {shutdown_date}\n"
            text += "───────────────────\n"

    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"

    # Формируем разметку
    markup = types.InlineKeyboardMarkup()

    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"view_group_stats_{group_id}_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"view_group_stats_{group_id}_{page+1}"))
        if row:
            markup.row(*row)

    markup.add(types.InlineKeyboardButton("👥 Все группы", callback_data="admin_view_groups"))
    markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )









#=======================================================================================
#=======================================================================================
#===================================АДМИНКА=====================================
#=======================================================================================
#=======================================================================================
#=======================================================================================


@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def admin_panel(call):
    user_id = call.from_user.id
    broadcast_state.pop(user_id, None)
    with treasury_lock:
        if call.from_user.id in active_treasury_admins:
            del active_treasury_admins[call.from_user.id]

    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()

            # Считаем статистику
            cursor.execute('SELECT COUNT(*) FROM numbers')
            total_numbers = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "отстоял"')
            stood_numbers = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "невалид"')
            invalid_numbers = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "слетел"')
            dropped_numbers = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "ожидает"')
            pending_numbers = cursor.fetchone()[0]

            # Красивый текст панели
            admin_text = (
                "⚙️ <b>Панель администратора</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Всего номеров в БД:</b> <code>{total_numbers}</code>\n"
                f"🏆 <b>Отстоявших номеров:</b> <code>{stood_numbers}</code>\n"
                f"🚫 <b>Невалидных номеров:</b> <code>{invalid_numbers}</code>\n"
                f"📉 <b>Слетевших номеров:</b> <code>{dropped_numbers}</code>\n"
                f"⏳ <b>Ожидают проверки:</b> <code>{pending_numbers}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━"
            )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="Gv"))
        markup.add(types.InlineKeyboardButton("👥 Модераторы", callback_data="moderators"))
        markup.add(types.InlineKeyboardButton("👤 Все пользователи", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("📝 Заявки на вступление", callback_data="pending_requests"))
        markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("📱 Все номера", callback_data="all_numbers"))
        markup.add(types.InlineKeyboardButton("🔍 Найти номер", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="broadcast"))
        markup.add(types.InlineKeyboardButton("💰 Казна", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings"))
        markup.add(types.InlineKeyboardButton("🗃 БД", callback_data="db_menu"))
        markup.add(types.InlineKeyboardButton("📩 Апелляция номеров", callback_data="admin_search_appeal"))
        markup.add(types.InlineKeyboardButton("Вернуться назад", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass

        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)


def check_time():
    while True:
        current_time = datetime.now().strftime("%H:%M")
        if current_time == config.CLEAR_TIME:
            clear_database()
            time.sleep(61)
        time.sleep(30)




#ЧИСТКА ЛИБО В РУЧНУЮ ЛИБО АВТОМАТИЧЕСКИ БАЗЫ ДАННЫХ ( НОМЕРА )

def clear_database(chat_id=None):
    """Очищает все номера из таблицы numbers и обнуляет баланс пользователей."""
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            
            # Получаем пользователей, у которых есть номера, исключая админов и модераторов
            cursor.execute('''
                SELECT DISTINCT ID_OWNER 
                FROM numbers 
                WHERE ID_OWNER NOT IN (SELECT ID FROM personal WHERE TYPE IN ('ADMIN', 'moder'))
            ''')
            users_with_numbers = [row[0] for row in cursor.fetchall()]
            
            # Получаем всех пользователей для обнуления баланса
            cursor.execute('SELECT ID FROM users')
            all_users = [row[0] for row in cursor.fetchall()]
            
            # Удаляем все номера
            cursor.execute('DELETE FROM numbers')
            deleted_numbers = cursor.rowcount
            
            # Обнуляем баланс всех пользователей
            cursor.execute('UPDATE users SET BALANCE = 0')
            reset_balances = cursor.rowcount
            conn.commit()
            
            logging.info(f"Удалено {deleted_numbers} номеров и обнулено {reset_balances} балансов в {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.")
            
            # Уведомляем пользователей с номерами
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            for user_id in users_with_numbers:
                try:
                    bot.send_message(
                        user_id,
                        "🔄 Все номера очищены, а ваш баланс обнулён.\n📱 Пожалуйста, поставьте свои номера снова.",
                        reply_markup=markup
                    )
                    logging.info(f"Уведомление отправлено пользователю {user_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
            
            # Уведомляем админов
            admin_message = (
                f"🔄 Все номера и балансы очищены.\n"
                f"🗑 Удалено {deleted_numbers} номеров.\n"
                f"💸 Обнулено {reset_balances} балансов."
            )
            for admin_id in config.ADMINS_ID:
                try:
                    bot.send_message(admin_id, admin_message)
                    logging.info(f"Уведомление отправлено админу {admin_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
            
            # Если очистка вызвана админом, отправляем подтверждение
            if chat_id:
                bot.send_message(
                    chat_id,
                    f"✅ Таблица номеров и балансы очищены.\n"
                    f"🗑 Удалено {deleted_numbers} номеров.\n"
                    f"💸 Обнулено {reset_balances} балансов."
                )

    except Exception as e:
        logging.error(f"Ошибка при очистке таблицы numbers или обнулении балансов: {e}")
        if chat_id:
            bot.send_message(chat_id, "❌ Ошибка при очистке номеров и балансов.")

def download_numbers(chat_id):
    """Создаёт и отправляет текстовый файл с данными из таблицы numbers."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM numbers')
            rows = cursor.fetchall()
            
            if not rows:
                bot.send_message(chat_id, "📭 Таблица номеров пуста.")
                return
            
            # Создаём текстовый файл в памяти
            output = io.StringIO()
            # Заголовки столбцов
            columns = [desc[0] for desc in cursor.description]
            output.write(','.join(columns) + '\n')
            # Данные
            for row in rows:
                output.write(','.join(str(val) if val is not None else '' for val in row) + '\n')
            
            # Подготовка файла для отправки
            output.seek(0)
            file_content = output.getvalue().encode('utf-8')
            file = io.BytesIO(file_content)
            file.name = 'numbers.txt'
            
            # Отправка файла
            bot.send_document(chat_id, file, caption="📄 Данные из таблицы номеров")
            logging.info(f"Файл numbers.txt отправлен админу {chat_id}")
    
    except Exception as e:
        logging.error(f"Ошибка при скачивании таблицы numbers: {e}")
        bot.send_message(chat_id, "❌ Ошибка при скачивании таблицы номеров.")

def schedule_clear_database():
    """Настраивает планировщик для очистки таблицы numbers и обнуления балансов в указанное время."""
    schedule.every().day.at(config.CLEAR_TIME).do(clear_database)
    logging.info(f"Планировщик настроен для очистки номеров и балансов в {config.CLEAR_TIME}")

    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logging.info("Планировщик очистки запущен.")


#АППЕЛЯЦИЯ НОМЕРОВ
pending_appeals = {}


@bot.callback_query_handler(func=lambda call: call.data == "admin_search_appeal")
def admin_search_appeal_start(call):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))

    bot.send_message(
        call.message.chat.id,
        "Введите номер телефона для поиска апелляции:",
        reply_markup=markup
    )
    bot.register_next_step_handler(call.message, admin_process_appeal_number)

def admin_process_appeal_number(message):
    number = message.text.strip()

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.NUMBER, u.USERNAME, u.ID
            FROM numbers n
            JOIN users u ON n.ID_OWNER = u.ID
            WHERE n.NUMBER = ? AND n.STATUS = "невалид"
        ''', (number,))
        row = cursor.fetchone()

    if not row:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_search_appeal"))
        bot.send_message(message.chat.id, f"❌ Номер {number} не найден среди невалидных.", reply_markup=markup)
        return

    num, username, user_id = row
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_appeal_{num}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_appeal_{num}")
    )
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_search_appeal"))

    bot.send_message(
        message.chat.id,
        f"📱 <b>{num}</b>\n👤 @{username or 'без ника'}\n🆔 {user_id}",
        parse_mode='HTML',
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_approve_appeal_"))
def admin_approve_appeal(call):
    number = call.data.replace("admin_approve_appeal_", "")

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE numbers SET STATUS = "ожидает" WHERE NUMBER = ?', (number,))
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner_id = cursor.fetchone()[0]
        conn.commit()

    bot.send_message(call.message.chat.id, f"✅ Номер {number} переведён в статус 'ожидает'.")
    bot.send_message(owner_id, f"✅ Ваш номер {number} одобрен и переведён в статус 'ожидает'.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_appeal_"))
def admin_reject_appeal(call):
    number = call.data.replace("admin_reject_appeal_", "")

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner_id = cursor.fetchone()[0]
        conn.commit()

    bot.send_message(call.message.chat.id, f"❌ Номер {number} остался в статусе 'невалид'.")
    bot.send_message(owner_id, f"❌ Ваш номер {number} остался в статусе 'невалид'.")


#ПОИСК НОМЕРА ИНФОРМАЦИЯ О НЁМ

def run_bot():
    time_checker = threading.Thread(target=check_time)
    time_checker.daemon = True
    time_checker.start()
    bot.polling(none_stop=True, skip_pending=True)
class AdminStates(StatesGroup):
    waiting_for_number = State()


@bot.callback_query_handler(func=lambda call: call.data == "search_number")
def search_number_callback(call):
    user_id = call.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Отправляем сообщение с просьбой ввести номер
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
    msg = bot.edit_message_text(
        "📱 Пожалуйста, введите номер телефона в формате +79991234567 (используйте reply на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    # Регистрируем следующий шаг для обработки введённого номера
    bot.register_next_step_handler(msg, process_search_number, call.message.chat.id, msg.message_id)

def process_search_number(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Проверяем, что сообщение является ответом (reply)
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в админку", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода номера!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Нормализуем введённый номер
    number_input = message.text.strip()
    normalized_number = is_russian_number(number_input)
    if not normalized_number:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
        markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат номера! Используйте российский номер, например: +79991234567",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Удаляем сообщение с введённым номером
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с номером {normalized_number}: {e}")
    
    # Ищем информацию о номере в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT NUMBER, ID_OWNER, STATUS, TAKE_DATE, SHUTDOWN_DATE, CONFIRMED_BY_MODERATOR_ID, TG_NUMBER, SUBMIT_DATE, GROUP_CHAT_ID, fa
            FROM numbers
            WHERE NUMBER = ?
        ''', (normalized_number,))
        number_data = cursor.fetchone()
    
    # Формируем сообщение с информацией о номере
    if number_data:
        number, owner_id, status, take_date, shutdown_date, confirmed_by_moderator_id, tg_number, submit_date, group_chat_id, fa_code = number_data
        
        # Получаем имя группы
        group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
        
        # Формируем отображаемые даты
        take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
        shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
        
        # Получаем username модератора через Telegram API
        moderator_info = "Модератор: Не назначен"
        if confirmed_by_moderator_id:
            try:
                moderator_info_data = bot.get_chat_member(message.chat.id, confirmed_by_moderator_id).user
                moderator_username = f"@{moderator_info_data.username}" if moderator_info_data.username else f"ID {confirmed_by_moderator_id}"
                moderator_info = f"Модератор: {moderator_username}"
            except Exception as e:
                print(f"[ERROR] Не удалось получить username модератора {confirmed_by_moderator_id}: {e}")
                moderator_info = f"Модератор: ID {confirmed_by_moderator_id}"
        
        # Получаем username владельца из таблицы users
        cursor.execute('SELECT USERNAME FROM users WHERE ID = ?', (owner_id,))
        owner_data = cursor.fetchone()
        owner_username = f"@{owner_data[0]}" if owner_data and owner_data[0] else "Нет username"
        
        # Формируем 2FA текст
        fa_text = f"2FA: {fa_code}" if fa_code else "2FA: не установлен"
        
        # Формируем текст для номера с кликабельной ссылкой на ID владельца
        text = (
            f"📱 <b>Информация о номере:</b>\n\n"
            f"📱 Номер: <code>{number}</code>\n"
            f"👤 Владелец: <a href=\"tg://user?id={owner_id}\">ID {owner_id}</a>\n"
            f"Username: {owner_username}\n"
            f"{fa_text}\n"
            f"📊 Статус: {status}\n"
            f"🟢 Взято: {take_date_str}\n"
            f"🔴 Отстоял: {shutdown_date_str}\n"
            f"{moderator_info}\n"
            f"🏷 Группа: {group_name}\n"
            f"📱 ТГ: {tg_number or 'Не указан'}\n"
        )
    else:
        text = f"❌ Номер <code>{normalized_number}</code> не найден в базе данных."
    
    # Обновляем исходное сообщение
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Поиск другого номера", callback_data="search_number"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в админку", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        # Резервный вариант: отправляем новое сообщение с экранированием
        try:
            bot.send_message(
                original_chat_id,
                text,
                parse_mode='HTML',
                reply_markup=markup,
                disable_web_page_preview=True
            )
        except Exception as e2:
            print(f"[ERROR] Не удалось отправить новое сообщение: {e2}")
            # Последний резерв: отправляем без HTML
            bot.send_message(
                original_chat_id,
                text.replace('<b>', '').replace('</b>', '').replace('<code>', '').replace('</code>', '').replace('<a href="tg://user?id=', '').replace('">ID ', ': ID ').replace('</a>', ''),
                reply_markup=markup
            )
#============================

@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("⏱ Введите новое время холда (в минутах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_time = int(message.text)
            if new_time <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET HOLD_TIME = ?', (new_time,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Время холда изменено на {new_time} минут", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число!", reply_markup=markup)






#  КОД ДЛЯ ПРИНЯТИЕ ОТКАЗА ЗАЯВОК В БОТА

@bot.callback_query_handler(func=lambda call: call.data.startswith("pending_requests"))
def show_pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра заявок!")
        return
    
    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
            if page < 1:
                page = 1
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = "pending"')
        requests = cursor.fetchall()
    
    if not requests:
        text = "📭 Нет заявок на вступление."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )
        return
    
    # Пагинация
    items_per_page = 20
    total_pages = (len(requests) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_requests = requests[start_idx:end_idx]
    
    text = f"<b>📝 Заявки на вступление (страница {page}/{total_pages}):</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    
    for user_id, last_request in page_requests:
        try:
            user = bot.get_chat_member(user_id, user_id).user
            username = f"@{user.username}" if user.username else "Нет username"
        except:
            username = "Неизвестный пользователь"
        
        text += (
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: {username}\n"
            f"📅 Дата заявки: {last_request}\n"
            f"────────────────────\n"
        )
        
        markup.row(
            types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}"),
            types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
        )
    
    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"
    
    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"pending_requests_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"pending_requests_{page+1}"))
        if row:
            markup.row(*row)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )
    except:
        bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=markup,
            disable_notification=True  # Отключаем уведомление
        )

#ВСЕ ПОЛЬЗОВАТЕЛИ :
admin_page_context = {}

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_users_"))
def show_all_users(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра пользователей!")
        return
    
    try:
        page = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        page = 1  # Если что-то пошло не так, открываем первую страницу
    
    # Сохраняем текущую страницу для администратора
    admin_page_context[call.from_user.id] = page
    
    # Получаем только принятых пользователей из таблицы requests
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM requests WHERE STATUS = ?', ('approved',))
        all_users = cursor.fetchall()
    
    if not all_users:
        text = "📭 Нет принятых пользователей в боте."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        # Пагинация
        users_per_page = 8
        total_pages = (len(all_users) + users_per_page - 1) // users_per_page
        page = max(1, min(page, total_pages))  # Ограничиваем страницу допустимым диапазоном
        
        start_idx = (page - 1) * users_per_page
        end_idx = start_idx + users_per_page
        page_users = all_users[start_idx:end_idx]
        
        # Формируем текст
        text = f"<b>Управляйте принятыми пользователями:</b>\n({page} страница)\n\n"
        markup = types.InlineKeyboardMarkup()
        
        # Добавляем кнопки для каждого пользователя
        for user_data in page_users:
            user_id = user_data[0]
            try:
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
            except:
                username = "Неизвестный пользователь"
            
            markup.add(types.InlineKeyboardButton(f"{user_id} {username}", callback_data=f"user_details_{user_id}"))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = []
            if page > 1:
                row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_users_{page-1}"))
            row.append(types.InlineKeyboardButton(f"{page}", callback_data=f"all_users_{page}"))
            if page < total_pages:
                row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_users_{page+1}"))
            markup.row(*row)
        
        # Кнопка "Найти по username или userid"
        markup.add(types.InlineKeyboardButton("🔍 Найти по username или userid", callback_data="find_user"))
        
        # Кнопка "Вернуться в админ-панель"
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
  
#поиск пользователя по юзерид или юзернейм
@bot.callback_query_handler(func=lambda call: call.data == "find_user")
def find_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для поиска пользователей!")
        return
    
    # Запрашиваем у админа username или userid
    text = "🔍 Введите @username или userid пользователя для поиска:"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="all_users_1"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    # Регистрируем следующий шаг для обработки введённых данных
    bot.register_next_step_handler(msg, process_user_search, call.message.chat.id)

def process_user_search(message, original_chat_id):
    if message.chat.id != original_chat_id or message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ Ошибка: действие доступно только администратору!")
        return
    
    search_query = message.text.strip()
    
    # Удаляем сообщение с введёнными данными
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    
    # Проверяем, что ввёл пользователь
    user_id = None
    username = None
    
    if search_query.startswith('@'):
        username = search_query[1:]  # Убираем @ из username
    else:
        try:
            user_id = int(search_query)  # Пробуем преобразовать в число (userid)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат! Введите @username или userid (число).")
            return
    
    # Ищем пользователя в базе
    with db.get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute('SELECT ID FROM requests WHERE ID = ?', (user_id,))
        else:
            cursor.execute('SELECT ID FROM requests')
        
        users = cursor.fetchall()
    
    found_user_id = None
    if user_id:
        if users:
            found_user_id = users[0][0]  # Нашли по user_id
    else:
        # Ищем по username
        for uid in users:
            try:
                user = bot.get_chat_member(uid[0], uid[0]).user
                if user.username and user.username.lower() == username.lower():
                    found_user_id = uid[0]
                    break
            except:
                continue
    
    # Формируем ответ
    if found_user_id:
        try:
            user = bot.get_chat_member(found_user_id, found_user_id).user
            username_display = f"@{user.username}" if user.username else "Нет username"
        except:
            username_display = "Неизвестный пользователь"
        
        text = (
            f"<b>Найденный пользователь:</b>\n\n"
            f"🆔 ID: <code>{found_user_id}</code>\n"
            f"👤 Username: {username_display}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"{found_user_id} {username_display}", callback_data=f"user_details_{found_user_id}"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        text = "❌ Пользователь не найден!"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    # Отправляем новое сообщение (заменяем старое)
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_users")
def back_to_users(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра пользователей!")
        return
    
    # Извлекаем сохранённую страницу или используем первую
    page = admin_page_context.get(call.from_user.id, 1)
    
    # Получаем только принятых пользователей из таблицы requests
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM requests WHERE STATUS = ?', ('approved',))
        all_users = cursor.fetchall()
    
    if not all_users:
        text = "📭 Нет принятых пользователей в боте."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        # Пагинация
        users_per_page = 8
        total_pages = (len(all_users) + users_per_page - 1) // users_per_page
        page = max(1, min(page, total_pages))  # Ограничиваем страницу допустимым диапазоном
        
        start_idx = (page - 1) * users_per_page
        end_idx = start_idx + users_per_page
        page_users = all_users[start_idx:end_idx]
        
        # Формируем текст
        text = f"<b>Управляйте принятыми пользователями:</b>\n({page} страница)\n\n"
        markup = types.InlineKeyboardMarkup()
        
        # Добавляем кнопки для каждого пользователя
        for user_data in page_users:
            user_id = user_data[0]
            try:
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
            except:
                username = "Неизвестный пользователь"
            
            markup.add(types.InlineKeyboardButton(f"{user_id} {username}", callback_data=f"user_details_{user_id}"))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = []
            if page > 1:
                row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_users_{page-1}"))
            row.append(types.InlineKeyboardButton(f"{page}", callback_data=f"all_users_{page}"))
            if page < total_pages:
                row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_users_{page+1}"))
            markup.row(*row)
        
        # Кнопка "Найти по username или userid"
        markup.add(types.InlineKeyboardButton("🔍 Найти по username или userid", callback_data="find_user"))
        
        # Кнопка "Вернуться в админ-панель"
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_details_"))
def user_details(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    # Получаем информацию о пользователе
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        
        # Проверяем, есть ли пользователь в таблице requests
        cursor.execute('SELECT BLOCKED, CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user_data = cursor.fetchone()
        if not user_data:
            text = f"❌ Пользователь с ID {user_id} не найден!"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data=f"back_to_users_{admin_page_context.get(call.from_user.id, 1)}"))
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось удалить сообщение: {e}")
            bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
            return
        
        is_blocked = user_data[0]
        can_submit_numbers = user_data[1]
        
        # Получаем баланс из таблицы users
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        balance_data = cursor.fetchone()
        balance = balance_data[0] if balance_data and balance_data[0] is not None else 0.0
        print(f"[DEBUG] Баланс пользователя {user_id}: {balance:.2f}")
        
        # Статистика по номерам
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE ID_OWNER = ?', (user_id,))
        numbers = cursor.fetchall()
        
        total_numbers = len(numbers)  # Сколько всего залил
        successful_numbers = sum(1 for num in numbers if num[0] == 'отстоял')  # Сколько всего успешных
        shutdown_numbers = sum(1 for num in numbers if num[0] == 'слетел')  # Сколько слетело
        invalid_numbers = sum(1 for num in numbers if num[0] == 'невалид')  # Сколько не валидных
        active_numbers = sum(1 for num in numbers if num[0] == 'активен')  # Которые на данный момент работают
    
    # Получаем username через Telegram API
    try:
        user = bot.get_chat_member(user_id, user_id).user
        username = f"@{user.username}" if user.username else "Нет username"
    except Exception as e:
        print(f"[ERROR] Не удалось получить username для user_id {user_id}: {e}")
        username = "Неизвестный пользователь"
    
    # Формируем текст
    text = (
        f"<b>Пользователь {user_id} {username}</b>\n\n"
        f"💰 Баланс: {balance:.2f} $\n"
        f"📱 Сколько всего залил: {total_numbers}\n"
        f"✅ Сколько всего успешных: {successful_numbers}\n"
        f"⏳ Сколько слетело: {shutdown_numbers}\n"
        f"❌ Сколько не валидных: {invalid_numbers}\n"
        f"🔄 Которые на данный момент работают: {active_numbers}\n"
    )
    
    # Формируем кнопки
    markup = types.InlineKeyboardMarkup()
    
    # Кнопка блокировки/разблокировки
    if is_blocked:
        markup.add(types.InlineKeyboardButton("✅ Разблокировать в боте", callback_data=f"unblock_user_{user_id}"))
    else:
        markup.add(types.InlineKeyboardButton("❌ Заблокировать в боте", callback_data=f"block_user_{user_id}"))
    
    # Кнопка "Выгнать из бота"
    markup.add(types.InlineKeyboardButton("🚪 Выгнать из бота", callback_data=f"kick_user_{user_id}"))
    
    # Кнопка запрета/разрешения сдачи номеров
    if can_submit_numbers:
        markup.add(types.InlineKeyboardButton("🚫 Запретить сдавание номеров", callback_data=f"disable_numbers_{user_id}"))
    else:
        markup.add(types.InlineKeyboardButton("✅ Разрешить сдавание номеров", callback_data=f"enable_numbers_{user_id}"))
    
    # Кнопки навигации
    markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data=f"back_to_users_{admin_page_context.get(call.from_user.id, 1)}"))
    markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_users_"))
def back_to_users(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра пользователей!")
        return
    
    try:
        page = int(call.data.split("_")[3])
    except (IndexError, ValueError):
        page = 1  # Если что-то пошло не так, открываем первую страницу
    
    # Сохраняем текущую страницу для администратора
    admin_page_context[call.from_user.id] = page
    
    # Получаем только принятых пользователей из таблицы requests
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM requests WHERE STATUS = ?', ('approved',))
        all_users = cursor.fetchall()
    
    if not all_users:
        text = "📭 Нет принятых пользователей в боте."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        # Пагинация
        users_per_page = 8
        total_pages = (len(all_users) + users_per_page - 1) // users_per_page
        page = max(1, min(page, total_pages))  # Ограничиваем страницу допустимым диапазоном
        
        start_idx = (page - 1) * users_per_page
        end_idx = start_idx + users_per_page
        page_users = all_users[start_idx:end_idx]
        
        # Формируем текст
        text = f"<b>Управляйте принятыми пользователями:</b>\n({page} страница)\n\n"
        markup = types.InlineKeyboardMarkup()
        
        # Добавляем кнопки для каждого пользователя
        for user_data in page_users:
            user_id = user_data[0]
            try:
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
            except:
                username = "Неизвестный пользователь"
            
            markup.add(types.InlineKeyboardButton(f"{user_id} {username}", callback_data=f"user_details_{user_id}"))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = []
            if page > 1:
                row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_users_{page-1}"))
            row.append(types.InlineKeyboardButton(f"{page}", callback_data=f"all_users_{page}"))
            if page < total_pages:
                row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_users_{page+1}"))
            markup.row(*row)
        
        # Кнопка "Найти по username или userid"
        markup.add(types.InlineKeyboardButton("🔍 Найти по username или userid", callback_data="find_user"))
        
        # Кнопка "Вернуться в админ-панель"
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось удалить сообщение: {e}")
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("block_user_"))
def block_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])  # Убедимся, что user_id определён
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    
    try:
        bot.send_message(user_id, "🚫 Вас заблокировали в боте!")
    except:
        pass
    
    bot.answer_callback_query(call.id, f"Пользователь {user_id} заблокирован!")
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("unblock_user_"))
def unblock_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вас разблокировали в боте! Напишите /start")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователь {user_id} разблокирован!")
    user_details(call)





# Настройка логирования
logging.basicConfig(filename='bot.log', level=logging.DEBUG, format='%(asctime)s | %(levelname)s | %(message)s')

@bot.callback_query_handler(func=lambda call: call.data.startswith("kick_user_"))
def kick_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_kick_{user_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="all_users_1")
    )
    bot.edit_message_text(
        f"⚠️ Выгнать и удалить все данные пользователя {user_id}?",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    logging.debug(f"Админ {call.from_user.id} запросил удаление пользователя {user_id}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_kick_"))
def confirm_kick_user(call):
    user_id = int(call.data.split("_")[2])
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('BEGIN TRANSACTION')
            try:
                # Блокируем пользователя, но не удаляем его данные
                cursor.execute('UPDATE requests SET BLOCKED = 1, STATUS = "kicked" WHERE ID = ?', (user_id,))
                cursor.execute('UPDATE users SET STATUS = "kicked" WHERE ID = ?', (user_id,))
                conn.commit()
                logging.info(f"Пользователь {user_id} помечен как kicked и заблокирован.")
            except Exception as e:
                conn.rollback()
                logging.error(f"Ошибка при пометке пользователя {user_id} как kicked: {e}")
                raise e

        cooldowns.pop(user_id, None)

        try:
            bot.send_message(
                user_id,
                "🚪 Вас выгнали из бота! Ваши данные сохранены, но вы не можете работать в боте."
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пользователя {user_id}: {e}")

        bot.answer_callback_query(call.id, f"Пользователь {user_id} выгнан (данные сохранены)!")
        call.data = "all_users_1"
        show_all_users(call)

    except Exception as e:
        logging.error(f"Ошибка при кике пользователя {user_id}: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при удалении!")


@bot.callback_query_handler(func=lambda call: call.data.startswith("disable_numbers_"))
def disable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "🚫 Вам запретили сдавать номера!")
    except:
        pass  
    bot.answer_callback_query(call.id, f"Пользователю {user_id} запрещено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("enable_numbers_"))
def enable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вам разрешили сдавать номера!")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователю {user_id} разрешено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)


#СТАТИСТИКА ГРУПП
@bot.callback_query_handler(func=lambda call: call.data.startswith("group_statistics"))
def group_statistics(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups ORDER BY NAME')
        groups = cursor.fetchall()

    if not groups:
        text = "📭 Нет доступных групп."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        return

    items_per_page = 10
    total_pages = (len(groups) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_groups = groups[start_idx:end_idx]

    text = f"<b>📊 Список групп (страница {page}/{total_pages}):</b>\n\n"
    for group_id, group_name in page_groups:
        text += f"🏠 <b>{group_name}</b>\n"
        text += "────────────────────\n"

    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in page_groups:
        markup.add(types.InlineKeyboardButton(
            f"📊 {group_name}",
            callback_data=f"group_stats_{group_id}_1_{page}"  # Передаём и страницу списка
        ))

    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"group_statistics_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"group_statistics_{page+1}"))
        markup.row(*row)

    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("group_stats_"))
def show_group_stats(call):
    bot.answer_callback_query(call.id)

    parts = call.data.split("_")
    group_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1
    list_page = int(parts[4]) if len(parts) > 4 else 1  # Страница списка групп
    numbers_per_page = 5

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group_name = cursor.fetchone()
        if not group_name:
            bot.edit_message_text("❌ Группа не найдена.", call.message.chat.id, call.message.message_id, parse_mode='HTML')
            return
        group_name = group_name[0]

        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = ?', (group_id, 'moderator'))
        total_moderators = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*) 
            FROM numbers n
            WHERE n.GROUP_CHAT_ID = ? AND n.STATUS = 'отстоял'
        ''', (group_id,))
        total_numbers = cursor.fetchone()[0]

        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)

        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT n.NUMBER, n.TAKE_DATE, n.SHUTDOWN_DATE, n.STATUS 
            FROM numbers n
            WHERE n.GROUP_CHAT_ID = ? AND n.STATUS = 'отстоял'
            ORDER BY n.TAKE_DATE DESC
            LIMIT ? OFFSET ?
        ''', (group_id, numbers_per_page, offset))
        recent_numbers = cursor.fetchall()

    stats_text = (
        f"📊 <b>Статистика группы: {group_name}</b>\n\n"
        f"👥 Модераторов: <code>{total_moderators}</code>\n"
        f"📱 Успешных номеров: <code>{total_numbers}</code>\n\n"
        f"📋 <b>Список номеров (страница {page}/{total_pages}):</b>\n"
    )

    if not recent_numbers:
        stats_text += "📭 Успешных номеров нет."
    else:
        for number, take_date, shutdown_date, status in recent_numbers:
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
            stats_text += (
                f"\n📱 Номер: <code>{number}</code>\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str}\n"
            )

    markup = types.InlineKeyboardMarkup()

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"group_stats_{group_id}_{page-1}_{list_page}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"group_stats_{group_id}_{page+1}_{list_page}"))
        markup.add(*nav_buttons)

    markup.add(types.InlineKeyboardButton("🔙 К списку групп", callback_data=f"group_statistics_{list_page}"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(stats_text,
                          call.message.chat.id,
                          call.message.message_id,
                          reply_markup=markup,
                          parse_mode='HTML')


#------------------------------
#---------МОИ НОМЕРА         
#------------------------------
# ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ НОМЕРА:
@bot.callback_query_handler(func=lambda call: call.data.startswith("my_numbers"))
def show_my_numbers(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    db.update_last_activity(user_id)

    # Определяем страницу
    parts = call.data.split("_")
    page = int(parts[2]) if len(parts) > 2 else 1
    numbers_per_page = 5  # Сколько номеров на одной странице

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ?', (user_id,))
        total_numbers = cursor.fetchone()[0]

        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)

        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT NUMBER, STATUS, TAKE_DATE, SHUTDOWN_DATE 
            FROM numbers 
            WHERE ID_OWNER = ? 
            ORDER BY TAKE_DATE DESC 
            LIMIT ? OFFSET ?
        ''', (user_id, numbers_per_page, offset))
        numbers = cursor.fetchall()

    # Заголовок
    numbers_text = (
        f"📱 <b>Ваши номера</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📄 Страница <b>{page}</b> из <b>{total_pages}</b>\n\n"
    )

    if not numbers:
        numbers_text += "📭 <i>У вас пока нет добавленных номеров.</i>"
    else:
        for idx, (number, status, take_date, shutdown_date) in enumerate(numbers, start=1):
            take_date_str = take_date if take_date not in ("0", "1") else "⏳ Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "🔄 Не завершён"
            status_emoji = {
                "отстоял": "✅",
                "активен": "🟢",
                "слетел": "⚠️",
                "невалид": "❌",
                "ожидает": "⏳"
            }.get(status, "ℹ️")

            numbers_text += (
                f"🔹 <b>Номер {idx}:</b> <code>{number}</code>\n"
                f"{status_emoji} <b>Статус:</b> {status}\n"
                f"📋 <b>Взято:</b> {take_date_str}\n"
                f"📝 <b>Отстоял:</b> {shutdown_date_str}\n"
                "━━━━━━━━━━━━━━━\n"
            )

    # Кнопки
    markup = types.InlineKeyboardMarkup()

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"my_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"my_numbers_{page+1}"))
        markup.row(*nav_buttons)

    markup.add(types.InlineKeyboardButton("🔙 Профиль", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(
        numbers_text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )


def safe_send_message(chat_id, text, parse_mode=None, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except ApiTelegramException as e:
        if e.result_json.get('error_code') == 429:
            time.sleep(1)
            safe_send_message(chat_id, text, parse_mode, reply_markup)
        else:
            logging.error(f"Ошибка отправки сообщения {chat_id}: {e}")
# Глобальная переменная для хранения данных номеров (можно заменить на временное хранилище в будущем)
numbers_data_cache = {}

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_numbers"))
def show_all_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра всех номеров!")
        return
    
    bot.answer_callback_query(call.id)
    
    page = int(call.data.split("_")[2]) if len(call.data.split("_")) > 2 else 1
    numbers_per_page = 5  # Количество номеров на странице
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers')
        total_numbers = cursor.fetchone()[0]
        
        # Вычисляем общее количество страниц
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)
        
        # Получаем номера для текущей страницы с username владельца
        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT n.NUMBER, n.STATUS, n.TAKE_DATE, n.SHUTDOWN_DATE, n.ID_OWNER, 
                   n.CONFIRMED_BY_MODERATOR_ID, n.GROUP_CHAT_ID, n.TG_NUMBER, u.USERNAME
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            ORDER BY n.TAKE_DATE DESC
            LIMIT ? OFFSET ?
        ''', (numbers_per_page, offset))
        numbers = cursor.fetchall()
    
    numbers_text = f"📋 <b>Все номера (страница {page}/{total_pages})</b>\n\n"
    if not numbers:
        numbers_text += "📭 Номера отсутствуют."
    else:
        for number, status, take_date, shutdown_date, owner_id, confirmed_by_moderator_id, group_chat_id, tg_number, username in numbers:
            # Получаем имя группы
            group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
            
            # Формируем отображаемые даты
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
            
            # Формируем информацию о модераторе
            moderator_info = f"Модератор: @{confirmed_by_moderator_id}" if confirmed_by_moderator_id else "Модератор: Не назначен"
            
            # Формируем username владельца
            username_display = f"@{username}" if username and username != "Не указан" else "Без username"
            
            # Формируем текст для номера
            numbers_text += (
                f"📱 Номер: <code>{number}</code>\n"
                f"👤 Владелец: <a href=\"tg://user?id={owner_id}\">{owner_id}</a> ({username_display})\n"
                f"📊 Статус: {status}\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str}\n"
                f"🏷 Группа: {group_name}\n"
                f"📱 ТГ: {tg_number or 'Не указан'}\n"
                f"{moderator_info}\n\n"
            )
    
    markup = types.InlineKeyboardMarkup()
    
    # Добавляем кнопки навигации
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"all_numbers_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            numbers_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(
            call.message.chat.id,
            numbers_text,
            reply_markup=markup,
            parse_mode='HTML'
        )

def show_numbers_page(call, page):
    user_id = call.from_user.id
    if user_id not in numbers_data_cache:
        bot.answer_callback_query(call.id, "❌ Данные устарели, пожалуйста, запросите список заново!")
        return
    
    numbers = numbers_data_cache[user_id]
    items_per_page = 5  # По 5 номеров на страницу
    total_items = len(numbers)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    
    if page < 0 or page >= total_pages:
        bot.answer_callback_query(call.id, "❌ Страница недоступна!")
        return
    
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    page_numbers = numbers[start_idx:end_idx]
    
    # Формируем текст для текущей страницы
    text = f"<b>📱 Список всех номеров (Страница {page + 1} из {total_pages}):</b>\n\n"
    for number, take_date, shutdown_date, user_id, group_name, username in page_numbers:
        group_info = f"👥 Группа: {group_name}" if group_name else "👥 Группа: Не указана"
        user_info = f"🆔 Пользователь: {user_id}" if user_id else "🆔 Пользователь: Не указан"
        username_display = f"@{username}" if username and username != "Не указан" else "Без username"
        text += (
            f"📞 <code>{number}</code>\n"
            f"{user_info} ({username_display})\n"
            f"{group_info}\n"
            f"📅 Взят: {take_date}\n"
            f"📴 Отключён: {shutdown_date or 'Ещё активен'}\n\n"
        )
    
    # Создаём кнопки для навигации
    markup = types.InlineKeyboardMarkup()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"numbers_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"numbers_page_{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    # Отправляем или редактируем сообщение
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        print(f"Удалено старое сообщение {call.message.message_id} в чате {call.message.chat.id}")
    except Exception as e:
        print(f"Ошибка при удалении старого сообщения: {e}")
    
    bot.send_message(
        call.message.chat.id,
        text,
        parse_mode='HTML',
        reply_markup=markup
    )
    print(f"Страница {page + 1} отправлена успешно")

@bot.callback_query_handler(func=lambda call: call.data.startswith("numbers_page_"))
def numbers_page_callback(call):
    page = int(call.data.split("_")[2])
    show_numbers_page(call, page)










# Словарь для отслеживания сообщений с кодами
code_messages = {}  # {number: {"chat_id": int, "message_id": int, "timestamp": datetime, "tg_number": int, "owner_id": int}}



def check_code_timeout():
    """Проверяет, истекло ли 2 минуты с момента отправки кода. Если да, подтверждает номер как активный."""
    print("Запуск функции check_code_timeout")
    while True:
        try:
            current_time = datetime.now()
            
            for number, data in list(code_messages.items()):
                try:
                    # Проверка корректности timestamp
                    if not isinstance(data["timestamp"], datetime):
                        print(f"[TIMEOUT_CHECK] Некорректный timestamp для номера {number}: {data['timestamp']}")
                        del code_messages[number]
                        continue

                    elapsed_time = (current_time - data["timestamp"]).total_seconds() / 60
                    print(f"[TIMEOUT_CHECK] Номер {number}, прошло времени: {elapsed_time:.2f} минут, TG: {data.get('tg_number', 'N/A')}")

                    if elapsed_time >= 2:
                        print(f"[TIMEOUT_CHECK] Время истекло для номера {number} ({elapsed_time:.2f} минут)")
                        with db_module.get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute('SELECT ID_OWNER, STATUS, MODERATOR_ID, VERIFICATION_CODE, fa FROM numbers WHERE NUMBER = ?', (number,))
                            result = cursor.fetchone()

                            if not result:
                                logging.warning(f"[TIMEOUT_CHECK] Номер {number} не найден в базе данных")
                                del code_messages[number]
                                continue

                            owner_id, status, moderator_id, verification_code, fa = result
                            print(f"[TIMEOUT_CHECK] Номер {number}, статус: {status}, владелец: {owner_id}, модератор: {moderator_id}")

                            if status not in ("на проверке", "taken"):
                                logging.warning(f"[TIMEOUT_CHECK] Номер {number} имеет неподходящий статус: {status}, пропускаем")
                                del code_messages[number]
                                continue

                            current_date = current_time.strftime("%Y-%m-%d %H:%M:%S")
                            cursor.execute(
                                'UPDATE numbers SET STATUS = "активен", TAKE_DATE = ?, VERIFICATION_CODE = NULL, CONFIRMED_BY_MODERATOR_ID = NULL WHERE NUMBER = ?',
                                (current_date, number)
                            )
                            conn.commit()
                            print(f"[TIMEOUT_CHECK] Номер {number} автоматически подтверждён как активный через 2 минуты.")

                            markup_owner = types.InlineKeyboardMarkup()
                            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            try:
                                safe_send_message(
                                    owner_id,
                                    f"✅ Ваш номер {number} автоматически подтверждён и теперь активен.\n"
                                    f"📱 Код: {verification_code}\n"
                                    f"🔒 2FA: {fa}\n"
                                    f"⏳ Встал: {current_date}.",
                                    parse_mode='HTML',
                                    reply_markup=markup_owner
                                )
                                print(f"[TIMEOUT_CHECK] Отправлено уведомление владельцу {owner_id}")
                            except Exception as e:
                                print(f"[TIMEOUT_CHECK] Ошибка отправки уведомления владельцу {owner_id}: {e}")

                            if moderator_id:
                                markup_mod = types.InlineKeyboardMarkup()
                                markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                                markup_mod.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                try:
                                    safe_send_message(
                                        moderator_id,
                                        f"📱 Номер {number} автоматически подтверждён через 2 минуты бездействия.\n"
                                        f"📱 Код: {verification_code}\n"
                                        f"🔒 2FA: {fa}",
                                        parse_mode='HTML',
                                        reply_markup=markup_mod
                                    )
                                    print(f"[TIMEOUT_CHECK] Отправлено уведомление модератору {moderator_id}")
                                except Exception as e:
                                    print(f"[TIMEOUT_CHECK] Ошибка отправки уведомления модератору {moderator_id}: {e}")

                            try:
                                bot.edit_message_text(
                                    f"📱 <b>ТГ {data['tg_number']}</b>\n"
                                    f"✅ Номер {number} автоматически подтверждён в {current_date}.\n"
                                    f"📱 Код: {verification_code}\n"
                                    f"🔒 2FA: {fa}",
                                    data["chat_id"],
                                    data["message_id"],
                                    parse_mode='HTML'
                                )
                                print(f"[TIMEOUT_CHECK] Обновлено сообщение в группе {data['chat_id']}")
                            except Exception as e:
                                print(f"[TIMEOUT_CHECK] Не удалось отредактировать сообщение для номера {number}: {e}")

                            print(f"[TIMEOUT_CHECK] Удаление номера {number} из отслеживания после автоподтверждения")
                            del code_messages[number]

                except Exception as e:
                    print(f"[TIMEOUT_CHECK] Ошибка при обработке номера {number}: {str(e)}")
                    logging.error(f"[TIMEOUT_CHECK] Ошибка при обработке номера {number}: {str(e)}", exc_info=True)
                    continue

            time.sleep(5)
        except Exception as e:
            print(f"[TIMEOUT_CHECK] Критическая ошибка в check_code_timeout: {str(e)}")
            logging.error(f"[TIMEOUT_CHECK] Критическая ошибка в check_code_timeout: {str(e)}", exc_info=True)
            time.sleep(5)






































# ==========================
# 📱 Сдача номера
# ==========================
@bot.callback_query_handler(func=lambda call: call.data == "submit_number")
def submit_number(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)

    # Проверка запрета на сдачу
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 0:
            bot.answer_callback_query(call.id, "🚫 Вам запрещено сдавать номера!")
            return

    # Получаем тариф и холд
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)

    # Устанавливаем состояние
    set_state(user_id, "submit_number")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="go_back"))

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

    bot.send_message(
        call.message.chat.id,
        f"📱 <b>Сдача номера</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Введите номер телефона, который хотите сдать.\n"
        f"📌 Пример: <code>+79991234567</code>\n\n"
        f"⚠ Разрешены только российские номера.\n"
        f"💵 Текущий тариф: <b>{price}$</b> | ⏱ Холд: {hold_time} мин",
        parse_mode='HTML',
        reply_markup=markup
    )


@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("state") == "submit_number")
def process_numbers(message):
    user_id = message.from_user.id

    if not message or not message.text:
        bot.send_message(message.chat.id, "❌ Пожалуйста, отправьте номера текстом!")
        return

    numbers = [n.strip() for n in message.text.strip().split('\n') if n.strip()]
    if not numbers:
        bot.send_message(message.chat.id, "❌ Вы не указали ни одного номера!")
        return

    valid_numbers = []
    invalid_numbers = []
    restricted_status_numbers = []

    for number in numbers:
        corrected_number = is_russian_number(number)
        if corrected_number:
            valid_numbers.append(corrected_number)
        else:
            invalid_numbers.append(number)

    if not valid_numbers:
        text = (
            "❌ <b>Все введённые номера некорректны!</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "Формат должен быть: <code>+79991234567</code>\n"
        )
        if invalid_numbers:
            text += "\n❌ Неверный формат:\n" + "\n".join(invalid_numbers)
        bot.send_message(message.chat.id, text, parse_mode='HTML')
        return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            success_count = 0
            already_exists = 0
            restricted_status_count = 0
            successfully_added = []

            for number in valid_numbers:
                cursor.execute(
                    'SELECT NUMBER, STATUS FROM numbers WHERE NUMBER = ? AND STATUS IN (?, ?, ?, ?)',
                    (number, 'отстоял', 'активен', 'слетел', 'невалид')
                )
                restricted_number = cursor.fetchone()
                if restricted_number:
                    restricted_status_numbers.append(f"{number} ({restricted_number[1]})")
                    restricted_status_count += 1
                    continue

                cursor.execute('SELECT NUMBER, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
                existing_number = cursor.fetchone()

                if existing_number:
                    if existing_number[1] == "0":
                        already_exists += 1
                        continue
                    else:
                        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))

                cursor.execute(
                    'INSERT INTO numbers (NUMBER, ID_OWNER, TAKE_DATE, SHUTDOWN_DATE, STATUS) VALUES (?, ?, ?, ?, ?)',
                    (number, user_id, '0', '0', 'ожидает')
                )
                success_count += 1
                successfully_added.append(number)

            conn.commit()

        response_text = "📊 <b>Результаты добавления</b>\n━━━━━━━━━━━━━━━\n"
        if success_count > 0:
            response_text += f"✅ Успешно добавлено: {success_count}\n📱 {', '.join(successfully_added)}\n\n"
        if already_exists > 0:
            response_text += f"⚠ Уже существуют: {already_exists}\n"
        if restricted_status_count > 0:
            response_text += f"🚫 Запрещённый статус: {restricted_status_count}\n📱 {', '.join(restricted_status_numbers)}\n"
        if invalid_numbers:
            response_text += f"❌ Неверный формат:\n" + "\n".join(invalid_numbers)

    except Exception as e:
        print(f"[ERROR] Ошибка process_numbers: {e}")
        response_text = "❌ Произошла ошибка при добавлении номеров."

    # Очищаем состояние
    clear_state(user_id)

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📱 Добавить ещё", callback_data="submit_number"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.send_message(message.chat.id, response_text, parse_mode='HTML', reply_markup=markup)




@bot.callback_query_handler(func=lambda call: call.data == "db_menu")
def db_menu_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к управлению БД!")
        return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📥 Скачать БД (НОМЕРА)", callback_data="download_numbers"))
    markup.add(InlineKeyboardButton("🗑 Очистить БД (НОМЕРА+БАЛАНС)", callback_data="clear_numbers"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    bot.edit_message_text("🗃 Управление базой данных", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "download_numbers")
def download_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для скачивания БД!")
        return
    
    download_numbers(call.message.chat.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "clear_numbers")
def clear_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для очистки БД!")
        return
    
    clear_database(call.message.chat.id)
    bot.answer_callback_query(call.id, "✅ Номера и балансы очищены!")




#=============================================================================================================





@bot.callback_query_handler(func=lambda call: call.data == "Gv")
def settingssss(data):
    # Определяем, является ли входной параметр callback (call) или сообщением (message)
    is_callback = hasattr(data, 'message')
    user_id = data.from_user.id
    chat_id = data.message.chat.id if is_callback else data.chat.id
    message_id = data.message.message_id if is_callback else data.message_id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        if is_callback:
            bot.answer_callback_query(data.id, "❌ У вас нет прав для выполнения этого действия!")
        else:
            bot.send_message(chat_id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    # Очищаем активные обработчики, чтобы избежать нежелательной реакции на ввод текста
    bot.clear_step_handler_by_chat_id(chat_id)
    
    # Формируем текст и кнопки для меню
    menu_text = "📋 <b>Меню:</b>"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    # Редактируем или отправляем сообщение в зависимости от типа вызова
    try:
        if is_callback:
            bot.edit_message_text(
                menu_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        else:
            bot.send_message(
                chat_id,
                menu_text,
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Не удалось обработать сообщение: {e}")
        bot.send_message(
            chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )


#Выдать чек
@bot.callback_query_handler(func=lambda call: call.data == "send_check")
def send_check_start(call):
    user_id = call.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    text = "📝 <b>Укажите user ID или @username</b> (используйте reply на это сообщение):"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    try:
        msg = bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_user_id_for_check, call.message.chat.id, call.message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        msg = bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_user_id_for_check, call.message.chat.id, msg.message_id)

def process_user_id_for_check(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода user ID или @username!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    input_text = message.text.strip()
    target_user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]
        print(f"[DEBUG] Processing username: {username}")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                target_user_id = user[0]
                print(f"[DEBUG] Found user ID {target_user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    message.chat.id,
                    f"❌ Пользователь с @username '{username}' не найден!",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
    else:
        try:
            target_user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {target_user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                "❌ Неверный формат! Введите числовой ID или @username.",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
    
    # Проверяем, существует ли пользователь и получаем баланс
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        if not user:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ Пользователь с ID {target_user_id} не найден!",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
        target_user_id, balance, username = user
        print(f"[DEBUG] Пользователь {target_user_id}: текущий баланс={balance}, username={username}")
    
    # Проверяем, что баланс больше 0
    if balance <= 0:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            f"❌ Баланс пользователя {target_user_id} ({username if username else 'Нет username'}) равен {balance:.2f} $. Чек не может быть создан!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Удаляем сообщение с user ID
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с user ID: {e}")
    
    # Списываем весь баланс до 0.0 с блокировкой
    with db_lock:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Повторно проверяем баланс перед списанием
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            print(f"[DEBUG] Повторная проверка баланса пользователя {target_user_id}: {user[0] if user else 'не найден'}")
            if not user or user[0] <= 0:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    original_chat_id,
                    f"❌ Баланс пользователя {target_user_id} равен {user[0]:.2f} $. Чек не может быть создан!",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
            
            amount = round(float(user[0]), 2)  # Округляем баланс до 2 знаков
            print(f"[DEBUG] Создание чека на сумму {amount:.2f} для пользователя {target_user_id}")
            
            # Обнуляем баланс
            print(f"[DEBUG] Выполняется UPDATE для пользователя {target_user_id}, установка BALANCE = 0")
            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (target_user_id,))
            if cursor.rowcount == 0:
                print(f"[ERROR] UPDATE не затронул строки: пользователь {target_user_id} не найден или баланс не изменён")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка: не удалось обнулить баланс пользователя {target_user_id}.",
                    parse_mode='HTML'
                )
                return
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            new_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Перед фиксацией транзакции: новый баланс={new_balance:.2f}")
            conn.commit()
            print(f"[DEBUG] Транзакция зафиксирована: баланс пользователя {target_user_id} обнулён, новый баланс: {new_balance:.2f}")
            # Проверка баланса после фиксации
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            verified_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Проверка после фиксации: баланс={verified_balance:.2f}")
            if verified_balance != 0.0:
                print(f"[ERROR] Несоответствие баланса после фиксации: ожидалось 0.0, получено {verified_balance:.2f}")
    
    # Создаём чек через CryptoBot API
    crypto_api = crypto_pay.CryptoPay()
    cheque_result = crypto_api.create_check(
        amount=str(amount),
        asset="USDT",
        description=f"Выплата всего баланса для пользователя {target_user_id}",
        pin_to_user_id=target_user_id
    )
    print(f"[DEBUG] Результат создания чека: {cheque_result}")
    
    if cheque_result.get("ok", False):
        cheque = cheque_result.get("result", {})
        cheque_link = cheque.get("bot_check_url", "")
        
        if cheque_link:
            # Обновляем баланс казны
            try:
                db_module.update_treasury_balance(-amount)
                print(f"[DEBUG] Баланс казны уменьшен на {amount:.2f}")
            except Exception as treasury_error:
                print(f"[ERROR] Ошибка при обновлении баланса казны: {treasury_error}")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка при обновлении баланса казны: {str(treasury_error)}.",
                    parse_mode='HTML'
                )
                return
            
            # Уведомляем пользователя
            markup_user = types.InlineKeyboardMarkup()
            markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            try:
                safe_send_message(
                    target_user_id,
                    f"✅ Вам отправлен чек на {amount:.2f}$ (весь ваш баланс)!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance:.2f}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )
                print(f"[DEBUG] Уведомление отправлено пользователю {target_user_id}")
            except Exception as notify_error:
                print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {notify_error}")
                bot.send_message(
                    original_chat_id,
                    f"⚠️ Не удалось уведомить пользователя {target_user_id}: {notify_error}",
                    parse_mode='HTML'
                )
            
            # Уведомляем администратора
            username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
            markup_admin = types.InlineKeyboardMarkup()
            markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
            try:
                bot.edit_message_text(
                    f"✅ Чек на {amount:.2f}$ (весь баланс) успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance:.2f}$",
                    original_chat_id,
                    original_message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            except Exception as e:
                print(f"[ERROR] Не удалось обновить сообщение для администратора: {e}")
                bot.send_message(
                    original_chat_id,
                    f"✅ Чек на {amount:.2f}$ (весь баланс) успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance:.2f}$",
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            
            # Логируем операцию
            try:
                db_module.log_treasury_operation("Вывод (чек на весь баланс)", -amount, db_module.get_treasury_balance())
                print(f"[DEBUG] Операция логирована: Вывод (чек) на {amount:.2f}$")
            except Exception as log_error:
                print(f"[ERROR] Ошибка при логировании операции: {log_error}")
        else:
            print("[ERROR] Ссылка на чек отсутствует")
            bot.send_message(
                original_chat_id,
                f"❌ Не удалось создать чек для пользователя {target_user_id}: нет ссылки.",
                parse_mode='HTML'
            )
    else:
        error_msg = cheque_result.get('error', {}).get('name', 'Неизвестная ошибка')
        print(f"[ERROR] Ошибка при создании чека: {error_msg}")
        bot.send_message(
            original_chat_id,
            f"❌ Ошибка при создании чека: {error_msg}",
            parse_mode='HTML'
        )
    
    # Возвращаемся к главному меню
    menu_text = "📋 <b>Меню:</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    
    try:
        bot.edit_message_text(
            menu_text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

def process_check_amount(message, target_user_id, original_chat_id, original_message_id, current_balance, username_display):
    user_id = message.from_user.id
    
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода суммы!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат суммы! Введите положительное число (например, 10.5).",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Списываем сумму с баланса пользователя с блокировкой
    with db_lock:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Повторно проверяем баланс перед списанием
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            print(f"[DEBUG] Повторная проверка баланса пользователя {target_user_id}: {user[0] if user else 'не найден'}")
            if not user or user[0] < amount:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
                bot.send_message(
                    message.chat.id,
                    f"❌ Недостаточно средств на балансе пользователя {target_user_id}! Текущий баланс: {user[0] if user else 0} $",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                return
            
            # Уменьшаем баланс
            print(f"[DEBUG] Выполняется UPDATE для пользователя {target_user_id}, уменьшение на {amount}")
            cursor.execute('UPDATE users SET BALANCE = BALANCE - ? WHERE ID = ?', (amount, target_user_id))
            if cursor.rowcount == 0:
                print(f"[ERROR] UPDATE не затронул строки: пользователь {target_user_id} не найден или баланс не изменён")
                bot.send_message(
                    message.chat.id,
                    f"❌ Ошибка: не удалось обновить баланс пользователя {target_user_id}.",
                    parse_mode='HTML'
                )
                return
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            new_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Перед фиксацией транзакции: новый баланс={new_balance}")
            conn.commit()
            print(f"[DEBUG] Транзакция зафиксирована: баланс пользователя {target_user_id} уменьшен на {amount}$, новый баланс: {new_balance}")
            # Проверка баланса после фиксации
            cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
            verified_balance = cursor.fetchone()[0]
            print(f"[DEBUG] Проверка после фиксации: баланс={verified_balance}")
            if verified_balance != new_balance:
                print(f"[ERROR] Несоответствие баланса после фиксации: ожидалось {new_balance}, получено {verified_balance}")
    
    # Удаляем сообщение с суммой
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с суммой: {e}")
    
    # Создаём чек через CryptoBot API
    crypto_api = crypto_pay.CryptoPay()
    cheque_result = crypto_api.create_check(
        amount=amount,
        asset="USDT",
        description=f"Выплата для пользователя {target_user_id}",
        pin_to_user_id=target_user_id
    )
    print(f"[DEBUG] Результат создания чека: {cheque_result}")
    
    if cheque_result.get("ok", False):
        cheque = cheque_result.get("result", {})
        cheque_link = cheque.get("bot_check_url", "")
        
        if cheque_link:
            # Обновляем баланс казны
            try:
                db_module.update_treasury_balance(-amount)
                print(f"[DEBUG] Баланс казны уменьшен на {amount}")
            except Exception as treasury_error:
                print(f"[ERROR] Ошибка при обновлении баланса казны: {treasury_error}")
                bot.send_message(
                    original_chat_id,
                    f"❌ Ошибка при обновлении баланса казны: {str(treasury_error)}.",
                    parse_mode='HTML'
                )
                return
            
            # Уведомляем пользователя
            markup_user = types.InlineKeyboardMarkup()
            markup_user.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            try:
                safe_send_message(
                    target_user_id,
                    f"✅ Вам отправлен чек на {amount}$!\n"
                    f"🔗 Ссылка на чек: {cheque_link}\n"
                    f"💰 Ваш новый баланс: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_user
                )
                print(f"[DEBUG] Уведомление отправлено пользователю {target_user_id}")
            except Exception as notify_error:
                print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {notify_error}")
                bot.send_message(
                    original_chat_id,
                    f"⚠️ Не удалось уведомить пользователя {target_user_id}: {notify_error}",
                    parse_mode='HTML'
                )
            
            # Уведомляем администратора
            markup_admin = types.InlineKeyboardMarkup()
            markup_admin.add(types.InlineKeyboardButton("🔙 Назад в заявки", callback_data="pending_withdrawals"))
            try:
                bot.edit_message_text(
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    original_chat_id,
                    original_message_id,
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            except Exception as e:
                print(f"[ERROR] Не удалось обновить сообщение для администратора: {e}")
                bot.send_message(
                    original_chat_id,
                    f"✅ Чек на {amount}$ успешно отправлен пользователю {target_user_id} ({username_display}).\n"
                    f"💰 Новый баланс пользователя: {new_balance}$",
                    parse_mode='HTML',
                    reply_markup=markup_admin
                )
            
            # Логируем операцию
            try:
                db_module.log_treasury_operation("Вывод (чек)", -amount, db_module.get_treasury_balance())
                print(f"[DEBUG] Операция логирована: Вывод (чек) на {amount}$")
            except Exception as log_error:
                print(f"[ERROR] Ошибка при логировании операции: {log_error}")
        else:
            print("[ERROR] Ссылка на чек отсутствует")
            bot.send_message(
                original_chat_id,
                f"❌ Не удалось создать чек для пользователя {target_user_id}: нет ссылки.",
                parse_mode='HTML'
            )
    else:
        error_msg = cheque_result.get('error', {}).get('name', 'Неизвестная ошибка')
        print(f"[ERROR] Ошибка при создании чека: {error_msg}")
        bot.send_message(
            original_chat_id,
            f"❌ Ошибка при создании чека: {error_msg}",
            parse_mode='HTML'
        )
    
    # Возвращаемся к главному меню
    menu_text = "📋 <b>Меню:</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    
    try:
        bot.edit_message_text(
            menu_text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

#ИЗМЕНИТ ЦЕНУ:
# bot.py
@bot.callback_query_handler(func=lambda call: call.data == "change_price")
def change_price_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))

    msg = bot.edit_message_text(
        "📝 Введите ID пользователя или @username, для которого хотите установить индивидуальную цену (ответьте на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_user_id_for_price)

def process_user_id_for_price(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    input_text = message.text.strip()
    user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]  # Убираем @ (например, Devshop19)
        print(f"[DEBUG] Processing username: {username}")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                print(f"[DEBUG] Found user ID {user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
                bot.send_message(message.chat.id, f"❌ Пользователь с @username '{username}' не найден!", reply_markup=markup)
                return
    else:
        try:
            user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.send_message(message.chat.id, "❌ Неверный формат! Введите числовой ID или @username.", reply_markup=markup)
            return
    
    # Проверяем, существует ли пользователь
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (user_id,))
        if not cursor.fetchone():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.send_message(message.chat.id, f"❌ Пользователь с ID {user_id} не найден!", reply_markup=markup)
            return
    
    msg = bot.send_message(
        message.chat.id,
        f"💵 Введите новую цену (в $) для пользователя {user_id} (ответьте на это сообщение):"
    )
    bot.register_next_step_handler(msg, process_price, user_id)

def process_price(message, user_id):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    try:
        price = float(message.text.strip())
        if price <= 0:
            raise ValueError("Цена должна быть положительной!")
        
        db_module.set_custom_price(user_id, price)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            f"✅ Индивидуальная цена для пользователя {user_id} установлена: {price}$",
            reply_markup=markup
        )
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                f"💵 Ваша индивидуальная цена за номер изменена на {price}$!"
            )
        except Exception as e:
            print(f"[ERROR] Не удалось уведомить пользователя {user_id}: {e}")
            
    except ValueError as e:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}", reply_markup=markup)


# Переменная для хранения состояния
AFK_STATE = {}

@bot.callback_query_handler(func=lambda call: call.data == "afk_settings")
def afk_settings(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к настройкам АФК!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    msg = bot.edit_message_text(
        "⚙️ <b>Настройки АФК</b>\n\nВведите ID пользователя или @username для управления его АФК-статусом:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    # Сохраняем состояние
    AFK_STATE[call.from_user.id] = {"step": "awaiting_user_id", "message_id": msg.message_id}
    bot.register_next_step_handler(msg, process_afk_user_id)

def process_afk_user_id(message):
    admin_id = message.from_user.id
    if admin_id not in AFK_STATE or AFK_STATE[admin_id]["step"] != "awaiting_user_id":
        print(f"[DEBUG] Invalid state for admin_id {admin_id}: {AFK_STATE.get(admin_id)}")
        return

    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return

    input_text = message.text.strip()
    print(f"[DEBUG] Input text: '{input_text}'")

    target_user_id = None
    username = None
    if input_text.startswith('@'):
        username = input_text[1:]  # Убираем @ (например, Devshop19)
        print(f"[DEBUG] Processing username: {username}")
    else:
        try:
            target_user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {target_user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
            bot.send_message(message.chat.id, "❌ Неверный формат. Введите числовой ID или @username.", reply_markup=markup)
            bot.register_next_step_handler(message, process_afk_user_id)
            return

    if username:
        with db.get_db() as conn:
            cursor = conn.cursor()
            # Отладка: выводим всех пользователей
            cursor.execute('SELECT ID, USERNAME FROM users')
            all_users = cursor.fetchall()
            print(f"[DEBUG] All users in DB: {all_users}")
            
            # Поиск без учёта регистра
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                target_user_id = user[0]
                print(f"[DEBUG] Found user ID {target_user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
                bot.send_message(message.chat.id, f"❌ Пользователь с @username '{username}' не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_afk_user_id)
                print(f"[DEBUG] Username {username} not found in DB")
                return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            if not user:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
                bot.send_message(message.chat.id, f"❌ Пользователь с ID {target_user_id} не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_afk_user_id)
                return
            
            is_afk, afk_locked, username = user
            print(f"[DEBUG] User {target_user_id}: IS_AFK={is_afk}, AFK_LOCKED={afk_locked}, USERNAME={username}")
            afk_status_text = "Включён" if is_afk else "Выключен"
            
            username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
            username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
                types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
            )
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
            
            bot.send_message(
                message.chat.id,
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}\n"
                f"🔒 <b>Блокировка АФК:</b> {'Да' if afk_locked else 'Нет'}",
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Ошибка при обработке AFK для пользователя {target_user_id}: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при получении данных. Попробуйте позже.")
    
    AFK_STATE.pop(admin_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_enable_afk_"))
def admin_enable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    target_user_id = int(call.data.replace("admin_enable_afk_", ""))
    
    # Очистка данных номеров пользователя из confirmation_messages и code_messages
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NUMBER FROM numbers WHERE ID_OWNER = ?', (target_user_id,))
        numbers = cursor.fetchall()
        for number_tuple in numbers:
            number = number_tuple[0]
            confirmation_messages.pop(f"{number}_{target_user_id}", None)
            code_messages.pop(number, None)
    
    # Обновляем статус AFK в базе данных
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (1, 1, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК включён для пользователя {target_user_id} с блокировкой")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked, username = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
        username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n{username_text}🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    # Отправляем уведомление пользователю
    try:
        bot.send_message(
            target_user_id,
            "🔔 <b>Ваш АФК-статус был изменён администратором</b>\n\n"
            "Теперь ваш АФК: <b>Включён</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "✅ АФК включён для пользователя!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_disable_afk_"))
def admin_disable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем target_user_id из callback_data
    target_user_id = int(call.data.replace("admin_disable_afk_", ""))
    
    # Обновляем статус AFK в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (0, 0, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК выключен для пользователя {target_user_id}, блокировка снята")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        # Извлекаем IS_AFK, AFK_LOCKED и USERNAME
        cursor.execute('SELECT IS_AFK, AFK_LOCKED, USERNAME FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked, username = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        # Форматируем username как кликабельную ссылку
        username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
        username_text = f"👤 Username: <a href=\"tg://user?id={target_user_id}\">{username_display}</a>\n" if username and username != "Не указан" else "👤 Username: Нет username\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"{username_text}"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n{username_text}🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    # Отправляем уведомление пользователю
    try:
        bot.send_message(
            target_user_id,
            "🔔 <b>Ваш АФК-статус был изменён администратором</b>\n\n"
            "Теперь ваш АФК: <b>Выключен</b>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление пользователю {target_user_id}: {e}")

    bot.answer_callback_query(call.id, "✅ АФК выключен для пользователя!")



def cancel_old_checks(crypto_api):
    try:
        checks_result = crypto_api.get_checks(status="active")
        if checks_result.get("ok", False):
            for check in checks_result["result"]["items"]:
                check_id = check["check_id"]
                crypto_api.delete_check(check_id=check_id)
                print(f"[INFO] Отменён чек {check_id}, высвобождено {check['amount']} USDT")
    except Exception as e:
        print(f"[ERROR] Не удалось отменить старые чеки: {e}")



@bot.callback_query_handler(func=lambda call: call.data == "send_all_checks")
def send_all_checks(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    crypto_api = crypto_pay.CryptoPay()
    
    try:
        cancel_old_checks(crypto_api)
        balance_result = crypto_api.get_balance()
        if not balance_result.get("ok", False):
            bot.edit_message_text(
                "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        
        usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        
        print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
        
        if usdt_balance <= 0:
            bot.edit_message_text(
                f"❌ Недостаточно средств на балансе CryptoPay.\nДоступно: {usdt_balance} USDT\nВ резерве: {usdt_onhold} USDT",
                call.message.chat.id,
                call.message.message_id
            )
            return
    except Exception as e:
        print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
        bot.edit_message_text(
            "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
            call.message.chat.id,
            call.message.message_id
        )
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if treasury_balance <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(
                "❌ Недостаточно средств в казне для выплат.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        # Получаем пользователей с балансом > 0.2, включая USERNAME
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE BALANCE > 0.2')
        users = cursor.fetchall()
        
        if not users:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="Gv"))
            bot.edit_message_text(
                "❌ Нет пользователей с балансом больше 0.2$.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        success_count = 0
        total_amount = 0
        failed_users = []
        checks_report = []  # Список для отчёта
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for user_id, balance, username in users:
            # Проверяем, достаточно ли средств перед попыткой выплаты
            if float(balance) > treasury_balance:
                failed_users.append((user_id, balance, username, "Недостаточно средств в казне"))
                continue
            if float(balance) > usdt_balance:
                failed_users.append((user_id, balance, username, "Недостаточно средств на CryptoPay"))
                continue
            
            for attempt in range(3):
                try:
                    cheque_result = crypto_api.create_check(
                        amount=str(balance),
                        asset="USDT",
                        pin_to_user_id=user_id,
                        description=f"Автоматическая выплата для пользователя {user_id}"
                    )
                    
                    # Проверяем, является ли cheque_result строкой, и парсим её как JSON
                    if isinstance(cheque_result, str):
                        try:
                            cheque_result = json.loads(cheque_result)
                        except json.JSONDecodeError as e:
                            print(f"[ERROR] Не удалось распарсить ответ от create_check: {cheque_result}, ошибка: {e}")
                            failed_users.append((user_id, balance, username, "Ошибка парсинга ответа от CryptoPay"))
                            break
                    
                    # Проверяем, если метод createCheck отключён
                    if isinstance(cheque_result, dict) and not cheque_result.get("ok", False):
                        error = cheque_result.get("error", {})
                        if isinstance(error, dict) and error.get("code") == 403 and error.get("name") == "METHOD_DISABLED":
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                            bot.edit_message_text(
                                "❌ В @CryptoBot отключена возможность создавать чеки. Включите метод createCheck в настройках приложения.",
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )
                            return
                        else:
                            error_name = error.get("name", "Неизвестная ошибка") if isinstance(error, dict) else "Неизвестная ошибка"
                            failed_users.append((user_id, balance, username, f"Ошибка CryptoPay: {error_name}"))
                            break
                    
                    if cheque_result.get("ok", False):
                        cheque = cheque_result.get("result", {})
                        cheque_link = cheque.get("bot_check_url", "")
                        
                        if cheque_link:
                            # Записываем чек в базу данных
                            cursor.execute('''
                                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (user_id, balance, cheque_link, 'pending', current_time))
                            conn.commit()
                            
                            # Обнуляем баланс пользователя
                            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
                            conn.commit()
                            
                            # Обновляем баланс казны
                            treasury_balance -= float(balance)
                            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
                            conn.commit()
                            db_module.log_treasury_operation("Автоматический вывод (массовый)", balance, treasury_balance)
                            
                            # Формируем отчёт
                            username_display = username if username and username != "Не указан" else "Не указан"
                            checks_report.append({
                                "cheque_link": cheque_link,
                                "user_id": user_id,
                                "username": username_display,
                                "amount": balance
                            })
                            
                            # Отправляем сообщение пользователю
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            try:
                                bot.send_message(
                                    user_id,
                                    f"✅ Ваш чек на сумму {balance}$ готов!\n"
                                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                                    reply_markup=markup,
                                    parse_mode='HTML'
                                )
                            except Exception as e:
                                print(f"[ERROR] Не удалось отправить сообщение пользователю {user_id}: {e}")
                                failed_users.append((user_id, balance, username, "Ошибка отправки сообщения"))
                                break
                            
                            # Логируем успех
                            log_entry = f"[{current_time}] | Массовая выплата | Пользователь {user_id} | Сумма {balance}$ | Успех"
                            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                                log_file.write(log_entry + "\n")
                            
                            success_count += 1
                            total_amount += balance
                            usdt_balance -= float(balance)
                            break
                    else:
                        error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка") if isinstance(cheque_result, dict) else "Неизвестная ошибка"
                        failed_users.append((user_id, balance, username, f"Ошибка CryptoPay: {error}"))
                        break
                except RequestException as e:
                    print(f"[ERROR] Попытка {attempt + 1} для пользователя {user_id}: {e}")
                    if attempt == 2:
                        failed_users.append((user_id, balance, username, f"Ошибка запроса: {str(e)}"))
                    continue
        
        # Формируем отчёт для администратора
        report = (
            f"✅ Отправлено чеков: {success_count}\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$\n"
        )
        if checks_report:
            report += "\n📋 Успешные выплаты:\n"
            for entry in checks_report:
                report += (
                    f"ID: {entry['user_id']}, "
                    f"Username: @{entry['username']}, "
                    f"Сумма: {entry['amount']}$, "
                    f"Ссылка: {entry['cheque_link']}\n"
                    f""
                    f"————————————————————————"
                )
        if failed_users:
            report += "\n❌ Не удалось обработать для пользователей:\n"
            for user_id, balance, username, error in failed_users:
                username_display = username if username and username != "Не указан" else "Не указан"
                report += f"ID: {user_id}, Username: @{username_display}, Сумма: {balance}$, Ошибка: {error}\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        # Сохраняем отчёт в файл
        report_filename = f"checks_report_{current_time.replace(':', '-')}.txt"
        with open(report_filename, "w", encoding="utf-8") as report_file:
            report_file.write(report)
        
        # Уведомляем администраторов
        admin_message = (
            f"💸 <b>Массовая отправка чеков завершена</b>\n\n"
            f"✅ Успешно отправлено: {success_count} чеков\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Остаток в казне: {treasury_balance}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$\n"
        )
        if checks_report:
            admin_message += "\n📋 Успешные выплаты:\n"
            for entry in checks_report:
                admin_message += (
                    f"ID: {entry['user_id']}, "
                    f"Username: @{entry['username']}, "
                    f"Сумма: {entry['amount']}$, "
                    f"Ссылка: {entry['cheque_link']}\n"
                )
        if failed_users:
            admin_message += "\n❌ Ошибки:\n"
            for user_id, balance, username, error in failed_users:
                username_display = username if username and username != "Не указан" else "Не указан"
                admin_message += f"ID: {user_id}, Username: @{username_display}, Сумма: {balance}$, Ошибка: {error}\n"
        
        for admin_id in config.ADMINS_ID:
            try:
                bot.send_message(admin_id, admin_message, parse_mode='HTML')
            except:
                continue


# bot.py
SEND_CHECK_STATE = {}
search_state = {}

@bot.callback_query_handler(func=lambda call: call.data == "send_check")
def send_check_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    msg = bot.edit_message_text(
        "Введите user_id или @username пользователя, которому нужно отправить чек (ответьте на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    
    SEND_CHECK_STATE[call.from_user.id] = {"step": "awaiting_user_id", "message_id": msg.message_id}
    bot.register_next_step_handler(msg, process_user_id_input)

def process_user_id_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_user_id":
        return
    
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите user_id или @username.", reply_markup=markup)
        bot.register_next_step_handler(message, process_user_id_input)
        return
    
    input_text = message.text.strip()
    user_id = None
    username = None
    
    if input_text.startswith('@'):
        username = input_text[1:]
        print(f"[DEBUG] Processing username: {username}")
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM users WHERE LOWER(USERNAME) = ?', (username.lower(),))
            user = cursor.fetchone()
            if user:
                user_id = user[0]
                print(f"[DEBUG] Found user ID {user_id} for username {username}")
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Пользователь с @username '{username}' не найден.", reply_markup=markup)
                bot.register_next_step_handler(message, process_user_id_input)
                return
    else:
        try:
            user_id = int(input_text)
            print(f"[DEBUG] Processing user ID: {user_id}")
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.reply_to(message, "❌ Неверный формат! Введите числовой ID или @username.", reply_markup=markup)
            bot.register_next_step_handler(message, process_user_id_input)
            return
    
    # Проверяем, существует ли пользователь
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, BALANCE, USERNAME FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if not user:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="send_check"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Пользователь с ID {user_id} не найден.", reply_markup=markup)
            bot.register_next_step_handler(message, process_user_id_input)
            return
        user_id, current_balance, username = user
    
    # Запрашиваем сумму
    username_display = f"@{username}" if username and username != "Не указан" else "Нет username"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    msg = bot.reply_to(
        message,
        f"Введите сумму чека в USDT для пользователя {user_id} ({username_display})\n"
        f"Текущий баланс пользователя: {current_balance} $:",
        reply_markup=markup
    )
    
    SEND_CHECK_STATE[admin_id] = {
        "step": "awaiting_amount",
        "user_id": user_id,
        "message_id": msg.message_id
    }
    bot.register_next_step_handler(msg, process_amount_input)

def process_amount_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_amount":
        return
    
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите сумму в USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    amount_str = message.text.strip()
    if not re.match(r'^\d+(\.\d{1,2})?$', amount_str):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Сумма должна быть числом (например, 1.5). Попробуйте снова:", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    amount = float(amount_str)
    user_id = SEND_CHECK_STATE[admin_id]["user_id"]
    
    if amount < 0.1:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Минимальная сумма чека — 0.1 USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if amount > treasury_balance:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Недостаточно средств в казне. В казене: {treasury_balance} USDT.", reply_markup=markup)
            return
        
        # Проверка баланса CryptoPay
        crypto_api = crypto_pay.CryptoPay()
        try:
            balance_result = crypto_api.get_balance()
            if not balance_result.get("ok", False):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
                return
            
            usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            
            print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
            
            if amount > usdt_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Недостаточно средств на CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT.", reply_markup=markup)
                return
        except Exception as e:
            print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
            return
        
        # Создаём чек
        try:
            cheque_result = crypto_api.create_check(
                amount=str(amount),
                asset="USDT",
                pin_to_user_id=user_id,
                description=f"Чек для пользователя {user_id} от администратора"
            )
            
            if not cheque_result.get("ok", False):
                error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Ошибка при создании чека: {error}", reply_markup=markup)
                return
            
            cheque = cheque_result.get("result", {})
            cheque_link = cheque.get("bot_check_url", "")
            
            if not cheque_link:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Не удалось получить ссылку на чек.", reply_markup=markup)
                return
            
            # Сохраняем чек в базе
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, amount, cheque_link, 'pending', current_time))
            
            # Обновляем баланс казны
            treasury_balance -= amount
            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
            conn.commit()
            db_module.log_treasury_operation("Ручной чек", amount, treasury_balance)
            
            # Логируем операцию
            log_entry = f"[{current_time}] | Ручной чек | Пользователь {user_id} | Сумма {amount}$ | Успех"
            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                log_file.write(log_entry + "\n")
            
            # Отправляем чек пользователю
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(
                    user_id,
                    f"✅ Ваш чек на сумму {amount}$ готов!\n"
                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отправить чек пользователю {user_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Не удалось отправить чек пользователю {user_id}: {e}", reply_markup=markup)
                return
            
            # Уведомляем администратора
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"✅ Чек на {amount}$ успешно отправлен пользователю {user_id}.", reply_markup=markup)
            
            SEND_CHECK_STATE.pop(admin_id, None)
        
        except Exception as e:
            print(f"[ERROR] Не удалось создать чек для пользователя {user_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Ошибка при создании чека: {e}", reply_markup=markup)
            return


# Обработчик текстового ввода для поиска
@bot.message_handler(func=lambda message: search_state.get(message.from_user.id, {}).get("awaiting_search", False))
def handle_search_query(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для поиска!")
        return
    
    query = message.text.strip()
    search_state[message.from_user.id] = {"query": query}
    bot.reply_to(message, f"🔍 Выполняется поиск по запросу: '{query}'...")
    
    # Вызываем соответствующую функцию обработки в зависимости от контекста
    if search_state[message.from_user.id].get("context") == "send_check":
        process_user_id_input(message)
    # Добавьте другие контексты, если они есть (например, change_price, reduce_balance)


#ДОБАВЛЕНИЕ ИД ГРУППЫ ДЛЯ ПРИНЯТИЕ НОМЕРОВ
@bot.callback_query_handler(func=lambda call: call.data == "add_group")
def add_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="groups"))
    
    msg = bot.edit_message_text(
        "📝 Введите ID группы (например, -1002453887941):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    
    bot.register_next_step_handler(msg, process_group_id_add)

def process_group_id_add(message):
    try:
        group_id = int(message.text.strip())
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM groups WHERE ID = ?', (group_id,))
            if cursor.fetchone():
                bot.reply_to(message, "❌ Эта группа уже зарегистрирована для принятия номеров!")
                return
            cursor.execute('INSERT INTO groups (ID, NAME) VALUES (?, ?)', (group_id, f"{group_id}"))
            conn.commit()
        bot.reply_to(message, f"✅ Группа с ID {group_id} успешно добавлена для принятия номеров!")
        # Возвращаем в админ-панель
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)
    except ValueError:
        bot.reply_to(message, "❌ Неверный формат ID! Введите числовое значение.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при добавлении группы: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "remove_group")
def remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups')
        groups = cursor.fetchall()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Вернуться назад", callback_data="groups"))
    if not groups:
        bot.edit_message_text(
            "📭 Нет зарегистрированных групп для удаления.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        return
    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in groups:
        markup.add(types.InlineKeyboardButton(f"➖ {group_name} (ID: {group_id})", callback_data=f"confirm_remove_{group_id}"))
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    bot.edit_message_text(
        "<b>➖ Выберите группу для удаления:</b>",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_"))
def confirm_remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group = cursor.fetchone()
        if not group:
            bot.answer_callback_query(call.id, "❌ Группа не найдена!")
            return
        group_name = group[0]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Подтвердить удаление", callback_data=f"remove_confirmed_{group_id}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="remove_group"))
        bot.edit_message_text(
            f"<b>Подтвердите удаление группы:</b>\n🏠 {group_name} (ID: {group_id})",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_confirmed_"))
def remove_confirmed_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM groups WHERE ID = ?', (group_id,))
        conn.commit()
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    bot.edit_message_text(
        f"✅ Группа с ID {group_id} успешно удалена!",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, "Группа удалена!")




#=============================================================================================================

#НОМЕРА КОТОРЫЕ НЕ ОБРАБАТЫВАЛИ В ТЕЧЕНИЕ 10 МИНУТ +
def check_number_timeout():
    """Проверяет, истекло ли время ожидания кода (10 минут)."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT NUMBER, TAKE_DATE, ID_OWNER, MODERATOR_ID, STATUS FROM numbers')
                numbers = cursor.fetchall()
                
                current_time = datetime.now()
                for number, take_date, owner_id, moderator_id, status in numbers:
                    if take_date in ("0", "1") or status not in ("на проверке", "taken"):
                        continue
                    try:
                        take_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        elapsed_time = (current_time - take_time).total_seconds() / 60
                        # Проверяем, не был ли номер автоматически подтверждён
                        cursor.execute('SELECT CONFIRMED_BY_MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
                        confirmed_by = cursor.fetchone()[0]
                        if elapsed_time >= 10 and confirmed_by is not None:
                            # Номер возвращается в очередь только если не был автоматически подтверждён
                            cursor.execute('UPDATE numbers SET MODERATOR_ID = NULL, TAKE_DATE = "0", STATUS = "ожидает" WHERE NUMBER = ?', (number,))
                            conn.commit()
                            logging.info(f"Номер {number} возвращён в очередь из-за бездействия модератора.")
                            
                            if owner_id:
                                markup_owner = types.InlineKeyboardMarkup()
                                markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
                                markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    owner_id,
                                    f"📱 Ваш номер {number} возвращён в очередь из-за бездействия модератора.",
                                    parse_mode='HTML',
                                    reply_markup=markup_owner
                                )
                            
                            if moderator_id:
                                markup_mod = types.InlineKeyboardMarkup()
                                markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                                markup_mod.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    moderator_id,
                                    f"📱 Номер {number} возвращён в очередь из-за бездействия.",
                                    parse_mode='HTML',
                                    reply_markup=markup_mod
                                )
                    except ValueError as e:
                        logging.error(f"Неверный формат времени для номера {number}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            logging.error(f"Ошибка в check_number_timeout: {e}")
            time.sleep(60)
# Запускаем фоновую задачу



def check_number_hold_time():
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT HOLD_TIME FROM settings')
                result = cursor.fetchone()
                hold_time = result[0] if result else 5

                cursor.execute('''
                    SELECT NUMBER, ID_OWNER, TAKE_DATE, STATUS, CONFIRMED_BY_MODERATOR_ID
                    FROM numbers 
                    WHERE STATUS = 'активен' AND TAKE_DATE NOT IN ('0', '1')
                ''')
                numbers = cursor.fetchall()

                current_time = datetime.now()
                for number, owner_id, take_date, status, mod_id in numbers:
                    try:
                        start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        time_elapsed = (current_time - start_time).total_seconds() / 60
                        if time_elapsed < hold_time:
                            logging.debug(f"Номер {number} ещё не отстоял: {time_elapsed:.2f}/{hold_time} минут")
                            continue

                        # Проверяем текущий статус номера
                        cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
                        current_status = cursor.fetchone()[0]
                        if current_status != 'активен':
                            logging.info(f"Номер {number} пропущен: статус изменился на {current_status}")
                            continue

                        # Получаем индивидуальную цену пользователя
                        price = db_module.get_user_price(owner_id)

                        # Устанавливаем SHUTDOWN_DATE как текущее время
                        shutdown_date = current_time.strftime("%Y-%m-%d %H:%M:%S")
                        cursor.execute('''
                            UPDATE numbers 
                            SET STATUS = 'отстоял', 
                                SHUTDOWN_DATE = ? 
                            WHERE NUMBER = ?
                        ''', (shutdown_date, number))
                        # Начисляем оплату
                        cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (price, owner_id))
                        conn.commit()
                        logging.info(f"Номер {number} отстоял. SHUTDOWN_DATE: {shutdown_date}, начислено {price}$ пользователю {owner_id}")

                        # Уведомляем владельца
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        safe_send_message(
                            owner_id,
                        f"📌 <b>Время холда завершено</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📞 <b>Номер:</b> <code>{number} успешно отстоял</code>\n"
                        f"<b>🟢Встал:</b> {take_date} \n"
                        f"⏳ <b>Отстоял: </b> {shutdown_date} \n"
                         f"💰 Начислено: {price}$\n"
                        "✅ <i>Вы можете сдать новый номер.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━",
                        parse_mode='HTML',
                        reply_markup=markup
                        )
                    except ValueError as e:
                        logging.error(f"Неверный формат времени для номера {number}: {e}")
                    except Exception as e:
                        logging.error(f"Ошибка при обработке номера {number}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в check_number_hold_time: {e}")
        
        time.sleep(60)  # Проверяем каждую минуту

# МОДЕРАЦИЯ НОМЕРОВ:


#Обработчики для получеяяния номеров

@bot.callback_query_handler(func=lambda call: call.data == "get_number")
def get_number(call):
    user_id = call.from_user.id
    if not db_module.is_moderator(user_id):
        bot.answer_callback_query(call.id, "❌ У вас нет прав для получения номера!")
        return
    
    number = db_module.get_available_number(user_id)
    
    if number:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT fa, ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            fa_code, owner_id = result
            cursor.execute('UPDATE numbers SET TAKE_DATE = ?, MODERATOR_ID = ?, GROUP_CHAT_ID = ? WHERE NUMBER = ?',
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, call.message.chat.id, number))
            conn.commit()
        
        fa_text = f"2FA: {fa_code}" if fa_code else "2FA: не установлен"
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{call.message.chat.id}"),
            types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"📱 Новый номер для проверки: <code>{number}</code>\n"
            f"{fa_text}\n"
            "Ожидайте код от владельца или отметьте номер как невалидный.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(
            "📭 Нет доступных номеров для проверки.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

# bot.py
def get_number_in_group(user_id, chat_id, message_id, tg_number):
    # Проверяем, является ли пользователь модератором
    if not db_module.is_moderator(user_id) and user_id not in config.ADMINS_ID:
        bot.send_message(chat_id, "❌ У вас нет прав для получения номера!", reply_to_message_id=message_id)
        return
    
    # Проверяем, зарегистрирована ли группа в таблице groups
    group_ids = db_module.get_all_group_ids()
    if chat_id not in group_ids:
        bot.send_message(chat_id, "❌ Эта группа не зарегистрирована для принятия номеров!", reply_to_message_id=message_id)
        return
    
    # Получаем доступный номер
    number = db_module.get_available_number(user_id)
    
    if number:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT fa, ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            fa_code, owner_id = result
            cursor.execute('UPDATE numbers SET TAKE_DATE = ?, MODERATOR_ID = ?, GROUP_CHAT_ID = ?, TG_NUMBER = ? WHERE NUMBER = ?',
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, chat_id, tg_number, number))
            conn.commit()
        
        fa_text = f"2FA: {fa_code}" if fa_code else "2FA: не установлен"
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{chat_id}_{tg_number}"),
            types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.send_message(
            chat_id,
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"📱 <b>Новый номер для проверки:</b> <code>{number}</code>\n"
            f"{fa_text}\n"
            "Ожидайте код от владельца или отметьте номер как невалидный.",
            parse_mode='HTML',
            reply_markup=markup,
            reply_to_message_id=message_id
        )

        # Сохраняем сообщение в code_messages для последующего редактирования
        code_messages[number] = {
            "timestamp": datetime.now(),
            "chat_id": chat_id,
            "message_id": msg.message_id,
            "tg_number": tg_number
        }
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(
            chat_id,
            f"📭 Нет доступных номеров для проверки (ТГ {tg_number}).",
            parse_mode='HTML',
            reply_markup=markup,
            reply_to_message_id=message_id
        )
#Обработчики для отправки и подтверждения кодов




@bot.callback_query_handler(func=lambda call: call.data.startswith("send_code_"))
def send_verification_code(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4]) if len(parts) > 4 else 1

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()

        if owner:
            owner_id = owner[0]

            # Проверка AFK_LOCKED
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT AFK_LOCKED FROM users WHERE ID = ?', (owner_id,))
                afk_locked = cursor.fetchone()
            if afk_locked and afk_locked[0] == 1:
                bot.answer_callback_query(call.id, "🔒 Пользователь заблокирован для сдачи номеров!")
                return

            try:
                # Редактируем сообщение в группе
                message_data = code_messages.get(number)
                if message_data:
                    try:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(
                            types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
                        )
                        bot.edit_message_text(
                            (
                                "📌 <b>Отправлен запрос кода</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                f"📱 <b>ТГ:</b> {tg_number}\n"
                                f"📞 <b>Номер:</b> <code>{number}</code>\n"
                                "✉️ <i>Запрос кода отправлен владельцу. Ожидаем ответ...</i>\n"
                                "━━━━━━━━━━━━━━━━━━━━"
                            ),
                            chat_id=group_chat_id,
                            message_id=message_data["message_id"],
                            reply_markup=markup,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
                        # Fallback: отправляем новое сообщение
                        msg = bot.send_message(
                            group_chat_id,
                            (
                                "📌 <b>Отправлен запрос кода</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                f"📱 <b>ТГ:</b> {tg_number}\n"
                                f"📞 <b>Номер:</b> <code>{number}</code>\n"
                                "✉️ <i>Запрос кода отправлен владельцу. Ожидаем ответ...</i>\n"
                                "━━━━━━━━━━━━━━━━━━━━"
                            ),
                            reply_markup=markup,
                            parse_mode='HTML'
                        )
                        code_messages[number]["message_id"] = msg.message_id
                else:
                    bot.answer_callback_query(call.id, "❌ Сообщение для редактирования не найдено!")
                    return

                # Отправляем запрос кода владельцу
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"mark_invalid_{number}_{group_chat_id}_{tg_number}")
                )
                msg = bot.send_message(
                    owner_id,
                    (
                        "📌 <b>Запрос кода подтверждения</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>Номер:</b> <code>{number}</code>\n"
                        f"📨 <i>Пожалуйста, введите код, который будет отправлен модератору.</i>\n\n"
                        "✏️ <b>Ответьте на это сообщение, чтобы отправить код</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    reply_markup=markup,
                    parse_mode='HTML'
                )

                with db.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('UPDATE numbers SET VERIFICATION_CODE = "", MODERATOR_ID = ?, GROUP_CHAT_ID = ? WHERE NUMBER = ?', 
                                  (call.from_user.id, group_chat_id, number))
                    conn.commit()

                if owner_id not in active_code_requests:
                    active_code_requests[owner_id] = {}
                active_code_requests[owner_id][number] = msg.message_id

                bot.register_next_step_handler(
                    msg,
                    process_verification_code_input,
                    number,
                    call.from_user.id,
                    group_chat_id,
                    msg.chat.id,
                    msg.message_id,
                    tg_number
                )

                bot.answer_callback_query(call.id, "✅ Запрос кода отправлен владельцу.")

            except telebot.apihelper.ApiTelegramException as e:
                if e.error_code == 403 and "user is deactivated" in e.description:
                    bot.answer_callback_query(call.id, "❌ Пользователь деактивирован, номер помечен как невалидный!")
                    with db.get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
                        conn.commit()
                        print(f"[DEBUG] Номер {number} помечен как невалид из-за деактивированного пользователя")
                    if owner_id in active_code_requests and number in active_code_requests[owner_id]:
                        del active_code_requests[owner_id][number]
                        if not active_code_requests[owner_id]:
                            del active_code_requests[owner_id]
                else:
                    raise e
        else:
            bot.answer_callback_query(call.id, "❌ Владелец номера не найден!")

    except Exception as e:
        print(f"[ERROR] Ошибка в send_verification_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_invalid_"))
def mark_number_invalid(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    try:
        # Разбираем callback_data
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4])

        # Проверяем, существует ли номер в базе и является ли пользователь владельцем
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            owner_id, moderator_id = result

            # Проверяем, что вызывающий пользователь является владельцем номера
            if call.from_user.id != owner_id:
                bot.answer_callback_query(call.id, "❌ У вас нет прав для пометки этого номера как невалидного!")
                return

            # Обновляем статус номера на "невалид"
            try:
                cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
                conn.commit()
                print(f"[DEBUG] Номер {number} помечен как невалид")
            except Exception as e:
                print(f"[ERROR] Ошибка при обновлении статуса номера {number}: {e}")
                raise e

        # Формируем confirmation_key
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key in confirmation_messages:
            try:
                bot.delete_message(
                    confirmation_messages[confirmation_key]["chat_id"],
                    confirmation_messages[confirmation_key]["message_id"]
                )
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении сообщения подтверждения {confirmation_key}: {e}")
            del confirmation_messages[confirmation_key]
            print(f"[DEBUG] Удалён confirmation_key {confirmation_key} из confirmation_messages")

        # Очищаем active_code_requests и уведомляем владельца, если есть активный запрос
        if owner_id in active_code_requests and number in active_code_requests[owner_id]:
            message_id = active_code_requests[owner_id][number]
            try:
                bot.edit_message_text(
                    f"❌ Запрос кода для номера {number} отменён, так как номер помечен как невалидный.",
                    owner_id,
                    message_id,
                    parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось обновить сообщение для owner_id {owner_id}, message_id {message_id}: {e}")
            del active_code_requests[owner_id][number]
            print(f"[DEBUG] Удалён номер {number} из active_code_requests для owner_id {owner_id}")
            if not active_code_requests[owner_id]:
                del active_code_requests[owner_id]
                print(f"[DEBUG] Удалён owner_id {owner_id} из active_code_requests")

        # Уведомляем владельца
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        bot.edit_message_text(
            f"❌ Вы отметили номер {number} как невалидный. Номер помечен как невалид.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup_owner,
            parse_mode='HTML'
        )

        # Уведомляем модератора в группе
        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                group_chat_id,
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"❌ Владелец номера {number} отметил его как невалидный. \n Приносим свои извинения, пожалуйста, возьмите новый номер",
                reply_markup=markup_mod,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            print(f"[ERROR] Не удалось отправить сообщение в группу {group_chat_id}: {e}")
            if moderator_id:
                try:
                    bot.send_message(
                        moderator_id,
                        f"📱 <b>ТГ {tg_number}</b>\n"
                        f"❌ Владелец номера {number} отметил его как невалидный. Номер помечен как невалид.\n"
                        f"⚠️ Не удалось отправить сообщение в группу (ID: {group_chat_id}).",
                        reply_markup=markup_mod,
                        parse_mode='HTML'
                    )
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"[ERROR] Не удалось отправить сообщение модератору {moderator_id}: {e}")

        bot.answer_callback_query(call.id, "✅ Номер отмечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в mark_number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_invalid_"))
def handle_moderator_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        owner_id = int(parts[4])

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
            conn.commit()
            print(f"[DEBUG] Номер {number} помечен как невалид модератором")

        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                owner_id,
                f"❌ Ваш номер {number} был помечен как невалидный модератором.\n📱 Проверьте номер и сдайте заново.",
                reply_markup=markup_owner,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение владельцу {owner_id}: {e}")

        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
        )
        bot.edit_message_text(
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"❌ Номер {number} помечен как невалидный модератором.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup_mod,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер помечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в handle_moderator_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")
# Словари для хранения контекста
confirmation_messages = {}
button_contexts = {}
code_messages = {}
active_code_requests = {}


# ==========================
# 1. Ввод кода подтверждения
# ==========================
def process_verification_code_input(message, number, moderator_id, group_chat_id, original_chat_id, original_message_id, tg_number):
    try:
        user_id = message.from_user.id
        db.update_last_activity(user_id)

        # Проверка существования номера
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                try:
                    bot.delete_message(original_chat_id, original_message_id)
                except Exception as e:
                    print(f"[ERROR] Не удалось удалить сообщение для номера {number}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                    types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
                )
                bot.send_message(
                    message.chat.id,
                    (
                        "📌 <b>Запрос кода отменён</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        "❌ <i>Номер не найден или удалён из системы.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                active_code_requests.pop(user_id, {}).pop(number, None)
                return

        # Проверка, что сообщение — ответ на нужный запрос
        if not message.reply_to_message or \
           message.reply_to_message.chat.id != original_chat_id or \
           message.reply_to_message.message_id != original_message_id:

            try:
                bot.delete_message(original_chat_id, original_message_id)
            except Exception as e:
                print(f"[ERROR] Не удалось удалить сообщение для номера {number}: {e}")

            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}"))

            msg = bot.send_message(
                message.chat.id,
                (
                    "📌 <b>Введите код подтверждения</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📞 <b>Номер:</b> <code>{number}</code>\n"
                    "🗒️ <i>Ответьте на это сообщение, указав код.</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                ),
                reply_markup=markup,
                parse_mode="HTML"
            )
            bot.register_next_step_handler(
                msg,
                process_verification_code_input,
                number,
                moderator_id,
                group_chat_id,
                msg.chat.id,
                msg.message_id,
                tg_number
            )
            return

        user_input = message.text.strip()

        # Проверка формата кода
        if not re.match(r'^\d{5}$', user_input):
            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}"))

            try:
                bot.edit_message_text(
                    (
                        "📌 <b>Неверный формат кода</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "Код должен содержать ровно <b>5 цифр</b>.\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        "🗒️ <i>Отправьте корректный код, ответив на это сообщение.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    chat_id=original_chat_id,
                    message_id=original_message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
                msg = bot.send_message(
                    message.chat.id,
                    (
                        "📌 <b>Неверный формат кода</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "Код должен содержать ровно <b>5 цифр</b>.\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        "🗒️ <i>Отправьте корректный код, ответив на это сообщение.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                original_message_id = msg.message_id

            bot.register_next_step_handler(
                msg,
                process_verification_code_input,
                number,
                moderator_id,
                group_chat_id,
                original_chat_id,
                original_message_id,
                tg_number
            )
            return

        # Сохраняем код в базу
        with db.get_db() as conn:
            cursor = conn.cursor()
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                '''
                UPDATE numbers 
                SET VERIFICATION_CODE = ?, TAKE_DATE = ?, STATUS = 'на проверке' 
                WHERE NUMBER = ?
                ''',
                (user_input, current_date, number)
            )
            conn.commit()

        # Кнопки подтверждения
        markup = types.InlineKeyboardMarkup()
        confirm_key = str(uuid.uuid4())[:8]
        change_key = str(uuid.uuid4())[:8]
        invalid_key = str(uuid.uuid4())[:8]

        button_contexts[confirm_key] = {
            "action": "confirm_code",
            "number": number,
            "user_input": user_input,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }
        button_contexts[change_key] = {
            "action": "change_code",
            "number": number,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }
        button_contexts[invalid_key] = {
            "action": "mark_invalid_confirmation",
            "number": number,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }

        markup.add(
            types.InlineKeyboardButton("✅ Да, код верный", callback_data=f"btn_{confirm_key}"),
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"btn_{change_key}")
        )
        markup.add(types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}"))

        # Обновляем оригинальное сообщение
        try:
            bot.edit_message_text(
                (
                    "📌 <b>Проверка кода</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📞 <b>Номер:</b> <code>{number}</code>\n"
                    f"🔐 <b>Введённый код:</b> <code>{user_input}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "Подтвердите правильность кода ⬇️"
                ),
                chat_id=original_chat_id,
                message_id=original_message_id,
                reply_markup=markup,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
            # Fallback: отправляем новое сообщение, если редактирование не удалось
            confirmation_msg = bot.send_message(
                message.chat.id,
                (
                    "📌 <b>Проверка кода</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📞 <b>Номер:</b> <code>{number}</code>\n"
                    f"🔐 <b>Введённый код:</b> <code>{user_input}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "Подтвердите правильность кода ⬇️"
                ),
                reply_markup=markup,
                parse_mode='HTML'
            )
            original_message_id = confirmation_msg.message_id

        # Обновляем confirmation_messages
        confirmation_messages[f"{number}_{user_id}"] = {
            "chat_id": original_chat_id,
            "message_id": original_message_id
        }

        # Чистим запросы
        active_code_requests.pop(user_id, {}).pop(number, None)

    except Exception as e:
        print(f"[ERROR] Ошибка в process_verification_code_input: {e}")
        bot.send_message(
            message.chat.id,
            "❌ <b>Ошибка при обработке кода. Попробуйте снова.</b>",
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_code_"))
def confirm_code(call):
    try:    
        parts = call.data.split("_")
        if len(parts) < 6:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        
        number = parts[2]
        code = parts[3]
        group_chat_id = int(parts[4])
        tg_number = int(parts[5])
        
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT MODERATOR_ID, GROUP_CHAT_ID, ID_OWNER, fa, 
                       (SELECT IS_AFK FROM users WHERE ID = numbers.ID_OWNER) AS is_afk
                FROM numbers 
                WHERE NUMBER = ?
            ''', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            moderator_id, stored_chat_id, owner_id, fa_code, is_afk = result
        
            if is_afk:
                bot.answer_callback_query(call.id, "❌ Номер недоступен: владелец в режиме АФК!")
                confirmation_messages.pop(f"{number}_{owner_id}", None)
                code_messages.pop(number, None)
                return
        
        if stored_chat_id != group_chat_id:
            cursor.execute('UPDATE numbers SET GROUP_CHAT_ID = ? WHERE NUMBER = ?', (group_chat_id, number))
            conn.commit()

        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key not in confirmation_messages:
            bot.answer_callback_query(call.id, "❌ Данные о подтверждении не найдены!")
            return
        confirmation_data = confirmation_messages[confirmation_key]
        confirmation_chat_id = confirmation_data["chat_id"]
        confirmation_message_id = confirmation_data["message_id"]

        try:    
            bot.edit_message_text(
                f"📌 <b>Код принят</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📞 <b>Номер:</b> <code>{number}</code>\n"
                f"🔐 <b>Код:</b> <code>{code}</code>\n"
                "✅ <i>Код успешно передан модератору</i>\n"
                "━━━━━━━━━━━━━━━━━━━━",
                confirmation_chat_id,
                confirmation_message_id,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Ошибка при редактировании сообщения: {e}")
            bot.answer_callback_query(call.id, "❌ Не удалось обновить сообщение!")
            return
        
        del confirmation_messages[confirmation_key]
        bot.answer_callback_query(call.id)
        
        if moderator_id:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Да, встал", callback_data=f"number_active_{number}_{tg_number}"),
                types.InlineKeyboardButton("❌ Нет, изменить", callback_data=f"number_invalid_{number}_{tg_number}")
            )
            fa_text = f"🔑 <b>2FA:</b> <code>{fa_code}</code>" if fa_code else "🔑 <b>2FA:</b> <i>не установлен</i>"
            try:
                message = bot.send_message(
                    group_chat_id,
                    f"📌 <b>Проверка номера</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>ТГ:</b> <code>{tg_number}</code>\n"
                    f"📞 <b>Номер:</b> <code>{number}</code>\n"
                    f"🔐 <b>Код:</b> <code>{code}</code>\n"
                    f"{fa_text}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "❓ <i>Встал ли номер?</i>",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                code_messages[number] = {
                    "chat_id": group_chat_id,
                    "message_id": message.message_id,
                    "timestamp": datetime.now(),
                    "tg_number": tg_number,
                    "owner_id": owner_id
                }
            except telebot.apihelper.ApiTelegramException as e:
                print(f"Не удалось отправить сообщение в группу {group_chat_id}: {e}")
                try:
                    message = bot.send_message(
                        moderator_id,
                        f"📌 <b>Проверка номера</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>ТГ:</b> <code>{tg_number}</code>\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        f"🔐 <b>Код:</b> <code>{code}</code>\n"
                        f"{fa_text}\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "❓ <i>Встал ли номер?</i>\n"
                        f"⚠️ <i>Не удалось отправить в группу (ID: {group_chat_id})</i>",
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                    code_messages[number] = {
                        "chat_id": moderator_id,
                        "message_id": message.message_id,
                        "timestamp": datetime.now(),
                        "tg_number": tg_number,
                        "owner_id": owner_id
                    }
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"Не удалось отправить сообщение модератору {moderator_id}: {e}")
                    for admin_id in config.ADMINS_ID:
                        try:
                            bot.send_message(
                                admin_id,
                                f"⚠️ <b>Ошибка:</b> Не удалось отправить код модератору <code>{moderator_id}</code> для номера <code>{number}</code>\n"
                                f"Проверьте права бота в группе <code>{group_chat_id}</code>.",
                                parse_mode='HTML'
                            )
                        except:
                            continue
    except Exception as e:
        print(f"Ошибка в confirm_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении кода!")



@bot.callback_query_handler(func=lambda call: call.data.startswith("btn_"))
def handle_button_context(call):
    try:
        key = call.data.replace("btn_", "")
        context = button_contexts.get(key)
        if not context:
            bot.answer_callback_query(call.id, "❌ Неверный контекст кнопки!")
            return

        action = context["action"]
        number = context["number"]
        group_chat_id = context["group_chat_id"]
        tg_number = context["tg_number"]
        user_id = context["user_id"]

        if action == "confirm_code":
            user_input = context["user_input"]
            # Получаем данные сообщения в группе из code_messages
            message_data = code_messages.get(number)
            if not message_data:
                bot.answer_callback_query(call.id, "❌ Сообщение в группе не найдено!")
                return

            # Проверка статуса fa
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT fa FROM numbers WHERE NUMBER = ?', (number,))
                fa_result = cursor.fetchone()
                two_fa_status = "не установлен" if not fa_result or not fa_result[0] else "установлен"

            # Редактируем сообщение в группе
            try:
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✅ Да, встал", callback_data=f"number_active_{number}_{tg_number}"),
                    types.InlineKeyboardButton("❌ Нет, изменить", callback_data=f"number_invalid_{number}_{tg_number}")
                )
                bot.edit_message_text(
                    (
                        "📌 <b>Проверка номера</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>ТГ:</b> {tg_number}\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        f"🔐 <b>Код:</b> <code>{user_input}</code>\n"
                        f"🔑 2FA: {two_fa_status}\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "❓ Встал ли номер?"
                    ),
                    chat_id=group_chat_id,
                    message_id=message_data["message_id"],
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отредактировать сообщение в группе для номера {number}: {e}")
                # Fallback: отправляем новое сообщение
                msg = bot.send_message(
                    group_chat_id,
                    (
                        "📌 <b>Проверка номера</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>ТГ:</b> {tg_number}\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        f"🔐 <b>Код:</b> <code>{user_input}</code>\n"
                        f"🔑 2FA: {two_fa_status}\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "❓ Встал ли номер?"
                    ),
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                code_messages[number]["message_id"] = msg.message_id

            # Обновляем сообщение пользователя
            try:
                bot.edit_message_text(
                    (
                        "📌 <b>Код отправлен на проверку</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        f"🔐 <b>Введённый код:</b> <code>{user_input}</code>\n"
                        "⏳ <i>Ожидайте подтверждения от модератора.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отредактировать сообщение пользователя для номера {number}: {e}")

            bot.answer_callback_query(call.id, "✅ Код отправлен модератору.")

        elif action == "change_code":
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
                result = cursor.fetchone()
                if not result:
                    bot.answer_callback_query(call.id, "❌ Номер не найден!")
                    return
                moderator_id = result[0] if result else call.from_user.id

            # Получаем данные сообщения из confirmation_messages
            message_data = confirmation_messages.get(f"{number}_{user_id}")
            if not message_data:
                bot.answer_callback_query(call.id, "❌ Сообщение для редактирования не найдено!")
                return

            chat_id = message_data["chat_id"]
            message_id = message_data["message_id"]

            # Создаём кнопку "Не валид"
            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid_confirmation",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}"))

            # Редактируем оригинальное сообщение
            try:
                bot.edit_message_text(
                    (
                        "📌 <b>Введите новый код подтверждения</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        "🗒️ <i>Отправьте новый код, ответив на это сообщение.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
                # Fallback: отправляем новое сообщение
                msg = bot.send_message(
                    chat_id,
                    (
                        "📌 <b>Введите новый код подтверждения</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📞 <b>Номер:</b> <code>{number}</code>\n"
                        "🗒️ <i>Отправьте новый код, ответив на это сообщение.</i>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                confirmation_messages[f"{number}_{user_id}"] = {
                    "chat_id": msg.chat.id,
                    "message_id": msg.message_id
                }
                message_id = msg.message_id

            # Регистрируем обработчик для нового кода
            bot.register_next_step_handler_by_chat_id(
                chat_id,
                process_verification_code_input,
                number,
                moderator_id,
                group_chat_id,
                chat_id,
                message_id,
                tg_number
            )

            bot.answer_callback_query(call.id, "✏️ Введите новый код.")

        elif action == "mark_invalid_confirmation":
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
                conn.commit()

            bot.edit_message_text(
                (
                    "📌 <b>Номер помечен как невалидный</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📞 <b>Номер:</b> <code>{number}</code>\n"
                    "❌ <i>Номер отклонён.</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                ),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "❌ Номер помечен как невалидный.")

            # Уведомляем модератора
            bot.send_message(
                group_chat_id,
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"📞 Номер <code>{number}</code> помечен как невалидный.",
                parse_mode="HTML"
            )

            # Очищаем данные
            confirmation_messages.pop(f"{number}_{user_id}", None)
            code_messages.pop(number, None)
            button_contexts.pop(key, None)

    except Exception as e:
        print(f"[ERROR] Ошибка в handle_button_context: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("change_code_"))
def change_code(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4])
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            moderator_id = result[0] if result else call.from_user.id

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")

        markup = types.ReplyKeyboardRemove()
        msg = bot.send_message(
            call.from_user.id,
            (
                "📌 <b>Введите новый код подтверждения</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📞 <b>Номер:</b> <code>{number}</code>\n"
                "🗒️ <i>Отправьте новый код, ответив на это сообщение.</i>\n"
                "🔐 <i>Код будет передан модератору для проверки.</i>\n"
                "━━━━━━━━━━━━━━━━━━━━"
            ),
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        bot.register_next_step_handler(
            msg,
            process_verification_code_input,
            number,
            moderator_id,
            group_chat_id,
            msg.chat.id,
            msg.message_id,
            tg_number
        )
    
    except Exception as e:
        print(f"Ошибка в change_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при изменении кода!")


def create_back_to_main_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    return markup

#Обработчики для подтверждения/отклонения номеров



@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_reject_"))
def handle_number_rejection(call):
    try:
        number = call.data.split("_")[2]
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()
            cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
            conn.commit()
            print(f"[DEBUG] Номер {number} помечен как невалид")

            if owner:
                markup_owner = types.InlineKeyboardMarkup()
                markup_owner.add(
                    types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                    types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
                )
                try:
                    bot.send_message(
                        owner[0],
                        f"❌ Ваш номер {number} был отклонён модератором.\n📱 Проверьте номер и сдайте заново.",
                        reply_markup=markup_owner,
                        parse_mode='HTML'
                    )
                    print(f"[DEBUG] Отправлено сообщение владельцу {owner[0]}")
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"[ERROR] Не удалось отправить сообщение владельцу {owner[0]}: {e}")

        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
        )
        bot.send_message(
            call.message.chat.id,
            f"📱 Номер {number} отклонён и помечен как невалидный.",
            reply_markup=markup_mod,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер помечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в handle_number_rejection: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_invalid_"))
def handle_moderator_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        owner_id = int(parts[4])

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
            conn.commit()
            print(f"[DEBUG] Номер {number} помечен как невалид модератором")

        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                owner_id,
                f"❌ Ваш номер {number} был помечен как невалидный модератором.\n📱 Проверьте номер и сдайте заново.",
                reply_markup=markup_owner,
                parse_mode='HTML'
            )
            print(f"[DEBUG] Отправлено сообщение владельцу {owner_id}")
        except telebot.apihelper.ApiTelegramException as e:
            print(f"[ERROR] Не удалось отправить сообщение владельцу {owner_id}: {e}")

        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
        )
        bot.send_message(
            call.message.chat.id,
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"❌ Номер {number} помечен как невалидный модератором.",
            reply_markup=markup_mod,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер помечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в handle_moderator_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_active_"))
def number_active(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        tg_number = int(parts[3])

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, VERIFICATION_CODE, fa FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return

            owner_id, verification_code, fa = result
            # Устанавливаем статус 'активен', ID модератора и время подтверждения
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                '''
                UPDATE numbers 
                SET STATUS = ?, 
                    CONFIRMED_BY_MODERATOR_ID = ?, 
                    TAKE_DATE = ? 
                WHERE NUMBER = ?
                ''',
                ('активен', call.from_user.id, current_time, number)
            )
            conn.commit()
            print(f"[DEBUG] Номер {number} подтверждён модератором {call.from_user.id}, статус: активен, TAKE_DATE: {current_time}")

        if owner_id:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                owner_text = (
                    "✅ <b>Ваш номер успешно подтверждён!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📱 <b>Номер:</b> <code>{number}</code>\n"
                    f"🔢 <b>Код подтверждения:</b> <code>{verification_code}</code>\n"
                    f"🔒 <b>2FA:</b> <code>{fa}</code>\n"
                    f"🟢 <b>Встал:</b> {current_time}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "🎉 <i>Теперь он активен </i>"
                )
                bot.send_message(
                    owner_id,
                    owner_text,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                print(f"[DEBUG] Уведомление отправлено владельцу {owner_id} о подтверждении номера {number}")
            except Exception as e:
                print(f"[ERROR] Не удалось отправить уведомление владельцу {owner_id}: {e}")

        # Сообщение модератору
        moderator_text = (
            f"📌 <b>Подтверждение номера</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>ТГ:</b> {tg_number}\n"
            f"✅ <b>Номер:</b> <code>{number}</code>\n"
            f"🔢 <b>Код:</b> <code>{verification_code}</code>\n"
            f"🔒 <b>2FA:</b> <code>{fa}</code>\n"
            f"🕒 <b>Подтверждён:</b> {current_time}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <i>Статус обновлён на «активен»</i>"
        )

        bot.edit_message_text(
            moderator_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер успешно подтверждён!")


    except Exception as e:
        print(f"[ERROR] Ошибка в number_active: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении номера!")


@bot.callback_query_handler(func=lambda call: call.data.startswith("invalid_"))
def handle_invalid_number(call):
    number = call.data.split("_")[1]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner = cursor.fetchone()
        cursor.execute('UPDATE numbers SET STATUS = ? WHERE NUMBER = ?', ('невалид', number))
        conn.commit()

        if owner:
            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(owner[0], 
                               f"❌ Ваш номер {number} был помечен как невалидный модератором.\n📱 Проверьте номер и сдайте заново.", 
                               reply_markup=markup_owner,
                               parse_mode='HTML')
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось отправить сообщение владельцу {owner[0]}: {e}")

    bot.edit_message_text(f"✅ Номер {number} помечен как невалид", 
                         call.message.chat.id, 
                         call.message.message_id,
                         parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_failed_"))
def handle_number_failed(call):
    number = call.data.split("_")[2]
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS FROM numbers WHERE NUMBER = ?', (number,))
            data = cursor.fetchone()
            if not data:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            
            take_date, owner_id, confirmed_by_moderator_id, status = data
            
            if status == "отстоял":
                bot.answer_callback_query(call.id, "✅ Номер уже отстоял своё время!")
                return

            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5
            
            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time
            
            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                          (shutdown_date, number))
            conn.commit()
        
        mod_message = (
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            mod_message += f"🟢 Встал: {take_date}\n"
        mod_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
        mod_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        owner_message = (
            f"❌ Ваш номер {number} слетел.\n"
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            owner_message += f"🟢 Встал: {take_date}\n"
        owner_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        bot.send_message(owner_id, owner_message, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderator_numbers"))
        bot.edit_message_text(mod_message, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_number_failed: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")


#Просмотр номеров:

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_failed_"))
def mark_failed(call):
    number = call.data.split("_")[2]
    user_id = call.from_user.id
    
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS FROM numbers WHERE NUMBER = ?', (number,))
            data = cursor.fetchone()
            if not data:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            
            take_date, owner_id, confirmed_by_moderator_id, status = data
            
            if status == "отстоял":
                bot.answer_callback_query(call.id, "✅ Номер уже отстоял своё время!")
                return
            
            if confirmed_by_moderator_id != user_id:
                bot.answer_callback_query(call.id, "❌ Вы не можете пометить этот номер как слетевший!")
                return
            
            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5
            
            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time
            
            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                          (shutdown_date, number))
            conn.commit()
        
        mod_message = (
            f"📱 Номер: <code>{number}</code>\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            mod_message += f"🟢 Встал: {take_date}\n"
        mod_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
        mod_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        owner_message = (
            f"❌ Ваш номер {number} слетел.\n"
            f"📱 Номер: <code>{number}</code>\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            owner_message += f"🟢 Встал: {take_date}\n"
        owner_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        bot.send_message(owner_id, owner_message, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data="moderator_numbers"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(mod_message, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
    
    except Exception as e:
        print(f"[ERROR] Ошибка в mark_failed: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_failed_number_"))
def view_failed_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🔴 Слетел: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_stood_number_"))
def view_stood_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🟢 Отстоял: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)


     













































#КОД ДЛЯ РЕАГИРОВАНИЙ НУ ну Тг тг
@bot.message_handler(content_types=['text'], func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_group_commands(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    # Проверяем, есть ли chat_id в таблице groups
    group_ids = db_module.get_all_group_ids()
    if chat_id not in group_ids:
        return  # Игнорируем, если чат не зарегистрирован

    was_afk = db_module.get_afk_status(user_id)
    db_module.update_last_activity(user_id)

    if was_afk:
        safe_send_message(user_id, "🔔 Вы вышли из режима АФК. Ваши номера снова видны.", parse_mode='HTML')

    tg_pattern = r'^тг(\d{1,2})$'
    match = re.match(tg_pattern, text.lower())
    if match:
        tg_number = int(match.group(1))
        if 1 <= tg_number <= 70:
            get_number_in_group(user_id, chat_id, message.message_id, tg_number)
        return

    failed_pattern = r'^/?(?:сл[её]т\s*\+?7\s*|\+?7\s*)([\d\s]*)$'
    failed_match = re.match(failed_pattern, text, re.IGNORECASE)
    if failed_match:
        handle_failed_number(message)  # Вызываем существующую функцию
        return


def safe_send_message(chat_id, text, parse_mode=None, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения пользователю {chat_id}: {e}")

@bot.message_handler(regexp=r'^/?(?:сл[её]т\s*\+?\s*7\s*|\+?\s*7\s*)([\d\s]*)$')
def handle_failed_number(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.lower().strip()

    # Проверка прав пользователя
    if not db_module.is_moderator(user_id) and user_id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для выполнения этой команды!")
        return

    # Регулярное выражение для команды «слет» и номера
    failed_pattern = r'^/?(?:сл[её]т\s*\+?7\s*|\+?7\s*)([\d\s]*)$'
    failed_match = re.match(failed_pattern, text, re.IGNORECASE)

    if not failed_match:
        bot.reply_to(message, "❌ Неверный формат команды! Используйте, например: слет +79991234567, Слёт+7965, слет +7 926 016 6647 или /слет +79991234567")
        return

    # Извлекаем цифры номера, удаляя пробелы
    number_input = ''.join(failed_match.group(1).split())

    if not number_input:
        bot.reply_to(message, "❌ Номер не указан! Введите номер, например: +79991234567 или +7965")
        return

    # Нормализуем номер: добавляем +7, если это полный номер
    normalized_number = f"+7{number_input}" if len(number_input) >= 10 else f"+7{number_input}"

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Поиск номера в базе: либо точное совпадение, либо частичное (для сокращённых номеров)
            cursor.execute('''
                SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS, TG_NUMBER, MODERATOR_ID
                FROM numbers
                WHERE NUMBER = ? OR NUMBER LIKE ?
            ''', (normalized_number, f"%{number_input}%"))
            data = cursor.fetchone()

            if not data:
                bot.reply_to(message, f"❌ Номер {normalized_number} не найден в базе!")
                return

            take_date, owner_id, confirmed_by_moderator_id, status, tg_number, moderator_id = data
            tg_number = tg_number or 1

            if status == "отстоял":
                bot.reply_to(message, f"✅ Номер {normalized_number} уже отстоял своё время и не может быть помечен как слетевший!")
                return
            if status not in ("активен", "taken"):
                bot.reply_to(message, f"❌ Номер {normalized_number} не активен (статус: {status})!")
                return

            if confirmed_by_moderator_id != user_id and moderator_id != user_id:
                bot.reply_to(message, f"❌ Вы не можете пометить этот номер как слетевший!")
                return

            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5

            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time

            # Проверка: если номер отстоял минимальное время, запрещаем пометить как "слетел"
            if worked_enough:
                bot.reply_to(message, f"✅ Номер {normalized_number} отработал минимальное время ({hold_time} минут) и не может быть помечен как слетевший!")
                # Обновляем статус на "отстоял", если он ещё не обновлён
                cursor.execute('UPDATE numbers SET STATUS = "отстоял" WHERE NUMBER = ?', (normalized_number,))
                conn.commit()
                logging.info(f"Номер {normalized_number} отстоял время, статус обновлён на 'отстоял' пользователем {user_id}")
                return

            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?',
                          (shutdown_date, normalized_number))
            conn.commit()
            logging.info(f"Модератор {user_id} пометил номер {normalized_number} как слетел в чате {chat_id} ({message.chat.type})")

            mod_message = (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📌 <b>Отчёт по номеру</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 <b>ТГ:</b> <code>{tg_number}</code>\n"
            f"📱 <b>Номер:</b> <code>{normalized_number}</code>\n"
            f"📊 <b>Статус:</b> 🟥 <b>СЛЕТЕЛ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n")

            if take_date not in ("0", "1"):
                mod_message += f"🟢 <b>Встал:</b> {take_date}\n"
            mod_message += f"🔴 <b>Слетел:</b> {shutdown_date}\n"

            if not worked_enough:
                mod_message += f"⚠️ <b>Не отработал минимум:</b> {hold_time} мин\n"

            mod_message += f"⏳ <b>Время работы:</b> {work_time:.2f} мин\n"
            mod_message += "━━━━━━━━━━━━━━━━━━━━"


            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

            # Отправка сообщения в зависимости от типа чата
            try:
                if message.chat.type in ['group', 'supergroup']:
                    bot.reply_to(message, mod_message, parse_mode='HTML', reply_markup=markup)
                else:
                    bot.send_message(chat_id, mod_message, parse_mode='HTML', reply_markup=markup)
            except telebot.apihelper.ApiTelegramException as e:
                logging.error(f"Ошибка отправки сообщения в чат {chat_id}: {e}")
                if message.chat.type in ['group', 'supergroup']:
                    bot.send_message(user_id, "❌ Не удалось отправить ответ в группу. Проверьте права бота.", parse_mode='HTML')
                return

            owner_message = (
                "🚫 <b>Ваш номер был снят с обработки!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 <b>Номер:</b> <code>{normalized_number}</code>\n"
                f"📊 <b>Статус:</b> 🟥 <b>СЛЕТЕЛ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
            )
            if take_date not in ("0", "1"):
                owner_message += f"🟢 <b>Встал:</b> {take_date}\n"
            owner_message += f"🔴 <b>Слетел:</b> {shutdown_date}\n"
            owner_message += f"⏳ <b>Время работы:</b> {work_time:.2f} мин\n"
            owner_message += "━━━━━━━━━━━━━━━━━━━━\n"
            owner_message += "⚠️ <i>Вы можете сдать новый номер в любое время.</i>"

            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            safe_send_message(owner_id, owner_message, parse_mode='HTML', reply_markup=markup_owner)

    except Exception as e:
        logging.error(f"Ошибка при обработке команды 'слет' для номера {normalized_number}: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при обработке номера.")
        
# Глобальный словарь для отслеживания активных запросов кодов по user_id
active_code_requests = {}




@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_confirm_"))
def back_to_confirm(call):
    try:
        number = call.data.split("_")[3]
        
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, VERIFICATION_CODE, TAKE_DATE, TG_NUMBER, fa, (SELECT IS_AFK FROM users WHERE ID = numbers.ID_OWNER) AS is_afk FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            
            if not result:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Номер {number} больше недоступен.\nПожалуйста, получите новый номер.",
                    reply_markup=markup
                )
                return
            
            owner_id, code, take_date, tg_number, fa_code, is_afk = result
            if not tg_number:
                tg_number = 1
            
            # Проверка АФК-статуса владельца номера
            if is_afk:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Номер {number} недоступен: пользователь в режиме АФК!\nПожалуйста, выберите другой номер.",
                    reply_markup=markup
                )
                # Очищаем данные номера из confirmation_messages и code_messages
                confirmation_messages.pop(f"{number}_{owner_id}", None)
                code_messages.pop(number, None)
                return
            
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception as e:
                print(f"Ошибка при удалении сообщения: {e}")
            
            fa_text = f"2FA: {fa_code}" if fa_code else "2FA: не установлен"
            if code and take_date != "0":
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✅ Да, встал", callback_data=f"number_active_{number}_{tg_number}"),
                    types.InlineKeyboardButton("❌ Нет, изменить", callback_data=f"number_invalid_{number}_{tg_number}")
                )
                bot.send_message(
                    call.message.chat.id,
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Код по номеру {number}\n"
                    f"Код: {code}\n"
                    f"{fa_text}\n\n"
                    "Встал ли номер?",
                    parse_mode='HTML',
                    reply_markup=markup
                )
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{call.message.chat.id}_{tg_number}"),
                    types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
                )
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(
                    call.message.chat.id,
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Новый номер для проверки: <code>{number}</code>\n"
                    f"{fa_text}\n\n"
                    "Ожидайте код от владельца или отметьте номер как невалидный.",
                    parse_mode='HTML',
                    reply_markup=markup
                )
    except Exception as e:
        print(f"Ошибка в back_to_confirm: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при возврате к подтверждению!")       


@bot.callback_query_handler(func=lambda call: call.data == "toggle_afk")
def toggle_afk(call):
    user_id = call.from_user.id
    # Убираем db.update_last_activity(user_id) — он сбивает АФК
    new_afk_status = db_module.toggle_afk_status(user_id)
    
    print(f"[DEBUG] Пользователь {user_id} изменил статус АФК на {'включён' if new_afk_status else 'выключен'}")
    
    try:
        if new_afk_status:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вошли в режим АФК. Ваши номера скрыты. Чтобы выйти, нажмите кнопку Выключить афк.",
                parse_mode='HTML'
            )
        else:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вышли из режима АФК. Ваши номера снова видны.",
                parse_mode='HTML'
            )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление о смене АФК пользователю {user_id}: {e}")
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
    
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    
    if is_moderator:
        welcome_text = "Заявки"
    else:
        welcome_text = (
            f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
            f"• <i>Моментальные выплаты</i> – <b>после {hold_time} минут работы.</b>\n\n"
            "<b>💰 Тарифы на сдачу номеров:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
            "✅ <i>Прозрачные условия сотрудничества</i>\n"
            "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
            "✅ <i>Оперативная поддержка 24/7</i>\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )
    
    markup = types.InlineKeyboardMarkup()
    if not is_moderator or is_admin:
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )
    if is_admin:
        markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
    if is_moderator:
        markup.add(
            types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"),
            types.InlineKeyboardButton("📋 Мои номера", callback_data="moderator_numbers")
        )

    # Новые кнопки
    markup.add(types.InlineKeyboardButton("🗑️ Удалить номер", callback_data="delete_number"))
    markup.add(types.InlineKeyboardButton("✏️ Изменить номер", callback_data="change_number"))
    markup.add(types.InlineKeyboardButton("📩 Апелляция номера", callback_data="appeal_number"))
    markup.add(types.InlineKeyboardButton("🔐 2FA", callback_data="manage_2fa"))
    markup.add(types.InlineKeyboardButton("🔓 Сбросить 2FA", callback_data="reset_2fa"))

    # Кнопка АФК
    afk_button_text = "🟢 Включить АФК" if not new_afk_status else "🔴 Выключить АФК"
    markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
    
    bot.edit_message_text(
        welcome_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML' if not is_moderator else None,
        reply_markup=markup
    )
    
    status_text = "включён" if new_afk_status else "выключен"
    bot.answer_callback_query(call.id, f"Режим АФК {status_text}. Ваши номера {'скрыты' if new_afk_status else 'видимы'}.")


def init_db():
    db_module.create_tables()
    db_module.migrate_db()
    """Инициализирует базу данных, добавляя отсутствующие столбцы в таблицы numbers и users."""
    with db.get_db() as conn:
        cursor = conn.cursor()

        # Проверка столбцов в таблице numbers
        cursor.execute("PRAGMA table_info(numbers)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'GROUP_CHAT_ID' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN GROUP_CHAT_ID INTEGER')
                print("[INFO] Столбец GROUP_CHAT_ID успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец GROUP_CHAT_ID: {e}")

        if 'TG_NUMBER' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN TG_NUMBER INTEGER DEFAULT 1')
                print("[INFO] Столбец TG_NUMBER успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец TG_NUMBER: {e}")

        # Проверка столбцов в таблице users
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in cursor.fetchall()]
        
        if 'IS_AFK' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN IS_AFK INTEGER DEFAULT 0')
                print("[INFO] Столбец IS_AFK успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец IS_AFK: {e}")

        if 'LAST_ACTIVITY' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN LAST_ACTIVITY TEXT')
                print("[INFO] Столбец LAST_ACTIVITY успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец LAST_ACTIVITY: {e}")

        conn.commit()

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_invalid_"))
def number_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        
        # Сохраняем tg_number в базе данных
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET TG_NUMBER = ? WHERE NUMBER = ?', (tg_number, number))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код заново", callback_data=f"send_code_{number}_{call.message.chat.id}_{tg_number}"),
            types.InlineKeyboardButton("❌ Не валидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_confirm_{number}"))
        
        # Редактируем существующее сообщение вместо удаления и отправки нового
        bot.edit_message_text(
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"📱 Номер: {number}\n"
            "Пожалуйста, выберите действие:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
    
    except Exception as e:
        print(f"Ошибка в number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")

db_lock = Lock()

def check_inactivity():
    """Проверяет неактивность пользователей и переводит их в АФК через 10 минут."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT ID, LAST_ACTIVITY, IS_AFK FROM users')
                users = cursor.fetchall()
                current_time = datetime.now()
                for user_id, last_activity, is_afk in users:
                    # Пропускаем пользователей, которые уже в АФК или без активности
                    if is_afk or not last_activity:
                        continue
                    # Проверяем, является ли пользователь модератором
                    cursor.execute('SELECT ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
                    is_moder = cursor.fetchone() is not None
                    # Проверяем, является ли пользователь администратором из config.ADMINS_ID
                    is_admin = user_id in config.ADMINS_ID
                    if is_moder or is_admin:
                        print(f"[DEBUG] Пользователь {user_id} — {'модератор' if is_moder else ''}{'администратор' if is_admin else ''}, пропускаем АФК")
                        continue  # Пропускаем модераторов и администраторов
                    try:
                        last_activity_time = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
                        if current_time - last_activity_time >= timedelta(minutes=10):
                            # Переводим в АФК, только если пользователь ещё не в АФК
                            if not db_module.get_afk_status(user_id):
                                db_module.toggle_afk_status(user_id)
                                print(f"[DEBUG] Пользователь {user_id} переведён в режим АФК")
                                try:
                                    bot.send_message(
                                        user_id,
                                        "🔔 Вы были переведены в режим АФК из-за неактивности (10 минут). "
                                        "Ваши номера скрыты. Нажмите 'Выключить АФК' в главном меню, чтобы вернуться.",
                                        parse_mode='HTML'
                                    )
                                except Exception as e:
                                    print(f"[ERROR] Не удалось отправить уведомление об АФК пользователю {user_id}: {e}")
                    except ValueError as e:
                        print(f"[ERROR] Неверный формат времени активности для пользователя {user_id}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            print(f"[ERROR] Ошибка в check_inactivity: {e}")
            time.sleep(60)

if __name__ == "__main__":
    init_db()
    timeout_thread = threading.Thread(target=check_number_timeout, daemon=True)
    timeout_thread.start()
    hold_time_thread = threading.Thread(target=check_number_hold_time, daemon=True)
    hold_time_thread.start()
    inactivity_thread = threading.Thread(target=check_inactivity, daemon=True)
    inactivity_thread.start()
    code_timeout_thread = threading.Thread(target=check_code_timeout, daemon=True)
    code_timeout_thread.start()
    run_bot()