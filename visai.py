import os
import sqlite3
import threading
import time
import feedparser
from datetime import datetime
from flask import Flask, request, abort, render_template_string, url_for

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

app = Flask(__name__)

# ==========================================
# 設定・定数
# ==========================================

# 環境変数から読み込むように変更
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# アプリの公開URL（RenderのURLが確定したら修正)
# 例: "https://your-app-name.onrender.com"
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://visai-1.onrender.com")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OPENAI_API_KEY]):
    print("🚨 必要な環境変数が設定されていません。アプリは実行されますが、LINEやOpenAIの機能は動作しません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
web_hook_handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_API_KEY)

# Renderでは /opt/render/project/src に書き込み権限がある
DB_PATH = os.path.join(os.path.dirname(__file__), "user_settings.db")
DB_NAME = DB_PATH

# ニュースカテゴリ定義
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
# データベース関連 (SQLite)
# ==========================================
def init_db():
    """データベースの初期化"""
    try:
        # ディレクトリが存在するか確認
        db_dir = os.path.dirname(DB_NAME)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        # ユーザー設定テーブル: ID, 時間, ジャンル
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                delivery_time TEXT DEFAULT '08:00',
                genre TEXT DEFAULT 'トップ'
            )
        ''')
        conn.commit()
        conn.close()
        print(f"✅ Database initialized at {DB_NAME}")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

def get_user_settings(user_id):
    """ユーザー設定を取得（なければデフォルトを作成）"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT delivery_time, genre FROM users WHERE user_id = ?', (user_id,))
        res = c.fetchone()
        if res is None:
            # 新規ユーザーはデフォルト登録
            c.execute('INSERT INTO users (user_id, delivery_time, genre) VALUES (?, ?, ?)', (user_id, '08:00', 'トップ'))
            conn.commit()
            res = ('08:00', 'トップ')
        conn.close()
        return {"time": res[0], "genre": res[1]}
    except Exception as e:
        print(f"❌ get_user_settings error: {e}")
        return {"time": "08:00", "genre": "トップ"}

def update_user_settings(user_id, delivery_time, genre):
    """ユーザー設定を更新"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''
            INSERT INTO users (user_id, delivery_time, genre) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET delivery_time=excluded.delivery_time, genre=excluded.genre
        ''', (user_id, delivery_time, genre))
        conn.commit()
        conn.close()
        print(f"✅ Updated settings for {user_id}: {delivery_time}, {genre}")
    except Exception as e:
        print(f"❌ update_user_settings error: {e}")

def get_users_by_time(target_time):
    """指定した時間に配信すべきユーザーリストを取得"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT user_id, genre FROM users WHERE delivery_time = ?', (target_time,))
        users = c.fetchall()
        conn.close()
        return users
    except Exception as e:
        print(f"❌ get_users_by_time error: {e}")
        return []

# ==========================================
# ニュース取得・AI要約ロジック
# ==========================================
def get_news_content(category):
    if category not in RSS_URL:
        category = "トップ"
    
    feed = feedparser.parse(RSS_URL[category])
    items = []
    # ニュースを5件取得
    for entry in feed.entries[:5]:
        items.append(f"・{entry.title} ({entry.link})")
    
    return "\n".join(items)

def generate_ai_summary(news_text, category):
    """
    ニュース全体をまとめて600文字程度で要約する
    """
    system_prompt = (
        "あなたは明るく親しみやすいニュース解説AIです。"
        "ユーザーに最新情報をわかりやすく伝えてください。"
    )
    
    user_prompt = f"""
    以下のニュース記事（ジャンル:{category}）を元に、LINEで送るニュースダイジェストを作成してください。

    【条件】
    1. 全体の文字数は「600文字程度」に収めてください。
    2. 絵文字を適度に使用し、視覚的に楽しく読みやすくしてください（例: 💡, 📰, ⚡）。
    3. 各記事をバラバラに要約するのではなく、重要なトピックを中心に流れを作って解説してください。
    4. 記事のURLは要約内には含めず、文章のみで構成してください（URLは別途付与するため）。
    5. 時間帯によって冒頭に「おはようございます！」や「お疲れ様です！」など、読む人に寄り添う挨拶を入れてください。
    6. それぞれのニュースに1.ニュースのタイトルの後にニュースを解説してほしいです。

    【ニュース内容】
    {news_text}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", # コストパフォーマンスの良いモデルを指定
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=800,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"OpenAI Error: {e}")
        return "申し訳ありません。AIによる要約の生成に失敗しました。"

