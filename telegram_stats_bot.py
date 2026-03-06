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
OUTPUT_DIR = os.getenv('OUTPUT_DIR', 'output')



def parse_channel_input(raw: str) -> str:
    raw = raw.strip()
    if re.match(r'https?://t\.me/(joinchat/|\+)', raw):
        return raw
    m = re.match(r'https?://t\.me/([\w]+)', raw)
    if m:
        return m.group(1)
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


def ask_start_date() -> datetime:
    while True:
        raw = input("Дата начала: ").strip()
        try:
            dt = datetime.strptime(raw, '%Y-%m-%d')
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            print("⚠️  Неверный формат. Используйте ГГГГ-ММ-ДД (например 2025-01-01).")


def ask_end_date() -> datetime:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    while True:
        raw = input(f"Дата конца   [Enter = сегодня {today}]: ").strip()
        if not raw:
            return datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)
        try:
            dt = datetime.strptime(raw, '%Y-%m-%d')
            return dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            print("⚠️  Неверный формат. Используйте ГГГГ-ММ-ДД (например 2025-03-06).")


def ask_dates() -> tuple:
    print("\n📅 Укажите диапазон дат (формат: ГГГГ-ММ-ДД)\n")
    while True:
        start = ask_start_date()
        end = ask_end_date()
        if end >= start:
            return start, end
        print("⚠️  Дата конца должна быть не раньше даты начала.\n")


def ask_sort() -> tuple:
    print("\n📋 Сортировка постов:")
    print("   1 — по дате (по умолчанию, Enter)")
    print("   2 — по пересылкам (макс → мин)")
    print("   3 — по реакциям  (макс → мин)\n")
    options = {
        '':  ('date',      'по дате'),
        '1': ('date',      'по дате'),
        '2': ('forwards',  'по пересылкам'),
        '3': ('reactions', 'по реакциям'),
    }
    while True:
        raw = input("Выбор [1/2/3]: ").strip()
        if raw in options:
            return options[raw]
        print("⚠️  Введите 1, 2, 3 или нажмите Enter.")


async def resolve_channel(client, channel: str):
    try:
        return await client.get_entity(channel)
    except Exception as e:
        print(f"\n❌ Канал не найден: {e}")
        return None


async def get_channel_stats(client, channel: str, start_date: datetime, end_date: datetime):
    entity = await resolve_channel(client, channel)
    if entity is None:
        return None

    print(f"\n✅ Канал найден: {entity.title}")
    print(f"   ID: {entity.id}")
    print(f"   Период: {start_date.date()} — {end_date.date()}")
    print(f"\n📊 Начинаем сбор статистики...\n")

    posts_data = []

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
    channel = ask_channel()
    start_date, end_date = ask_dates()
    sort_key, sort_label = ask_sort()

    client = TelegramClient('session_name', API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("\n🔑 Авторизация успешна!")

    try:
        while True:
            stats = await get_channel_stats(client, channel, start_date, end_date)
            if stats is not None:
                break
            print("   Попробуйте ввести канал ещё раз.")
            channel = ask_channel()

        if sort_key == 'date':
            stats_sorted = sorted(stats, key=lambda x: x['date'])
        else:
            stats_sorted = sorted(stats, key=lambda x: x[sort_key], reverse=True)

        result = {
            'channel': channel,
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
            },
            'sort': sort_label,
            'total_posts': len(stats_sorted),
            'total_views': sum(p['views'] for p in stats_sorted),
            'total_forwards': sum(p['forwards'] for p in stats_sorted),
            'total_reactions': sum(p['reactions'] for p in stats_sorted),
            'posts': stats_sorted,
        }

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        filename = os.path.join(
            OUTPUT_DIR,
            f'telegram_stats_{channel}_'
            f'{start_date.strftime("%Y%m%d")}_{end_date.strftime("%Y%m%d")}.json'
        )

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"\n✅ Статистика сохранена: {filename}")
        print(f"🔃 Сортировка:       {sort_label}")
        print(f"📊 Всего постов:     {result['total_posts']}")
        print(f"👁  Всего просмотров: {result['total_views']}")
        print(f"↪️  Всего пересылок: {result['total_forwards']}")
        print(f"❤️  Всего реакций:   {result['total_reactions']}")

        if stats_sorted:
            top_label = "последних" if sort_key == "date" else "топ"
            print(f"\n🔥 5 {top_label} постов ({sort_label}):")
            for i, post in enumerate(stats_sorted[:5], 1):
                print(f"  {i}. Пересылок: {post['forwards']}, Реакций: {post['reactions']}, Просмотров: {post['views']}")
                print(f"     Текст: {post['text'][:60]}...")
                print(f"     Дата:  {post['date']}\n")
        else:
            print("\n⚠️  За указанный период постов не найдено.")

    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
