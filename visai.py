import os
import sqlite3
import threading
import time
import feedparser
import random
from datetime import datetime
from pytz import timezone
from flask import Flask, request, abort, render_template_string, url_for

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

app = Flask(__name__)

# ==========================================
# 設定・定数
# ==========================================

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://visai.onrender.com")
BOOTH_SUPPORT_URL = "https://visai.booth.pm/items/7763380"

# 🔧 PostgreSQL接続情報（Renderの永続DB用）
DATABASE_URL = os.environ.get("DATABASE_URL")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OPENAI_API_KEY]):
    print("🚨 必要な環境変数が設定されていません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
web_hook_handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_API_KEY)

# SQLite用のパス（PostgreSQLが使えない場合のフォールバック）
DB_PATH = os.path.join(os.path.dirname(__file__), "user_settings.db")

JST = timezone('Asia/Tokyo')

RSS_URL = {
    "トップ": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "社会": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "国際": "https://news.yahoo.co.jp/rss/topics/world.xml",
    "経済": "https://news.yahoo.co.jp/rss/topics/business.xml",
    "スポーツ": "https://news.yahoo.co.jp/rss/topics/sports.xml",
    "エンタメ": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
    "IT": "https://news.yahoo.co.jp/rss/topics/it.xml",
}

# ==========================================
# データベース接続管理
# ==========================================
def get_db_connection():
    """PostgreSQLまたはSQLiteへの接続を返す"""
    if DATABASE_URL:
        # PostgreSQLを使用（本番環境）
        import psycopg2
        from urllib.parse import urlparse
        
        result = urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            dbname=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port
        )
        return conn, 'postgres'
    else:
        # SQLiteを使用（開発環境）
        conn = sqlite3.connect(DB_PATH)
        return conn, 'sqlite'

# ==========================================
# データベース関連
# ==========================================
def init_db():
    """データベースの初期化"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        
        if db_type == 'postgres':
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    delivery_time TEXT DEFAULT '08:00',
                    genre TEXT DEFAULT 'トップ',
                    delivery_count INTEGER DEFAULT 0,
                    support_message_shown INTEGER DEFAULT 0
                )
            ''')
        else:
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    delivery_time TEXT DEFAULT '08:00',
                    genre TEXT DEFAULT 'トップ',
                    delivery_count INTEGER DEFAULT 0,
                    support_message_shown INTEGER DEFAULT 0
                )
            ''')
        
        conn.commit()
        conn.close()
        print(f"✅ Database initialized ({db_type})")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

def get_user_settings(user_id):
    """ユーザー設定を取得"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT delivery_time, genre, delivery_count, support_message_shown FROM users WHERE user_id = %s' if db_type == 'postgres' else 'SELECT delivery_time, genre, delivery_count, support_message_shown FROM users WHERE user_id = ?', (user_id,))
        res = c.fetchone()
        
        if res is None:
            if db_type == 'postgres':
                c.execute('INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) VALUES (%s, %s, %s, %s, %s)', 
                         (user_id, '08:00', 'トップ', 0, 0))
            else:
                c.execute('INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) VALUES (?, ?, ?, ?, ?)', 
                         (user_id, '08:00', 'トップ', 0, 0))
            conn.commit()
            res = ('08:00', 'トップ', 0, 0)
        
        conn.close()
        return {
            "time": res[0], 
            "genre": res[1],
            "delivery_count": res[2],
            "support_message_shown": res[3]
        }
    except Exception as e:
        print(f"❌ get_user_settings error for user {user_id}: {e}")
        return {"time": "08:00", "genre": "トップ", "delivery_count": 0, "support_message_shown": 0}

