#!/usr/bin/env python3
"""
Скрипт выгрузки и резервного копирования содержимого чата "Маркетинг".
- Подключается через Telethon (MTProto).
- Автоматически находит чат "Маркетинг" (22 участника).
- Скачивает полную историю сообщений.
- Дописывает новые сообщения в .json файл без потери ранее созданных/сохраненных 
  (даже если сообщения были удалены из самого чата в Telegram).
- Включает всю метаинформацию (отправитель, текст, реакции, реплаи, альбомы, медиа, действия).
- Опционально скачивает медиафайлы через флаг --download-media.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl import types as tl_types
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeImageSize,
    DocumentAttributeVideo,
    MessageActionChatAddUser,
    MessageActionChatCreate,
    MessageActionChatDeleteUser,
    MessageActionChatEditTitle,
    MessageActionPinMessage,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
    ReactionCustomEmoji,
    ReactionEmoji,
    User,
)

# Загружаем переменные окружения (.env)
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
SESSION_NAME = os.getenv("SESSION_NAME", "session_name")


def check_auth_config():
    if not all([API_ID, API_HASH, PHONE]):
        print("❌ Ошибка: В файле .env отсутствуют параметры API_ID, API_HASH или PHONE.")
        sys.exit(1)


def isoformat_utc(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def serialize_user(user_entity) -> Optional[Dict[str, Any]]:
    if not user_entity:
        return None
    if isinstance(user_entity, User):
        return {
            "id": user_entity.id,
            "username": user_entity.username,
            "first_name": user_entity.first_name,
            "last_name": user_entity.last_name,
            "phone": getattr(user_entity, "phone", None),
            "is_bot": getattr(user_entity, "bot", False),
        }
    elif isinstance(user_entity, (Channel, Chat)):
        return {
            "id": user_entity.id,
            "title": getattr(user_entity, "title", None),
            "username": getattr(user_entity, "username", None),
            "type": type(user_entity).__name__,
        }
    return {"id": getattr(user_entity, "id", None), "name": str(user_entity)}


def parse_reactions(msg) -> List[Dict[str, Any]]:
    results = []
    if not hasattr(msg, "reactions") or not msg.reactions:
        return results

    if hasattr(msg.reactions, "results") and msg.reactions.results:
        for r in msg.reactions.results:
            emoji_str = None
            if isinstance(r.reaction, ReactionEmoji):
                emoji_str = r.reaction.emoticon
            elif isinstance(r.reaction, ReactionCustomEmoji):
                emoji_str = f"custom_{r.reaction.document_id}"
            else:
                emoji_str = str(r.reaction)

            results.append({"emoji": emoji_str, "count": r.count})
    return results


def parse_action(action_obj) -> Optional[Dict[str, Any]]:
    if not action_obj:
        return None

    action_type = type(action_obj).__name__
    info: Dict[str, Any] = {"type": action_type}

    if isinstance(action_obj, MessageActionChatAddUser):
        info["user_ids"] = list(action_obj.users)
    elif isinstance(action_obj, MessageActionChatDeleteUser):
        info["user_id"] = action_obj.user_id
    elif isinstance(action_obj, MessageActionChatEditTitle):
        info["title"] = action_obj.title
    elif isinstance(action_obj, MessageActionChatCreate):
        info["title"] = action_obj.title
        info["user_ids"] = list(action_obj.users)
    elif isinstance(action_obj, MessageActionPinMessage):
        info["pinned"] = True
    elif type(action_obj).__name__ in ("MessageActionTopicCreate", "MessageActionTopicEdit"):
        if hasattr(action_obj, "title"):
            info["title"] = action_obj.title

    return info


def parse_media_info(msg) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    """
    Возвращает (media_dict, media_obj_for_download).
    """
    if not msg.media:
        return None, None

    media = msg.media
    media_dict: Dict[str, Any] = {"type": type(media).__name__}

    if isinstance(media, MessageMediaPhoto) and isinstance(media.photo, tl_types.Photo):
        media_dict["type"] = "photo"
        media_dict["mime_type"] = "image/jpeg"
        media_dict["id"] = media.photo.id
        sizes = getattr(media.photo, "sizes", [])
        if sizes:
            last_size = sizes[-1]
            media_dict["width"] = getattr(last_size, "w", None)
            media_dict["height"] = getattr(last_size, "h", None)
            media_dict["file_size"] = getattr(last_size, "size", None)
        return media_dict, media.photo

    elif isinstance(media, MessageMediaDocument) and isinstance(media.document, tl_types.Document):
        doc = media.document
        media_dict["type"] = "document"
        media_dict["id"] = doc.id
        media_dict["mime_type"] = doc.mime_type
        media_dict["file_size"] = doc.size

        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                media_dict["file_name"] = attr.file_name
            elif isinstance(attr, DocumentAttributeImageSize):
                media_dict["width"] = attr.w
                media_dict["height"] = attr.h
            elif isinstance(attr, DocumentAttributeVideo):
                media_dict["duration"] = attr.duration
                media_dict["width"] = attr.w
                media_dict["height"] = attr.h
                media_dict["supports_streaming"] = getattr(attr, "supports_streaming", False)
                media_dict["round_message"] = bool(attr.round_message)
            elif isinstance(attr, DocumentAttributeAudio):
                media_dict["duration"] = attr.duration
                media_dict["title"] = getattr(attr, "title", None)
                media_dict["performer"] = getattr(attr, "performer", None)
            elif isinstance(attr, tl_types.DocumentAttributeSticker):
                media_dict["sticker_emoji"] = getattr(attr, "alt", None)

        attributes = {type(attr): attr for attr in doc.attributes}
        if tl_types.DocumentAttributeSticker in attributes:
            media_dict["type"] = "sticker"
        elif tl_types.DocumentAttributeAnimated in attributes or doc.mime_type == "image/gif":
            media_dict["type"] = "animation"
        elif DocumentAttributeVideo in attributes:
            media_dict["type"] = "video_note" if media_dict["round_message"] else "video"
        elif DocumentAttributeAudio in attributes:
            media_dict["type"] = "voice" if attributes[DocumentAttributeAudio].voice else "audio"
        elif DocumentAttributeImageSize in attributes or doc.mime_type.startswith("image/"):
            media_dict["type"] = "image"
        return media_dict, doc

    elif isinstance(media, MessageMediaPoll) and media.poll:
        poll = media.poll
        poll_info = {
            "id": poll.id,
            "question": poll.question.text if hasattr(poll.question, "text") else str(poll.question),
            "question_entities": [entity.to_dict() for entity in getattr(poll.question, "entities", [])],
            "closed": bool(poll.closed),
            "quiz": bool(poll.quiz),
            "multiple_choice": bool(poll.multiple_choice),
            "options": [
                {
                    "text": opt.text.text if hasattr(opt.text, "text") else str(opt.text),
                    "data": opt.option.hex(),
                    "text_entities": [entity.to_dict() for entity in getattr(opt.text, "entities", [])],
                }
                for opt in poll.answers
            ],
        }
        results = media.results
        votes = {item.option.hex(): item for item in (getattr(results, "results", None) or [])}
        poll_info["total_voters"] = getattr(results, "total_voters", None)
        poll_info["results_min"] = bool(results.min) if results is not None else None
        lines = [poll_info["question"]]
        for index, option in enumerate(poll_info["options"], 1):
            result = votes.get(option["data"])
            option["voters"] = getattr(result, "voters", None)
            option["correct"] = getattr(result, "correct", None)
            line = f'{index}. {option["text"]}'
            if option["voters"] is not None:
                line += f' (голоса: {option["voters"]})'
            if option["correct"]:
                line += ' [правильный ответ]'
            lines.append(line)
        if poll_info["total_voters"] is not None:
            lines.append(f'Всего проголосовало: {poll_info["total_voters"]}')
        for flag, label in (("quiz", "Викторина"), ("multiple_choice", "Можно выбрать несколько вариантов"), ("closed", "Опрос закрыт")):
            if poll_info[flag]:
                lines.append(label)
        if getattr(results, "solution", None) is not None:
            poll_info["solution"] = results.solution
            poll_info["solution_entities"] = [entity.to_dict() for entity in (results.solution_entities or [])]
            lines.append(f'Пояснение: {results.solution}')
        media_dict["type"] = "poll"
        media_dict["poll"] = poll_info
        media_dict["poll_text"] = '\n'.join(lines)
        return media_dict, None

    elif isinstance(media, (MessageMediaGeo, tl_types.MessageMediaGeoLive, tl_types.MessageMediaVenue)):
        media_dict["type"] = "location"
        return media_dict, None

    elif isinstance(media, tl_types.MessageMediaDice):
        media_dict.update(type="dice", value=media.value, emoticon=media.emoticon)
        return media_dict, None

    elif isinstance(media, (tl_types.MessageMediaGiveaway, tl_types.MessageMediaGiveawayResults)):
        media_dict["type"] = "giveaway_results" if isinstance(media, tl_types.MessageMediaGiveawayResults) else "giveaway"
        for field in ("channels", "quantity", "countries_iso2", "prize_description", "months", "stars",
                      "only_new_subscribers", "winners_are_visible", "winners_count", "unclaimed_count",
                      "winners", "channel_id", "launch_msg_id", "additional_peers_count", "refunded"):
            value = getattr(media, field, None)
            if value is not None:
                media_dict[field] = value
        media_dict["until_date"] = isoformat_utc(media.until_date)
        return media_dict, None

    elif isinstance(media, tl_types.MessageMediaStory):
        media_dict.update(type="story", story_id=media.id, peer=media.peer.to_dict())
        if getattr(media.story, "caption", None) is not None:
            media_dict["caption"] = media.story.caption
        return media_dict, None

    elif isinstance(media, tl_types.MessageMediaPaidMedia):
        media_dict.update(type="paid_media", stars_amount=media.stars_amount,
                          attachment_count=len(media.extended_media))
        return media_dict, None

    elif isinstance(media, MessageMediaContact):
        media_dict["type"] = "contact"
        media_dict["phone_number"] = media.phone_number
        media_dict["first_name"] = media.first_name
        media_dict["last_name"] = media.last_name
        media_dict["user_id"] = media.user_id
        return media_dict, None

    elif isinstance(media, MessageMediaWebPage):
        web = media.webpage
        media_dict["type"] = "web_page"
        media_dict["url"] = getattr(web, "url", None)
        media_dict["title"] = getattr(web, "title", None)
        media_dict["description"] = getattr(web, "description", None)
        return media_dict, None

    return media_dict, None


async def serialize_message(
    msg, client: TelegramClient, download_media: bool = False, media_dir: Optional[str] = None
) -> Dict[str, Any]:
    sender_dict = serialize_user(msg.sender) if hasattr(msg, "sender") else None

    # Обработка реплая
    reply_to_id = None
    reply_to_top_id = None
    if msg.reply_to:
        reply_to_id = getattr(msg.reply_to, "reply_to_msg_id", None)
        reply_to_top_id = getattr(msg.reply_to, "reply_to_top_id", None)

    # Обработка медиа
    media_info, media_obj = parse_media_info(msg)
    local_path = None

    if download_media and media_obj and media_dir:
        try:
            os.makedirs(media_dir, exist_ok=True)
            saved_path = await client.download_media(msg, file=media_dir)
            if saved_path:
                local_path = os.path.relpath(saved_path, start=os.path.dirname(media_dir) or ".")
        except Exception as e:
            print(f"⚠️ Ошибка при скачивании медиа для сообщения {msg.id}: {e}")

    if media_info and local_path:
        media_info["local_path"] = local_path

    msg_data = {
        "id": msg.id,
        "date": isoformat_utc(msg.date),
        "edit_date": isoformat_utc(msg.edit_date),
        "sender_id": msg.sender_id,
        "sender": sender_dict,
        "text": msg.message or "",
        "raw_text": msg.raw_text or "",
        "reply_to_msg_id": reply_to_id,
        "reply_to_top_id": reply_to_top_id,
        "grouped_id": msg.grouped_id,
        "pinned": getattr(msg, "pinned", False),
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "reactions": parse_reactions(msg),
        "media": media_info,
        "action": parse_action(msg.action) if hasattr(msg, "action") else None,
        "post_author": getattr(msg, "post_author", None),
    }

    return msg_data


async def find_marketing_chat(client: TelegramClient, explicit_id: Optional[int] = None):
    """
    Ищет чат 'Маркетинг' в диалогах (в приоритете 22 участника).
    """
    if explicit_id:
        try:
            entity = await client.get_entity(explicit_id)
            print(f"🎯 Использован указанный ID чата: {explicit_id} ({getattr(entity, 'title', 'No Title')})")
            return entity
        except Exception as e:
            print(f"⚠️ Не удалось найти чат по указанному ID {explicit_id}: {e}")

    print("🔍 Поиск чата 'Маркетинг' среди диалогов...")
    exact_match = None
    name_matches = []

    async for dialog in client.iter_dialogs():
        name = dialog.name or ""
        entity = dialog.entity
        count = getattr(entity, "participants_count", None)

        if "маркетинг" in name.lower():
            name_matches.append((dialog, count))
            if name.strip().lower() == "маркетинг" and count == 22:
                exact_match = dialog
                break
            elif count == 22 and "маркетинг" in name.lower():
                exact_match = dialog

    if exact_match:
        target = exact_match
        count = getattr(target.entity, "participants_count", None)
        print(f"✅ Точно найден целевой чат: '{target.name}' (ID: {target.id}, Участников: {count})")
        return target.entity

    if name_matches:
        best = name_matches[0][0]
        count = getattr(best.entity, "participants_count", None)
        print(f"✅ Найден чат по названию: '{best.name}' (ID: {best.id}, Участников: {count})")
        return best.entity

    raise RuntimeError("❌ Чат 'Маркетинг' не найден среди активных диалогов!")


def load_existing_backup(json_path: str) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    """
    Загружает ранее сохраненный бэкап .json (если он существует).
    Возвращает словарь {message_id: message_dict} и метаданные.
    """
    if not os.path.exists(json_path):
        return {}, {}

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        messages_list = data.get("messages", [])
        messages_map = {}
        for m in messages_list:
            if isinstance(m, dict) and "id" in m:
                messages_map[m["id"]] = m

        meta = data.get("chat_info", {})
        print(f"📂 Загружен существующий бэкап: {len(messages_map)} сообщений из '{json_path}'")
        return messages_map, meta
    except Exception as e:
        print(f"⚠️ Предупреждение: Не удалось прочитать существующий файл {json_path}: {e}")
        return {}, {}


def save_backup_atomic(json_path: str, data: Dict[str, Any]):
    """
    Атомарная запись в JSON (через временный файл).
    """
    target_dir = os.path.dirname(os.path.abspath(json_path))
    os.makedirs(target_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name

    os.replace(temp_name, json_path)


async def export_chat(
    output_file: str,
    chat_id: Optional[int] = None,
    download_media: bool = False,
    media_dir: str = "marketing_backup_media",
    limit: Optional[int] = None,
):
    check_auth_config()

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start(phone=PHONE)
    print("✅ Успешная авторизация в Telegram API!")

    try:
        target_entity = await find_marketing_chat(client, explicit_id=chat_id)
        chat_title = getattr(target_entity, "title", "Маркетинг")
        participants_count = getattr(target_entity, "participants_count", None)

        # 1. Загружаем ранее сохраненные сообщения (если есть)
        existing_messages, prev_meta = load_existing_backup(output_file)
        initial_count = len(existing_messages)

        # 2. Скачиваем текущую историю из Telegram
        print(f"\n📥 Скачивание сообщений из чата '{chat_title}'...")
        fetched_count = 0
        new_count = 0
        updated_count = 0

        async for msg in client.iter_messages(target_entity, limit=limit):
            fetched_count += 1
            msg_dict = await serialize_message(
                msg, client, download_media=download_media, media_dir=media_dir
            )

            if msg.id in existing_messages:
                # Обновляем сообщение актуальной метаинформацией
                existing_messages[msg.id] = msg_dict
                updated_count += 1
            else:
                existing_messages[msg.id] = msg_dict
                new_count += 1

            if fetched_count % 50 == 0:
                print(f"   ...обработано {fetched_count} сообщений из Telegram...")

        # 3. Сортируем все сообщения по ID (от самых старых к новым)
        sorted_messages = sorted(existing_messages.values(), key=lambda x: x["id"])

        backup_payload = {
            "chat_info": {
                "id": target_entity.id,
                "title": chat_title,
                "type": type(target_entity).__name__,
                "participants_count": participants_count,
                "last_backup_at": datetime.now(timezone.utc).isoformat(),
            },
            "total_messages": len(sorted_messages),
            "messages": sorted_messages,
        }

        # 4. Сохраняем в JSON
        save_backup_atomic(output_file, backup_payload)

        # 5. Автоматически генерируем сжатые версии для LLM
        try:
            from compact_marketing_chat import compress_for_llm
            base_dir = os.path.dirname(os.path.abspath(output_file))
            json_llm = os.path.join(base_dir, "marketing_chat_llm.json")
            txt_llm = os.path.join(base_dir, "marketing_chat_llm.txt")
            compress_for_llm(output_file, json_llm, txt_llm)
        except Exception as e:
            print(f"⚠️ Предупреждение: Не удалось сформировать компактную копию для LLM: {e}")

        print("\n" + "=" * 60)
        print("🎉 ВЫГРУЗКА И БЭКАП УСПЕШНО ЗАВЕРШЕНЫ!")
        print("=" * 60)
        print(f"📊 Итоги:")
        print(f"   • Было в предыдущем бэкапе: {initial_count}")
        print(f"   • Загружено из Telegram:   {fetched_count}")
        print(f"   • Добавлено новых:        {new_count}")
        print(f"   • Всего в итоговом .json:  {len(sorted_messages)}")
        print(f"💾 Файл полного бэкапа: {os.path.abspath(output_file)}")
        print(f"🤖 Файлы для работы с LLM: marketing_chat_llm.json & marketing_chat_llm.txt")
        print("=" * 60)

    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Выгрузка и инкрементальный бэкап чата 'Маркетинг' в JSON."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="marketing_chat_backup.json",
        help="Путь к итоговому .json файлу (по умолчанию: marketing_chat_backup.json)",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="Явный ID чата (если не нужно автоопределение)",
    )
    parser.add_argument(
        "--download-media",
        action="store_true",
        help="Скачивать медиафайлы (картинки, документы, голосовые) локально",
    )
    parser.add_argument(
        "--media-dir",
        default="marketing_backup_media",
        help="Папка для сохранения медиафайлов при включенном --download-media",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничение количества сообщений для выгрузки (по умолчанию: без ограничений)",
    )

    args = parser.parse_args()

    asyncio.run(
        export_chat(
            output_file=args.output,
            chat_id=args.chat_id,
            download_media=args.download_media,
            media_dir=args.media_dir,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    main()
