import json
import os

def sort_posts_interactive(input_file):
    """
    Интерактивная сортировка постов
    """
    # Загружаем JSON файл
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    posts = data.get('posts', [])
    
    if not posts:
        print("⚠️ В файле нет постов!")
        return
    
    print(f"📊 Загружено постов: {len(posts)}\n")
    
    # Вопрос 1: По чему сортировать
    print("1️⃣ По чему сортировать?")
    print("   1 - По количеству пересылок")
    print("   2 - По количеству реакций")
    sort_choice = input("Выберите (1 или 2): ").strip()
    
    if sort_choice == '1':
        sort_key = 'forwards'
        sort_name = 'пересылкам'
    elif sort_choice == '2':
        sort_key = 'reactions'
        sort_name = 'реакциям'
    else:
        print("❌ Неверный выбор!")
        return
    
    # Вопрос 2: Направление сортировки
    print(f"\n2️⃣ Как сортировать?")
    print("   1 - От максимума к минимуму (сначала самые популярные)")
    print("   2 - От минимума к максимуму (сначала самые непопулярные)")
    order_choice = input("Выберите (1 или 2): ").strip()
    
    if order_choice == '1':
        reverse = True
        order_name = 'от_макс_к_мин'
    elif order_choice == '2':
        reverse = False
        order_name = 'от_мин_к_макс'
    else:
        print("❌ Неверный выбор!")
        return
    
    # Вопрос 3: Сколько постов взять
    print(f"\n3️⃣ Сколько постов оставить?")
    top_n = input(f"Введите число (максимум {len(posts)}): ").strip()
    
    try:
        top_n = int(top_n)
        if top_n <= 0 or top_n > len(posts):
            print(f"❌ Число должно быть от 1 до {len(posts)}")
            return
    except ValueError:
        print("❌ Введите корректное число!")
        return
    
    # Сортируем
    sorted_posts = sorted(posts, key=lambda x: x[sort_key], reverse=reverse)
    top_posts = sorted_posts[:top_n]
    
    # Создаем результат
    result = {
        'channel': data.get('channel'),
        'period': data.get('period'),
        'filter': f'TOP {top_n} по {sort_name} ({order_name})',
        'total_posts': len(top_posts),
        'total_views': sum(p['views'] for p in top_posts),
        'total_forwards': sum(p['forwards'] for p in top_posts),
        'total_reactions': sum(p['reactions'] for p in top_posts),
        'posts': top_posts
    }
    
    # Формируем имя файла
    base_name = os.path.splitext(input_file)[0]
    output_file = f"{base_name}_{sort_key}_top{top_n}_{order_name}.json"
    
    # Сохраняем
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Сохранено {len(top_posts)} постов в файл:")
    print(f"   {output_file}")
    print(f"\n📊 Статистика:")
    print(f"   👁 Всего просмотров: {result['total_views']}")
    print(f"   ↪️ Всего пересылок: {result['total_forwards']}")
    print(f"   ❤️ Всего реакций: {result['total_reactions']}")
    
    # Показываем топ-5
    print(f"\n🔥 ТОП-5 из выбранных (по {sort_name}):")
    for i, post in enumerate(top_posts[:5], 1):
        print(f"\n{i}. {sort_key.capitalize()}: {post[sort_key]}, Просмотров: {post['views']}")
        print(f"   Текст: {post['text'][:70]}...")
        if post.get('link'):
            print(f"   Ссылка: {post['link']}")


if __name__ == '__main__':
    print("=" * 60)
    print("📈 СОРТИРОВКА ПОСТОВ ПО СТАТИСТИКЕ")
    print("=" * 60)
    
    # Получаем путь к файлу
    input_file = input("\n📁 Введите путь к JSON файлу: ").strip()
    
    if not os.path.exists(input_file):
        print(f"❌ Файл не найден: {input_file}")
    else:
        print()
        sort_posts_interactive(input_file)
