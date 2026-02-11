from telethon import TelegramClient
from datetime import datetime, timezone
import json
import asyncio
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Получение данных из .env
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
PHONE = os.getenv('PHONE')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')

# Парсинг дат с учетом временной зоны UTC
START_DATE = datetime.strptime(os.getenv('START_DATE'), '%Y-%m-%d').replace(tzinfo=timezone.utc)
END_DATE = datetime.strptime(os.getenv('END_DATE'), '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


async def get_channel_stats(client, channel, start_date, end_date):
    """
    Получает статистику постов из канала за указанный период
    """
    posts_data = []
    
    entity = await client.get_entity(channel)
    print(f"Загрузка постов из {entity.title}...")
    print(f"ID канала: {entity.id}")
    print(f"Период: {start_date.date()} - {end_date.date()}")
    
    print(f"\n📊 Начинаем сбор статистики...\n")
    
    async for message in client.iter_messages(entity):
        # Проверяем дату с учетом timezone
        if message.date < start_date:
            # Если дошли до постов старше начальной даты, останавливаемся
            break
        
        if message.date > end_date:
            continue
            
        if not message.text and not message.message:
            continue
        
        # Получаем количество реакций
        reactions_count = 0
        if message.reactions:
            reactions_count = sum(
                reaction.count for reaction in message.reactions.results
            )
        
        post_info = {
            'message_id': message.id,
            'date': message.date.strftime('%Y-%m-%d %H:%M:%S'),
            'text': message.text or message.message or '[Медиа без текста]',
            'views': message.views or 0,
            'forwards': message.forwards or 0,
            'reactions': reactions_count,
            'link': f'https://t.me/{entity.username}/{message.id}' if entity.username else None
        }
        
        posts_data.append(post_info)
        
        if len(posts_data) % 10 == 0:
            print(f"Обработано постов: {len(posts_data)}")
            await asyncio.sleep(1)  # Небольшая пауза для безопасности
    
    return posts_data


async def main():
    """
    Основная функция
    """
    client = TelegramClient('session_name', API_ID, API_HASH)
    
    await client.start(phone=PHONE)
    print("✅ Авторизация успешна!")
    
    try:
        stats = await get_channel_stats(
            client, 
            CHANNEL_USERNAME,
            START_DATE,
            END_DATE
        )
        
        stats_sorted = sorted(
            stats, 
            key=lambda x: x['forwards'], 
            reverse=True
        )
        
        result = {
            'channel': CHANNEL_USERNAME,
            'period': {
                'start': START_DATE.strftime('%Y-%m-%d'),
                'end': END_DATE.strftime('%Y-%m-%d')
            },
            'total_posts': len(stats_sorted),
            'total_views': sum(p['views'] for p in stats_sorted),
            'total_forwards': sum(p['forwards'] for p in stats_sorted),
            'total_reactions': sum(p['reactions'] for p in stats_sorted),
            'posts': stats_sorted
        }
        
        filename = f'telegram_stats_{START_DATE.strftime("%Y%m%d")}_{END_DATE.strftime("%Y%m%d")}.json'
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        print(f"\n✅ Статистика сохранена в файл: {filename}")
        print(f"📊 Всего постов: {len(stats_sorted)}")
        print(f"👁 Всего просмотров: {result['total_views']}")
        print(f"↪️ Всего пересылок: {result['total_forwards']}")
        print(f"❤️ Всего реакций: {result['total_reactions']}")
        
        if stats_sorted:
            print("\n🔥 ТОП-5 постов по пересылкам:")
            for i, post in enumerate(stats_sorted[:5], 1):
                print(f"{i}. Пересылок: {post['forwards']}, Просмотров: {post['views']}")
                print(f"   Текст: {post['text'][:50]}...")
                print(f"   Дата: {post['date']}\n")
        else:
            print("\n⚠️ За указанный период постов не найдено")
        
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
