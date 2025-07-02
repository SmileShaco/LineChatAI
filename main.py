import os

import openai
from dotenv import load_dotenv
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (FollowEvent, MessageEvent, TextMessage,
                            TextSendMessage)

load_dotenv()
app = Flask(__name__)

# 環境変数からキーを取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI APIクライアントの設定
client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ユーザーごとの会話履歴を管理（メモリベース）
user_chat_histories = {}
# ユーザーごとのAIチャット有効/無効状態を管理
user_ai_chat_enabled = {}
# ユーザーごとのトークン使用量を管理
user_token_usage = {}

# トークン価格設定（100万トークンあたりの価格）- GPT-4o
TOKEN_PRICES = {
    'input': 2.50,
    'cached_input': 1.25,
    'output': 10.00
}

# 夫婦チャット用のシステムプロンプトテンプレート
COUPLE_CHAT_TEMPLATE = """以下の条件で、夫婦のチャット内容に対してメッセージを返してください：

# 目的
わかりやすく質問に対して回答すること

# 語り手の人物像
・静かで優しくて、あたたかい空気をまとう女性
・ゆったりと落ち着いた口調で話す
・お姉さんでもお母さんでもない。少し距離のある、でも信頼できる存在
・ロマンチックな表現や詩的な言い回しは避け、日常的で素直な日本語を使う

# 文章の特徴
・やさしい口調で語りかけるように
・必要あれば語り手自身が「こんなふうにしてみるのはどうかな」と提案する
・責めたり決めつけたりせず、安心感のある文体にする

# 出力の形式
会話を読んだ語り手が、状況をそっと受けとめながら、ふたりにやさしく語りかける

# 入力の形式
[ユーザーの入力]

{user_message}"""


def is_ai_chat_enabled(user_id):
    """ユーザーのAIチャット有効状態を取得"""
    return user_ai_chat_enabled.get(user_id, False)


def set_ai_chat_enabled(user_id, enabled):
    """ユーザーのAIチャット有効状態を設定"""
    user_ai_chat_enabled[user_id] = enabled


def is_first_message(user_id):
    """ユーザーの初回メッセージかどうかを判定"""
    return user_id not in user_chat_histories or len(user_chat_histories[user_id]) == 0


def initialize_token_usage(user_id):
    """ユーザーのトークン使用量を初期化"""
    if user_id not in user_token_usage:
        user_token_usage[user_id] = {
            'input_tokens': 0,
            'cached_input_tokens': 0,
            'output_tokens': 0
        }


def update_token_usage(user_id, usage_data):
    """トークン使用量を更新"""
    initialize_token_usage(user_id)

    try:
        # 入力トークン数（CompletionUsageオブジェクトから直接取得）
        prompt_tokens = getattr(usage_data, 'prompt_tokens', 0)

        # キャッシュされた入力トークン数（利用可能な場合）
        cached_tokens = 0
        if hasattr(usage_data, 'prompt_tokens_details') and usage_data.prompt_tokens_details:
            cached_tokens = getattr(
                usage_data.prompt_tokens_details, 'cached_tokens', 0)

        # 実際の入力トークン数（キャッシュを除く）
        actual_input_tokens = prompt_tokens - cached_tokens

        # 出力トークン数（CompletionUsageオブジェクトから直接取得）
        completion_tokens = getattr(usage_data, 'completion_tokens', 0)

        # 累計に追加
        user_token_usage[user_id]['input_tokens'] += actual_input_tokens
        user_token_usage[user_id]['cached_input_tokens'] += cached_tokens
        user_token_usage[user_id]['output_tokens'] += completion_tokens

        print(
            f"トークン使用量記録: Input={actual_input_tokens}, Cached={cached_tokens}, Output={completion_tokens}")

    except Exception as e:
        print(f"トークン使用量記録エラー: {type(e).__name__}: {str(e)}")
        print(f"usage_data type: {type(usage_data)}")
        print(f"usage_data attributes: {dir(usage_data)}")


def calculate_cost(user_id):
    """ユーザーの累計コストを計算"""
    initialize_token_usage(user_id)

    usage = user_token_usage[user_id]

    # 各トークンタイプのコスト計算（100万トークンあたりの価格）
    input_cost = (usage['input_tokens'] / 1_000_000) * TOKEN_PRICES['input']
    cached_input_cost = (usage['cached_input_tokens'] /
                         1_000_000) * TOKEN_PRICES['cached_input']
    output_cost = (usage['output_tokens'] / 1_000_000) * TOKEN_PRICES['output']

    total_cost = input_cost + cached_input_cost + output_cost

    return {
        'input_cost': input_cost,
        'cached_input_cost': cached_input_cost,
        'output_cost': output_cost,
        'total_cost': total_cost
    }


def reset_token_usage(user_id):
    """ユーザーのトークン使用量をリセット"""
    if user_id in user_token_usage:
        user_token_usage[user_id] = {
            'input_tokens': 0,
            'cached_input_tokens': 0,
            'output_tokens': 0
        }


