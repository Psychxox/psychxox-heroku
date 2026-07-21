# modules/aimoderator2.py
# -*- coding: utf-8 -*-
"""
AI Moderator Module для Userbot
Версия 7.8: Глобальный лог-канал для всех чатов
"""

import re
import os
import time
import logging
import asyncio
import io
import random
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

try:
    MODULE_DIR = os.path.dirname(__file__)
except NameError:
    MODULE_DIR = os.getcwd()

DB_PATH = os.path.join(MODULE_DIR, "AIModeratrDB.db")

try:
    import aiosqlite
except ImportError:
    logger.warning("⚠️ aiosqlite не найден, попытка установки...")
    import subprocess, sys
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "aiosqlite"], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import aiosqlite
        logger.info("✅ aiosqlite установлен")
    except:
        logger.error("❌ Не удалось установить aiosqlite")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    logger.warning("⚠️ Pillow не найден, попытка установки...")
    import subprocess, sys
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from PIL import Image, ImageDraw, ImageFont
        logger.info("✅ Pillow установлен")
    except:
        logger.error("❌ Не удалось установить Pillow")

from telethon import events, Button

try:
    from telethon.tl.types import (
        ChannelParticipantAdmin,
        ChannelParticipantCreator,
        ChannelParticipantsAdmins
    )
except ImportError:
    ChannelParticipantAdmin = None
    ChannelParticipantCreator = None
    ChannelParticipantsAdmins = None

try:
    from telethon.tl.functions.channels import GetParticipantRequest
except ImportError:
    GetParticipantRequest = None

try:
    from telethon.errors import FloodWaitError
except ImportError:
    FloodWaitError = Exception

from .. import loader


