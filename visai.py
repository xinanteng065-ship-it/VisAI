import os
import json
import threading
import time
import feedparser
import requests
from datetime import datetime, timedelta
from pytz import timezone
from flask import Flask, request, abort, render_template_string
from bs4 import BeautifulSoup
from collections import Counter
import re
from pathlib import Path

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

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://visai-1.onrender.com")
BOOTH_SUPPORT_URL = "https://visai.booth.pm/items/7763380"

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OPENAI_API_KEY]):
    print("🚨 必要な環境変数が設定されていません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
web_hook_handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_API_KEY)

# JSONファイルのパス
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USER_DATA_FILE = os.path.join(DATA_DIR, "user_settings.json")

JST = timezone('Asia/Tokyo')

# ファイルロック用
data_lock = threading.Lock()

# ==========================================
# 多角的ニュース比較用のRSSフィード設定
# ==========================================

NEWS_SOURCES = {
    "Yahoo": {
        "トップ": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
        "社会": "https://news.yahoo.co.jp/rss/topics/domestic.xml",
        "国際": "https://news.yahoo.co.jp/rss/topics/world.xml",
        "経済": "https://news.yahoo.co.jp/rss/topics/business.xml",
        "スポーツ": "https://news.yahoo.co.jp/rss/topics/sports.xml",
        "エンタメ": "https://news.yahoo.co.jp/rss/topics/entertainment.xml",
        "IT": "https://news.yahoo.co.jp/rss/topics/it.xml",
    }
}

# ==========================================
# JSONベースのデータ管理
# ==========================================

def init_data_storage():
    """データ保存用ディレクトリとファイルを初期化"""
    try:
        # ディレクトリ作成
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        
        # ファイルが存在しない場合は空のJSONを作成
        if not os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
            print("✅ JSON data storage initialized")
        else:
            print("✅ JSON data storage found")
    except Exception as e:
        print(f"❌ Data storage initialization error: {e}")

