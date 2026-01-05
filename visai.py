import os
import sqlite3
import threading
import time
import feedparser
import random
from datetime import datetime
from pytz import timezone
from flask import Flask, request, abort, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

app = Flask(__name__)

# ==========================================
# 環境変数の読み込み
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://visai.onrender.com")
BOOTH_SUPPORT_URL = "https://visai.booth.pm/items/7763380"
LINE_BOT_ID = os.environ.get("LINE_BOT_ID", "@298qcfgk")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OPENAI_API_KEY]):
    raise ValueError("🚨 必要な環境変数が設定されていません")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
webhook_handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

JST = timezone('Asia/Tokyo')
DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

# ニュースカテゴリー
NEWS_CATEGORIES = {
    "トップ": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "社会": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "国際": "https://news.yahoo.co.jp/rss/topics/world.xml",
    "経済": "https://news.yahoo.co.jp/rss/topics/business.xml",
    "スポーツ": "https://news.yahoo.co.jp/rss/topics/sports.xml",
    "エンタメ": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
    "IT": "https://news.yahoo.co.jp/rss/topics/it.xml",
}

# ==========================================
# データベース接続
# ==========================================
def get_db():
    """SQLite接続を取得"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# データベース初期化
# ==========================================
def init_database():
    """テーブルを作成"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                delivery_time TEXT NOT NULL DEFAULT '08:00',
                genre TEXT NOT NULL DEFAULT 'トップ',
                delivery_count INTEGER DEFAULT 0,
                support_shown INTEGER DEFAULT 0,
                last_delivery_date TEXT
            )
        ''')
        
        # 既存テーブルに last_delivery_date カラムがない場合は追加
        try:
            cursor.execute("SELECT last_delivery_date FROM users LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE users ADD COLUMN last_delivery_date TEXT")
            print("Added last_delivery_date column")
        
        conn.commit()
        conn.close()
        print("✅ Database initialized")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

# ==========================================
# ユーザー設定の取得
# ==========================================
def get_user_settings(user_id):
    """ユーザー設定を取得"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT delivery_time, genre, delivery_count, support_shown, last_delivery_date FROM users WHERE user_id = ?',
            (user_id,)
        )
        row = cursor.fetchone()
        
        if not row:
            cursor.execute(
                'INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_shown) VALUES (?, ?, ?, ?, ?)',
                (user_id, '08:00', 'トップ', 0, 0)
            )
            conn.commit()
            conn.close()
            return {'time': '08:00', 'genre': 'トップ', 'delivery_count': 0, 'support_shown': 0, 'last_delivery_date': None}
        
        result = {
            'time': row['delivery_time'],
            'genre': row['genre'],
            'delivery_count': row['delivery_count'],
            'support_shown': row['support_shown'],
            'last_delivery_date': row['last_delivery_date']
        }
        
        conn.close()
        return result
        
    except Exception as e:
        print(f"❌ get_user_settings error: {e}")
        return {'time': '08:00', 'genre': 'トップ', 'delivery_count': 0, 'support_shown': 0, 'last_delivery_date': None}

# ==========================================
# ユーザー設定の更新
# ==========================================
def update_user_settings(user_id, delivery_time, genre):
    """配信時間とジャンルを更新"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO users (user_id, delivery_time, genre, delivery_count, support_shown)
            VALUES (?, ?, ?, 0, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                delivery_time = excluded.delivery_time,
                genre = excluded.genre
        ''', (user_id, delivery_time, genre))
        
        conn.commit()
        conn.close()
        print(f"✅ Updated settings: {user_id[:8]}... -> {delivery_time}, {genre}")
    except Exception as e:
        print(f"❌ update_user_settings error: {e}")

