import glob
import json
import os
from datetime import datetime

import openai
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (FollowEvent, MessageAction, MessageEvent,
                            PostbackAction, PostbackEvent, QuickReply,
                            QuickReplyButton, TextMessage, TextSendMessage)

app = Flask(__name__)

# 環境変数からLINEのキーを取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI APIクライアントの設定
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ユーザーごとの現在のチャットファイルを管理
user_chat_files = {}


def ensure_directories():
    """必要なディレクトリが存在することを確認"""
    os.makedirs("chatlog", exist_ok=True)
    os.makedirs("summarylog", exist_ok=True)


def create_new_chat_file(user_id):
    """新しいチャットファイルを作成"""
    ensure_directories()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}.txt"
    chat_path = f"chatlog/{filename}"
    summary_path = f"summarylog/{filename}"

    # 空のファイルを作成
    try:
        open(chat_path, 'w', encoding='utf-8').close()
        open(summary_path, 'w', encoding='utf-8').close()
        user_chat_files[user_id] = filename
        return filename
    except Exception as e:
        print(f"ファイル作成エラー: {e}")
        return None


def add_to_chat_log(filename, user_name, user_message, gpt_response):
    """チャットログにメッセージを追加"""
    chat_path = f"chatlog/{filename}"
    try:
        with open(chat_path, 'a', encoding='utf-8') as f:
            f.write(f"[{user_name}]: {user_message}\n")
            f.write(f"[gpt]: {gpt_response}\n")
    except Exception as e:
        print(f"チャットログ書き込みエラー: {e}")


def get_summary(filename):
    """サマリーファイルの内容を取得"""
    summary_path = f"summarylog/{filename}"
    try:
        with open(summary_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        print(f"サマリー読み込みエラー: {e}")
        return ""


def create_summary(filename):
    """チャットログをサマライズしてサマリーファイルに保存"""
    chat_path = f"chatlog/{filename}"
    summary_path = f"summarylog/{filename}"

    try:
        with open(chat_path, 'r', encoding='utf-8') as f:
            chat_content = f.read()

        if not chat_content.strip():
            return False

        # OpenAI APIでサマライズ
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": f"{chat_content}\n\n1000文字以内に文字で要約して"}
            ],
            max_tokens=500
        )

        summary = response.choices[0].message.content

        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(summary)
        return True
    except Exception as e:
        print(f"サマリー作成エラー: {e}")
        return False


def get_gpt_response(user_message, filename=None):
    """OpenAI APIを使ってレスポンスを取得"""
    try:
        # サマリーがある場合は追加
        message_content = user_message
        if filename:
            summary = get_summary(filename)
            if summary:
                message_content += f"\n\n過去のやり取り\n{summary}"

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": message_content}
            ],
            max_tokens=1000
        )

        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI APIエラー: {e}")
        return "申し訳ございません。現在、AIサービスに接続できません。しばらく後にお試しください。"


def get_chat_history_list():
    """チャット履歴のリストを取得（新しい順）"""
    ensure_directories()
    chat_files = glob.glob("chatlog/*.txt")
    # ファイル名でソート（新しい順）
    chat_files.sort(reverse=True)
    return [os.path.basename(f) for f in chat_files]


def get_main_menu():
    """メインメニューのクイックリプライを作成"""
    return QuickReply(
        items=[
            QuickReplyButton(
                action=MessageAction(label="新しいチャット", text="新しいチャット")
            ),
            QuickReplyButton(
                action=MessageAction(label="過去の履歴", text="過去の履歴")
            )
        ]
    )


def get_chat_history_menu():
    """チャット履歴選択メニューのクイックリプライを作成"""
    chat_files = get_chat_history_list()
    items = []

    for chat_file in chat_files[:10]:  # 最大10件まで表示
        # ファイル名を読みやすい形式に変換
        timestamp = chat_file.replace('.txt', '')
        try:
            dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
            display_name = dt.strftime("%m/%d %H:%M")
        except:
            display_name = chat_file

        items.append(
            QuickReplyButton(
                action=MessageAction(label=display_name,
                                     text=f"履歴選択:{chat_file}")
            )
        )

    items.append(
        QuickReplyButton(
            action=MessageAction(label="メインメニューに戻る", text="メインメニュー")
        )
    )

    return QuickReply(items=items)