def get_gpt_response(user_message, user_id):
    """OpenAI APIを使ってレスポンスを取得（会話履歴込み）"""
    try:
        if not OPENAI_API_KEY or not client:
            return "OpenAI APIキーが設定されていません。管理者にお問い合わせください。"

        # 初回メッセージの場合は夫婦チャット用テンプレートを使用
        if is_first_message(user_id):
            formatted_message = COUPLE_CHAT_TEMPLATE.format(
                user_message=user_message)
            messages = [
                {"role": "user", "content": formatted_message}
            ]
        else:
            # 2回目以降は会話履歴を使用（トークン節約）
            chat_history = user_chat_histories.get(user_id, [])
            messages = []

            # 過去の会話履歴を追加
            messages.extend(chat_history)

            # 現在のユーザーメッセージを追加
            messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=1000,
            temperature=0.7
        )

        assistant_response = response.choices[0].message.content

        # トークン使用量を記録
        if hasattr(response, 'usage') and response.usage:
            update_token_usage(user_id, response.usage)

        # 会話履歴を更新
        if user_id not in user_chat_histories:
            user_chat_histories[user_id] = []

        user_chat_histories[user_id].append(
            {"role": "user", "content": user_message})
        user_chat_histories[user_id].append(
            {"role": "assistant", "content": assistant_response})

        # 会話履歴が長くなりすぎた場合は古いものから削除（最新20回の会話のみ保持）
        if len(user_chat_histories[user_id]) > 40:  # 20回の会話 = 40メッセージ
            user_chat_histories[user_id] = user_chat_histories[user_id][-40:]

        return assistant_response

    except Exception as e:
        print(f"OpenAI APIエラー: {type(e).__name__}: {str(e)}")
        return "申し訳ございません。現在、AIサービスに接続できません。しばらく後にお試しください。"


def clear_chat_history(user_id):
    """指定ユーザーの会話履歴をクリア"""
    if user_id in user_chat_histories:
        del user_chat_histories[user_id]
        return True
    return False


def get_chat_summary(user_id):
    """現在の会話数を取得"""
    if user_id in user_chat_histories:
        return len(user_chat_histories[user_id]) // 2  # ユーザー+アシスタントで1回の会話
    return 0


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

このBotでは、夫婦の会話に優しくアドバイスする
AIアシスタントと対話できます。

【コマンド】
📝 「on」または「ON」: AI会話モードを開始
📴 「off」または「OFF」: AI会話モードを停止
🗑️ 「クリア」「clear」「Clear」「CLEAR」: 会話履歴をリセット

まずは「on」と入力してAIチャットを開始してください。"""

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome_message)
    )


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text

    # ユーザー名を取得
    try:
        profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except:
        user_name = "ユーザー"

    if text.lower() == "on":
        # AIチャットを有効化
        set_ai_chat_enabled(user_id, True)

        reply = "……ラファエルによる応答機能を起動しました 📝"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text.lower() == "off":
        # AIチャットを無効化
        set_ai_chat_enabled(user_id, False)

        reply = "……ラファエルによる応答機能を停止しました 📴\n\n以後もメッセージの受信は継続されますが、ラファエルからの返答は行われません。\n\n再開を希望される場合は「on」と入力してください。\n\n──設定、正常に反映されました。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text in ["クリア", "clear", "Clear", "CLEAR"]:
        # 会話履歴をクリア
        chat_count = get_chat_summary(user_id)
        clear_chat_history(user_id)

        reply = f"……会話履歴を消去しました 🗑️\n（累計 {chat_count} 回の対話ログを初期化）\n\n次のメッセージより、新規会話として処理を開始します。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text.lower() in ["cost", "コスト"]:
        # コスト情報を表示
        initialize_token_usage(user_id)
        usage = user_token_usage[user_id]
        costs = calculate_cost(user_id)

        reply = f"""💰 累計コスト情報

【トークン使用量】
Input: {usage['input_tokens']:,} tokens
Cached Input: {usage['cached_input_tokens']:,} tokens  
Output: {usage['output_tokens']:,} tokens

【費用詳細】
Input: ${costs['input_cost']:.6f}
Cached Input: ${costs['cached_input_cost']:.6f}
Output: ${costs['output_cost']:.6f}

🔸 合計費用: ${costs['total_cost']:.6f}"""

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text.lower() in ["reset cost", "コストリセット"]:
        # コストをリセット
        reset_token_usage(user_id)

        reply = "💰 累計コストを初期化しました\n\n全てのトークン使用量と費用が0にリセットされました。"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text == "メインメニュー":
        # メインメニューを表示
        chat_count = get_chat_summary(user_id)
        ai_status = "ON" if is_ai_chat_enabled(user_id) else "OFF"

        reply = f"""📊 現在の状態

AIチャット状態: {ai_status}
現在の会話数: {chat_count}回

【コマンド】
📝 「on」: AI会話モードを開始
📴 「off」: AI会話モードを停止
🗑️ 「クリア」: 会話履歴をリセット
💰 「cost」: 累計コストを表示
🔄 「reset cost」: コストをリセット"""

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

    elif text == "デバッグ情報":
        # デバッグ情報を表示
        chat_count = get_chat_summary(user_id)
        ai_enabled = is_ai_chat_enabled(user_id)
        is_first = is_first_message(user_id)

        debug_info = f"""🔧 デバッグ情報:
OpenAI APIキー設定: {'✅' if OPENAI_API_KEY else '❌'}
AIチャット状態: {'ON' if ai_enabled else 'OFF'}
現在の会話数: {chat_count}回
初回メッセージ: {'はい' if is_first else 'いいえ'}
メモリ使用中のユーザー数: {len(user_chat_histories)}人
AIチャット有効ユーザー数: {len([u for u in user_ai_chat_enabled.values() if u])}人
トークン追跡ユーザー数: {len(user_token_usage)}人"""

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=debug_info)
        )

    else:
        # 通常のメッセージ処理
        if not is_ai_chat_enabled(user_id):
            # AIチャットがOFFの場合は処理を弾く（何も返さない）
            return

        # AIチャットがONの場合はGPTとの会話
        gpt_response = get_gpt_response(text, user_id)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=gpt_response)
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
