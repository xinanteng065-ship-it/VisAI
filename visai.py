import os
import sqlite3
import threading
import time
import feedparser
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
        
        if db_type == 'postgres':
            c.execute('''
                INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) 
                VALUES (%s, %s, %s, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET delivery_time=EXCLUDED.delivery_time, genre=EXCLUDED.genre
            ''', (user_id, delivery_time, genre))
        else:
            c.execute('''
                INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_message_shown) 
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET delivery_time=excluded.delivery_time, genre=excluded.genre
            ''', (user_id, delivery_time, genre))
        
        conn.commit()
        conn.close()
        print(f"✅ Updated settings for {user_id}: {delivery_time}, {genre}")
    except Exception as e:
        print(f"❌ update_user_settings error for user {user_id}: {e}")

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
# ニュース取得・AI要約ロジック
# ==========================================
def get_news_content(category):
    if category not in RSS_URL:
        category = "トップ"
    
    feed = feedparser.parse(RSS_URL[category])
    items = []
    for entry in feed.entries[:5]:
        items.append(f"・{entry.title} ({entry.link})")
    
    return "\n".join(items)

# ==========================================
# ニュース取得・AI要約ロジック
# ==========================================
def generate_ai_summary(news_text, category):
    """ニュースを多角的な視点で要約する"""
    system_prompt = (
        "あなたは中立公正かつ親しみやすいニュース解説アナリストです。"
        "複雑なニュースを「評価・批判・中立」の3つの視点から整理し、ユーザーが多角的に判断できるように助けます。"
    )
    
    user_prompt = f"""
    以下のニュースリスト（ジャンル：{category}）を読み、LINE配信用のダイジェストを作成してください。

    ### ニュース内容:
    {news_text}

    ### 指示事項:
    1. 冒頭に「お疲れ様です！本日の{category}ニュースを多角的にお届けします💡」という挨拶を入れてください。
    2. 各記事について、以下の形式を厳守して記述してください。
       【数字. ニュースのタイトル】
       ・内容解説: (短く要約)
       ・👍評価: (このニュースに対するポジティブな意見やメリット)
       ・🤔批判: (懸念点、反対意見、デメリット)
       ・⚖️中立: (客観的な事実や、今後の注視点)
    3. 各意見（評価・批判・中立）は、一般的な世論やニュースソースに基づいた推測を含めて構成してください。
    4. 全体の文字数は600〜800文字程度、絵文字を使い読みやすくしてください。
    5. 最後にそれぞれのニュースURLを提示してください。
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=800,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ OpenAI API Error: {e}")
        return "申し訳ありません。AIによる要約の生成に失敗しました。"

def push_news(user_id, category):
    """指定ユーザーにニュースを送信する処理"""
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"📤 [{timestamp}] Start pushing news to {user_id} (Genre: {category})")
    
    try:
        feed = feedparser.parse(RSS_URL.get(category, RSS_URL["トップ"]))
        if not feed.entries:
            print(f"⚠️ [{timestamp}] No news entries found for {user_id} in category {category}")
            return

        articles_for_ai = []
        for entry in feed.entries[:5]:
            articles_for_ai.append(f"タイトル: {entry.title}\nリンク: {entry.link}")
        input_text = "\n\n".join(articles_for_ai)

        summary_text = generate_ai_summary(input_text, category)

        links_text = "\n".join([f"🔗 {e.title[:15]}...\n{e.link}" for e in feed.entries[:5]])
        final_message = f"{summary_text}\n\n👇 気になる記事をチェック\n{links_text}"

        line_bot_api.push_message(user_id, TextSendMessage(text=final_message))
        print(f"✅ [{timestamp}] Successfully sent news to {user_id}")
        
        increment_delivery_count(user_id)
        
        settings = get_user_settings(user_id)
        
        if settings['delivery_count'] >= 6 and settings['support_message_shown'] == 0:
            support_message = (
                "いつもVisAIを使ってくれてありがとうございます！🙏\n\n"
                "このbotは学生の個人開発で、サーバー代やAIの利用料を自腹で運営しています。\n\n"
                "もし応援してもいいかなと思ってもらえたら、100円の応援PDFをBoothに置いています。\n"
                "無理はしないでください🙏\n\n"
                f"↓応援はこちらから\n{BOOTH_SUPPORT_URL}"
            )
            
            try:
                time.sleep(2)
                line_bot_api.push_message(user_id, TextSendMessage(text=support_message))
                mark_support_message_shown(user_id)
                print(f"💝 [{timestamp}] Support message sent to {user_id}")
            except Exception as e:
                print(f"❌ [{timestamp}] Failed to send support message to {user_id}: {e}")
        
    except Exception as e:
        print(f"❌ [{timestamp}] Push Error for {user_id}: {e}")

# ==========================================
# スケジューラー（改善版）
# ==========================================
def schedule_checker():
    """毎分00秒に正確に実行"""
    print("🚀 Scheduler thread started")
    
    # 起動時に次の分まで待機
    now = datetime.now(JST)
    wait_seconds = 60 - now.second
    print(f"⏱️ Waiting {wait_seconds}s to sync with minute boundary...")
    time.sleep(wait_seconds)
    
    last_checked_minute = None
    
    while True:
        try:
            now_jst = datetime.now(JST)
            current_time_str = now_jst.strftime("%H:%M")
            current_minute_key = now_jst.strftime("%Y%m%d%H%M")
            timestamp = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            
            # 同じ分に複数回実行しないようにチェック
            if current_minute_key == last_checked_minute:
                time.sleep(1)  # 短いスリープで次のループへ
                continue
            
            last_checked_minute = current_minute_key
            
            print(f"⏰ [{timestamp}] Checking scheduled deliveries for {current_time_str}...")
            
            targets = get_users_by_time(current_time_str)
            
            if targets:
                print(f"📬 [{timestamp}] Found {len(targets)} user(s) to deliver")
                for user_id, genre in targets:
                    print(f"   → User: {user_id}, Genre: {genre}")
                    threading.Thread(target=push_news, args=(user_id, genre), daemon=True).start()
            else:
                print(f"   No deliveries scheduled for {current_time_str}")
            
            # 次の分の00秒まで待機
            now = datetime.now(JST)
            wait_seconds = 60 - now.second
            time.sleep(wait_seconds)
            
        except Exception as e:
            error_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            print(f"❌ [{error_time}] Scheduler error: {e}")
            time.sleep(60)

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
        return "エラー: ユーザーIDが見つかりません。LINEから再度アクセスしてください。"

    if request.method == 'POST':
        new_time = request.form.get('delivery_time')
        new_genre = request.form.get('genre')
        
        print(f"⚙️ [{timestamp}] Settings update requested by {user_id}: time={new_time}, genre={new_genre}")
        update_user_settings(user_id, new_time, new_genre)
        
        return """
        <div style="text-align:center; padding: 20px; font-family: sans-serif;">
            <h2>✅ 設定を保存しました!</h2>
            <p>設定した時間にニュースが届きます。</p>
            <p>LINEの画面に戻ってください。</p>
        </div>
        """

    current_settings = get_user_settings(user_id)
    print(f"📖 [{timestamp}] Settings page accessed by {user_id}")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ニュース配信設定</title>
        <style>
            body {{ font-family: sans-serif; padding: 20px; background-color: #f0f0f0; }}
            .container {{ max-width: 400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            h2 {{ text-align: center; color: #333; }}
            label {{ display: block; margin-top: 15px; font-weight: bold; }}
            select, input[type="time"] {{ width: 100%; padding: 10px; margin-top: 5px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ width: 100%; padding: 12px; background-color: #00B900; color: white; border: none; border-radius: 4px; margin-top: 20px; font-size: 16px; cursor: pointer; }}
            button:hover {{ background-color: #009900; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>⚙️ 配信設定</h2>
            <form method="POST">
                <label>配信時間:</label>
                <input type="time" name="delivery_time" value="{current_settings['time']}" required>
                
                <label>ニュースジャンル:</label>
                <select name="genre">
                    {''.join([f'<option value="{k}" {"selected" if k == current_settings["genre"] else ""}>{k}</option>' for k in RSS_URL.keys()])}
                </select>
                
                <button type="submit">設定を保存</button>
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
    user_id = event.source.user_id
    msg = event.message.text.strip()
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"💬 [{timestamp}] Message received from {user_id}: '{msg}'")

    if msg == "今すぐ":
        settings = get_user_settings(user_id)
        print(f"🚀 [{timestamp}] Immediate delivery requested by {user_id}")
        threading.Thread(target=push_news, args=(user_id, settings['genre']), daemon=True).start()
        return

    if msg == "設定変更":
        settings_url = f"{APP_PUBLIC_URL}/settings?user_id={user_id}"
        
        reply_text = (
            "⚙️ 設定変更\n"
            "以下のリンクから配達時間とジャンルを変更できます。\n\n"
            f"{settings_url}\n\n"
            "※リンクを知っている人は誰でも設定を変更できてしまうため、他人に教えないでください。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_text))
        print(f"⚙️ [{timestamp}] Settings link sent to {user_id}")
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("💡メニュー\n・「今すぐ」: 今すぐニュースを受信\n・「設定変更」: 時間やジャンルを変更")
    )
    print(f"ℹ️ [{timestamp}] Help menu sent to {user_id}")

# ==========================================
# アプリ起動時の初期化
# ==========================================
init_db()

scheduler_thread = threading.Thread(target=schedule_checker, daemon=True)
scheduler_thread.start()

startup_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
print(f"✅ [{startup_time}] VisAI LINE Bot started successfully")

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=10000)