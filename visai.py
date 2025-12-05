from flask import Flask, request, abort, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import feedparser
import openai
import threading
import re
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

app = Flask(__name__)

# ここに自分のキーをそのまま書く
LINE_CHANNEL_ACCESS_TOKEN = "bK+iKhs41Ng48iSxREkWy/bzt+oICA31xL7CtYE0P407xIXtbmZ/TGieQ695Rqr7wsBWkqak0lfalonJXgvkZKbVTzjF3+wcRT9uKADAEaCdaPiLwGUvjKQ0Hht15fEDcP/Slmg96++xNas+tMDZNQdB04t89/1O/w1cDnyilFU="
LINE_CHANNEL_SECRET = "5bb44277689780213c3c32bc57720b50"
OPENAI_API_KEY = "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"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
web_hook_handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ユーザー設定を保存
user_settings = {}

# RSS URL
RSS_URL = {
    "トップ": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "社会": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
    "国際": "https://news.yahoo.co.jp/rss/topics/world.xml",
    "経済": "https://news.yahoo.co.jp/rss/topics/business.xml",
    "スポーツ": "https://news.yahoo.co.jp/rss/topics/sports.xml",
    "エンタメ": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
    "IT": "https://news.yahoo.co.jp/rss/topics/it.xml",
    "ライフ": "https://news.yahoo.co.jp/rss/topics/life.xml",
}

# スケジューラー初期化
scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.start()


# ------------------------------------------------------
# ユーザー設定の取得
# ------------------------------------------------------
def get_user_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = {
            "delivery_time": "08:00",
            "category": "トップ"
        }
    return user_settings[user_id]


# ------------------------------------------------------
# ニュース取得
# ------------------------------------------------------
def get_news(category="トップ"):
    if category not in RSS_URL:
        category = "トップ"

    feed = feedparser.parse(RSS_URL[category])
    items = []
    for entry in feed.entries[:5]:
        items.append({
            "title": entry.title,
            "link": entry.link,
            "summary": entry.summary if "summary" in entry else entry.title
        })
    return items


# ------------------------------------------------------
# OpenAI 要約（絵文字付き、600文字程度）
# ------------------------------------------------------
def summarize_articles(articles, category):
    prompt = (
        f"以下はYahooニュース「{category}」の最新記事5件です。\n\n"
        "【要約の条件】\n"
        "- 全体で600文字程度にまとめてください\n"
        "- 各記事を【記事1】【記事2】のように番号付きで分けてください\n"
        "- 絵文字を適度に使用し、読みやすく親しみやすい文章にしてください\n"
        "- ニュースの内容を分かりやすく、要点を押さえて伝えてください\n"
        "- 各記事は2-3文程度で簡潔にまとめてください\n\n"
    )

    for i, art in enumerate(articles, 1):
        prompt += (
            f"記事{i}:\n"
            f"タイトル: {art['title']}\n"
            f"内容: {art['summary']}\n\n"
        )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print("OpenAI error:", e)
        return None


# ------------------------------------------------------
# 要約テキストを記事ごとに分割
# ------------------------------------------------------
def parse_summaries_block(summaries_text, articles):
    if not summaries_text:
        return [None] * len(articles)

    pattern = r"【記事\s*(\d+)\】\s*(.*?)(?=【記事\s*\d+\】|$)"
    matches = re.findall(pattern, summaries_text, flags=re.DOTALL)

    if matches:
        summaries_dict = {int(num): content.strip() for num, content in matches}
        return [summaries_dict.get(i+1, "要約がありません。") for i in range(len(articles))]

    return [summaries_text] + [None] * (len(articles) - 1)


# ------------------------------------------------------
# ニュース取得 → 要約 → Push
# ------------------------------------------------------
def process_and_push(user_id, category):
    articles = get_news(category)

    summaries_text = summarize_articles(articles, category)
    if summaries_text is None:
        line_bot_api.push_message(
            user_id,
            TextSendMessage("要約の取得に失敗しました。時間をおいてもう一度お試しください。")
        )
        return

    summaries = parse_summaries_block(summaries_text, articles)

    reply = f"📰 本日のニュースまとめ（{category}）\n\n"
    for i, art in enumerate(articles, 1):
        summary = summaries[i - 1] if i - 1 < len(summaries) and summaries[i - 1] else "要約がありません。"
        reply += (
            f"【{i}. {art['title']}】\n"
            f"{summary}\n"
            f"🔗 {art['link']}\n\n"
        )

    line_bot_api.push_message(user_id, TextSendMessage(reply))


# ------------------------------------------------------
# 定期配信
# ------------------------------------------------------
def scheduled_delivery():
    print(f"[{datetime.now()}] Checking scheduled deliveries...")
    current_time = datetime.now().strftime("%H:%M")
    
    for user_id, settings in user_settings.items():
        if settings["delivery_time"] == current_time:
            print(f"Delivering to {user_id} at {current_time}")
            threading.Thread(target=process_and_push, args=(user_id, settings["category"])).start()