# ==========================================
# 配信回数のカウント
# ==========================================
def increment_delivery_count(user_id):
    """配信回数を1増やし、今日の日付を記録"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        today = datetime.now(JST).strftime("%Y-%m-%d")
        
        cursor.execute(
            'UPDATE users SET delivery_count = delivery_count + 1, last_delivery_date = ? WHERE user_id = ?',
            (today, user_id)
        )
        
        conn.commit()
        conn.close()
        print(f"✅ Incremented delivery count for {user_id[:8]}...")
    except Exception as e:
        print(f"❌ increment_delivery_count error: {e}")

# ==========================================
# 応援メッセージフラグ
# ==========================================
def mark_support_shown(user_id):
    """応援メッセージを表示済みにする"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET support_shown = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ mark_support_shown error: {e}")

# ==========================================
# 配信対象ユーザーを取得
# ==========================================
def get_users_for_delivery(target_time):
    """指定時刻に配信すべきユーザーを取得（今日まだ配信していない人のみ）"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        today = datetime.now(JST).strftime("%Y-%m-%d")
        
        # 今日の日付が last_delivery_date と異なる、または NULL のユーザーを取得
        cursor.execute('''
            SELECT user_id, genre FROM users 
            WHERE delivery_time = ? 
            AND (last_delivery_date IS NULL OR last_delivery_date != ?)
        ''', (target_time, today))
        
        users = cursor.fetchall()
        conn.close()
        
        return [(row['user_id'], row['genre']) for row in users]
        
    except Exception as e:
        print(f"❌ get_users_for_delivery error: {e}")
        return []

# ==========================================
# ニュース取得
# ==========================================
def fetch_news(category):
    """指定カテゴリーのニュースをランダムに1件＋関連5件取得"""
    url = NEWS_CATEGORIES.get(category, NEWS_CATEGORIES["トップ"])
    
    try:
        feed = feedparser.parse(url)
        
        if not feed.entries:
            return None, []
        
        main_article = random.choice(feed.entries[:10])
        related_articles = [e for e in feed.entries if e.link != main_article.link][:5]
        
        return main_article, related_articles
        
    except Exception as e:
        print(f"❌ RSS fetch error: {e}")
        return None, []

# ==========================================
# AI深掘り分析
# ==========================================
def analyze_news_with_ai(article, category):
    """AIでニュースを深掘り分析"""
    system_prompt = (
        "あなたは中立公正で信頼されるニュース解説アナリストです。"
        "1つのニュースを深く掘り下げ、多角的な視点から分析し、"
        "中学生でも理解しやすい文章で解説してください。"
    )
    
    user_prompt = f"""以下のニュース記事について、深掘り分析を行ってください。

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
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=600,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        print(f"❌ OpenAI API Error: {e}")
        return "申し訳ありません。AIによる分析の生成に失敗しました。"

# ==========================================
# ニュースメッセージ作成
# ==========================================
def create_news_message(user_id, category):
    """ニュース本文を作成"""
    try:
        main_article, related_articles = fetch_news(category)
        
        if not main_article:
            return "申し訳ありません。現在ニュースが取得できませんでした。"
        
        analysis = analyze_news_with_ai(main_article, category)
        message = f"{analysis}\n\n🔗 詳細記事はこちら\n{main_article.link}"
        
        if related_articles:
            message += "\n\n━━━━━━━━━━━━━━━━\n📰 その他の関連ニュース\n"
            for i, article in enumerate(related_articles, 1):
                message += f"\n{i}. {article.title}\n{article.link}\n"
        
        increment_delivery_count(user_id)
        
        return message
    except Exception as e:
        print(f"❌ create_news_message error: {e}")
        return "ニュースの生成中にエラーが発生しました。"