@app.route("/", methods=["GET"])
def hello():
    return "LineChatAI Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK", 200


@handler.add(FollowEvent)
def handle_follow(event):
    """友達追加時の処理"""
    welcome_message = """LineChatAIへようこそ！🤖

このBotでは、OpenAI ChatGPT-4oと会話できます。

📝 新しいチャット: 新しい会話を開始
📋 過去の履歴: 過去の会話を確認・継続

まずは下のメニューからお選びください。"""

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome_message, quick_reply=get_main_menu())
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text

    # ユーザー名を取得（実際の実装では、Lineプロフィールから取得することも可能）
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except:
        user_name = "ユーザー"

    if text == "新しいチャット":
        # 新しいチャットを開始
        filename = create_new_chat_file(user_id)
        if filename:
            timestamp = filename.replace('.txt', '')
            try:
                dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                display_time = dt.strftime("%Y年%m月%d日 %H:%M")
            except:
                display_time = filename

            reply = f"新しいチャットを開始しました 📝\n作成時刻: {display_time}\n\n何かご質問はありますか？"
        else:
            reply = "チャットファイルの作成に失敗しました。もう一度お試しください。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text == "過去の履歴":
        # 過去の履歴を表示
        chat_files = get_chat_history_list()
        if chat_files:
            reply = "過去のチャット履歴を選択してください 📋"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=reply, quick_reply=get_chat_history_menu())
            )
        else:
            reply = "まだチャット履歴がありません。\n「新しいチャット」を開始してください 😊"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply, quick_reply=get_main_menu())
            )

    elif text.startswith("履歴選択:"):
        # 特定の履歴を選択
        filename = text.replace("履歴選択:", "")
        user_chat_files[user_id] = filename

        # チャット履歴を表示
        chat_path = f"chatlog/{filename}"
        try:
            with open(chat_path, 'r', encoding='utf-8') as f:
                history = f.read()

            timestamp = filename.replace('.txt', '')
            try:
                dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                display_time = dt.strftime("%Y年%m月%d日 %H:%M")
            except:
                display_time = filename

            if history.strip():
                # 履歴が長い場合は末尾のみ表示
                if len(history) > 1000:
                    history = "...(省略)...\n" + history[-800:]
                reply = f"📋 チャット履歴 ({display_time}):\n\n{history}\n\n続きからチャットできます。"
            else:
                reply = f"📋 チャット履歴 ({display_time}) は空です。\n\n新しくチャットを開始できます。"
        except FileNotFoundError:
            reply = "チャット履歴が見つかりませんでした。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text == "メインメニュー":
        # メインメニューを表示
        reply = "メニューを選択してください 🎯"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply, quick_reply=get_main_menu())
        )

    elif text == "サマリー作成":
        # 現在のチャットをサマライズ
        if user_id in user_chat_files:
            filename = user_chat_files[user_id]
            if create_summary(filename):
                reply = f"📝 チャット履歴のサマリーを作成しました。\n\n今後のメッセージではこのサマリーが文脈として活用されます。"
            else:
                reply = "サマリーの作成に失敗しました。チャット内容が空か、APIエラーが発生しました。"
        else:
            reply = "アクティブなチャットがありません。\n「新しいチャット」を開始してください。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    else:
        # 通常のチャット
        if user_id not in user_chat_files:
            # まだチャットファイルがない場合
            reply = "まず「新しいチャット」を開始してください 😊"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply, quick_reply=get_main_menu())
            )
        else:
            # GPTにメッセージを送信
            filename = user_chat_files[user_id]
            gpt_response = get_gpt_response(text, filename)

            # チャットログに追加
            add_to_chat_log(filename, user_name, text, gpt_response)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=gpt_response)
            )


if __name__ == "__main__":
    ensure_directories()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