def push_news(user_id, category):
    """指定ユーザーにニュースを送信する処理"""
    print(f"Start pushing news to {user_id} (Genre: {category})")
    
    # 1. RSS取得
    feed = feedparser.parse(RSS_URL.get(category, RSS_URL["トップ"]))
    if not feed.entries:
        return

    # 2. AI用テキスト作成
    articles_for_ai = []
    for entry in feed.entries[:5]:
        articles_for_ai.append(f"タイトル: {entry.title}\nリンク: {entry.link}")
    input_text = "\n\n".join(articles_for_ai)

    # 3. AI要約生成
    summary_text = generate_ai_summary(input_text, category)

    # 4. メッセージ構築
    # AI要約 + リンク一覧という構成にする
    links_text = "\n".join([f"🔗 {e.title[:15]}...\n{e.link}" for e in feed.entries[:5]]) # リンクは5つ添える
    
    final_message = f"{summary_text}\n\n👇 気になる記事をチェック\n{links_text}"

    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=final_message))
    except Exception as e:
        print(f"Push Error: {e}")

# ==========================================
# スケジューラー（定期実行）
# ==========================================
def schedule_checker():
    """毎分実行し、設定時刻になったユーザーに送信"""
    while True:
        now_str = datetime.now().strftime("%H:%M")
        # その時間のユーザーを取得
        targets = get_users_by_time(now_str)
        
        for user_id, genre in targets:
            # スレッドで並列処理（人数が多い場合の遅延防止）
            threading.Thread(target=push_news, args=(user_id, genre)).start()
        
        time.sleep(60)

# ==========================================
# Flask Webルート (設定画面)
# ==========================================
@app.route("/")
def health_check():
    return "OK"

@app.route("/settings", methods=['GET', 'POST'])
def settings():
    user_id = request.args.get('user_id')
    
    if not user_id:
        return "エラー: ユーザーIDが見つかりません。LINEから再度アクセスしてください。"

    if request.method == 'POST':
        new_time = request.form.get('delivery_time')
        new_genre = request.form.get('genre')
        
        update_user_settings(user_id, new_time, new_genre)
        
        return """
        <div style="text-align:center; padding: 20px; font-family: sans-serif;">
            <h2>✅ 設定を保存しました！</h2>
            <p>設定した時間にニュースが届きます。</p>
            <p>LINEの画面に戻ってください。</p>
        </div>
        """

    # 現在の設定を取得
    current_settings = get_user_settings(user_id)
    
    # 設定画面HTML
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
        abort(400)

    return "OK"

@web_hook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    msg = event.message.text.strip()

    # 1. 「今すぐ」: 現在の設定で即時配信
    if msg == "今すぐ":
        settings = get_user_settings(user_id)
        # 重い処理なので別スレッドで実行
        threading.Thread(target=push_news, args=(user_id, settings['genre'])).start()
        return

    # 2. 「設定変更」: 設定ページのリンクを案内
    if msg == "設定変更":
        # クエリパラメータにuser_idを含める（簡易的な実装）
        settings_url = f"{APP_PUBLIC_URL}/settings?user_id={user_id}"
        
        reply_text = (
            "⚙️ 設定変更\n"
            "以下のリンクから配達時間とジャンルを変更できます。\n\n"
            f"{settings_url}\n\n"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_text))
        return

    # 3. その他のメッセージ
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("💡メニュー\n・「今すぐ」: 今すぐニュースを受信\n・「設定変更」: 時間やジャンルを変更")
    )

# ==========================================
# アプリ起動時の初期化
# ==========================================
# データベースを初期化（アプリ起動時に実行）
init_db()

# スケジューラーをバックグラウンドで起動
scheduler_thread = threading.Thread(target=schedule_checker, daemon=True)
scheduler_thread.start()
print("✅ Scheduler started")

# ==========================================
# アプリ起動
# ==========================================
if __name__ == "__main__":
    # ローカル開発用
    app.run(debug=True, host='0.0.0.0', port=10000)