def update_user_settings(user_id, delivery_time, genre):
    """ユーザー設定を更新"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        
        print(f"🔧 Updating settings for {user_id}: time={delivery_time}, genre={genre}, db_type={db_type}")
        
        if db_type == 'postgres':
            c.execute('''
                INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) 
                VALUES (%s, %s, %s, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET 
                    delivery_time=EXCLUDED.delivery_time, 
                    genre=EXCLUDED.genre
            ''', (user_id, delivery_time, genre))
        else:
            c.execute('''
                INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) 
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET 
                    delivery_time=excluded.delivery_time, 
                    genre=excluded.genre
            ''', (user_id, delivery_time, genre))
        
        conn.commit()
        
        # 確認のため更新後の値を取得
        placeholder = '%s' if db_type == 'postgres' else '?'
        c.execute(f'SELECT delivery_time, genre FROM users WHERE user_id = {placeholder}', (user_id,))
        result = c.fetchone()
        print(f"✅ Verified settings for {user_id}: {result}")
        
        conn.close()
    except Exception as e:
        print(f"❌ update_user_settings error for user {user_id}: {e}")
        import traceback
        traceback.print_exc()

def increment_delivery_count(user_id):
    """配信回数をインクリメント"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        
        placeholder = '%s' if db_type == 'postgres' else '?'
        c.execute(f'UPDATE users SET delivery_count = delivery_count + 1 WHERE user_id = {placeholder}', (user_id,))
        
        conn.commit()
        conn.close()
        print(f"✅ Incremented delivery count for {user_id}")
    except Exception as e:
        print(f"❌ increment_delivery_count error for user {user_id}: {e}")

def mark_support_message_shown(user_id):
    """応援メッセージ表示済みフラグを立てる"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        
        placeholder = '%s' if db_type == 'postgres' else '?'
        c.execute(f'UPDATE users SET support_message_shown = 1 WHERE user_id = {placeholder}', (user_id,))
        
        conn.commit()
        conn.close()
        print(f"✅ Marked support message as shown for {user_id}")
    except Exception as e:
        print(f"❌ mark_support_message_shown error for user {user_id}: {e}")

def get_users_by_time(target_time):
    """指定した時間に配信すべきユーザーリストを取得"""
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        
        placeholder = '%s' if db_type == 'postgres' else '?'
        c.execute(f'SELECT user_id, genre FROM users WHERE delivery_time = {placeholder}', (target_time,))
        users = c.fetchall()
        
        conn.close()
        return users
    except Exception as e:
        print(f"❌ get_users_by_time error for time {target_time}: {e}")
        return []

# ==========================================
# ニュース取得・AI深掘り分析・本文生成
# ==========================================
def get_random_news_and_related(category):
    """指定カテゴリーからランダムに1件のニュースと関連ニュース5件を取得"""
    if category not in RSS_URL:
        category = "トップ"
    
    feed = feedparser.parse(RSS_URL[category])
    
    if not feed.entries:
        return None, []
    
    # ランダムに1件選択
    main_article = random.choice(feed.entries[:10])
    
    # 関連ニュースとして残りの記事から5件取得（重複を除く）
    related_articles = [entry for entry in feed.entries if entry.link != main_article.link][:5]
    
    return main_article, related_articles

def generate_deep_dive_summary(article, category):
    """選択されたニュースを深掘り分析"""
    system_prompt = (
        "あなたは中立公正で信頼されるニュース解説アナリストです。"
        "1つのニュースを深く掘り下げ、多角的な視点から分析し、"
        "中学生でも理解しやすい文章で解説してください。"
    )
    
    user_prompt = f"""
以下のニュース記事について、深掘り分析を行ってください。

### 記事情報:
タイトル: {article.title}
ジャンル: {category}

### 出力形式（必ず以下の構成で記述してください）:

1. 【挨拶】
お疲れ様です！本日の{category}ニュースを深掘りしてお届けします📰

2. 【ニュースタイトル】
{article.title}

3. 【ニュース内容】
このニュースの背景や詳細な内容を3〜4文で丁寧に説明してください。

4. 【👍 評価している意見】
このニュースに対して肯定的・評価する立場からの意見を2〜3点挙げてください。
それぞれの意見について、なぜそう考えられるのか理由も含めて説明してください。

5. 【🤔 反対している意見】
このニュースに対して批判的・懸念を示す立場からの意見を2〜3点挙げてください。
それぞれの意見について、どのような問題点があるのか具体的に説明してください。

6. 【💡 まとめ】
このニュースについて、両方の視点を踏まえた上での簡潔なまとめを2〜3文で記述してください。