# スケジューラーに毎分実行を設定
scheduler.add_job(scheduled_delivery, 'cron', minute='*')


# ------------------------------------------------------
# 設定画面HTML
# ------------------------------------------------------
SETTINGS_HTML = '''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ニュース配信設定</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 500px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        h1 {
            color: #333;
            margin-bottom: 10px;
            font-size: 24px;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
            font-size: 14px;
        }
        .form-group {
            margin-bottom: 25px;
        }
        label {
            display: block;
            color: #333;
            font-weight: 600;
            margin-bottom: 10px;
            font-size: 16px;
        }
        select, input[type="time"] {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 16px;
            transition: border 0.3s;
        }
        select:focus, input[type="time"]:focus {
            outline: none;
            border-color: #667eea;
        }
        .btn {
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 18px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
        }
        .current-settings {
            background: #f5f5f5;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        .current-settings p {
            color: #666;
            margin: 5px 0;
        }
        .success {
            background: #4caf50;
            color: white;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📰 ニュース配信設定</h1>
        <p class="subtitle">配信時間とカテゴリを選択してください</p>
        
        {% if success %}
        <div class="success">
            ✅ 設定を保存しました！
        </div>
        {% endif %}
        
        <div class="current-settings">
            <p><strong>現在の設定</strong></p>
            <p>⏰ 配信時間: {{ current_time }}</p>
            <p>📑 カテゴリ: {{ current_category }}</p>
        </div>
        
        <form method="POST">
            <div class="form-group">
                <label for="delivery_time">⏰ 配信時間</label>
                <input type="time" id="delivery_time" name="delivery_time" value="{{ current_time }}" required>
            </div>
            
            <div class="form-group">
                <label for="category">📑 ニュースカテゴリ</label>
                <select id="category" name="category" required>
                    <option value="トップ" {% if current_category == "トップ" %}selected{% endif %}>トップニュース</option>
                    <option value="社会" {% if current_category == "社会" %}selected{% endif %}>社会</option>
                    <option value="国際" {% if current_category == "国際" %}selected{% endif %}>国際</option>
                    <option value="経済" {% if current_category == "経済" %}selected{% endif %}>経済</option>
                    <option value="スポーツ" {% if current_category == "スポーツ" %}selected{% endif %}>スポーツ</option>
                    <option value="エンタメ" {% if current_category == "エンタメ" %}selected{% endif %}>エンタメ</option>
                    <option value="IT" {% if current_category == "IT" %}selected{% endif %}>IT</option>
                    <option value="ライフ" {% if current_category == "ライフ" %}selected{% endif %}>ライフ</option>
                </select>
            </div>
            
            <button type="submit" class="btn">💾 設定を保存</button>
        </form>
    </div>
</body>
</html>
'''


# ------------------------------------------------------
# 設定画面ルート
# ------------------------------------------------------
@app.route("/settings/<user_id>", methods=['GET', 'POST'])
def settings(user_id):
    settings = get_user_settings(user_id)
    success = False
    
    if request.method == 'POST':
        settings["delivery_time"] = request.form.get("delivery_time")
        settings["category"] = request.form.get("category")
        success = True
    
    return render_template_string(
        SETTINGS_HTML,
        current_time=settings["delivery_time"],
        current_category=settings["category"],
        success=success
    )


# ------------------------------------------------------
# LINE webhook
# ------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        web_hook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ------------------------------------------------------
# メッセージ処理
# ------------------------------------------------------
@web_hook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    msg = event.message.text.strip()

    # 「設定変更」→ 設定画面URLを送信
    if msg == "設定変更":
        settings_url = f"{request.url_root}settings/{user_id}"
        text = f"⚙️ 設定変更はこちらから\n{settings_url}"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text)
        )
        return

    # 「今すぐ」→ 即座にニュース配信
    if msg == "今すぐ":
        settings = get_user_settings(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("📰 ニュースをお届けします！少々お待ちください...")
        )
        threading.Thread(target=process_and_push, args=(user_id, settings["category"])).start()
        return

    # カテゴリ指定
    if msg in RSS_URL:
        threading.Thread(target=process_and_push, args=(user_id, msg)).start()
        return

    # ヘルプメッセージ
    text = (
        "📰 ニュースBot 使い方\n\n"
        "『設定変更』→ 配信時間とカテゴリを変更\n"
        "『今すぐ』→ すぐにニュース配信\n"
        "『社会』『経済』など → 指定カテゴリのニュース\n\n"
        "※初期設定は毎朝8:00にトップニュースが届きます"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text)
    )


# ------------------------------------------------------
# ヘルスチェック
# ------------------------------------------------------
@app.route("/")
def health_check():
    return "LINE News Bot is running!"


if __name__ == "__main__":
    app.run(debug=True)