def load_user_data():
    """ユーザーデータをJSONファイルから読み込み"""
    try:
        with data_lock:
            if os.path.exists(USER_DATA_FILE):
                with open(USER_DATA_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
    except Exception as e:
        print(f"❌ Error loading user data: {e}")
        return {}

def save_user_data(data):
    """ユーザーデータをJSONファイルに保存"""
    try:
        with data_lock:
            with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Error saving user data: {e}")

def get_user_settings(user_id):
    """ユーザーの設定を取得"""
    try:
        all_data = load_user_data()
        
        if user_id not in all_data:
            # デフォルト設定で新規ユーザーを作成
            all_data[user_id] = {
                "delivery_time": "08:00",
                "genre": "トップ",
                "delivery_count": 0,
                "support_message_shown": 0,
                "comparison_mode": 1
            }
            save_user_data(all_data)
        
        return all_data[user_id]
    except Exception as e:
        print(f"❌ get_user_settings error: {e}")
        return {
            "delivery_time": "08:00",
            "genre": "トップ",
            "delivery_count": 0,
            "support_message_shown": 0,
            "comparison_mode": 1
        }

def update_user_settings(user_id, delivery_time, genre, comparison_mode=1):
    """ユーザー設定を更新"""
    try:
        all_data = load_user_data()
        
        if user_id not in all_data:
            all_data[user_id] = {
                "delivery_time": delivery_time,
                "genre": genre,
                "delivery_count": 0,
                "support_message_shown": 0,
                "comparison_mode": comparison_mode
            }
        else:
            all_data[user_id]["delivery_time"] = delivery_time
            all_data[user_id]["genre"] = genre
            all_data[user_id]["comparison_mode"] = comparison_mode
        
        save_user_data(all_data)
        print(f"✅ Updated settings for {user_id}")
    except Exception as e:
        print(f"❌ update_user_settings error: {e}")

def increment_delivery_count(user_id):
    """配信回数をインクリメント"""
    try:
        all_data = load_user_data()
        
        if user_id in all_data:
            all_data[user_id]["delivery_count"] = all_data[user_id].get("delivery_count", 0) + 1
            save_user_data(all_data)
    except Exception as e:
        print(f"❌ increment_delivery_count error: {e}")

def mark_support_message_shown(user_id):
    """サポートメッセージ表示済みフラグを立てる"""
    try:
        all_data = load_user_data()
        
        if user_id in all_data:
            all_data[user_id]["support_message_shown"] = 1
            save_user_data(all_data)
    except Exception as e:
        print(f"❌ mark_support_message_shown error: {e}")

def get_users_by_time(target_time):
    """指定時刻に配信予定のユーザーを取得"""
    try:
        all_data = load_user_data()
        users = []
        
        for user_id, settings in all_data.items():
            if settings.get("delivery_time") == target_time:
                users.append((
                    user_id,
                    settings.get("genre", "トップ"),
                    settings.get("comparison_mode", 1)
                ))
        
        return users
    except Exception as e:
        print(f"❌ get_users_by_time error: {e}")
        return []

# ==========================================
# ニュース取得ロジック（多角的比較用）
# ==========================================

def fetch_rss_articles(source_name, category):
    """指定したソースとカテゴリのRSS記事を取得"""
    try:
        if source_name not in NEWS_SOURCES:
            return []
        
        feed_url = NEWS_SOURCES[source_name].get(category)
        if not feed_url:
            return []
        
        feed = feedparser.parse(feed_url)
        articles = []
        
        for entry in feed.entries[:10]:
            articles.append({
                "title": entry.title,
                "link": entry.link,
                "source": source_name,
                "published": entry.get("published", ""),
            })
        
        return articles
    except Exception as e:
        print(f"❌ Error fetching RSS from {source_name}: {e}")
        return []

def extract_keywords(text):
    """テキストからキーワードを抽出（簡易版）"""
    # 日本語の名詞的な単語を抽出（簡易的な実装）
    words = re.findall(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}', text)
    return [w for w in words if len(w) >= 2]

def find_trending_topics(all_articles):
    """複数ソースで共通して扱われている話題を特定"""
    all_keywords = []
    
    for article in all_articles:
        keywords = extract_keywords(article['title'])
        all_keywords.extend(keywords)
    
    # 出現頻度の高いキーワードを特定
    keyword_counts = Counter(all_keywords)
    trending_keywords = [kw for kw, count in keyword_counts.most_common(20) if count >= 2]
    
    # キーワードを含む記事をグループ化
    topic_groups = {}
    
    for article in all_articles:
        title = article['title']
        matched_keywords = [kw for kw in trending_keywords if kw in title]
        
        if matched_keywords:
            key = matched_keywords[0]  # 最初にマッチしたキーワードをトピックキーとする
            if key not in topic_groups:
                topic_groups[key] = []
            topic_groups[key].append(article)
    
    # 複数ソースで扱われているトピックのみを返す
    trending_topics = []
    for keyword, articles in topic_groups.items():
        sources = set([a['source'] for a in articles])
        if len(sources) >= 2:  # 2つ以上のソースで扱われている
            trending_topics.append({
                "keyword": keyword,
                "articles": articles,
                "source_count": len(sources)
            })
    
    # 話題性の高い順にソート
    trending_topics.sort(key=lambda x: x['source_count'], reverse=True)
    
    return trending_topics[:5]  # 上位5つの話題を返す

def generate_comparison_analysis(article, category):
    """Yahooの記事を起点に、他メディアの視点をAIが検索・比較分析する"""
    
    title = article['title']
    link = article['link']
    
    system_prompt = """あなたは国内外の報道機関の論調に精通したプロのニュースアナリストです。
提供されたニュースに対し、OpenAIの検索機能や知識を用いて、他の主要メディア（朝日、産経、日経、ロイター、BBC等）が
この件をどのような切り口で報じているか、あるいは報じる可能性があるかを分析してください。
読者が「情報の偏り」や「多角的な視点」に気づけるような深い分析を提供してください。"""

    user_prompt = f"""
以下のYahooニュースについて、他メディアとの比較分析を行ってください。

【元のニュース】: {title}
【カテゴリー】: {category}
【URL】: {link}

以下の形式で出力してください。

■ ニュース題名：(内容がパッとわかるタイトル)

【1. このニュースの核心】
(何が起きているのか、なぜ重要なのかを3行程度で解説)

【2. 他メディアとの視点の違い】
・(リベラル寄りメディア/保守寄りメディアでの扱いの差)
・(経済紙や専門紙が注目している独自のポイント)
・(もし海外に関連する場合、海外メディアの冷めた視点や独自の指摘)

【3. ネット・SNSの反応】
(Yahooコメント欄やXなどで、どのような賛否両論が起きているか)

【4. このニュースの「裏側」を読む】
(今後、どのような展開が予想されるか、読者が注意すべき点は何か)
"""

    try:
        # ツール(検索)の使用を想定した最新のGPT-4oモデルを使用
        response = client.chat.completions.create(
            model="gpt-4o-mini", # 検索精度を高めるため4oを推奨
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=1000,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ OpenAI API Error: {e}")
        return f"【{title}】に関する分析に失敗しました。"

def get_comparative_news(category):
    """Yahooからニュースを取得し、一つのニュースを深掘りするメイン処理"""
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"📊 [{timestamp}] Starting AI deep-analysis for {category} (Yahoo based)")
    
    # Yahooから記事を取得
    articles = fetch_rss_articles("Yahoo", category)
    
    if not articles:
        return "現在、Yahooニュースを取得できませんでした。"
    
    # 最初の1件（最も注目されている記事）を詳細分析の対象にする
    target_article = articles[0]
    
    print(f"   → Analyzing top article: {target_article['title']}")
    
    # AIによる多角的分析の生成
    analysis = generate_comparison_analysis(target_article, category)
    
    final_message = f"📰 【{category}】AI多角的ニュース比較\n\n" + analysis
    
    # 元記事へのリンク
    final_message += f"\n\n{'='*40}\n👇 元のニュース（Yahoo）\n{target_article['title']}\n{target_article['link']}"
    
    return final_message

# ==========================================
# 従来の要約機能（シンプルモード用）
# ==========================================

def get_simple_news_summary(category):
    """従来通りのシンプルな要約"""
    try:
        articles = fetch_rss_articles("Yahoo", category)
        
        if not articles:
            return "ニュースを取得できませんでした。"
        
        articles_text = "\n".join([f"・{a['title']} ({a['link']})" for a in articles[:5]])
        
        system_prompt = "あなたは明るく親しみやすいニュース解説AIです。"
        
        user_prompt = f"""
以下のニュース記事(ジャンル:{category})を元に、LINEで送るニュースダイジェストを作成してください。

【条件】
1. 全体の文字数は「600文字程度」に収めてください。
2. 絵文字を適度に使用し、視覚的に楽しく読みやすくしてください。
3. 各記事をバラバラに要約するのではなく、重要なトピックを中心に流れを作って解説してください。
4. 記事のURLは要約内には含めず、文章のみで構成してください。
5. 冒頭に「お疲れ様です!」など、読む人に寄り添う挨拶を入れてください。
6. それぞれのニュースタイトルの前に数字をつけてください。

【ニュース内容】
{articles_text}
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=800,
            temperature=0.7
        )
        
        summary = response.choices[0].message.content.strip()
        
        links_text = "\n".join([f"🔗 {a['title'][:15]}...\n{a['link']}" for a in articles[:5]])
        
        return f"{summary}\n\n👇 気になる記事をチェック\n{links_text}"
        
    except Exception as e:
        print(f"❌ Error in get_simple_news_summary: {e}")
        return "ニュースの取得に失敗しました。"

# ==========================================
# プッシュ配信
# ==========================================

def push_news(user_id, category, comparison_mode=1):
    """ニュースをプッシュ配信"""
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"📤 [{timestamp}] Pushing news to {user_id} (Genre: {category}, Mode: {'Comparison' if comparison_mode else 'Simple'})")
    
    try:
        if comparison_mode == 1:
            final_message = get_comparative_news(category)
        else:
            final_message = get_simple_news_summary(category)
        
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
            
            time.sleep(2)
            line_bot_api.push_message(user_id, TextSendMessage(text=support_message))
            mark_support_message_shown(user_id)
            print(f"💝 [{timestamp}] Support message sent to {user_id}")
        
    except Exception as e:
        print(f"❌ [{timestamp}] Push Error for {user_id}: {e}")

# ==========================================
# スケジューラー
# ==========================================

def schedule_checker():
    print("🚀 Scheduler thread started")
    
    now = datetime.now(JST)
    wait_seconds = 60 - now.second
    print(f"⏱️ Waiting {wait_seconds}s to sync...")
    time.sleep(wait_seconds)
    
    last_checked_minute = None
    
    while True:
        try:
            now_jst = datetime.now(JST)
            current_time_str = now_jst.strftime("%H:%M")
            current_minute_key = now_jst.strftime("%Y%m%d%H%M")
            timestamp = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            
            if current_minute_key == last_checked_minute:
                time.sleep(1)
                continue
            
            last_checked_minute = current_minute_key
            
            print(f"⏰ [{timestamp}] Checking deliveries for {current_time_str}...")
            
            targets = get_users_by_time(current_time_str)
            
            if targets:
                print(f"📬 [{timestamp}] Found {len(targets)} user(s)")
                for user_id, genre, comparison_mode in targets:
                    threading.Thread(
                        target=push_news, 
                        args=(user_id, genre, comparison_mode), 
                        daemon=True
                    ).start()
            
            now = datetime.now(JST)
            wait_seconds = 60 - now.second
            time.sleep(wait_seconds)
            
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            time.sleep(60)

# ==========================================
# Flask Routes
# ==========================================

@app.route("/")
def health_check():
    return "OK - Multi-Perspective News Bot (JSON Storage)"

@app.route("/settings", methods=['GET', 'POST'])
def settings():
    user_id = request.args.get('user_id')
    
    if not user_id:
        return "エラー: ユーザーIDが見つかりません。"

    if request.method == 'POST':
        new_time = request.form.get('delivery_time')
        new_genre = request.form.get('genre')
        new_mode = int(request.form.get('comparison_mode', 1))
        
        update_user_settings(user_id, new_time, new_genre, new_mode)
        
        return """
        <div style="text-align:center; padding: 20px; font-family: sans-serif;">
            <h2>✅ 設定を保存しました!</h2>
            <p>LINEの画面に戻ってください。</p>
        </div>
        """

    current_settings = get_user_settings(user_id)
    
    genre_options = ['トップ', '社会', '国際', '経済', 'スポーツ', 'エンタメ', 'IT']
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ニュース配信設定</title>
        <style>
            body {{ font-family: sans-serif; padding: 20px; background-color: #f0f0f0; }}
            .container {{ max-width: 400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }}
            h2 {{ text-align: center; color: #333; }}
            label {{ display: block; margin-top: 15px; font-weight: bold; }}
            select, input[type="time"] {{ width: 100%; padding: 10px; margin-top: 5px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            .mode-option {{ margin: 10px 0; padding: 10px; border: 2px solid #ddd; border-radius: 4px; cursor: pointer; }}
            .mode-option input {{ margin-right: 10px; }}
            .mode-option.selected {{ border-color: #00B900; background-color: #f0fff0; }}
            button {{ width: 100%; padding: 12px; background-color: #00B900; color: white; border: none; border-radius: 4px; margin-top: 20px; font-size: 16px; cursor: pointer; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>⚙️ 配信設定</h2>
            <form method="POST">
                <label>配信時間:</label>
                <input type="time" name="delivery_time" value="{current_settings['delivery_time']}" required>
                
                <label>ニュースジャンル:</label>
                <select name="genre">
                    {''.join([f'<option value="{k}" {"selected" if k == current_settings["genre"] else ""}>{k}</option>' for k in genre_options])}
                </select>
                
                <label>配信モード:</label>
                <div class="mode-option {'selected' if current_settings.get('comparison_mode', 1) == 1 else ''}">
                    <input type="radio" name="comparison_mode" value="1" {'checked' if current_settings.get('comparison_mode', 1) == 1 else ''}>
                    <strong>📊 多角的比較モード</strong><br>
                    <small>複数メディアの視点を比較</small>
                </div>
                <div class="mode-option {'selected' if current_settings.get('comparison_mode', 1) == 0 else ''}">
                    <input type="radio" name="comparison_mode" value="0" {'checked' if current_settings.get('comparison_mode', 1) == 0 else ''}>
                    <strong>📰 シンプルモード</strong><br>
                    <small>従来通りの要約</small>
                </div>
                
                <button type="submit">設定を保存</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

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
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"💬 [{timestamp}] Message from {user_id}: '{msg}'")

    if msg == "今すぐ":
        settings = get_user_settings(user_id)
        threading.Thread(
            target=push_news, 
            args=(user_id, settings['genre'], settings.get('comparison_mode', 1)), 
            daemon=True
        ).start()
        line_bot_api.reply_message(
            event.reply_token, 
        )
        return

    if msg == "設定変更":
        settings_url = f"{APP_PUBLIC_URL}/settings?user_id={user_id}"
        reply_text = (
            "⚙️ 設定変更\n\n"
            "📊 新機能：多角的ニュース比較モード\n"
            "複数メディアの報道を比較できます！\n\n"
            f"{settings_url}\n\n"
            "※他人に教えないでください。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_text))
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            "💡メニュー\n"
            "・「今すぐ」: 今すぐニュースを受信\n"
            "・「設定変更」: 時間・ジャンル・モードを変更\n\n"
            "📊 新機能！\n"
            "多角的比較モードで複数メディアの視点を比較できます"
        )
    )

# ==========================================
# アプリ起動
# ==========================================
init_data_storage()

scheduler_thread = threading.Thread(target=schedule_checker, daemon=True)
scheduler_thread.start()

startup_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
print(f"✅ [{startup_time}] Multi-Perspective News Bot started (JSON Storage)")

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=10000)