@loader.tds
class AIModeratorMod(loader.Module):
    """AI Moderator - Модуль автоматической модерации чатов."""
    
    strings = {
        "name": "AIModerator",
        "author": "YourName",
        "version": "7.8"
    }
    
    def __init__(self):
        self._initialized = False
        self._db_conn = None
        self._bot_username = None
        self.active_punishments = {}
        self._admin_cache = {}
        self._admin_cache_time = {}
        self._cache_duration = 300
        self._tasks = []
        self._global_log_channel = None  # Кэш глобального лог-канала
        
    async def client_ready(self, client, db):
        """Инициализация модуля."""
        self.client = client
        
        try:
            await self._migrate_database()
            
            self._db_conn = await aiosqlite.connect(DB_PATH)
            logger.info(f"✅ Подключение к БД создано: {DB_PATH}")
            
            await self._db_conn.execute("PRAGMA journal_mode=WAL")
            await self._create_tables()
            
            # Загружаем глобальный лог-канал при старте
            self._global_log_channel = await self._get_global_log_channel()
            
            self._initialized = True
            
            me = await self.client.get_me()
            self._bot_username = me.username or str(me.id)
            
            logger.info(f"✅ AIModerator v{self.strings['version']} инициализирован")
            logger.info(f"📨 Глобальный лог-канал: {self._global_log_channel or 'не установлен'}")
            
            task1 = asyncio.ensure_future(self._check_expired_punishments())
            task2 = asyncio.ensure_future(self._auto_stats_sender())
            self._tasks = [task1, task2]
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации: {e}", exc_info=True)
            self._initialized = False
    
    async def _migrate_database(self):
        """Миграция старой БД на новую структуру."""
        if not os.path.exists(DB_PATH):
            logger.info("📦 БД не существует, миграция не нужна")
            return
        
        try:
            old_conn = await aiosqlite.connect(DB_PATH)
            
            # Проверяем структуру таблицы log_channels
            cursor = await old_conn.execute("PRAGMA table_info(log_channels)")
            columns = await cursor.fetchall()
            column_names = [c[1] for c in columns]
            
            # Если старая структура (chat_id вместо source_chat_id) - просто удаляем,
            # так как теперь используем global_log_channel
            if "chat_id" in column_names and "source_chat_id" not in column_names:
                logger.warning("⚠️ Обнаружена старая структура log_channels, удаляю...")
                await old_conn.execute("DROP TABLE IF EXISTS log_channels")
                await old_conn.commit()
                logger.info("✅ Старая таблица log_channels удалена")
            
            await old_conn.close()
            
        except Exception as e:
            logger.error(f"❌ Ошибка миграции: {e}")
    
    async def _create_tables(self):
        """Создание таблиц в БД."""
        if not self._db_conn:
            return
        
        tables = [
            """CREATE TABLE IF NOT EXISTS forbidden_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL,
                duration INTEGER DEFAULT 0,
                rating INTEGER DEFAULT 0,
                hits INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS active_chats (
                chat_id INTEGER PRIMARY KEY,
                enabled BOOLEAN DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # НОВАЯ ТАБЛИЦА: глобальный лог-канал (один для всех чатов)
            """CREATE TABLE IF NOT EXISTS global_log_channel (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                log_channel_id INTEGER NOT NULL,
                set_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS violation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                trigger_word TEXT,
                action TEXT,
                duration INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_violations INTEGER DEFAULT 0,
                total_bans INTEGER DEFAULT 0,
                total_mutes INTEGER DEFAULT 0,
                total_kicks INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS active_punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                action TEXT,
                until_timestamp INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        ]
        
        for table in tables:
            try:
                await self._db_conn.execute(table)
            except Exception as e:
                logger.error(f"❌ Ошибка создания таблицы: {e}")
        
        await self._db_conn.commit()
        logger.info("✅ Таблицы БД созданы/проверены")
    
    # ============ МЕТОДЫ РАБОТЫ С БД ============
    
    async def _db_execute(self, query, params=None):
        if not self._db_conn:
            return None
        try:
            if params:
                return await self._db_conn.execute(query, params)
            return await self._db_conn.execute(query)
        except Exception as e:
            logger.error(f"❌ Ошибка SQL: {e}")
            return None
    
    async def _db_fetchone(self, query, params=None):
        cursor = await self._db_execute(query, params)
        if cursor:
            try:
                return await cursor.fetchone()
            except:
                pass
        return None
    
    async def _db_fetchall(self, query, params=None):
        cursor = await self._db_execute(query, params)
        if cursor:
            try:
                return await cursor.fetchall()
            except:
                pass
        return []
    
    async def _db_commit(self):
        if self._db_conn:
            try:
                await self._db_commit.commit()
            except:
                pass
    
    # ============ ГЛОБАЛЬНЫЙ ЛОГ-КАНАЛ ============
    
    async def _get_global_log_channel(self) -> Optional[int]:
        """Получить ID глобального лог-канала."""
        if not self._db_conn:
            return None
        result = await self._db_fetchone(
            "SELECT log_channel_id FROM global_log_channel WHERE id = 1"
        )
        channel_id = result[0] if result else None
        logger.info(f"🔍 Глобальный лог-канал: {channel_id or 'НЕ УСТАНОВЛЕН'}")
        return channel_id
    
    async def _set_global_log_channel(self, channel_id: int):
        """Установить глобальный лог-канал."""
        if not self._db_conn:
            return
        await self._db_execute("DELETE FROM global_log_channel")
        await self._db_execute(
            "INSERT INTO global_log_channel (id, log_channel_id) VALUES (1, ?)",
            (channel_id,)
        )
        await self._db_commit()
        self._global_log_channel = channel_id
        logger.info(f"✅ Глобальный лог-канал установлен: {channel_id}")
    
    async def _clear_global_log_channel(self):
        """Очистить глобальный лог-канал."""
        if not self._db_conn:
            return
        await self._db_execute("DELETE FROM global_log_channel")
        await self._db_commit()
        self._global_log_channel = None
        logger.info("✅ Глобальный лог-канал очищен")
    
    # ============ ПРОВЕРКА АДМИНИСТРАТОРА ============
    
    async def _is_user_admin(self, chat_id: int, user_id: int) -> bool:
        cache_key = f"{chat_id}_{user_id}"
        current_time = time.time()
        
        if cache_key in self._admin_cache:
            if current_time - self._admin_cache_time.get(cache_key, 0) < self._cache_duration:
                return self._admin_cache[cache_key]
        
        try:
            if GetParticipantRequest:
                try:
                    result = await self.client(GetParticipantRequest(
                        channel=chat_id, participant=user_id
                    ))
                    is_admin = False
                    if ChannelParticipantAdmin and ChannelParticipantCreator:
                        if isinstance(result.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                            is_admin = True
                    if not is_admin and hasattr(result.participant, 'admin_rights'):
                        if result.participant.admin_rights:
                            is_admin = True
                    self._admin_cache[cache_key] = is_admin
                    self._admin_cache_time[cache_key] = current_time
                    return is_admin
                except:
                    pass
            
            try:
                if ChannelParticipantsAdmins:
                    admins = await self.client.get_participants(chat_id, filter=ChannelParticipantsAdmins)
                    for admin in admins:
                        if admin.id == user_id:
                            self._admin_cache[cache_key] = True
                            self._admin_cache_time[cache_key] = current_time
                            return True
            except:
                pass
            
            return False
        except:
            return False
    
    # ============ БИЗНЕС-ЛОГИКА ============
    
    async def _is_chat_active(self, chat_id: int) -> bool:
        if not self._db_conn:
            return False
        result = await self._db_fetchone(
            "SELECT 1 FROM active_chats WHERE chat_id = ? AND enabled = 1",
            (chat_id,)
        )
        return result is not None
    
    async def _get_forbidden_words(self) -> List[Dict[str, Any]]:
        if not self._db_conn:
            return []
        rows = await self._db_fetchall(
            "SELECT word, action, duration, rating, hits FROM forbidden_words"
        )
        return [
            {"word": r[0], "action": r[1], "duration": r[2], "rating": r[3], "hits": r[4]}
            for r in rows
        ]
    
    async def _is_whitelisted(self, user_id: int) -> bool:
        if not self._db_conn:
            return False
        result = await self._db_fetchone(
            "SELECT 1 FROM whitelist WHERE user_id = ?",
            (user_id,)
        )
        return result is not None
    
    async def _save_punishment(self, chat_id: int, user_id: int, action: str, duration: int):
        if duration <= 0 or not self._db_conn:
            return
        until_timestamp = int(time.time()) + duration
        await self._db_execute(
            "INSERT OR REPLACE INTO active_punishments (chat_id, user_id, action, until_timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, action, until_timestamp)
        )
        await self._db_commit()
        if chat_id not in self.active_punishments:
            self.active_punishments[chat_id] = {}
        self.active_punishments[chat_id][user_id] = {"until": until_timestamp, "action": action}
    
    async def _remove_punishment(self, chat_id: int, user_id: int):
        if not self._db_conn:
            return
        await self._db_execute(
            "DELETE FROM active_punishments WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        await self._db_commit()
        if chat_id in self.active_punishments:
            self.active_punishments[chat_id].pop(user_id, None)
    
    async def _log_violation(self, chat_id: int, user_id: int, username: str, 
                             trigger_word: str, action: str, duration: int):
        if not self._db_conn:
            return
        await self._db_execute(
            "INSERT INTO violation_logs (chat_id, user_id, username, trigger_word, action, duration) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, username, trigger_word, action, duration)
        )
        today = datetime.now().strftime("%Y-%m-%d")
        field_map = {"бан": "total_bans", "мут": "total_mutes", "кик": "total_kicks"}
        field = field_map.get(action.lower(), "total_violations")
        await self._db_execute(
            f"""INSERT INTO daily_stats (date, total_violations, total_bans, total_mutes, total_kicks)
            VALUES (?, 1, 0, 0, 0)
            ON CONFLICT(date) DO UPDATE SET
                total_violations = total_violations + 1,
                {field} = {field} + 1""",
            (today,)
        )
        await self._db_execute(
            "UPDATE forbidden_words SET hits = hits + 1 WHERE word = ?",
            (trigger_word,)
        )
        await self._db_commit()
    
    # ============ ФОНОВЫЕ ЗАДАЧИ ============
    
    async def _check_expired_punishments(self):
        while self._initialized and self._db_conn:
            try:
                current_time = int(time.time())
                for chat_id in list(self.active_punishments.keys()):
                    for user_id in list(self.active_punishments[chat_id].keys()):
                        punishment = self.active_punishments[chat_id][user_id]
                        if current_time >= punishment["until"]:
                            try:
                                if punishment["action"] == "мут":
                                    await self.client.edit_permissions(chat_id, user_id, send_messages=True)
                                elif punishment["action"] == "бан":
                                    await self.client.edit_permissions(chat_id, user_id, view_messages=True, send_messages=True)
                            except:
                                pass
                            await self._remove_punishment(chat_id, user_id)
                
                rows = await self._db_fetchall(
                    "SELECT id, chat_id, user_id, action, until_timestamp FROM active_punishments"
                )
                for row in rows:
                    if current_time >= row[4]:
                        try:
                            if row[3] == "мут":
                                await self.client.edit_permissions(row[1], row[2], send_messages=True)
                            elif row[3] == "бан":
                                await self.client.edit_permissions(row[1], row[2], view_messages=True, send_messages=True)
                        except:
                            pass
                        await self._db_execute("DELETE FROM active_punishments WHERE id = ?", (row[0],))
                        await self._db_commit()
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._initialized:
                    logger.error(f"❌ Ошибка проверки наказаний: {e}")
                await asyncio.sleep(5)
    
    async def _auto_stats_sender(self):
        while self._initialized and self._db_conn:
            try:
                now = datetime.now()
                next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                wait_seconds = (next_run - now).total_seconds()
                await asyncio.sleep(min(wait_seconds, 3600))
                
                if not self._db_conn:
                    continue
                
                # Отправляем статистику в глобальный лог-канал
                global_log = await self._get_global_log_channel()
                if global_log:
                    # Берем первый активный чат для генерации статистики
                    active_chat = await self._db_fetchone(
                        "SELECT chat_id FROM active_chats WHERE enabled = 1 LIMIT 1"
                    )
                    if active_chat:
                        img_data = await self._generate_stats_image(active_chat[0])
                        if img_data:
                            await self._send_as_bot(
                                global_log,
                                io.BytesIO(img_data),
                                "📊 **ЕЖЕДНЕВНАЯ СТАТИСТИКА**\n\nАвтоматический отчет за 24 часа"
                            )
                            today = datetime.now().strftime("%Y-%m-%d")
                            await self._db_execute("DELETE FROM daily_stats WHERE date = ?", (today,))
                            await self._db_commit()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._initialized:
                    logger.error(f"❌ Ошибка в авто-отправке: {e}")
                await asyncio.sleep(60)
    
    # ============ ОСНОВНОЙ WATCHER ============
    
    @loader.watcher(only_messages=True, no_commands=True, only_groups=True)
    async def watcher(self, message):
        """Обработчик всех входящих сообщений."""
        if not self._initialized or not self._db_conn:
            return
        
        try:
            chat_id = message.chat_id
            user_id = message.sender_id
            
            if not user_id:
                return
            if await self._is_user_admin(chat_id, user_id):
                return
            if not await self._is_chat_active(chat_id):
                return
            if await self._is_whitelisted(user_id):
                return
            
            message_text = message.text or message.message or ""
            if not message_text:
                return
            
            forbidden_words = await self._get_forbidden_words()
            if not forbidden_words:
                return
            
            triggered_word = None
            trigger_action = None
            trigger_duration = 0
            
            for word_info in forbidden_words:
                word = word_info["word"]
                pattern = rf"\b{re.escape(word)}\b"
                if re.search(pattern, message_text, re.IGNORECASE):
                    if random.random() * 100 < 80:
                        triggered_word = word
                        trigger_action = word_info["action"]
                        trigger_duration = word_info["duration"]
                        break
            
            if not triggered_word:
                return
            
            try:
                await message.delete()
            except:
                pass
            
            action_success = False
            action_type = trigger_action.lower()
            
            try:
                if action_type == "кик":
                    await self.client.kick_participant(chat_id, user_id)
                    action_success = True
                elif action_type == "мут":
                    if trigger_duration > 0:
                        await self._save_punishment(chat_id, user_id, "мут", trigger_duration)
                    await self.client.edit_permissions(chat_id, user_id, send_messages=False)
                    action_success = True
                elif action_type == "бан":
                    if trigger_duration > 0:
                        await self._save_punishment(chat_id, user_id, "бан", trigger_duration)
                    await self.client.edit_permissions(chat_id, user_id, view_messages=False, send_messages=False)
                    action_success = True
            except FloodWaitError as e:
                logger.warning(f"⏳ FloodWait: {e.seconds}с")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"❌ Ошибка применения наказания: {e}")
            
            if action_success:
                username = None
                try:
                    sender = await message.get_sender()
                    if sender and hasattr(sender, 'username'):
                        username = sender.username
                except:
                    pass
                
                await self._log_violation(
                    chat_id=chat_id, user_id=user_id,
                    username=username or str(user_id),
                    trigger_word=triggered_word,
                    action=trigger_action, duration=trigger_duration
                )
                
                # ===== ВАЖНО: Используем ГЛОБАЛЬНЫЙ лог-канал =====
                global_log = self._global_log_channel  # Используем кэш
                
                if global_log:
                    logger.info(f"📤 Отправка лога в глобальный лог-канал {global_log}")
                    await self._send_log_with_buttons(
                        target_chat=global_log,
                        user_id=user_id, username=username,
                        source_chat_id=chat_id,
                        chat_title=getattr(message.chat, 'title', str(chat_id)),
                        message_text=message_text,
                        trigger_word=triggered_word,
                        action=trigger_action, duration=trigger_duration
                    )
                else:
                    logger.info(f"📤 Глобальный лог-канал не установлен, отправка в Избранное")
                    await self._send_log_with_buttons(
                        target_chat="me",
                        user_id=user_id, username=username,
                        source_chat_id=chat_id,
                        chat_title=getattr(message.chat, 'title', str(chat_id)),
                        message_text=message_text,
                        trigger_word=triggered_word,
                        action=trigger_action, duration=trigger_duration
                    )
                
        except Exception as e:
            logger.error(f"❌ Ошибка в watcher: {e}")
    
    # ============ ОТПРАВКА СООБЩЕНИЙ ============
    
    async def _send_log_with_buttons(self, target_chat, user_id: int, 
                                      username: str, source_chat_id: int, chat_title: str,
                                      message_text: str, trigger_word: str,
                                      action: str, duration: int):
        """Отправка лога с кнопками."""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if duration == 0:
                duration_text = "Навсегда"
            elif duration < 60:
                duration_text = f"{duration} сек"
            elif duration < 3600:
                duration_text = f"{duration//60} мин"
            elif duration < 86400:
                duration_text = f"{duration//3600} ч"
            else:
                duration_text = f"{duration//86400} дн"
            
            log_text = (
                f"🚨 <b>НАРУШЕНИЕ ОБНАРУЖЕНО</b> 🚨\n\n"
                f"👤 <b>Нарушитель:</b>\n"
                f"   • ID: <code>{user_id}</code>\n"
                f"   • Username: @{username or 'не указан'}\n\n"
                f"💬 <b>Чат:</b>\n"
                f"   • ID: <code>{source_chat_id}</code>\n"
                f"   • Название: {chat_title}\n\n"
                f"📝 <b>Текст:</b>\n"
                f"   <code>{message_text[:300]}</code>\n\n"
                f"⚠️ <b>Слово:</b> <code>{trigger_word}</code>\n"
                f"⚡ <b>Действие:</b> {action.upper()}\n"
                f"⏱ <b>Длительность:</b> {duration_text}\n\n"
                f"🕐 <b>Время:</b> {now}\n\n"
                f"<b>Оцените срабатывание:</b>"
            )
            
            buttons = [
                [
                    Button.inline("✅ Класс", f"rate_good_{trigger_word}"),
                    Button.inline("❌ Ужасно", f"rate_bad_{trigger_word}")
                ]
            ]
            
            logger.info(f"📨 Отправка сообщения в {target_chat}")
            await self.client.send_message(
                target_chat, 
                log_text, 
                parse_mode="html", 
                buttons=buttons
            )
            logger.info(f"✅ Лог успешно отправлен в {target_chat}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки лога в {target_chat}: {e}")
    
    async def _send_as_bot(self, chat_id, file=None, text=None, buttons=None, parse_mode="html"):
        """Отправка сообщения."""
        try:
            if file:
                await self.client.send_file(chat_id, file, caption=text or "", parse_mode=parse_mode, buttons=buttons)
            else:
                await self.client.send_message(chat_id, text or "", parse_mode=parse_mode, buttons=buttons)
            logger.info(f"✅ Отправлено в {chat_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки в {chat_id}: {e}")
    
    # ============ ГЕНЕРАЦИЯ СТАТИСТИКИ ============
    
    async def _generate_stats_image(self, chat_id: int) -> Optional[bytes]:
        """Генерация изображения статистики."""
        try:
            words = await self._get_forbidden_words()
            total_triggers = len(words)
            total_hits = sum(w["hits"] for w in words)
            
            today = datetime.now().strftime("%Y-%m-%d")
            
            today_result = await self._db_fetchone(
                "SELECT total_violations, total_bans, total_mutes, total_kicks FROM daily_stats WHERE date = ?",
                (today,)
            )
            
            today_stats = {
                "total": today_result[0] if today_result else 0,
                "bans": today_result[1] if today_result else 0,
                "mutes": today_result[2] if today_result else 0,
                "kicks": today_result[3] if today_result else 0
            }
            
            total_result = await self._db_fetchone(
                "SELECT COUNT(*), SUM(CASE WHEN action = 'бан' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN action = 'мут' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN action = 'кик' THEN 1 ELSE 0 END) FROM violation_logs"
            )
            
            width, height = 900, 600
            image = Image.new('RGB', (width, height), color='#0a0a0a')
            draw = ImageDraw.Draw(image)
            
            try:
                font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
                font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
                font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
                font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
            except:
                font_title = ImageFont.load_default()
                font_text = ImageFont.load_default()
                font_small = ImageFont.load_default()
                font_header = ImageFont.load_default()
            
            colors = ['#00ff41', '#ff00ff', '#00ffff', '#ff6600', '#ff0040']
            
            for i in range(height):
                color_value = int(10 + (i / height) * 20)
                draw.line([(0, i), (width, i)], fill=(color_value, 0, color_value))
            
            for i in range(5):
                offset = i * 3
                draw.rectangle([offset, offset, width-offset, height-offset], outline=colors[i % len(colors)], width=1)
            
            title_text = "⚡ AI MODERATOR STATS ⚡"
            title_bbox = draw.textbbox((0, 0), title_text, font=font_title)
            title_width = title_bbox[2] - title_bbox[0]
            draw.text(((width - title_width) // 2, 20), title_text, fill=colors[0], font=font_title)
            
            draw.line([(50, 70), (width-50, 70)], fill=colors[2], width=2)
            
            y_pos = 100
            stats_info = [
                (f"📊 Всего триггеров: {total_triggers}", colors[0]),
                (f"🎯 Всего срабатываний: {total_hits}", colors[1]),
                ("", None),
                ("📅 За сегодня:", colors[2]),
                (f"   • Нарушений: {today_stats['total']}", colors[3]),
                (f"   • Банов: {today_stats['bans']}", colors[0]),
                (f"   • Мутов: {today_stats['mutes']}", colors[1]),
                (f"   • Киков: {today_stats['kicks']}", colors[4]),
                ("", None),
                ("📈 За все время:", colors[2]),
                (f"   • Нарушений: {total_result[0] if total_result else 0}", colors[3]),
                (f"   • Банов: {total_result[1] if total_result else 0}", colors[0]),
                (f"   • Мутов: {total_result[2] if total_result else 0}", colors[1]),
                (f"   • Киков: {total_result[3] if total_result else 0}", colors[4])
            ]
            
            for line, color in stats_info:
                if not line:
                    y_pos += 10
                    continue
                font = font_header if line.startswith("📅") or line.startswith("📈") else font_text
                draw.text((50, y_pos), line, fill=color or colors[0], font=font)
                y_pos += 30
            
            y_pos += 10
            if words:
                draw.line([(50, y_pos), (width-50, y_pos)], fill=colors[2], width=1)
                y_pos += 20
                draw.text((50, y_pos), "🏆 ТОП-3 ТРИГГЕРА:", fill=colors[2], font=font_header)
                y_pos += 35
                
                sorted_words = sorted(words, key=lambda x: x["hits"], reverse=True)[:3]
                for i, w in enumerate(sorted_words, 1):
                    rating_text = "👍" if w["rating"] > 0 else "👎" if w["rating"] < 0 else "⚖️"
                    line_text = f"{i}. {w['word']} - {w['hits']} раз(а) | {w['action'].upper()} | {w['rating']} {rating_text}"
                    draw.text((70, y_pos), line_text, fill=colors[i % len(colors)], font=font_small)
                    y_pos += 28
            
            img_bytes = io.BytesIO()
            image.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            return img_bytes.getvalue()
            
        except Exception as e:
            logger.error(f"❌ Ошибка генерации статистики: {e}")
            return None
    
    # ============ КОМАНДЫ ============
    
    @loader.command(alias="modon")
    async def modon(self, message):
        """Включить работу AIModerator в данном чате"""
        chat_id = message.chat_id
        await self._db_execute(
            "INSERT OR REPLACE INTO active_chats (chat_id, enabled) VALUES (?, 1)",
            (chat_id,)
        )
        await self._db_commit()
        await message.edit(f"<b>✅ Модерация включена</b>\nID чата: <code>{chat_id}</code>", parse_mode="html")
    
    @loader.command(alias="modoff")
    async def modoff(self, message):
        """Отключить работу AIModerator в данном чате"""
        chat_id = message.chat_id
        await self._db_execute("UPDATE active_chats SET enabled = 0 WHERE chat_id = ?", (chat_id,))
        await self._db_commit()
        await message.edit("<b>✅ Модерация выключена</b>", parse_mode="html")
    
    @loader.command(alias="modadd")
    async def modadd(self, message):
        """Добавить запрещённые слово/фразу"""
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.edit(
                "<b>Использование:</b>\n<code>.modadd слово | действие [время]</code>\n"
                "<b>Действия:</b> кик, мут, бан\n<b>Время:</b> в секундах (0 = навсегда)",
                parse_mode="html"
            )
            return
        try:
            full_args = args[1].strip()
            if " | " not in full_args:
                await message.edit("<b>❌ Используйте разделитель ' | '</b>", parse_mode="html")
                return
            word_part, action_part = full_args.split(" | ", 1)
            word = word_part.strip().lower()
            action_parts = action_part.strip().split()
            if not action_parts:
                await message.edit("<b>❌ Укажите действие</b>", parse_mode="html")
                return
            action = action_parts[0].lower()
            duration = 0
            if action not in ["кик", "мут", "бан"]:
                await message.edit("<b>❌ Доступные действия: кик, мут, бан</b>", parse_mode="html")
                return
            if action in ["мут", "бан"] and len(action_parts) > 1:
                try:
                    duration = int(action_parts[1])
                except ValueError:
                    await message.edit("<b>❌ Время должно быть числом</b>", parse_mode="html")
                    return
            await self._db_execute(
                "INSERT OR REPLACE INTO forbidden_words (word, action, duration) VALUES (?, ?, ?)",
                (word, action, duration)
            )
            await self._db_commit()
            duration_text = "Навсегда" if duration == 0 else f"{duration} сек"
            await message.edit(
                f"<b>✅ Добавлено:</b> <code>{word}</code>\n"
                f"<b>Действие:</b> {action.upper()}\n<b>Длительность:</b> {duration_text}",
                parse_mode="html"
            )
        except Exception as e:
            await message.edit(f"<b>❌ Ошибка: {e}</b>", parse_mode="html")
    
    @loader.command(alias="moddel")
    async def moddel(self, message):
        """Удалить запрещённые слово/фразу"""
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.edit("<b>❌ Использование:</b> <code>.moddel [слово]</code>", parse_mode="html")
            return
        word = args[1].strip().lower()
        await self._db_execute("DELETE FROM forbidden_words WHERE word = ?", (word,))
        await self._db_commit()
        await message.edit(f"<b>✅ Удалено:</b> <code>{word}</code>", parse_mode="html")
    
    @loader.command(alias="modlist")
    async def modlist(self, message):
        """Список запрещённых слов/фраз"""
        words = await self._get_forbidden_words()
        if not words:
            await message.edit("<b>📋 Список пуст</b>", parse_mode="html")
            return
        word_list = []
        for w in words:
            duration_text = "Навсегда" if w["duration"] == 0 else f"{w['duration']}с"
            word_list.append(
                f"• <code>{w['word']}</code> | {w['action'].upper()} | "
                f"{duration_text} | Рейтинг: {w['rating']} | Срабатываний: {w['hits']}"
            )
        text = "\n".join(word_list)
        if len(text) > 3500:
            file = io.BytesIO("\n".join([
                f"{w['word']} | {w['action']} | {w['duration']} | {w['rating']} | {w['hits']}"
                for w in words
            ]).encode('utf-8'))
            file.name = "forbidden_words.txt"
            await message.delete()
            await self.client.send_file(message.chat_id, file, caption=f"<b>📋 Запрещённых слов: {len(words)}</b>", parse_mode="html")
        else:
            await message.edit(f"<b>📋 Запрещённые слова ({len(words)}):</b>\n\n{text}", parse_mode="html")
    
    @loader.command(alias="modwl")
    async def modwl(self, message):
        """Добавить пользователя в белый список"""
        user_id = None
        if message.is_reply:
            replied = await message.get_reply_message()
            user_id = replied.sender_id
        else:
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.edit("<b>❌ Использование:</b> <code>.modwl [reply/username/ID]</code>", parse_mode="html")
                return
            arg = args[1].strip()
            if arg.isdigit():
                user_id = int(arg)
            else:
                try:
                    entity = await self.client.get_entity(arg)
                    user_id = entity.id
                except:
                    await message.edit("<b>❌ Пользователь не найден</b>", parse_mode="html")
                    return
        if not user_id:
            await message.edit("<b>❌ Не удалось определить пользователя</b>", parse_mode="html")
            return
        await self._db_execute(
            "INSERT OR IGNORE INTO whitelist (user_id, added_by) VALUES (?, ?)",
            (user_id, message.sender_id)
        )
        await self._db_commit()
        await message.edit(f"<b>✅ Добавлен в белый список</b>\nID: <code>{user_id}</code>", parse_mode="html")
    
    @loader.command(alias="modwlrm")
    async def modwlrm(self, message):
        """Убрать пользователя из белого списка"""
        user_id = None
        if message.is_reply:
            replied = await message.get_reply_message()
            user_id = replied.sender_id
        else:
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.edit("<b>❌ Использование:</b> <code>.modwlrm [reply/username/ID]</code>", parse_mode="html")
                return
            arg = args[1].strip()
            if arg.isdigit():
                user_id = int(arg)
            else:
                try:
                    entity = await self.client.get_entity(arg)
                    user_id = entity.id
                except:
                    await message.edit("<b>❌ Пользователь не найден</b>", parse_mode="html")
                    return
        if not user_id:
            await message.edit("<b>❌ Не удалось определить пользователя</b>", parse_mode="html")
            return
        await self._db_execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
        await self._db_commit()
        await message.edit(f"<b>✅ Удален из белого списка</b>\nID: <code>{user_id}</code>", parse_mode="html")
    
    @loader.command(alias="setlog")
    async def setlog(self, message):
        """
        Установить ГЛОБАЛЬНЫЙ чат для логов.
        Все логи со ВСЕХ чатов будут отправляться в этот чат.
        Использование: .setlog [ID чата] (или .setlog для текущего чата)
        """
        args = message.text.split(maxsplit=1)
        
        log_channel_id = message.chat_id
        
        if len(args) > 1 and args[1].strip().lstrip("-").isdigit():
            log_channel_id = int(args[1].strip())
        
        await self._set_global_log_channel(log_channel_id)
        
        await message.edit(
            f"<b>✅ ГЛОБАЛЬНЫЙ чат для логов установлен!</b>\n\n"
            f"📨 <b>Все логи будут отправляться в:</b> <code>{log_channel_id}</code>\n\n"
            f"<i>Логи со всех чатов, где включена модерация, будут приходить сюда.</i>",
            parse_mode="html"
        )
        logger.info(f"✅ setlog: глобальный лог-канал = {log_channel_id}")
    
    @loader.command(alias="unsetlog")
    async def unsetlog(self, message):
        """
        Отвязать глобальный чат для логов.
        Логи будут приходить в Избранное.
        """
        await self._clear_global_log_channel()
        await message.edit(
            f"<b>✅ Глобальный лог-чат отвязан!</b>\n"
            f"<i>Логи будут отправляться в Избранное.</i>",
            parse_mode="html"
        )
    
    @loader.command(alias="modstatus")
    async def modstatus(self, message):
        """Проверка статуса AIModerator"""
        chat_id = message.chat_id
        is_active = await self._is_chat_active(chat_id)
        words = await self._get_forbidden_words()
        total_hits = sum(w["hits"] for w in words)
        
        # Получаем количество активных чатов
        active_chats = await self._db_fetchall(
            "SELECT COUNT(*) FROM active_chats WHERE enabled = 1"
        )
        active_count = active_chats[0][0] if active_chats else 0
        
        status = (
            f"<b>📊 Статус AI Moderator v{self.strings['version']}</b>\n\n"
            f"🔹 Модерация в этом чате: {'✅ Включена' if is_active else '❌ Выключена'}\n"
            f"🔹 Всего активных чатов: {active_count}\n"
            f"🔹 Запрещённых слов: {len(words)}\n"
            f"🔹 Всего срабатываний: {total_hits}\n"
            f"🔹 Глобальный лог-чат: "
        )
        
        if self._global_log_channel:
            status += f"<code>{self._global_log_channel}</code> ✅"
        else:
            status += "❌ Не установлен (логи в Избранное)"
        
        status += f"\n\n💡 ID текущего чата: <code>{chat_id}</code>"
        
        await message.edit(status, parse_mode="html")
    
    @loader.command(alias="stats")
    async def stats(self, message):
        """Ручная проверка статистики"""
        chat_id = message.chat_id
        await message.edit("<b>⏳ Генерация статистики...</b>", parse_mode="html")
        
        img_data = await self._generate_stats_image(chat_id)
        if img_data:
            # Отправляем в глобальный лог-канал или в Избранное
            target = self._global_log_channel if self._global_log_channel else "me"
            target_name = f"глобальный лог-чат {target}" if self._global_log_channel else "Избранное"
            
            await self._send_as_bot(
                target,
                file=io.BytesIO(img_data),
                text=f"📊 <b>СТАТИСТИКА МОДЕРАТОРА</b>\n\nРучной запрос из чата <code>{chat_id}</code>",
                parse_mode="html"
            )
            await message.edit(f"<b>✅ Статистика отправлена в {target_name}!</b>", parse_mode="html")
        else:
            await message.edit("<b>❌ Ошибка генерации статистики</b>", parse_mode="html")
    
    @loader.callback_handler(regex=r'rate_(good|bad)_(.+)')
    async def rate_callback(self, event):
        try:
            data = event.data.decode('utf-8')
            parts = data.split('_', 2)
            rating_type = parts[1]
            word = parts[2]
            
            rating_change = 1 if rating_type == 'good' else -1
            await self._db_execute(
                "UPDATE forbidden_words SET rating = rating + ? WHERE word = ?",
                (rating_change, word)
            )
            await self._db_commit()
            
            emoji = '✅' if rating_type == 'good' else '❌'
            await event.answer(f"{emoji} Оценка принята!", alert=False)
            await event.edit(
                text=event.message.raw_text + f"\n\n{emoji} Оценка: {'Класс' if rating_type == 'good' else 'Ужасно'}",
                buttons=None
            )
        except Exception as e:
            logger.error(f"❌ Ошибка оценки: {e}")
    
    async def on_unload(self):
        self._initialized = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._db_conn:
            try:
                await self._db_conn.close()
                logger.info("🔒 Подключение к БД закрыто")
            except:
                pass
        logger.info("🔒 AIModerator выгружен")