注意事項:
- "###"や"**"などは使わないでください（絵文字などは使っても大丈夫です。）
- 各セクションの間に空行を入れて読みやすくしてください
- 専門用語は必要に応じて簡単に説明してください
- 感情的にならず、客観的な分析を心がけてください
- 断定的な表現は避け、「〜と考えられます」「〜という意見があります」など柔らかい表現を使ってください
"""

    try:
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=550,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ OpenAI API Error: {e}")
        return "申し訳ありません。AIによる分析の生成に失敗しました。"

def create_news_content(user_id, category):
    """ニュースの本文を作成して返す（送信はしない・共通処理）"""
    try:
        # ランダムに1件のメインニュースと関連ニュース5件を取得
        main_article, related_articles = get_random_news_and_related(category)
        
        if not main_article:
            return "申し訳ありません。現在ニュースが取得できませんでした。"

        # メイン記事の深掘り分析を生成
        deep_dive_summary = generate_deep_dive_summary(main_article, category)

        # メイン記事のリンク
        main_link_text = f"\n\n🔗 詳細記事はこちら\n{main_article.link}"

        # 関連ニュースのリンク
        related_links_text = "\n\n━━━━━━━━━━━━━━━━\n📰 その他の関連ニュース\n"
        for i, entry in enumerate(related_articles, 1):
            related_links_text += f"\n{i}. {entry.title}\n{entry.link}\n"

        # 最終メッセージの組み立て
        final_message = f"{deep_dive_summary}{main_link_text}{related_links_text}"
        
        # 配信回数をインクリメント（見た回数としてカウント）
        increment_delivery_count(user_id)
        
        return final_message

    except Exception as e:
        print(f"❌ Error generating content: {e}")
        return "ニュースの生成中にエラーが発生しました。"

def push_news(user_id, category):
    """【スケジューラー用】指定ユーザーにニュースをプッシュ送信する（有料枠消費）"""
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"📤 [{timestamp}] Start pushing deep-dive news to {user_id} (Genre: {category})")
    
    try:
        # ニュース本文を作成
        content = create_news_content(user_id, category)
        
        # メッセージオブジェクトのリスト作成
        messages = [TextSendMessage(text=content)]
        
        # 応援メッセージが必要か確認
        settings = get_user_settings(user_id)
        if settings['delivery_count'] >= 6 and settings['support_message_shown'] == 0:
            support_message = (
                "いつもVisAIを使ってくれてありがとうございます！🙏\n\n"
                "このbotは中学生の個人開発で、サーバー代やAIの利用料を自腹で運営しています。\n\n"
                "もし応援してもいいかなと思ってもらえたら、100円の応援PDFをBoothに置いています。\n"
                "無理はしないでください🙏\n\n"
                f"↓応援はこちらから\n{BOOTH_SUPPORT_URL}"
            )
            messages.append(TextSendMessage(text=support_message))
            mark_support_message_shown(user_id)
            print(f"💝 [{timestamp}] Support message appended for {user_id}")

        # 送信（Push）
        line_bot_api.push_message(user_id, messages)
        print(f"✅ [{timestamp}] Successfully sent deep-dive news to {user_id}")
        
    except Exception as e:
        print(f"❌ [{timestamp}] Push Error for {user_id}: {e}")

# ==========================================
# スケジューラー（デバッグ強化版）
# ==========================================
def schedule_checker():
    """毎分00秒に正確に実行（デバッグ強化版）"""
    print("=" * 70)
    print("🚀 SCHEDULER THREAD STARTED")
    print("=" * 70)
    
    # 起動直後に全ユーザー設定を表示
    try:
        conn, db_type = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT user_id, delivery_time, genre FROM users')
        all_users = c.fetchall()
        conn.close()
        
        print(f"\n📋 DATABASE TYPE: {db_type}")
        print("📋 === ALL USER SETTINGS IN DATABASE ===")
        if all_users:
            for user_id, dtime, genre in all_users:
                print(f"   ✓ User: {user_id[:12]}..., Time: '{dtime}', Genre: {genre}")
        else:
            print("   ⚠️  NO USERS FOUND IN DATABASE!")
        print("=" * 70 + "\n")
    except Exception as e:
        print(f"❌ Failed to load users at startup: {e}")
        import traceback
        traceback.print_exc()
    
    # 起動時に次の分まで待機
    now = datetime.now(JST)
    wait_seconds = 60 - now.second - (now.microsecond / 1000000.0)
    if wait_seconds > 0:
        print(f"⏱️  Waiting {wait_seconds:.1f}s to sync with minute boundary...\n")
        time.sleep(wait_seconds)
    
    last_checked_minute = None
    check_count = 0
    
    while True:
        try:
            now_jst = datetime.now(JST)
            current_time_str = now_jst.strftime("%H:%M")
            current_minute_key = now_jst.strftime("%Y%m%d%H%M")
            timestamp = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            
            check_count += 1
            
            # 同じ分に複数回実行しないようにチェック
            if current_minute_key != last_checked_minute:
                last_checked_minute = current_minute_key
                
                print(f"\n{'='*70}")
                print(f"⏰ CHECK #{check_count} at {timestamp}")
                print(f"   🔍 Looking for users with delivery_time = '{current_time_str}'")
                
                # デバッグ: 実際のクエリとDB内容を確認
                try:
                    conn, db_type = get_db_connection()
                    c = conn.cursor()
                    
                    # 全ユーザーの時間を取得（デバッグ用）
                    c.execute('SELECT user_id, delivery_time FROM users')
                    all_times = c.fetchall()
                    print(f"   📊 All delivery times in DB:")
                    for uid, dt in all_times:
                        match = "✅ MATCH!" if dt == current_time_str else ""
                        print(f"      - {uid[:12]}...: '{dt}' {match}")
                    
                    # ターゲットユーザーを取得
                    placeholder = '%s' if db_type == 'postgres' else '?'
                    query = f'SELECT user_id, genre, delivery_time FROM users WHERE delivery_time = {placeholder}'
                    c.execute(query, (current_time_str,))
                    targets = c.fetchall()
                    conn.close()
                    
                    print(f"   📬 Query returned {len(targets)} matching user(s)")
                    
                    if targets:
                        print(f"   🎯 DELIVERING TO:")
                        for user_id, genre, dtime in targets:
                            print(f"      → User: {user_id[:12]}..., Genre: {genre}")
                            threading.Thread(target=push_news, args=(user_id, genre), daemon=True).start()
                        print(f"   ✅ Started {len(targets)} delivery thread(s)")
                    else:
                        print(f"   ℹ️  No deliveries scheduled for {current_time_str}")
                    
                except Exception as db_error:
                    print(f"   ❌ Database error during check: {db_error}")
                    import traceback
                    traceback.print_exc()
                
                print(f"{'='*70}")
            
            # 次のチェックまで1秒待機
            time.sleep(1)
            
        except Exception as e:
            error_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n❌ [{error_time}] SCHEDULER ERROR: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

# ==========================================
# Flask Webルート
# ==========================================
@app.route("/")
def health_check():
    return "OK"

@app.route("/settings", methods=['GET', 'POST'])
def settings():
    user_id = request.args.get('user_id')
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    
    if not user_id:
        print(f"⚠️ [{timestamp}] Settings page accessed without user_id")
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>エラー</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    padding: 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .container {
                    max-width: 400px;
                    background: white;
                    padding: 40px 30px;
                    border-radius: 16px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    text-align: center;
                }
                h2 {
                    color: #e74c3c;
                    margin-bottom: 15px;
                    font-size: 24px;
                }
                p {
                    color: #555;
                    font-size: 16px;
                    line-height: 1.6;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h2>⚠️ エラー</h2>
                <p>ユーザーIDが見つかりません。<br>LINEから再度アクセスしてください。</p>
            </div>
        </body>
        </html>
        """

    if request.method == 'POST':
        new_time = request.form.get('delivery_time')
        new_genre = request.form.get('genre')
        
        print(f"⚙️ [{timestamp}] Settings update requested by {user_id}: time={new_time}, genre={new_genre}")
        update_user_settings(user_id, new_time, new_genre)
        
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>設定完了</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    padding: 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    margin: 0;
                }
                .container {
                    max-width: 400px;
                    background: white;
                    padding: 50px 30px;
                    border-radius: 16px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    text-align: center;
                    animation: slideIn 0.4s ease-out;
                }
                @keyframes slideIn {
                    from {
                        opacity: 0;
                        transform: translateY(-20px);
                    }
                    to {
                        opacity: 1;
                        transform: translateY(0);
                    }
                }
                .success-icon {
                    width: 80px;
                    height: 80px;
                    background: #00B900;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    margin: 0 auto 25px;
                    font-size: 45px;
                }
                h2 {
                    color: #333;
                    margin-bottom: 20px;
                    font-size: 26px;
                    font-weight: 600;
                }
                p {
                    color: #666;
                    font-size: 18px;
                    line-height: 1.8;
                    margin: 12px 0;
                }
                .back-notice {
                    margin-top: 30px;
                    padding: 15px;
                    background: #f8f9fa;
                    border-radius: 8px;
                    color: #555;
                    font-size: 15px;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="success-icon">✓</div>
                <h2>設定を保存しました！</h2>
                <p>設定した時間にニュースが届きます。</p>
                <div class="back-notice">
                    LINEの画面に戻ってください
                </div>
            </div>
        </body>
        </html>
        """

    current_settings = get_user_settings(user_id)
    print(f"📖 [{timestamp}] Settings page accessed by {user_id}")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta charset="UTF-8">
        <title>ニュース配信設定 - VisAI</title>
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
                display: flex;
                align-items: center;
                justify-content: center;
            }}
            
            .container {{
                max-width: 420px;
                width: 100%;
                background: white;
                padding: 35px 30px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                animation: fadeIn 0.5s ease-out;
            }}
            
            @keyframes fadeIn {{
                from {{
                    opacity: 0;
                    transform: translateY(20px);
                }}
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            
            .header-icon {{
                font-size: 48px;
                margin-bottom: 10px;
            }}
            
            h2 {{
                color: #2c3e50;
                font-size: 24px;
                font-weight: 600;
                margin-bottom: 8px;
            }}
            
            .subtitle {{
                color: #7f8c8d;
                font-size: 14px;
            }}
            
            .form-group {{
                margin-bottom: 25px;
            }}
            
            label {{
                display: block;
                color: #2c3e50;
                font-weight: 600;
                font-size: 15px;
                margin-bottom: 10px;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            
            .label-icon {{
                font-size: 18px;
            }}
            
            input[type="time"],
            select {{
                width: 100%;
                padding: 14px 16px;
                font-size: 16px;
                border: 2px solid #e0e0e0;
                border-radius: 12px;
                background-color: #f8f9fa;
                transition: all 0.3s ease;
                font-family: inherit;
            }}
            
            input[type="time"]:focus,
            select:focus {{
                outline: none;
                border-color: #667eea;
                background-color: white;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }}
            
            select {{
                cursor: pointer;
                appearance: none;
                background-image: url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3e%3cpolyline points='6 9 12 15 18 9'%3e%3c/polyline%3e%3c/svg%3e");
                background-repeat: no-repeat;
                background-position: right 12px center;
                background-size: 20px;
                padding-right: 40px;
            }}
            
            select option {{
                padding: 10px;
            }}
            
            button {{
                width: 100%;
                padding: 16px;
                background: linear-gradient(135deg, #00B900 0%, #00a000 100%);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 17px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s ease;
                box-shadow: 0 4px 15px rgba(0, 185, 0, 0.3);
                margin-top: 10px;
            }}
            
            button:hover {{
                background: linear-gradient(135deg, #00a000 0%, #008f00 100%);
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(0, 185, 0, 0.4);
            }}
            
            button:active {{
                transform: translateY(0);
            }}
            
            .current-settings {{
                background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                padding: 15px;
                border-radius: 12px;
                margin-bottom: 25px;
                color: white;
                font-size: 14px;
                text-align: center;
            }}
            
            .current-settings strong {{
                font-weight: 600;
            }}
            
            .divider {{
                height: 1px;
                background: linear-gradient(to right, transparent, #e0e0e0, transparent);
                margin: 25px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-icon">⚙️</div>
                <h2>配信設定</h2>
                <p class="subtitle">お好みの時間とジャンルを設定できます</p>
            </div>
            
            <div class="current-settings">
                現在の設定: <strong>{current_settings['time']}</strong> に <strong>{current_settings['genre']}</strong>ニュース
            </div>
            
            <form method="POST">
                <div class="form-group">
                    <label>
                        <span class="label-icon">🕐</span>
                        配信時間
                    </label>
                    <input type="time" name="delivery_time" value="{current_settings['time']}" required>
                </div>
                
                <div class="divider"></div>
                
                <div class="form-group">
                    <label>
                        <span class="label-icon">📰</span>
                        ニュースジャンル
                    </label>
                    <select name="genre">
                        {''.join([f'<option value="{k}" {"selected" if k == current_settings["genre"] else ""}>{k}</option>' for k in RSS_URL.keys()])}
                    </select>
                </div>
                
                <button type="submit">💾 設定を保存する</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

# ==========================================
# LINE Webhook
# ==========================================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        web_hook_handler.handle(body, signature)
    except InvalidSignatureError:
        timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        print(f"❌ [{timestamp}] Invalid signature received")
        abort(400)

    return "OK"

@web_hook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """
    メッセージ受信時の処理
    【重要】「今すぐ」に対する処理をPushからReply（無料）に変更
    """
    user_id = event.source.user_id
    msg = event.message.text.strip()
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"💬 [{timestamp}] Message received from {user_id}: '{msg}'")

    if msg == "今すぐ":
        settings = get_user_settings(user_id)
        print(f"🚀 [{timestamp}] Immediate delivery requested by {user_id}")
        
        # ニュース本文の生成（Pushではなくテキスト生成だけを行う）
        # ※OpenAIの処理に時間がかかりすぎるとReplyToken（約30秒）が切れるリスクがありますが、
        #   無料化のためにここでは同期処理（待機）を行います。
        news_text = create_news_content(user_id, settings['genre'])
        
        # 返信用のメッセージリスト作成
        reply_messages = [TextSendMessage(text=news_text)]
        
        # 応援メッセージが必要な場合は2通目として追加（これもReplyに含めれば無料）
        if settings['delivery_count'] >= 6 and settings['support_message_shown'] == 0:
            support_message = (
                "いつもVisAIを使ってくれてありがとうございます！🙏\n\n"
                "このbotは中学生の個人開発で、サーバー代やAIの利用料を自腹で運営しています。\n\n"
                "もし応援してもいいかなと思ってもらえたら、100円の応援PDFをBoothに置いています。\n"
                "無理はしないでください🙏\n\n"
                f"↓応援はこちらから\n{BOOTH_SUPPORT_URL}"
            )
            reply_messages.append(TextSendMessage(text=support_message))
            mark_support_message_shown(user_id)
            print(f"💝 [{timestamp}] Support message appended to reply for {user_id}")
        
        # 無料のReplyMessageを使って送信
        line_bot_api.reply_message(event.reply_token, reply_messages)
        print(f"✅ [{timestamp}] Sent via Reply (Free) to {user_id}")
        return

    if msg == "設定":
        settings_url = f"{APP_PUBLIC_URL}/settings?user_id={user_id}"
        
        reply_text = (
            "⚙️ 設定\n"
            "以下のリンクから配達時間とジャンルを変更できます。\n\n"
            f"{settings_url}\n\n"
            "※リンクを知っている人は誰でも設定を変更できてしまうため、他人に教えないでください。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_text))
        print(f"⚙️ [{timestamp}] Settings link sent to {user_id}")
        return

    # メニュー表示（ここもReplyなので無料）
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("💡メニュー\n・「今すぐ」: 今すぐニュースを受信(無料)\n・「設定」: 時間やジャンルを変更")
    )
    print(f"ℹ️ [{timestamp}] Help menu sent to {user_id}")

# ==========================================
# アプリ起動時の初期化
# ==========================================
init_db()

scheduler_thread = threading.Thread(target=schedule_checker, daemon=True)
scheduler_thread.start()

startup_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
print(f"✅ [{startup_time}] VisAI LINE Bot started successfully (Deep-Dive & Free-Reply Mode)")

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=10000)