# ==========================================
# ニュース配信（Push送信）
# ==========================================
def send_news_to_user(user_id, category):
    """ユーザーにニュースをPush送信"""
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        print(f"📤 [{timestamp}] Sending news to {user_id[:8]}... (Genre: {category})")
        
        news_content = create_news_message(user_id, category)
        messages = [TextSendMessage(text=news_content)]
        
        settings = get_user_settings(user_id)
        if settings['delivery_count'] >= 6 and settings['support_shown'] == 0:
            support_message = (
                "いつもVisAIを使ってくれてありがとうございます！🙏\n\n"
                "このbotは中学生の個人開発で、サーバー代やAIの利用料を自腹で運営しています。\n\n"
                "もし応援してもいいかなと思ってもらえたら、100円の応援PDFをBoothに置いています。\n"
                "無理はしないでください🙏\n\n"
                f"↓応援はこちらから\n{BOOTH_SUPPORT_URL}"
            )
            messages.append(TextSendMessage(text=support_message))
            mark_support_shown(user_id)
            print(f"💝 [{timestamp}] Support message added")
        
        line_bot_api.push_message(user_id, messages)
        print(f"✅ [{timestamp}] Successfully sent to {user_id[:8]}...")
        
    except Exception as e:
        print(f"❌ [{timestamp}] Push error: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# スケジューラー（Thread方式）
# ==========================================
def schedule_checker():
    """毎分00秒に正確に実行するスケジューラー"""
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
                time.sleep(1)
                continue
            
            last_checked_minute = current_minute_key
            
            print(f"\n⏰ [{timestamp}] Checking scheduled deliveries for {current_time_str}...")
            
            targets = get_users_for_delivery(current_time_str)
            
            if targets:
                print(f"📬 [{timestamp}] Found {len(targets)} user(s) to deliver")
                for user_id, genre in targets:
                    print(f"   → User: {user_id[:8]}..., Genre: {genre}")
                    # 各配信を別スレッドで実行
                    threading.Thread(target=send_news_to_user, args=(user_id, genre), daemon=True).start()
            else:
                print(f"   ℹ️  No deliveries scheduled for {current_time_str}")
            
            # 次の分の00秒まで待機
            now = datetime.now(JST)
            wait_seconds = 60 - now.second
            time.sleep(wait_seconds)
            
        except Exception as e:
            error_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            print(f"❌ [{error_time}] Scheduler error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)

# ==========================================
# Flask Routes
# ==========================================
@app.route("/")
def index():
    """ヘルスチェック用エンドポイント"""
    return "VisAI Bot Running ✅"

@app.route("/settings", methods=['GET', 'POST'])
def settings():
    """設定画面"""
    try:
        user_id = request.args.get('user_id')
        
        if not user_id:
            return """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>エラー</title>
                <style>
                    body {
                        font-family: -apple-system, sans-serif;
                        background: linear-gradient(135deg, #667eea, #764ba2);
                        min-height: 100vh;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        padding: 20px;
                    }
                    .container {
                        background: white;
                        padding: 40px 30px;
                        border-radius: 16px;
                        box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                        text-align: center;
                        max-width: 400px;
                    }
                    h2 {
                        color: #e74c3c;
                        margin-bottom: 15px;
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
            """, 400
        
        if request.method == 'POST':
            new_time = request.form.get('delivery_time')
            new_genre = request.form.get('genre')
            
            update_user_settings(user_id, new_time, new_genre)
            
            return """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>設定完了</title>
                <style>
                    body {
                        font-family: -apple-system, sans-serif;
                        background: linear-gradient(135deg, #667eea, #764ba2);
                        min-height: 100vh;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        padding: 20px;
                    }
                    .container {
                        background: white;
                        padding: 50px 30px;
                        border-radius: 16px;
                        box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                        text-align: center;
                        max-width: 400px;
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
                        color: white;
                    }
                    h2 {
                        color: #333;
                        margin-bottom: 20px;
                        font-size: 26px;
                    }
                    p {
                        color: #666;
                        font-size: 18px;
                        line-height: 1.8;
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
        
        genre_options = ''
        for genre_name in NEWS_CATEGORIES.keys():
            selected = 'selected' if genre_name == current_settings['genre'] else ''
            genre_options += f'<option value="{genre_name}" {selected}>{genre_name}</option>'
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>ニュース配信設定 - VisAI</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
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
                .form-group {{
                    margin-bottom: 25px;
                }}
                label {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    color: #2c3e50;
                    font-weight: 600;
                    font-size: 15px;
                    margin-bottom: 10px;
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
                            {genre_options}
                        </select>
                    </div>
                    <button type="submit">💾 設定を保存する</button>
                </form>
            </div>
        </body>
        </html>
        """
        
        return render_template_string(html)
    
    except Exception as e:
        print(f"❌ Settings page error: {e}")
        import traceback
        traceback.print_exc()
        return f"Internal Server Error: {str(e)}", 500

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook"""
    try:
        signature = request.headers.get("X-Line-Signature")
        body = request.get_data(as_text=True)
        
        webhook_handler.handle(body, signature)
        return "OK"
    except InvalidSignatureError:
        print(f"❌ Invalid signature")
        abort(400)
    except Exception as e:
        print(f"❌ Callback error: {e}")
        import traceback
        traceback.print_exc()
        return "OK"

@webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """LINEメッセージを受信したときの処理"""
    try:
        user_id = event.source.user_id
        text = event.message.text.strip()
        timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"💬 [{timestamp}] Message from {user_id[:8]}...: '{text}'")
        
        # 今すぐニュースを配信
        if text == "今すぐ":
            settings = get_user_settings(user_id)
            print(f"🚀 [{timestamp}] Immediate delivery requested by {user_id[:8]}...")
            
            # 別スレッドで配信を実行
            threading.Thread(target=send_news_to_user, args=(user_id, settings['genre']), daemon=True).start()
            return
        
        # 設定画面へのリンクを送信
        if text == "設定":
            settings_url = f"{APP_PUBLIC_URL}/settings?user_id={user_id}"
            
            reply_text = (
                "⚙️ 設定\n"
                "以下のリンクから配信時間とジャンルを変更できます。\n\n"
                f"{settings_url}\n\n"
                "※リンクを知っている人は誰でも設定を変更できてしまうため、他人に教えないでください。"
            )
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
            print(f"⚙️ [{timestamp}] Settings link sent to {user_id[:8]}...")
            return
        
        # 友だちに紹介する機能
        if text in ["友だちに紹介する", "友達に紹介する", "紹介"]:
            line_add_url = f"https://line.me/R/ti/p/{LINE_BOT_ID}"
            
            reply_text = (
                "📢 友だちに紹介\n\n"
                "VisAIを友だちに紹介していただきありがとうございます！\n\n"
                "以下のリンクを友だちに転送してください👇\n\n"
                f"🔗 友だち追加リンク\n{line_add_url}\n\n"
                "📱 使い方\n"
                "① このメッセージを転送\n"
                "② リンクをタップして友だち追加\n"
                "③ 「今すぐ」でニュースを受け取れます\n\n"
                "💡 紹介してくれると開発の励みになります！"
            )
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
            print(f"👥 [{timestamp}] Friend referral sent to {user_id[:8]}...")
            return
        
        # デフォルトのヘルプメニュー
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="💡メニュー\n・「今すぐ」: 今すぐニュースを受信\n・「設定」: 時間やジャンルを変更\n・「友だちに紹介する」: 友だちに紹介")
        )
        print(f"ℹ️ [{timestamp}] Help menu sent to {user_id[:8]}...")
        
    except Exception as e:
        print(f"❌ handle_message error: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# アプリケーション起動
# ==========================================
if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("🚀 Starting VisAI LINE Bot")
    print("=" * 70 + "\n")
    
    init_database()
    
    # スケジューラースレッドを起動
    scheduler_thread = threading.Thread(target=schedule_checker, daemon=True)
    scheduler_thread.start()
    
    startup_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 70}")
    print(f"✅ Bot started successfully at {startup_time}")
    print(f"{'=' * 70}\n")
    
    app.run(host='0.0.0.0', port=10000, debug=False)
