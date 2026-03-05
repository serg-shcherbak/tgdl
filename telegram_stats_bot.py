from telethon import TelegramClient
from datetime import datetime, timezone
import json
import asyncio
import os
import re
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
PHONE = os.getenv('PHONE')


def parse_channel_input(raw: str) -> str:
    """
    Принимает любой из форматов:
      - @channelname
      - channelname
      - https://t.me/channelname
      - https://t.me/+InviteHash  (приватный канал)
      - https://t.me/joinchat/InviteHash  (приватный канал, старый формат)
    Возвращает строку, готовую для передачи в client.get_entity().
    """
    raw = raw.strip()

    # Ссылки на приватные каналы — передаём как есть, Telethon умеет их резолвить
    if re.match(r'https?://t\.me/(joinchat/|\+)', raw):
        return raw

    # Обычная t.me ссылка — вытаскиваем юзернейм
    m = re.match(r'https?://t\.me/([\w]+)', raw)
    if m:
        return m.group(1)

    # @username или просто username
    return raw.lstrip('@')


def ask_channel() -> str:
    print("\n📡 Введите канал для анализа.")
    print("   Форматы: @channelname | channelname | https://t.me/channelname")
    print("            https://t.me/+InviteHash (закрытый канал)\n")
    while True:
        raw = input("Канал: ").strip()
        if raw:
            return parse_channel_input(raw)
        print("⚠️  Поле не может быть пустым, попробуйте снова.")


def ask_date(prompt: str, end_of_day: bool = False) -> datetime:
    while True:
        raw = input(prompt).strip()
        try:
            dt = datetime.strptime(raw, '%Y-%m-%d')
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            print("⚠️  Неверный формат. Используйте ГГГГ-ММ-ДД (например 2025-01-01).")


def ask_dates() -> tuple[datetime, datetime]:
    print("\n📅 Укажите диапазон дат (формат: ГГГГ-ММ-ДД)\n")
    while True:
        start = ask_date("Дата начала: ")
        end = ask_date("Дата конца:  ", end_of_day=True)
        if end >= start:
            return start, end
        print("⚠️  Дата конца должна быть не раньше даты начала.\n")


async def get_channel_stats(client, channel: str, start_date: datetime, end_date: datetime) -> list:
    posts_data = []

    entity = await client.get_entity(channel)
    print(f"\n✅ Канал найден: {entity.title}")
    print(f"   ID: {entity.id}")
    print(f"   Период: {start_date.date()} — {end_date.date()}")
    print(f"\n📊 Начинаем сбор статистики...\n")

    async for message in client.iter_messages(entity):
        if message.date < start_date:
            break

        if message.date > end_date:
            continue

        if not message.text and not message.message:
            continue

        reactions_count = 0
        if message.reactions:
            reactions_count = sum(r.count for r in message.reactions.results)

        post_info = {
            'message_id': message.id,
            'date': message.date.strftime('%Y-%m-%d %H:%M:%S'),
            'text': message.text or message.message or '[Медиа без текста]',
            'views': message.views or 0,
            'forwards': message.forwards or 0,
            'reactions': reactions_count,
            'link': (
                f'https://t.me/{entity.username}/{message.id}'
                if getattr(entity, 'username', None) else None
            ),
        }
        posts_data.append(post_info)

        if len(posts_data) % 10 == 0:
            print(f"   Обработано постов: {len(posts_data)}")
        await asyncio.sleep(0.05)

    return posts_data


async def main():
    # --- Интерактивный ввод ---
    channel = ask_channel()
    start_date, end_date = ask_dates()

    # --- Подключение ---
    client = TelegramClient('session_name', API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("\n🔑 Авторизация успешна!")

    try:
        stats = await get_channel_stats(client, channel, start_date, end_date)

        stats_sorted = sorted(stats, key=lambda x: x['forwards'], reverse=True)

        result = {
            'channel': channel,
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
            },
            'total_posts': len(stats_sorted),
            'total_views': sum(p['views'] for p in stats_sorted),
            'total_forwards': sum(p['forwards'] for p in stats_sorted),
            'total_reactions': sum(p['reactions'] for p in stats_sorted),
            'posts': stats_sorted,
        }

        filename = (
            f'telegram_stats_{channel}_'
            f'{start_date.strftime("%Y%m%d")}_{end_date.strftime("%Y%m%d")}.json'
        )

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"\n✅ Статистика сохранена: {filename}")
        print(f"📊 Всего постов:     {result['total_posts']}")
        print(f"👁  Всего просмотров: {result['total_views']}")
        print(f"↪️  Всего пересылок: {result['total_forwards']}")
        print(f"❤️  Всего реакций:   {result['total_reactions']}")

        if stats_sorted:
            print("\n🔥 ТОП-5 постов по пересылкам:")
            for i, post in enumerate(stats_sorted[:5], 1):
                print(f"  {i}. Пересылок: {post['forwards']}, Просмотров: {post['views']}")
                print(f"     Текст: {post['text'][:60]}...")
                print(f"     Дата:  {post['date']}\n")
        else:
            print("\n⚠️  За указанный период постов не найдено.")

    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
