import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import google.generativeai as genai
import asyncio
from datetime import datetime, timedelta
import aiohttp
import json
import random
import traceback
import logging
from aiohttp import web
import threading

# 環境変数を読み込み
load_dotenv()

# Gemini AIの設定
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

# Tracker.gg API設定
TRACKER_API_KEY = os.getenv('TRACKER_API_KEY')
TRACKER_BASE_URL = "https://api.tracker.gg/api/v2/valorant"

# レート制限管理
user_last_request = {}
RATE_LIMIT_SECONDS = 30  # 1ユーザーあたり30秒間隔で制限（重複応答を確実に防ぐ）

# 重複処理防止
processed_messages = set()  # 処理済みメッセージIDの記録
user_message_cache = {}  # ユーザー別の最後のメッセージ内容とタイムスタンプ
command_executing = {}  # コマンド実行中フラグ（ユーザーID: コマンド名）

# Bot統計情報
bot_stats = {
    'start_time': datetime.now(),
    'commands_executed': 0,
    'messages_processed': 0,
    'errors_count': 0,
    'last_error': None,
    'last_heartbeat': datetime.now(),
    'restart_count': 0
}

# ヘルスチェック機能
async def health_monitor():
    """Botの健康状態を監視し、問題があれば警告"""
    while True:
        try:
            await asyncio.sleep(300)  # 5分ごとにチェック
            current_time = datetime.now()
            
            # ハートビートを更新
            bot_stats['last_heartbeat'] = current_time
            
            # メモリ使用量チェック
            try:
                import psutil
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                
                # メモリ使用量が100MBを超えたら警告
                if memory_mb > 100:
                    print(f"⚠️ 高メモリ使用量警告: {memory_mb:.1f}MB")
                    cleanup_memory()  # 自動クリーンアップ
                    
                # エラー率チェック
                if bot_stats['commands_executed'] > 0:
                    error_rate = (bot_stats['errors_count'] / bot_stats['commands_executed']) * 100
                    if error_rate > 20:  # エラー率20%以上
                        print(f"⚠️ 高エラー率警告: {error_rate:.1f}%")
                        
            except ImportError:
                pass  # psutilがない場合はスキップ
                
            # Discord接続状態チェック
            if bot.is_closed():
                print("❌ Discord接続が切断されています")
                bot_stats['errors_count'] += 1
                
            # 定期的な状態報告（1時間ごと）
            uptime = current_time - bot_stats['start_time']
            if uptime.total_seconds() % 3600 < 300:  # 1時間±5分の範囲
                print(f"📊 定期報告: 稼働時間 {uptime.days}日{uptime.seconds//3600}時間, "
                      f"コマンド実行 {bot_stats['commands_executed']}, "
                      f"エラー {bot_stats['errors_count']}")
                      
        except Exception as e:
            print(f"ヘルスモニターエラー: {e}")
            bot_stats['errors_count'] += 1

# 重複実行防止デコレーター
def prevent_duplicate_execution(func):
    """全コマンドに統一的な重複実行防止を適用するデコレーター"""
    async def wrapper(ctx, *args, **kwargs):
        # ユーザーIDベースの実行中チェック
        user_id = ctx.author.id
        command_name = func.__name__
        
        if user_id in command_executing:
            await ctx.send(f"⚠️ 他のコマンドが実行中です。少しお待ちください。")
            return
        
        # 実行中フラグを設定
        command_executing[user_id] = command_name
        
        try:
            # 元のコマンドを実行
            await func(ctx, *args, **kwargs)
            # 成功時に統計を更新
            bot_stats['commands_executed'] += 1
        except Exception as e:
            # エラー時に統計を更新
            bot_stats['errors_count'] += 1
            bot_stats['last_error'] = str(e)
            raise  # 元のエラーを再発生
        finally:
            # 実行中フラグをクリア
            command_executing.pop(user_id, None)
    
    return wrapper

# 会話履歴管理
conversation_history = {}  # チャンネルIDごとの会話履歴
MAX_HISTORY_LENGTH = 10   # 保存する会話数の上限
MAX_CONVERSATIONS = 50    # 保存するチャンネル数の上限

# Botの設定（メンバー情報取得対応）
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # メンバー情報取得に必要（Developer Portalで有効化済み前提）
# intents.presences = True  # ステータス情報取得に必要（要Developer Portal設定）
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)  # デフォルトhelpコマンドを無効化

# メンバー管理用のデータ構造
member_stats_dict = {}
welcome_messages_dict = {}
custom_commands_dict = {}
moderation_settings_dict = {}

# メモリクリーンアップ関数
def cleanup_memory():
    """メモリリークを防ぐためのクリーンアップ"""
    global processed_messages, user_message_cache, conversation_history, user_last_request
    
    # 古いprocessed_messagesをクリア
    if len(processed_messages) > 1000:
        processed_messages.clear()
    
    # 古いuser_message_cacheをクリア
    if len(user_message_cache) > 100:
        user_message_cache.clear()
    
    # 会話履歴の制限
    if len(conversation_history) > MAX_CONVERSATIONS:
        # 最も古いチャンネルを削除
        oldest_channels = sorted(conversation_history.keys())[:len(conversation_history) - MAX_CONVERSATIONS]
        for channel_id in oldest_channels:
            del conversation_history[channel_id]
    
    # 古いレート制限記録をクリア（24時間以上古い）
    current_time = datetime.now()
    old_requests = []
    for user_id, last_time in user_last_request.items():
        if (current_time - last_time).total_seconds() > 86400:  # 24時間
            old_requests.append(user_id)
    
    for user_id in old_requests:
        del user_last_request[user_id]

async def periodic_cleanup():
    """定期的なメモリクリーンアップ（30分ごと）"""
    while True:
        try:
            await asyncio.sleep(1800)  # 30分待機
            cleanup_memory()
            print(f"🧹 メモリクリーンアップ実行: {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"クリーンアップエラー: {e}")

async def internal_keep_alive():
    """内部HTTPサーバーによるKeep-alive機能"""
    while True:
        try:
            # 25分ごとに実行（30分のスリープタイマーより短く）
            await asyncio.sleep(1500)  # 25分
            
            # 内部的にアクティビティを生成
            current_time = datetime.now()
            bot_stats['last_heartbeat'] = current_time
            
            print(f"💓 内部Keep-alive実行: {current_time.strftime('%H:%M:%S')}")
            print(f"📊 稼働状況: コマンド {bot_stats['commands_executed']}, メッセージ {bot_stats['messages_processed']}")
            
            # メモリ使用量チェック
            try:
                import psutil
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                print(f"💾 メモリ使用量: {memory_mb:.1f}MB")
                
                if memory_mb > 80:  # 80MB以上で警告
                    print("⚠️ メモリ使用量が高めです。クリーンアップを実行...")
                    cleanup_memory()
                    
            except ImportError:
                print("📊 基本的なKeep-alive実行")
                    
        except Exception as e:
            print(f"⚠️ Internal Keep-alive error: {e}")
            # エラーが発生しても継続

# 定期的なクリーンアップタスク
@bot.event
async def on_ready():
    print(f'{bot.user}としてログインしました！')
    print(f'Bot ID: {bot.user.id}')
    print('------')
    
    # サーバー情報を表示
    print(f'接続中のサーバー数: {len(bot.guilds)}')
    for guild in bot.guilds:
        print(f'  - {guild.name} (ID: {guild.id}) - メンバー数: {guild.member_count}人')
        
        # メンバー情報取得完了
        human_members = [m for m in guild.members if not m.bot]
        print(f'    人間メンバー数: {len(human_members)}人')
    print('------')
    
    # HTTPサーバーを起動（Render.com Web Service対応）
    web_runner = await start_web_server()
    
    # バックグラウンドタスクを開始
    bot.loop.create_task(periodic_cleanup())  # メモリクリーンアップ
    bot.loop.create_task(health_monitor())    # ヘルスモニター
    
    # 内部Keep-alive機能（HTTPサーバーが動作している場合）
    if web_runner:
        print("🔄 内部Keep-alive機能を開始")
        bot.loop.create_task(internal_keep_alive())
    
    print("🚀 Discord Bot + Webサーバーが開始されました！")

@bot.event
async def on_member_join(member):
    """メンバー参加時の処理"""
    # ウェルカムメッセージの送信
    if member.guild.id in welcome_messages_dict:
        channel = member.guild.system_channel
        if channel:
            await channel.send(welcome_messages_dict[member.guild.id].format(member=member))
    
    # メンバー統計の初期化
    member_stats_dict[member.id] = {
        'messages': 0,
        'last_active': datetime.now(),
        'join_date': datetime.now()
    }

@bot.event
async def on_member_remove(member):
    """メンバー退出時の処理"""
    # 退出通知の送信
    channel = member.guild.system_channel
    if channel:
        await channel.send(f"👋 {member.name} がサーバーを退出しました。")

@bot.event
async def on_message(message):
    # Bot自身のメッセージは無視
    if message.author == bot.user:
        return

    # 重複処理を防ぐ
    if message.id in processed_messages:
        return
    processed_messages.add(message.id)
    
    # ユーザー別の重複チェック
    user_id = message.author.id
    current_time = datetime.now()
    
    if user_id in user_message_cache:
        last_message, last_time = user_message_cache[user_id]
        # 同じメッセージを3秒以内に処理していたらスキップ
        if last_message == message.content and (current_time - last_time).total_seconds() < 3:
            print(f"重複処理防止: {message.author} - '{message.content}' ({(current_time - last_time).total_seconds():.1f}秒前)")
            return
    
    user_message_cache[user_id] = (message.content, current_time)
    
    # メッセージ処理統計を更新
    bot_stats['messages_processed'] += 1
    
    # 古いキャッシュを削除（メモリリーク防止）
    if len(processed_messages) > 1000:
        processed_messages.clear()
    if len(user_message_cache) > 100:
        user_message_cache.clear()

    # メッセージ統計の更新
    if not message.author.bot:
        if message.author.id not in member_stats_dict:
            member_stats_dict[message.author.id] = {
                'messages': 0,
                'last_active': datetime.now(),
                'join_date': message.author.joined_at or datetime.now()
            }
        member_stats_dict[message.author.id]['messages'] += 1
        member_stats_dict[message.author.id]['last_active'] = datetime.now()
    
    # コマンドを最初に処理（重複防止のため）
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return
    
    # ボットがメンションされた場合の処理
    if bot.user.mentioned_in(message) and not message.mention_everyone:
        # メンションを除いたメッセージ内容を取得
        content = message.content
        # ボットのメンションを削除
        content = content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
        
        # 空のメッセージの場合はデフォルト応答
        if not content:
            content = "こんにちは！何かお手伝いできることはありますか？"
        
        # タイピング表示
        async with message.channel.typing():
            try:
                # Gemini AIに質問
                model = genai.GenerativeModel('gemini-1.5-flash')
                
                # 会話履歴を取得
                channel_id = message.channel.id
                history = conversation_history.get(channel_id, [])
                
                # プロンプトを作成（会話履歴を含む）
                if history:
                    context = "\n".join([f"{h['user']}: {h['message']}" for h in history[-5:]])  # 最新5件
                    prompt = f"以下は最近の会話履歴です：\n{context}\n\n現在の質問: {content}\n\n日本語で自然に答えてください。"
                else:
                    prompt = f"{content}\n\n日本語で自然に答えてください。"
                
                response = model.generate_content(prompt)
                
                # 応答が空でない場合のみ送信
                if response.text:
                    # 長すぎる場合は分割
                    if len(response.text) > 2000:
                        chunks = [response.text[i:i+2000] for i in range(0, len(response.text), 2000)]
                        for chunk in chunks:
                            await message.reply(chunk)
                    else:
                        await message.reply(response.text)
                    
                    # 会話履歴に追加
                    if channel_id not in conversation_history:
                        conversation_history[channel_id] = []
                    
                    conversation_history[channel_id].append({
                        'user': message.author.display_name,
                        'message': content,
                        'timestamp': datetime.now(),
                        'response': response.text
                    })
                    
                    # 履歴が長すぎる場合は古いものを削除
                    if len(conversation_history[channel_id]) > MAX_HISTORY_LENGTH:
                        conversation_history[channel_id] = conversation_history[channel_id][-MAX_HISTORY_LENGTH:]
                else:
                    await message.reply("すみません、応答を生成できませんでした。")
                    
            except Exception as e:
                await message.reply(f"申し訳ありません、エラーが発生しました: {str(e)}")
                print(f"Gemini APIエラー: {e}")
        return
    
    # チーム分けリクエストを検出（コマンドでない場合のみ）
    team_keywords = ['チーム分けし', 'チーム分け', 'チーム作', 'チームわ', 'team分', 'team作', 'チーム分けて', 'チーム決めて', 'チーム決め']
    # コマンドでない場合のみ自然言語検出を実行
    if (any(keyword in message.content for keyword in team_keywords) and 
        len(message.content) > 3 and 
        not message.content.startswith('!')):
        await handle_team_request(message)
        return
    
    # その他のメッセージ処理（コマンドは既に133行目で処理済み）
    # 通常のメッセージのみ（コマンド以外）なので、bot.process_commands()は不要

async def handle_team_request(message):
    """チーム分けリクエストの自動処理"""
    try:
        # 実行中チェック
        if message.author.id in command_executing and command_executing[message.author.id] == 'auto_team':
            await message.reply("⚠️ 自動チーム分けが既に実行中です。少しお待ちください。")
            return
        
        # 実行中フラグを設定
        command_executing[message.author.id] = 'auto_team'
        
        # レート制限チェック
        allowed, wait_time = check_rate_limit(message.author.id)
        if not allowed:
            command_executing.pop(message.author.id, None)  # フラグをクリア
            await message.reply(f"⏰ 少し待ってください。あと{wait_time:.1f}秒後に再度お試しください。")
            return
        
        # リクエスト時刻を記録
        user_last_request[message.author.id] = datetime.now()
        
        # 即座にチーム分けを実行
        guild = message.guild
        if not guild:
            await message.reply("❌ このコマンドはサーバー内でのみ使用できます。")
            return
        
        # オンラインの人間メンバーを取得
        online_members = []
        for member in guild.members:
            if not member.bot and member.status != discord.Status.offline:
                online_members.append(member)
        
        # 全メンバー（オフライン含む）
        all_human_members = [member for member in guild.members if not member.bot]
        
        if len(online_members) < 2:
            if len(all_human_members) >= 2:
                members_to_use = all_human_members
                status_note = "（全メンバー対象）"
            else:
                await message.reply("❌ チーム分けには最低2人のメンバーが必要です。")
                return
        else:
            members_to_use = online_members
            status_note = "（オンラインメンバー対象）"
        
        # メンバーをランダムシャッフル
        shuffled_members = members_to_use.copy()
        random.shuffle(shuffled_members)
        
        # チーム分け結果の作成
        member_count = len(shuffled_members)
        embed = discord.Embed(title="🎯 チーム分け結果", color=0x00ff00)
        
        if member_count == 2:
            # 1v1
            embed.add_field(
                name="🔴 プレイヤー1",
                value=f"• {shuffled_members[0].display_name}",
                inline=True
            )
            embed.add_field(
                name="🔵 プレイヤー2", 
                value=f"• {shuffled_members[1].display_name}",
                inline=True
            )
            embed.set_footer(text=f"自動選択: 1v1形式 {status_note}")
        elif member_count >= 3:
            # 2v1以上
            team_size = member_count // 2
            team1 = shuffled_members[:team_size]
            team2 = shuffled_members[team_size:team_size*2]
            
            embed.add_field(
                name=f"🔴 チーム1 ({len(team1)}人)",
                value="\n".join([f"• {m.display_name}" for m in team1]),
                inline=True
            )
            embed.add_field(
                name=f"🔵 チーム2 ({len(team2)}人)",
                value="\n".join([f"• {m.display_name}" for m in team2]),
                inline=True
            )
            
            if len(shuffled_members) > team_size * 2:
                extras = shuffled_members[team_size*2:]
                embed.add_field(
                    name="⚪ 待機",
                    value="\n".join([f"• {m.display_name}" for m in extras]),
                    inline=False
                )
            
            embed.set_footer(text=f"自動選択: {len(team1)}v{len(team2)}形式 {status_note}")
        
        # 統計情報を追加
        status_info = f"対象: {len(members_to_use)}人 (オンライン: {len(online_members)}人)"
        embed.add_field(name="📊 情報", value=status_info, inline=False)
        
        await message.reply(embed=embed)
        
    except Exception as e:
        await message.reply(f"❌ チーム分けでエラーが発生しました: {str(e)}")
        print(f"チーム分けエラー: {e}")
    finally:
        # 実行中フラグをクリア
        command_executing.pop(message.author.id, None)

@bot.command(name='hello', help='挨拶をします')
@prevent_duplicate_execution
async def hello(ctx):
    """簡単な挨拶コマンド"""
    await ctx.send(f'こんにちは、{ctx.author.mention}さん！')

@bot.command(name='ping', help='Botの応答速度を確認します')
@prevent_duplicate_execution
async def ping(ctx):
    """Pingコマンド - Botのレイテンシを表示"""
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! レイテンシ: {latency}ms')

@bot.command(name='help', aliases=['commands'], help='利用可能なコマンド一覧を表示')
@prevent_duplicate_execution
async def show_commands(ctx):
    """利用可能なコマンドを表示"""
    
    embed = discord.Embed(title="🤖 リオンのコマンド一覧", color=0x00ff00)
    
    # 基本コマンド
    basic_commands = [
        "!hello - 挨拶メッセージ",
        "!ping - 応答速度確認",
        "!info - サーバー情報",
        "!members - メンバー統計",
        "!channels - チャンネル情報",
        "!userinfo [@ユーザー] - ユーザー情報",
        "!mystats [@ユーザー] - メンバー統計情報",
        "!dice [面数] - サイコロを振る"
    ]
    
    embed.add_field(
        name="📝 基本コマンド",
        value="\n".join(basic_commands),
        inline=False
    )
    
    # チーム分けコマンド
    team_commands = [
        "!team - 自動チーム分け",
        "!team 2v1 - 2対1のチーム分け", 
        "!team 3v3 - 3対3のチーム分け",
        "!team 2v2 - 2対2のチーム分け",
        "!team 1v1 - 1対1のチーム分け",
        "!team 4v4 - 4対4のチーム分け",
        "!team 5v5 - 5対5のチーム分け",
        "!qt [形式] - クイックチーム分け",
        "!vc_team [形式] - VC内メンバーでチーム分け",
        "!vct [形式] - VC専用チーム分け（短縮版）"
    ]
    
    embed.add_field(
        name="🎯 チーム分けコマンド",
        value="\n".join(team_commands),
        inline=False
    )
    
    # AIコマンド
    ai_commands = [
        "!ai [質問] - AI会話",
        "!expert [質問] - 専門的な回答",
        "!creative [プロンプト] - 創作的な回答",
        "!translate [テキスト] - 翻訳",
        "!summarize [テキスト] - 要約",
        "!history - 会話履歴表示",
        "!clear_history - 会話履歴クリア",
        "!usage - AI使用量と制限情報"
    ]
    
    embed.add_field(
        name="🧠 AIコマンド",
        value="\n".join(ai_commands),
        inline=False
    )
    
    # VALORANTコマンド
    valorant_commands = [
        "!valorant [RiotID#Tag] - VALORANT統計表示",
        "!valorant_match [RiotID#Tag] - 試合履歴",
        "!map [数] - マップルーレット",
        "!maplist - 全マップ一覧",
        "!mapinfo [マップ名] - マップ詳細情報",
        "!rank - ランク管理システム",
        "!ranklist - 利用可能ランク一覧"
    ]
    
    embed.add_field(
        name="🎮 VALORANTコマンド",
        value="\n".join(valorant_commands),
        inline=False
    )
    
    # Bot管理コマンド
    admin_commands = [
        "!botstatus - Bot状態とパフォーマンス確認",
        "!cleanup - メモリクリーンアップ（管理者）",
        "!restart - Bot再起動（管理者）"
    ]
    
    embed.add_field(
        name="⚙️ Bot管理コマンド",
        value="\n".join(admin_commands),
        inline=False
    )
    
    # 自然な会話
    embed.add_field(
        name="💬 自然な会話",
        value="• @リオン + メッセージで自然な会話\n• 「チーム分けして」でチーム分け実行\n• 質問形式で自動応答",
        inline=False
    )
    
    # 現在登録されているコマンド数を表示
    command_count = len(bot.commands)
    embed.set_footer(text=f"登録済みコマンド数: {command_count}個 | 詳細: !help")
    
    await ctx.send(embed=embed)

@bot.command(name='info', help='詳細なサーバー情報を表示します')
@prevent_duplicate_execution
async def server_info(ctx):
    """詳細なサーバー情報を表示"""
    guild = ctx.guild
    if guild:
        # メンバー統計を計算
        total_members = guild.member_count
        online_members = sum(1 for member in guild.members if member.status != discord.Status.offline)
        bot_count = sum(1 for member in guild.members if member.bot)
        human_count = total_members - bot_count
        
        # チャンネル統計
        text_channels = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
        voice_channels = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
        categories = len([c for c in guild.channels if isinstance(c, discord.CategoryChannel)])
        
        # ロール数
        role_count = len(guild.roles) - 1  # @everyoneロールを除く
        
        # ブースト情報
        boost_level = guild.premium_tier
        boost_count = guild.premium_subscription_count or 0
        
        embed = discord.Embed(
            title=f"📊 サーバー情報: {guild.name}",
            color=discord.Color.blue(),
            timestamp=ctx.message.created_at
        )
        
        # 基本情報
        embed.add_field(name="🆔 サーバーID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="👑 オーナー", value=guild.owner.mention if guild.owner else "不明", inline=True)
        embed.add_field(name="📅 作成日", value=guild.created_at.strftime("%Y年%m月%d日"), inline=True)
        
        # メンバー情報
        embed.add_field(name="👥 総メンバー数", value=f"{total_members:,}人", inline=True)
        embed.add_field(name="🟢 オンライン", value=f"{online_members:,}人", inline=True)
        embed.add_field(name="👤 人間/🤖 Bot", value=f"{human_count:,}人 / {bot_count:,}体", inline=True)
        
        # チャンネル情報
        embed.add_field(name="💬 テキストチャンネル", value=f"{text_channels}個", inline=True)
        embed.add_field(name="🔊 ボイスチャンネル", value=f"{voice_channels}個", inline=True)
        embed.add_field(name="📁 カテゴリ", value=f"{categories}個", inline=True)
        
        # その他の情報
        embed.add_field(name="🎭 ロール数", value=f"{role_count}個", inline=True)
        embed.add_field(name="⭐ ブーストレベル", value=f"レベル {boost_level} ({boost_count}ブースト)", inline=True)
        embed.add_field(name="🛡️ 認証レベル", value=f"{guild.verification_level}".replace('_', ' ').title(), inline=True)
        
        # サーバーアイコンとバナー
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)
            
        embed.set_footer(text=f"情報取得者: {ctx.author.display_name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")

@bot.command(name='dice', help='サイコロを振ります（例: !dice 6）')
@prevent_duplicate_execution
async def roll_dice(ctx, sides: int = 6):
    """サイコロを振るコマンド"""
    import random
    
    if sides < 2:
        await ctx.send("サイコロの面数は2以上である必要があります。")
        return
    
    result = random.randint(1, sides)
    await ctx.send(f'🎲 {sides}面サイコロの結果: **{result}**')

@bot.command(name='userinfo', help='ユーザー情報を表示します')
@prevent_duplicate_execution
async def user_info(ctx, member: discord.Member = None):
    """ユーザー情報を表示"""
    if member is None:
        member = ctx.author
    
    embed = discord.Embed(
        title=f"ユーザー情報: {member.display_name}",
        color=member.color
    )
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    embed.add_field(name="ユーザー名", value=str(member), inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="アカウント作成日", value=member.created_at.strftime("%Y年%m月%d日"), inline=True)
    
    if ctx.guild and member.joined_at:
        embed.add_field(name="サーバー参加日", value=member.joined_at.strftime("%Y年%m月%d日"), inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='members', help='詳細なメンバー統計を表示します')
@prevent_duplicate_execution
async def member_stats(ctx):
    """詳細なメンバー統計を表示"""
    guild = ctx.guild
    if not guild:
        await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")
        return
    
    try:
        # 統計情報を収集
        total_members = guild.member_count
        
        # ステータス別カウント
        online = sum(1 for member in guild.members if member.status == discord.Status.online)
        idle = sum(1 for member in guild.members if member.status == discord.Status.idle)
        dnd = sum(1 for member in guild.members if member.status == discord.Status.dnd)
        offline = sum(1 for member in guild.members if member.status == discord.Status.offline)
        
        # Bot vs 人間
        bots = sum(1 for member in guild.members if member.bot)
        humans = total_members - bots
        
        # 最近参加したメンバー（上位5名）
        recent_members = sorted(guild.members, key=lambda m: m.joined_at or guild.created_at, reverse=True)[:5]
        
        # 管理者権限を持つメンバー
        admins = [member for member in guild.members if member.guild_permissions.administrator and not member.bot]
        
        embed = discord.Embed(
            title=f"👥 メンバー統計: {guild.name}",
            color=discord.Color.green(),
            timestamp=ctx.message.created_at
        )
        
        # メンバー数の詳細
        embed.add_field(name="📊 総メンバー数", value=f"**{total_members:,}**人", inline=True)
        embed.add_field(name="👤 人間", value=f"{humans:,}人", inline=True)
        embed.add_field(name="🤖 Bot", value=f"{bots:,}体", inline=True)
        
        # ステータス別統計
        embed.add_field(name="🟢 オンライン", value=f"{online:,}人", inline=True)
        embed.add_field(name="🟡 退席中", value=f"{idle:,}人", inline=True)
        embed.add_field(name="🔴 取り込み中", value=f"{dnd:,}人", inline=True)
        
        embed.add_field(name="⚫ オフライン", value=f"{offline:,}人", inline=True)
        embed.add_field(name="🛡️ 管理者", value=f"{len(admins):,}人", inline=True)
        embed.add_field(name="📈 アクティブ率", value=f"{((total_members - offline) / total_members * 100):.1f}%", inline=True)
        
        # 最近参加したメンバー
        if recent_members:
            recent_list = []
            for member in recent_members:
                join_date = member.joined_at.strftime("%m/%d") if member.joined_at else "不明"
                recent_list.append(f"• {member.display_name} ({join_date})")
            embed.add_field(name="🆕 最近の参加者", value="\n".join(recent_list), inline=False)
        
        # 管理者リスト（上位5名）
        if admins:
            admin_list = []
            for admin in admins[:5]:
                admin_list.append(f"• {admin.display_name}")
            if len(admins) > 5:
                admin_list.append(f"• ...他{len(admins) - 5}人")
            embed.add_field(name="👑 管理者", value="\n".join(admin_list), inline=False)
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            
        embed.set_footer(text=f"統計取得者: {ctx.author.display_name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ メンバー統計の取得中にエラーが発生しました: {str(e)}")
        print(f"メンバー統計エラー: {e}")

@bot.command(name='channels', help='チャンネル一覧と詳細を表示します')
@prevent_duplicate_execution
async def channel_info(ctx):
    """チャンネル情報を表示"""
    guild = ctx.guild
    if not guild:
        await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")
        return
    
    try:
        # チャンネル分類
        text_channels = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
        voice_channels = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
        categories = [c for c in guild.channels if isinstance(c, discord.CategoryChannel)]
        
        embed = discord.Embed(
            title=f"📁 チャンネル情報: {guild.name}",
            color=discord.Color.purple(),
            timestamp=ctx.message.created_at
        )
        
        # 統計情報
        embed.add_field(name="💬 テキストチャンネル", value=f"{len(text_channels)}個", inline=True)
        embed.add_field(name="🔊 ボイスチャンネル", value=f"{len(voice_channels)}個", inline=True)
        embed.add_field(name="📂 カテゴリ", value=f"{len(categories)}個", inline=True)
        
        # テキストチャンネル一覧（上位10個）
        if text_channels:
            text_list = []
            for channel in text_channels[:10]:
                text_list.append(f"• #{channel.name}")
            if len(text_channels) > 10:
                text_list.append(f"• ...他{len(text_channels) - 10}個")
            embed.add_field(name="💬 テキストチャンネル一覧", value="\n".join(text_list), inline=False)
        
        # ボイスチャンネル一覧（上位10個）
        if voice_channels:
            voice_list = []
            for channel in voice_channels[:10]:
                connected = len(channel.members) if hasattr(channel, 'members') else 0
                voice_list.append(f"• 🔊 {channel.name} ({connected}人)")
            if len(voice_channels) > 10:
                voice_list.append(f"• ...他{len(voice_channels) - 10}個")
            embed.add_field(name="🔊 ボイスチャンネル一覧", value="\n".join(voice_list), inline=False)
        
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            
        embed.set_footer(text=f"チャンネル情報取得者: {ctx.author.display_name}")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ チャンネル情報の取得中にエラーが発生しました: {str(e)}")
        print(f"チャンネル情報エラー: {e}")

def check_rate_limit(user_id):
    """レート制限チェック"""
    now = datetime.now()
    if user_id in user_last_request:
        time_diff = (now - user_last_request[user_id]).total_seconds()
        if time_diff < RATE_LIMIT_SECONDS:
            return False, RATE_LIMIT_SECONDS - time_diff
    return True, 0

@bot.command(name='ai', help='Gemini AIと会話します（例: !ai こんにちは）')
@prevent_duplicate_execution
async def ask_ai(ctx, *, question):
    """Gemini AIに質問するコマンド"""
    try:
        # レート制限チェック  
        allowed, wait_time = check_rate_limit(ctx.author.id)
        if not allowed:
            await ctx.send(f"⏰ 少し待ってください。あと{wait_time:.1f}秒後に再度お試しください。")
            return
            
        # 処理中メッセージを送信
        thinking_msg = await ctx.send("🤔 考え中...")
        
        # リクエスト時刻を記録
        user_last_request[ctx.author.id] = datetime.now()
        
        # 過去の会話履歴を取得
        channel_id = ctx.channel.id
        if channel_id not in conversation_history:
            conversation_history[channel_id] = []
        
        recent_history = conversation_history[channel_id][-3:] if conversation_history[channel_id] else []
        history_text = ""
        if recent_history:
            history_text = "\n\n【最近の会話履歴】\n" + "\n".join(recent_history)
        
        # サーバー情報を詳細取得
        guild = ctx.guild
        server_context = ""
        if guild:
            total_members = guild.member_count
            server_name = guild.name
            
            # メンバー一覧を取得
            members_list = []
            try:
                member_count = 0
                for member in guild.members:
                    if not member.bot:  # Bot以外の人間メンバー
                        members_list.append(f"• {member.display_name} ({member.name})")
                        member_count += 1
                    if member_count >= 15:  # 最大15人まで
                        break
                
                if not members_list:
                    members_list = ["※メンバー情報の取得にはServer Members Intentが必要です"]
                    
            except Exception as e:
                members_list = [f"メンバー取得エラー: {str(e)}"]
            
            # チャンネル情報
            text_channels = [f"#{ch.name}" for ch in guild.channels if hasattr(ch, 'name') and not str(ch.type).startswith('voice')][:8]
            
            server_context = f"""

【詳細サーバー情報】
🏷️ サーバー名: {server_name}
👥 総メンバー数: {total_members}人
🆔 ID: {guild.id}
📅 作成: {guild.created_at.strftime("%Y年%m月%d日")}

👤 メンバー:
{chr(10).join(members_list)}

💬 チャンネル:
{chr(10).join([f"• {ch}" for ch in text_channels])}
"""
        
        # Gemini AIモデルを初期化（軽量版・制限緩和）
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # 生成設定（高品質設定）
        generation_config = genai.types.GenerationConfig(
            temperature=0.7,  # 創造性レベル（0.0-1.0）
            top_p=0.8,        # 語彙の多様性
            top_k=40,         # 候補数制限
            max_output_tokens=2048,  # 最大出力トークン数
        )
        
        # サーバー情報と履歴を含めた質問をGemini AIに送信
        enhanced_question = f"""
        {question}{server_context}{history_text}
        
        指示：
        - 質問に直接答える
        - サーバー情報について聞かれた場合は、上記の具体的な数字を使って回答
        - 過去の会話履歴がある場合は文脈を理解して返答
        - 定型文や決まり文句は使わない
        - 簡潔で自然な日本語で回答
        - 「ちなみに〜」「他に何か〜」などの定型文は絶対に使わない
        """
        response = model.generate_content(enhanced_question, generation_config=generation_config)
        
        # 応答が長すぎる場合は分割
        if len(response.text) > 2000:
            # Discordの文字数制限（2000文字）に合わせて分割
            chunks = [response.text[i:i+1900] for i in range(0, len(response.text), 1900)]
            await thinking_msg.delete()
            
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"🤖 Gemini AI の回答 ({i+1}/{len(chunks)})",
                    description=chunk,
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"質問者: {ctx.author.display_name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)
                await ctx.send(embed=embed)
        else:
            # 通常の応答
            embed = discord.Embed(
                title="🤖 Gemini AI の回答",
                description=response.text,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"質問者: {ctx.author.display_name}", icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url)
            await thinking_msg.edit(content="", embed=embed)
        
        # 会話履歴に追加
        user_name = ctx.author.display_name
        bot_response = response.text[:100] + "..." if len(response.text) > 100 else response.text
        conversation_history[channel_id].append(f"{user_name}: {question}")
        conversation_history[channel_id].append(f"リオン: {bot_response}")
        
        # 履歴が長すぎる場合は古いものを削除
        if len(conversation_history[channel_id]) > MAX_HISTORY_LENGTH * 2:
            conversation_history[channel_id] = conversation_history[channel_id][-MAX_HISTORY_LENGTH * 2:]
            
    except Exception as e:
        await thinking_msg.edit(content=f"❌ エラーが発生しました: {str(e)}")
        print(f"Gemini AI エラー: {e}")

@bot.command(name='translate', help='テキストを翻訳します（例: !translate Hello）')
@prevent_duplicate_execution
async def translate_text(ctx, *, text):
    """テキスト翻訳コマンド"""
    try:
        thinking_msg = await ctx.send("🌐 翻訳中...")
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"以下のテキストを日本語に翻訳してください。もし既に日本語の場合は英語に翻訳してください: {text}"
        
        response = model.generate_content(prompt)
        
        embed = discord.Embed(
            title="🌐 翻訳結果",
            color=discord.Color.green()
        )
        embed.add_field(name="原文", value=text[:1000], inline=False)
        embed.add_field(name="翻訳", value=response.text[:1000], inline=False)
        embed.set_footer(text=f"翻訳者: {ctx.author.display_name}")
        
        await thinking_msg.edit(content="", embed=embed)
        
    except Exception as e:
        await thinking_msg.edit(content=f"❌ 翻訳エラー: {str(e)}")

@bot.command(name='summarize', help='テキストを要約します（例: !summarize 長いテキスト...）')
@prevent_duplicate_execution
async def summarize_text(ctx, *, text):
    """テキスト要約コマンド"""
    try:
        thinking_msg = await ctx.send("📝 要約中...")
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = f"以下のテキストを分かりやすく要約してください（日本語で回答）: {text}"
        
        response = model.generate_content(prompt)
        
        embed = discord.Embed(
            title="📝 要約結果",
            description=response.text,
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"要約依頼者: {ctx.author.display_name}")
        
        await thinking_msg.edit(content="", embed=embed)
        
    except Exception as e:
        await thinking_msg.edit(content=f"❌ 要約エラー: {str(e)}")

@bot.command(name='expert', help='専門的な質問に詳しく回答します（例: !expert 量子コンピュータについて）')
@prevent_duplicate_execution
async def expert_mode(ctx, *, question):
    """エキスパートモード - より詳細で専門的な回答"""
    try:
        thinking_msg = await ctx.send("🎓 専門家として考え中...")
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # エキスパート用の詳細設定
        expert_config = genai.types.GenerationConfig(
            temperature=0.3,  # 正確性重視
            top_p=0.9,
            top_k=50,
            max_output_tokens=4096,  # より長い回答
        )
        
        # 専門的なプロンプト
        expert_prompt = f"""
        あなたは専門知識を持つエキスパートです。以下の質問に対して、詳細で正確な回答をしてください：
        
        質問: {question}
        
        回答の形式:
        1. 概要説明
        2. 詳細な解説
        3. 具体例（可能であれば）
        4. 関連する重要なポイント
        
        日本語で分かりやすく、かつ専門的に回答してください。
        """
        
        response = model.generate_content(expert_prompt, generation_config=expert_config)
        
        # 長い回答の場合は分割
        if len(response.text) > 2000:
            chunks = [response.text[i:i+1900] for i in range(0, len(response.text), 1900)]
            await thinking_msg.delete()
            
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"🎓 エキスパート回答 ({i+1}/{len(chunks)})",
                    description=chunk,
                    color=discord.Color.gold()
                )
                embed.set_footer(text=f"専門分野の質問者: {ctx.author.display_name}")
                await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="🎓 エキスパート回答",
                description=response.text,
                color=discord.Color.gold()
            )
            embed.set_footer(text=f"専門分野の質問者: {ctx.author.display_name}")
            await thinking_msg.edit(content="", embed=embed)
            
    except Exception as e:
        await thinking_msg.edit(content=f"❌ エキスパートモードエラー: {str(e)}")

@bot.command(name='creative', help='創作や想像力を使った回答をします（例: !creative 未来の世界を描いて）')
@prevent_duplicate_execution
async def creative_mode(ctx, *, prompt):
    """クリエイティブモード - 創造性重視の回答"""
    try:
        thinking_msg = await ctx.send("🎨 創作中...")
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # クリエイティブ用設定
        creative_config = genai.types.GenerationConfig(
            temperature=0.9,  # 最大創造性
            top_p=0.95,
            top_k=60,
            max_output_tokens=3072,
        )
        
        # 創造的なプロンプト
        creative_prompt = f"""
        あなたは創造性豊かなクリエイターです。以下のテーマについて、想像力を働かせて魅力的で独創的な内容を作成してください：
        
        テーマ: {prompt}
        
        自由な発想で、面白く、印象的な内容にしてください。日本語で回答してください。
        """
        
        response = model.generate_content(creative_prompt, generation_config=creative_config)
        
        # 長い回答の場合は分割
        if len(response.text) > 2000:
            chunks = [response.text[i:i+1900] for i in range(0, len(response.text), 1900)]
            await thinking_msg.delete()
            
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"🎨 クリエイティブ作品 ({i+1}/{len(chunks)})",
                    description=chunk,
                    color=discord.Color.purple()
                )
                embed.set_footer(text=f"クリエイター: {ctx.author.display_name}")
                await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="🎨 クリエイティブ作品",
                description=response.text,
                color=discord.Color.purple()
            )
            embed.set_footer(text=f"クリエイター: {ctx.author.display_name}")
            await thinking_msg.edit(content="", embed=embed)
            
    except Exception as e:
        await thinking_msg.edit(content=f"❌ クリエイティブモードエラー: {str(e)}")

@bot.command(name='history', help='このチャンネルの会話履歴を表示します')
@prevent_duplicate_execution
async def show_history(ctx):
    """会話履歴を表示"""
    channel_id = ctx.channel.id
    
    if channel_id not in conversation_history or not conversation_history[channel_id]:
        await ctx.send("📝 このチャンネルには会話履歴がありません。")
        return
    
    history = conversation_history[channel_id]
    
    embed = discord.Embed(
        title="📝 会話履歴",
        color=discord.Color.gold(),
        timestamp=ctx.message.created_at
    )
    
    # 履歴を文字列として整理
    history_text = "\n".join(history[-10:])  # 最新10件
    
    if len(history_text) > 4000:
        # 長すぎる場合は分割
        chunks = [history_text[i:i+1900] for i in range(0, len(history_text), 1900)]
        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📝 会話履歴 ({i+1}/{len(chunks)})",
                description=chunk,
                color=discord.Color.gold()
            )
            await ctx.send(embed=embed)
    else:
        embed.description = history_text
        await ctx.send(embed=embed)

@bot.command(name='clear_history', help='このチャンネルの会話履歴をクリアします')
@prevent_duplicate_execution
async def clear_history(ctx):
    """会話履歴をクリア"""
    channel_id = ctx.channel.id
    
    if channel_id in conversation_history:
        conversation_history[channel_id] = []
        await ctx.send("🗑️ このチャンネルの会話履歴をクリアしました。")
    else:
        await ctx.send("📝 このチャンネルには会話履歴がありません。")

@bot.command(name='usage', help='AI使用量と制限情報を表示します')
@prevent_duplicate_execution
async def show_usage(ctx):
    """AI使用量情報を表示"""
    embed = discord.Embed(
        title="🔍 AI使用量情報",
        color=discord.Color.blue()
    )
    
    # 現在のレート制限状況
    user_id = ctx.author.id
    if user_id in user_last_request:
        last_time = user_last_request[user_id]
        time_diff = (datetime.now() - last_time).total_seconds()
        if time_diff < RATE_LIMIT_SECONDS:
            wait_time = RATE_LIMIT_SECONDS - time_diff
            embed.add_field(
                name="⏰ 次回利用可能まで", 
                value=f"{wait_time:.1f}秒", 
                inline=True
            )
        else:
            embed.add_field(
                name="✅ 利用状況", 
                value="すぐに利用可能", 
                inline=True
            )
    else:
        embed.add_field(
            name="✅ 利用状況", 
            value="すぐに利用可能", 
            inline=True
        )
    
    embed.add_field(
        name="📊 制限情報",
        value=f"• 1ユーザーあたり{RATE_LIMIT_SECONDS}秒間隔\n• 軽量モデル使用中（制限緩和）",
        inline=False
    )
    
    embed.add_field(
        name="💡 ヒント",
        value="• 短時間に多数のリクエストを避ける\n• 長すぎる文章は分割する\n• エラー時は少し待ってから再試行",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='botstatus', help='Botの状態とパフォーマンスを表示します')
@prevent_duplicate_execution
async def bot_status(ctx):
    """Botの状態を表示"""
    try:
        current_time = datetime.now()
        uptime = current_time - bot_stats['start_time']
        
        # メモリ使用量を取得
        process = psutil.Process()
        memory_usage = process.memory_info().rss / 1024 / 1024  # MB
        cpu_usage = process.cpu_percent()
        
        embed = discord.Embed(
            title="🤖 Bot ステータス",
            color=discord.Color.green() if bot_stats['errors_count'] < 10 else discord.Color.orange(),
            timestamp=current_time
        )
        
        # 稼働時間
        embed.add_field(
            name="⏰ 稼働時間",
            value=f"{uptime.days}日 {uptime.seconds//3600}時間 {(uptime.seconds%3600)//60}分",
            inline=True
        )
        
        # パフォーマンス
        embed.add_field(
            name="💾 メモリ使用量",
            value=f"{memory_usage:.1f} MB",
            inline=True
        )
        
        embed.add_field(
            name="🖥️ CPU使用率",
            value=f"{cpu_usage:.1f}%",
            inline=True
        )
        
        # 統計情報
        embed.add_field(
            name="📊 処理統計",
            value=f"実行コマンド: {bot_stats['commands_executed']:,}回\n"
                  f"処理メッセージ: {bot_stats['messages_processed']:,}件\n"
                  f"エラー回数: {bot_stats['errors_count']:,}回",
            inline=False
        )
        
        # 接続情報
        embed.add_field(
            name="🌐 接続情報",
            value=f"レイテンシ: {round(bot.latency * 1000)}ms\n"
                  f"サーバー数: {len(bot.guilds)}\n"
                  f"総ユーザー数: {len(bot.users):,}人",
            inline=False
        )
        
        # 最新エラー（あれば）
        if bot_stats['last_error']:
            embed.add_field(
                name="⚠️ 最新エラー",
                value=f"```{bot_stats['last_error'][:100]}...```",
                inline=False
            )
        
        # キャッシュ状況
        embed.add_field(
            name="🗄️ キャッシュ状況",
            value=f"処理済みメッセージ: {len(processed_messages)}\n"
                  f"ユーザーキャッシュ: {len(user_message_cache)}\n"
                  f"会話履歴: {len(conversation_history)}チャンネル\n"
                  f"実行中コマンド: {len(command_executing)}",
            inline=False
        )
        
        embed.set_footer(text=f"起動時刻: {bot_stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}")
        
        await ctx.send(embed=embed)
        
    except ImportError:
        # psutil がない場合の簡易版
        uptime = datetime.now() - bot_stats['start_time']
        
        embed = discord.Embed(
            title="🤖 Bot ステータス（簡易版）",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="⏰ 稼働時間",
            value=f"{uptime.days}日 {uptime.seconds//3600}時間 {(uptime.seconds%3600)//60}分",
            inline=False
        )
        
        embed.add_field(
            name="📊 処理統計",
            value=f"実行コマンド: {bot_stats['commands_executed']:,}回\n"
                  f"エラー回数: {bot_stats['errors_count']:,}回",
            inline=False
        )
        
        embed.add_field(
            name="🌐 接続情報",
            value=f"レイテンシ: {round(bot.latency * 1000)}ms\n"
                  f"サーバー数: {len(bot.guilds)}",
            inline=False
        )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ ステータス取得エラー: {str(e)}")

@bot.command(name='cleanup', help='手動でメモリクリーンアップを実行します（管理者用）')
@prevent_duplicate_execution
async def manual_cleanup(ctx):
    """手動メモリクリーンアップ"""
    # 管理者権限チェック
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ このコマンドは管理者のみ使用できます。")
        return
    
    try:
        # クリーンアップ前の状態
        before_processed = len(processed_messages)
        before_cache = len(user_message_cache)
        before_history = len(conversation_history)
        
        cleanup_memory()
        
        # クリーンアップ後の状態
        after_processed = len(processed_messages)
        after_cache = len(user_message_cache)
        after_history = len(conversation_history)
        
        embed = discord.Embed(
            title="🧹 メモリクリーンアップ完了",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="📊 クリーンアップ結果",
            value=f"処理済みメッセージ: {before_processed} → {after_processed}\n"
                  f"ユーザーキャッシュ: {before_cache} → {after_cache}\n"
                  f"会話履歴: {before_history} → {after_history}",
            inline=False
        )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ クリーンアップエラー: {str(e)}")

@bot.command(name='restart', help='Botを再起動します（管理者用）')
@prevent_duplicate_execution
async def restart_bot(ctx):
    """Bot再起動コマンド（管理者用）"""
    # 管理者権限チェック
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ このコマンドは管理者のみ使用できます。")
        return
    
    try:
        await ctx.send("🔄 Botを再起動しています...")
        
        # 統計情報を更新
        bot_stats['restart_count'] += 1
        
        # ログ出力
        print(f"🔄 管理者 {ctx.author} によりBot再起動が要求されました")
        print(f"📊 再起動回数: {bot_stats['restart_count']}")
        
        # 安全な再起動処理
        await bot.close()
        
    except Exception as e:
        await ctx.send(f"❌ 再起動エラー: {str(e)}")
        print(f"再起動エラー: {e}")

# VALORANT統計機能
async def get_valorant_stats(riot_id, tag):
    """VALORANT統計を取得"""
    if not TRACKER_API_KEY:
        return None, "Tracker.gg API Keyが設定されていません。"
    
    headers = {
        "TRN-Api-Key": TRACKER_API_KEY,
        "User-Agent": "Discord Bot"
    }
    
    url = f"{TRACKER_BASE_URL}/standard/profile/riot/{riot_id}%23{tag}"
    
    try:
        timeout = aiohttp.ClientTimeout(total=10)  # 10秒のタイムアウト
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                elif response.status == 404:
                    return None, "プレイヤーが見つかりません。Riot ID#Tagを確認してください。"
                elif response.status == 429:
                    return None, "API制限に達しています。しばらく待ってから再試行してください。"
                elif response.status == 403:
                    return None, "API認証エラー: API Keyを確認してください。"
                else:
                    return None, f"API エラー: {response.status}"
    except asyncio.TimeoutError:
        return None, "タイムアウト: サーバーへの接続がタイムアウトしました。"
    except aiohttp.ClientConnectorError:
        return None, "接続エラー: インターネット接続を確認してください。"
    except Exception as e:
        return None, f"接続エラー: {str(e)}"

@bot.command(name='valorant', help='VALORANT統計を表示します（例: !valorant PlayerName#1234）')
@prevent_duplicate_execution
async def valorant_stats(ctx, *, riot_id=None):
    """VALORANT統計表示コマンド"""
    if not riot_id:
        embed = discord.Embed(
            title="❌ 使用方法",
            description="**使用方法:** `!valorant RiotID#Tag`\n**例:** `!valorant SamplePlayer#1234`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    if '#' not in riot_id:
        embed = discord.Embed(
            title="❌ フォーマットエラー",
            description="Riot IDは `名前#タグ` の形式で入力してください。\n**例:** `SamplePlayer#1234`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    try:
        # Riot IDとタグを分離
        username, tag = riot_id.split('#', 1)
        
        # 取得中メッセージ
        loading_msg = await ctx.send("🔍 VALORANT統計を取得中...")
        
        # API呼び出し
        data, error = await get_valorant_stats(username, tag)
        
        if error:
            await loading_msg.edit(content=f"❌ {error}")
            return
        
        # データ解析
        profile = data.get('data', {})
        platform_info = profile.get('platformInfo', {})
        user_info = profile.get('userInfo', {})
        segments = profile.get('segments', [])
        
        # メイン統計（Overview）
        overview = None
        for segment in segments:
            if segment.get('type') == 'overview':
                overview = segment
                break
        
        if not overview:
            await loading_msg.edit(content="❌ 統計データが見つかりません。")
            return
        
        stats = overview.get('stats', {})
        
        # Embed作成
        embed = discord.Embed(
            title=f"🎯 VALORANT 統計: {platform_info.get('platformUserHandle', riot_id)}",
            color=discord.Color.red()  # VALORANTテーマカラー
        )
        
        # プロフィール情報
        if user_info.get('avatarUrl'):
            embed.set_thumbnail(url=user_info['avatarUrl'])
        
        # ランク情報
        rank_info = stats.get('rank', {})
        if rank_info:
            rank_name = rank_info.get('displayValue', 'Unranked')
            rank_icon = rank_info.get('displayIcon')
            embed.add_field(
                name="🏆 現在のランク",
                value=rank_name,
                inline=True
            )
            if rank_icon:
                embed.set_author(name="Current Rank", icon_url=rank_icon)
        
        # Peak Rank（最高ランク）
        peak_rank = stats.get('peakRank', {})
        if peak_rank:
            embed.add_field(
                name="⭐ 最高ランク",
                value=peak_rank.get('displayValue', 'Unknown'),
                inline=True
            )
        
        # 基本統計
        if stats.get('kills'):
            embed.add_field(
                name="💀 Total Kills",
                value=f"{stats['kills']['displayValue']:,}",
                inline=True
            )
        
        if stats.get('deaths'):
            embed.add_field(
                name="☠️ Total Deaths", 
                value=f"{stats['deaths']['displayValue']:,}",
                inline=True
            )
        
        if stats.get('kDRatio'):
            embed.add_field(
                name="📊 K/D Ratio",
                value=stats['kDRatio']['displayValue'],
                inline=True
            )
        
        if stats.get('timePlayed'):
            embed.add_field(
                name="⏰ プレイ時間",
                value=stats['timePlayed']['displayValue'],
                inline=True
            )
        
        if stats.get('matchesPlayed'):
            embed.add_field(
                name="🎮 総試合数",
                value=f"{stats['matchesPlayed']['displayValue']:,}",
                inline=True
            )
        
        if stats.get('wins'):
            embed.add_field(
                name="🏅 勝利数",
                value=f"{stats['wins']['displayValue']:,}",
                inline=True
            )
        
        # Win Rate計算
        if stats.get('wins') and stats.get('matchesPlayed'):
            wins = stats['wins']['value']
            matches = stats['matchesPlayed']['value']
            if matches > 0:
                win_rate = (wins / matches) * 100
                embed.add_field(
                    name="📈 勝率",
                    value=f"{win_rate:.1f}%",
                    inline=True
                )
        
        # ヘッドショット率
        if stats.get('headshotPct'):
            embed.add_field(
                name="🎯 ヘッドショット率",
                value=stats['headshotPct']['displayValue'],
                inline=True
            )
        
        # 平均ダメージ
        if stats.get('damagePerRound'):
            embed.add_field(
                name="💥 ラウンド平均ダメージ",
                value=stats['damagePerRound']['displayValue'],
                inline=True
            )
        
        # フッター
        embed.set_footer(
            text=f"データ提供: Tracker.gg | リクエスト者: {ctx.author.display_name}",
            icon_url="https://trackercdn.com/cdn/tracker.gg/favicon.ico"
        )
        
        await loading_msg.edit(content="", embed=embed)
        
    except ValueError:
        await ctx.send("❌ Riot IDの形式が正しくありません。`名前#タグ`の形式で入力してください。")
    except Exception as e:
        await loading_msg.edit(content=f"❌ エラーが発生しました: {str(e)}")

@bot.command(name='valorant_match', help='直近のVALORANT試合履歴を表示します（例: !valorant_match PlayerName#1234）')
@prevent_duplicate_execution
async def valorant_matches(ctx, *, riot_id=None):
    try:
        if not riot_id:
            await ctx.send("❌ Riot IDを指定してください。例: `!valorant_match PlayerName#1234`")
            return
        
        # Riot IDをパース
        if '#' not in riot_id:
            await ctx.send("❌ 正しい形式で入力してください。例: `PlayerName#1234`")
            return
        
        name, tag = riot_id.split('#', 1)
        
        # Typing開始
        async with ctx.typing():
            # プレイヤーIDを取得
            headers = {"TRN-Api-Key": TRACKER_API_KEY}
            
            # プレイヤーIDを取得
            search_url = f"{TRACKER_BASE_URL}/profile/riot/{name}/{tag}"
            
            timeout = aiohttp.ClientTimeout(total=15)  # 15秒のタイムアウト
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(search_url, headers=headers) as response:
                    if response.status != 200:
                        await ctx.send(f"❌ プレイヤー '{riot_id}' が見つかりませんでした。")
                        return
                    
                    data = await response.json()
                    
                    # 試合履歴を取得
                    matches_url = f"{TRACKER_BASE_URL}/profile/riot/{name}/{tag}/matches"
                    
                    async with session.get(matches_url, headers=headers) as matches_response:
                        if matches_response.status != 200:
                            await ctx.send("❌ 試合履歴の取得に失敗しました。")
                            return
                        
                        matches_data = await matches_response.json()
                        
                        if not matches_data.get('data'):
                            await ctx.send("❌ 試合履歴が見つかりませんでした。")
                            return
                        
                        # 直近5試合を表示
                        matches = matches_data['data'][:5]
                        
                        embed = discord.Embed(
                            title=f"🎯 {name}#{tag} の直近試合履歴",
                            color=0xff4654
                        )
                        
                        for i, match in enumerate(matches, 1):
                            metadata = match.get('metadata', {})
                            segments = match.get('segments', [])
                            
                            if not segments:
                                continue
                            
                            player_stats = segments[0].get('stats', {})
                            
                            # 試合結果
                            result = "勝利 🏆" if metadata.get('result', {}).get('outcome') == 'victory' else "敗北 💀"
                            
                            # 基本情報
                            map_name = metadata.get('mapName', '不明')
                            mode_name = metadata.get('modeName', '不明')
                            
                            # スコア
                            kills = player_stats.get('kills', {}).get('value', 0)
                            deaths = player_stats.get('deaths', {}).get('value', 0)
                            assists = player_stats.get('assists', {}).get('value', 0)
                            
                            # KD比
                            kd_ratio = round(kills / max(deaths, 1), 2)
                            
                            # 日時
                            match_date = metadata.get('timestamp')
                            if match_date:
                                match_time = datetime.fromisoformat(match_date.replace('Z', '+00:00'))
                                time_str = match_time.strftime('%m/%d %H:%M')
                            else:
                                time_str = '不明'
                            
                            embed.add_field(
                                name=f"試合 #{i} - {result}",
                                value=f"🗺️ **{map_name}** ({mode_name})\n"
                                      f"📊 **K/D/A:** {kills}/{deaths}/{assists} (KD: {kd_ratio})\n"
                                      f"⏰ **日時:** {time_str}",
                                inline=False
                            )
                            
                            embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━━━", inline=False)
                        
                        embed.set_footer(text="📈 VALORANT統計 by Tracker.gg")
                        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ エラーが発生しました: {str(e)}")

@bot.command(name='team', help='メンバーをランダムでチーム分けします（例: !team 2v1, !team 3v3, !team）')
@prevent_duplicate_execution
async def team_divide(ctx, format_type=None):
    """チーム分け機能"""
    try:
        # レート制限チェック
        user_id = ctx.author.id
        allowed, wait_time = check_rate_limit(user_id)
        if not allowed:
            await ctx.send(f"⏰ 少し待ってください。あと{wait_time:.1f}秒後に再度お試しください。")
            return
        
        # リクエスト時刻を記録
        user_last_request[user_id] = datetime.now()
        
        # サーバーの人間メンバーを取得（Bot除く）
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")
            return
        
        # オンラインの人間メンバーを取得
        online_members = []
        for member in guild.members:
            if not member.bot and member.status != discord.Status.offline:
                online_members.append(member)
        
        # 全メンバー（オフライン含む）
        all_human_members = [member for member in guild.members if not member.bot]
        
        if len(online_members) < 2:
            if len(all_human_members) >= 2:
                await ctx.send(f"⚠️ オンラインメンバーが少ないため、全メンバー({len(all_human_members)}人)でチーム分けします。\n"
                              f"オンライン: {len(online_members)}人 / 全体: {len(all_human_members)}人")
                members_to_use = all_human_members
            else:
                await ctx.send("❌ チーム分けには最低2人のメンバーが必要です。")
                return
        else:
            members_to_use = online_members

        
        # メンバーをランダムシャッフル
        shuffled_members = members_to_use.copy()
        random.shuffle(shuffled_members)
        
        embed = discord.Embed(title="🎯 チーム分け結果", color=0x00ff00)
        
        if format_type:
            format_type = format_type.lower()
            
            # 2v1形式
            if format_type in ['2v1', '2対1']:
                if len(shuffled_members) < 3:
                    await ctx.send(f"❌ 2v1には最低3人必要ですが、現在{len(shuffled_members)}人しかいません。\n💡 `!team 1v1`や`!team`（自動選択）をお試しください。")
                    return
                
                team1 = shuffled_members[:2]
                team2 = [shuffled_members[2]]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (1人)",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                
                if len(shuffled_members) > 3:
                    extras = shuffled_members[3:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
            
            # 3v3形式
            elif format_type in ['3v3', '3対3']:
                if len(shuffled_members) < 6:
                    await ctx.send(f"⚠️ 3v3には6人必要ですが、{len(shuffled_members)}人しかいません。")
                    # 可能な範囲でチーム分け
                    if len(shuffled_members) >= 4:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name="🔴 チーム1",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name="🔵 チーム2", 
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                    else:
                        await ctx.send("❌ チーム分けには最低4人必要です。")
                        return
                else:
                    team1 = shuffled_members[:3]
                    team2 = shuffled_members[3:6]
                    
                    embed.add_field(
                        name="🔴 チーム1 (3人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (3人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 6:
                        extras = shuffled_members[6:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
            
            # 2v2形式
            elif format_type in ['2v2', '2対2']:
                if len(shuffled_members) < 4:
                    await ctx.send("❌ 2v2には最低4人必要です。")
                    return
                
                team1 = shuffled_members[:2]
                team2 = shuffled_members[2:4]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 4:
                    extras = shuffled_members[4:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
            
            # 1v1形式
            elif format_type in ['1v1', '1対1']:
                if len(shuffled_members) < 2:
                    await ctx.send("❌ 1v1には最低2人必要です。")
                    return
                
                team1 = [shuffled_members[0]]
                team2 = [shuffled_members[1]]
                
                embed.add_field(
                    name="🔴 プレイヤー1",
                    value=f"• {team1[0].display_name}",
                    inline=True
                )
                embed.add_field(
                    name="🔵 プレイヤー2",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                
                if len(shuffled_members) > 2:
                    extras = shuffled_members[2:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
            
            # 5v5形式
            elif format_type in ['5v5', '5対5']:
                if len(shuffled_members) < 10:
                    await ctx.send(f"⚠️ 5v5には10人必要ですが、{len(shuffled_members)}人しかいません。")
                    # 可能な範囲でチーム分け
                    if len(shuffled_members) >= 6:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name=f"🔴 チーム1 ({len(team1)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name=f"🔵 チーム2 ({len(team2)}人)", 
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                        embed.set_footer(text="自動調整: 均等分け")
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1 = shuffled_members[:5]
                    team2 = shuffled_members[5:10]
                    
                    embed.add_field(
                        name="🔴 チーム1 (5人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (5人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 10:
                        extras = shuffled_members[10:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
            
            # 4v4形式
            elif format_type in ['4v4', '4対4']:
                if len(shuffled_members) < 8:
                    await ctx.send(f"⚠️ 4v4には8人必要ですが、{len(shuffled_members)}人しかいません。")
                    # 可能な範囲でチーム分け
                    if len(shuffled_members) >= 6:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name=f"🔴 チーム1 ({len(team1)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name=f"🔵 チーム2 ({len(team2)}人)", 
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                        embed.set_footer(text="自動調整: 均等分け")
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1 = shuffled_members[:4]
                    team2 = shuffled_members[4:8]
                    
                    embed.add_field(
                        name="🔴 チーム1 (4人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (4人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 8:
                        extras = shuffled_members[8:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
            
            else:
                await ctx.send("❌ 対応していない形式です。使用可能: `2v1`, `3v3`, `2v2`, `1v1`, `4v4`, `5v5`")
                return
        
        else:
            # 形式指定なし - 自動で最適な分け方を選択
            member_count = len(shuffled_members)
            
            if member_count == 2:
                # 1v1
                embed.add_field(
                    name="🔴 プレイヤー1",
                    value=f"• {shuffled_members[0].display_name}",
                    inline=True
                )
                embed.add_field(
                    name="🔵 プレイヤー2",
                    value=f"• {shuffled_members[1].display_name}",
                    inline=True
                )
                embed.set_footer(text="自動選択: 1v1形式")
                
            elif member_count == 3:
                # 2v1
                team1 = shuffled_members[:2]
                team2 = [shuffled_members[2]]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (1人)",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                embed.set_footer(text="自動選択: 2v1形式")
                
            elif member_count == 4:
                # 2v2
                team1 = shuffled_members[:2]
                team2 = shuffled_members[2:4]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                embed.set_footer(text="自動選択: 2v2形式")
                
            elif member_count >= 10:
                # 5v5（余りは待機）
                team1 = shuffled_members[:5]
                team2 = shuffled_members[5:10]
                
                embed.add_field(
                    name="🔴 チーム1 (5人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (5人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 10:
                    extras = shuffled_members[10:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 5v5形式")
                
            elif member_count >= 8:
                # 4v4（余りは待機）
                team1 = shuffled_members[:4]
                team2 = shuffled_members[4:8]
                
                embed.add_field(
                    name="🔴 チーム1 (4人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (4人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 8:
                    extras = shuffled_members[8:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 4v4形式")
                
            elif member_count >= 6:
                # 3v3（余りは待機）
                team1 = shuffled_members[:3]
                team2 = shuffled_members[3:6]
                
                embed.add_field(
                    name="🔴 チーム1 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 6:
                    extras = shuffled_members[6:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 3v3形式")
                
            else:
                # 5人の場合は不均等に分ける
                team1 = shuffled_members[:3]
                team2 = shuffled_members[3:5]
                
                embed.add_field(
                    name="🔴 チーム1 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                embed.set_footer(text="自動選択: 3v2形式")
        
        # オンライン状況を表示
        status_info = f"対象: {len(members_to_use)}人 (オンライン: {len(online_members)}人)"
        embed.add_field(name="📊 情報", value=status_info, inline=False)
        
        await ctx.send(embed=embed)
        
        # ランダム性を示すために小さなメッセージ
        await ctx.send("🎲 ランダムでチーム分けしました！ 再実行すると違う組み合わせになります。")
        
    except Exception as e:
        await ctx.send(f"❌ チーム分けでエラーが発生しました: {str(e)}")
        print(f"チーム分けエラー: {e}")

@bot.command(name='quick_team', aliases=['qt'], help='簡単チーム分け（例: !qt, !quick_team 2v1）')
@prevent_duplicate_execution
async def quick_team(ctx, format_type=None):
    """簡単チーム分け（エイリアス）"""
    await team_divide(ctx, format_type)

@bot.command(name='vc_team', aliases=['vct'], help='VC内メンバーでチーム分けします（例: !vc_team, !vc_team 2v2）')
@prevent_duplicate_execution
async def vc_team_divide(ctx, format_type=None):
    """VC内メンバー専用チーム分け機能"""
    try:
        
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")
            return
        
        # 全てのボイスチャンネルからメンバーを取得
        vc_members = []
        voice_channels_with_members = []
        
        for channel in guild.voice_channels:
            if channel.members:  # メンバーがいるVCのみ
                channel_members = [member for member in channel.members if not member.bot]
                if channel_members:
                    vc_members.extend(channel_members)
                    voice_channels_with_members.append(f"🔊 {channel.name} ({len(channel_members)}人)")
        
        # 重複を除去（複数のVCにいる場合は考慮しない、実際にはあり得ない）
        vc_members = list(set(vc_members))
        
        if len(vc_members) < 2:
            embed = discord.Embed(
                title="❌ VC内メンバー不足", 
                color=discord.Color.red()
            )
            embed.add_field(
                name="現在の状況",
                value=f"VC内人間メンバー: {len(vc_members)}人\nチーム分けには最低2人必要です。",
                inline=False
            )
            
            if voice_channels_with_members:
                embed.add_field(
                    name="アクティブなVC",
                    value="\n".join(voice_channels_with_members),
                    inline=False
                )
            else:
                embed.add_field(
                    name="💡 ヒント",
                    value="まずボイスチャンネルに参加してから再度実行してください。",
                    inline=False
                )
            
            await ctx.send(embed=embed)
            return
        
        # メンバーをランダムシャッフル
        shuffled_members = vc_members.copy()
        random.shuffle(shuffled_members)
        
        embed = discord.Embed(title="🎤 VC チーム分け結果", color=0xff6b47)  # オレンジ色でVC専用を表現
        
        if format_type:
            format_type = format_type.lower()
            
            # 各形式の処理（既存のコードと同じロジック）
            if format_type in ['2v1', '2対1']:
                if len(shuffled_members) < 3:
                    await ctx.send(f"❌ 2v1には最低3人必要ですが、VC内に{len(shuffled_members)}人しかいません。\n💡 `!vc_team 1v1`や`!vc_team`（自動選択）をお試しください。")
                    return
                
                team1 = shuffled_members[:2]
                team2 = [shuffled_members[2]]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (1人)",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                
                if len(shuffled_members) > 3:
                    extras = shuffled_members[3:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="指定形式: 2v1 (VC内メンバー)")
            
            elif format_type in ['3v3', '3対3']:
                if len(shuffled_members) < 6:
                    await ctx.send(f"⚠️ 3v3には6人必要ですが、VC内に{len(shuffled_members)}人しかいません。")
                    if len(shuffled_members) >= 4:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name="🔴 チーム1",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name="🔵 チーム2",
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                        embed.set_footer(text="自動調整: 均等分け (VC内メンバー)")
                    else:
                        await ctx.send("❌ チーム分けには最低4人必要です。")
                        return
                else:
                    team1 = shuffled_members[:3]
                    team2 = shuffled_members[3:6]
                    
                    embed.add_field(
                        name="🔴 チーム1 (3人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (3人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 6:
                        extras = shuffled_members[6:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
                    embed.set_footer(text="指定形式: 3v3 (VC内メンバー)")
            
            elif format_type in ['2v2', '2対2']:
                if len(shuffled_members) < 4:
                    await ctx.send("❌ 2v2には最低4人必要です。")
                    return
                
                team1 = shuffled_members[:2]
                team2 = shuffled_members[2:4]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 4:
                    extras = shuffled_members[4:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="指定形式: 2v2 (VC内メンバー)")
            
            elif format_type in ['1v1', '1対1']:
                if len(shuffled_members) < 2:
                    await ctx.send("❌ 1v1には最低2人必要です。")
                    return
                
                team1 = [shuffled_members[0]]
                team2 = [shuffled_members[1]]
                
                embed.add_field(
                    name="🔴 プレイヤー1",
                    value=f"• {team1[0].display_name}",
                    inline=True
                )
                embed.add_field(
                    name="🔵 プレイヤー2",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                
                if len(shuffled_members) > 2:
                    extras = shuffled_members[2:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="指定形式: 1v1 (VC内メンバー)")
            
            # 5v5形式（VC版）
            elif format_type in ['5v5', '5対5']:
                if len(shuffled_members) < 10:
                    await ctx.send(f"⚠️ 5v5には10人必要ですが、VC内に{len(shuffled_members)}人しかいません。")
                    # 可能な範囲でチーム分け
                    if len(shuffled_members) >= 6:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name=f"🔴 チーム1 ({len(team1)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name=f"🔵 チーム2 ({len(team2)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                        embed.set_footer(text="自動調整: 均等分け (VC内メンバー)")
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1 = shuffled_members[:5]
                    team2 = shuffled_members[5:10]
                    
                    embed.add_field(
                        name="🔴 チーム1 (5人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (5人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 10:
                        extras = shuffled_members[10:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
                    embed.set_footer(text="指定形式: 5v5 (VC内メンバー)")
            
            # 4v4形式（VC版）
            elif format_type in ['4v4', '4対4']:
                if len(shuffled_members) < 8:
                    await ctx.send(f"⚠️ 4v4には8人必要ですが、VC内に{len(shuffled_members)}人しかいません。")
                    # 可能な範囲でチーム分け
                    if len(shuffled_members) >= 6:
                        mid = len(shuffled_members) // 2
                        team1 = shuffled_members[:mid]
                        team2 = shuffled_members[mid:]
                        
                        embed.add_field(
                            name=f"🔴 チーム1 ({len(team1)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team1]),
                            inline=True
                        )
                        embed.add_field(
                            name=f"🔵 チーム2 ({len(team2)}人)",
                            value="\n".join([f"• {m.display_name}" for m in team2]),
                            inline=True
                        )
                        embed.set_footer(text="自動調整: 均等分け (VC内メンバー)")
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1 = shuffled_members[:4]
                    team2 = shuffled_members[4:8]
                    
                    embed.add_field(
                        name="🔴 チーム1 (4人)",
                        value="\n".join([f"• {m.display_name}" for m in team1]),
                        inline=True
                    )
                    embed.add_field(
                        name="🔵 チーム2 (4人)",
                        value="\n".join([f"• {m.display_name}" for m in team2]),
                        inline=True
                    )
                    
                    if len(shuffled_members) > 8:
                        extras = shuffled_members[8:]
                        embed.add_field(
                            name="⚪ 待機",
                            value="\n".join([f"• {m.display_name}" for m in extras]),
                            inline=False
                        )
                    embed.set_footer(text="指定形式: 4v4 (VC内メンバー)")
            
            else:
                await ctx.send("❌ 対応していない形式です。使用可能: `2v1`, `3v3`, `2v2`, `1v1`, `4v4`, `5v5`")
                return
        
        else:
            # 形式指定なし - 自動で最適な分け方を選択
            member_count = len(shuffled_members)
            
            if member_count == 2:
                # 1v1
                embed.add_field(
                    name="🔴 プレイヤー1",
                    value=f"• {shuffled_members[0].display_name}",
                    inline=True
                )
                embed.add_field(
                    name="🔵 プレイヤー2",
                    value=f"• {shuffled_members[1].display_name}",
                    inline=True
                )
                embed.set_footer(text="自動選択: 1v1形式 (VC内メンバー)")
                
            elif member_count == 3:
                # 2v1
                team1 = shuffled_members[:2]
                team2 = [shuffled_members[2]]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (1人)",
                    value=f"• {team2[0].display_name}",
                    inline=True
                )
                embed.set_footer(text="自動選択: 2v1形式 (VC内メンバー)")
                
            elif member_count == 4:
                # 2v2
                team1 = shuffled_members[:2]
                team2 = shuffled_members[2:4]
                
                embed.add_field(
                    name="🔴 チーム1 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                embed.set_footer(text="自動選択: 2v2形式 (VC内メンバー)")
                
            elif member_count >= 10:
                # 5v5（余りは待機）
                team1 = shuffled_members[:5]
                team2 = shuffled_members[5:10]
                
                embed.add_field(
                    name="🔴 チーム1 (5人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (5人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 10:
                    extras = shuffled_members[10:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 5v5形式 (VC内メンバー)")
                
            elif member_count >= 8:
                # 4v4（余りは待機）
                team1 = shuffled_members[:4]
                team2 = shuffled_members[4:8]
                
                embed.add_field(
                    name="🔴 チーム1 (4人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (4人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 8:
                    extras = shuffled_members[8:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 4v4形式 (VC内メンバー)")
                
            elif member_count >= 6:
                # 3v3（余りは待機）
                team1 = shuffled_members[:3]
                team2 = shuffled_members[3:6]
                
                embed.add_field(
                    name="🔴 チーム1 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                
                if len(shuffled_members) > 6:
                    extras = shuffled_members[6:]
                    embed.add_field(
                        name="⚪ 待機",
                        value="\n".join([f"• {m.display_name}" for m in extras]),
                        inline=False
                    )
                embed.set_footer(text="自動選択: 3v3形式 (VC内メンバー)")
                
            else:
                # 5人の場合は不均等に分ける
                team1 = shuffled_members[:3]
                team2 = shuffled_members[3:5]
                
                embed.add_field(
                    name="🔴 チーム1 (3人)",
                    value="\n".join([f"• {m.display_name}" for m in team1]),
                    inline=True
                )
                embed.add_field(
                    name="🔵 チーム2 (2人)",
                    value="\n".join([f"• {m.display_name}" for m in team2]),
                    inline=True
                )
                embed.set_footer(text="自動選択: 3v2形式 (VC内メンバー)")
        
        # VC情報を表示
        if voice_channels_with_members:
            embed.add_field(
                name="🎤 対象VC", 
                value="\n".join(voice_channels_with_members), 
                inline=False
            )
        
        embed.add_field(
            name="📊 情報", 
            value=f"VC内メンバー: {len(vc_members)}人", 
            inline=False
        )
        
        await ctx.send(embed=embed)
        
        # 追加メッセージ
        await ctx.send("🎲 VC内メンバーでランダムチーム分けしました！ 再実行すると違う組み合わせになります。")
        
    except Exception as e:
        await ctx.send(f"❌ VC チーム分けでエラーが発生しました: {str(e)}")
        print(f"VC チーム分けエラー: {e}")
    finally:
        # 実行中フラグをクリア
        command_executing.pop(ctx.author.id, None)

@bot.event
async def on_disconnect():
    """Discord接続が切れた時の処理"""
    print(f"⚠️ Discord接続が切断されました: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

@bot.event
async def on_resumed():
    """Discord接続が復旧した時の処理"""
    print(f"✅ Discord接続が復旧しました: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

@bot.event
async def on_command_error(ctx, error):
    """エラーハンドリング"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("そのコマンドは存在しません。`!help`でコマンド一覧を確認してください。")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"必要な引数が不足しています。`!help {ctx.command}`で使い方を確認してください。")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("引数の形式が正しくありません。")
    elif isinstance(error, discord.HTTPException):
        print(f"Discord HTTPエラー: {error}")
        await ctx.send("Discord APIエラーが発生しました。少し待ってから再試行してください。")
    elif isinstance(error, discord.ConnectionClosed):
        print(f"Discord接続エラー: {error}")
    else:
        print(f"予期しないエラー: {error}")
        import traceback
        traceback.print_exc()
        try:
            # 詳細なエラー情報を表示
            error_msg = f"❌ エラーが発生しました:\n```\n{str(error)}\n```\nコマンド: `{ctx.message.content}`"
            if len(error_msg) > 2000:
                error_msg = f"❌ エラーが発生しました: {str(error)[:1900]}..."
            await ctx.send(error_msg)
        except:
            print("エラーメッセージの送信も失敗しました")

@bot.command(name='mystats', help='メンバーの統計情報を表示します')
@prevent_duplicate_execution
async def show_member_stats(ctx, member: discord.Member = None):
    """メンバーの統計情報を表示"""
    try:
        target = member or ctx.author
        embed = discord.Embed(title=f"{target.name}の統計情報", color=0x00ff00)
        embed.add_field(name="アカウント作成日", value=target.created_at.strftime('%Y-%m-%d'))
        if target.joined_at:
            embed.add_field(name="サーバー参加日", value=target.joined_at.strftime('%Y-%m-%d'))
        embed.add_field(name="ユーザーID", value=target.id)
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Stats error: {str(e)}")
        await ctx.send("❌ 統計情報の取得中にエラーが発生しました。")

# VALORANTマップ情報
VALORANT_MAPS = {
    "Ascent": {
        "name": "アセント",
        "sites": "A・B",
        "description": "イタリア・ヴェネツィアをモチーフにした標準的なマップ",
        "emoji": "🏛️",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/ascent.png"
    },
    "Bind": {
        "name": "バインド",
        "sites": "A・B",
        "description": "モロッコをモチーフにしたテレポーター付きマップ",
        "emoji": "🕌",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/bind.png"
    },
    "Haven": {
        "name": "ヘイヴン",
        "sites": "A・B・C",
        "description": "ブータンをモチーフにした3サイトマップ",
        "emoji": "🏔️",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/haven.png"
    },
    "Split": {
        "name": "スプリット",
        "sites": "A・B",
        "description": "日本・東京をモチーフにした縦長マップ",
        "emoji": "🏙️",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/split.png"
    },
    "Icebox": {
        "name": "アイスボックス",
        "sites": "A・B",
        "description": "ロシア・シベリアをモチーフにした寒冷地マップ",
        "emoji": "🧊",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/icebox.png"
    },
    "Breeze": {
        "name": "ブリーズ",
        "sites": "A・B",
        "description": "カリブ海の島をモチーフにした開放的なマップ",
        "emoji": "🏝️",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/breeze.png"
    },
    "Fracture": {
        "name": "フラクチャー",
        "sites": "A・B",
        "description": "アメリカをモチーフにした特殊構造マップ",
        "emoji": "⚡",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/fracture.png"
    },
    "Pearl": {
        "name": "パール",
        "sites": "A・B",
        "description": "ポルトガル・リスボンをモチーフにした水中都市マップ",
        "emoji": "🐚",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/pearl.png"
    },
    "Lotus": {
        "name": "ロータス",
        "sites": "A・B・C",
        "description": "インドをモチーフにした3サイトマップ",
        "emoji": "🪷",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/lotus.png"
    },
    "Sunset": {
        "name": "サンセット",
        "sites": "A・B",
        "description": "アメリカ・ロサンゼルスをモチーフにしたマップ",
        "emoji": "🌅",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/sunset.png"
    },
    "Abyss": {
        "name": "アビス",
        "sites": "A・B",
        "description": "OMEGA EARTHの実験施設をモチーフにしたマップ",
        "emoji": "🕳️",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/abyss.png"
    },
    "Carod": {
        "name": "カロード",
        "sites": "A・B",
        "description": "フランス城下町を舞台にした多層構造マップ",
        "emoji": "🏰",
        "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/maps/carod.jpg"
    }
}

@bot.command(name='map', aliases=['マップ', 'valmap'], help='VALORANTのマップをランダムに選択します')
@prevent_duplicate_execution
async def valorant_map_roulette(ctx, count: int = 1):
    """VALORANTマップルーレット"""
    try:
        # カウント数の制限
        if count < 1:
            count = 1
        elif count > 5:
            count = 5
            await ctx.send("⚠️ 一度に選択できるマップは最大5つまでです。")
        
        # マップをランダムに選択
        selected_maps = random.sample(list(VALORANT_MAPS.keys()), min(count, len(VALORANT_MAPS)))
        
        if count == 1:
            # 単一マップの場合は詳細表示
            map_key = selected_maps[0]
            map_info = VALORANT_MAPS[map_key]
            
            embed = discord.Embed(
                title="🎯 VALORANTマップルーレット",
                description=f"**{map_info['emoji']} {map_key} ({map_info['name']})**",
                color=0xff4655
            )
            
            embed.add_field(name="📍 サイト", value=map_info['sites'], inline=True)
            embed.add_field(name="ℹ️ 説明", value=map_info['description'], inline=False)
            
            # マップ画像を表示
            if 'image_url' in map_info:
                embed.set_image(url=map_info['image_url'])
            
            embed.set_footer(text="Good luck, have fun! 🎮")
            
        else:
            # 複数マップの場合はリスト表示
            embed = discord.Embed(
                title=f"🎯 VALORANTマップルーレット ({count}マップ)",
                color=0xff4655
            )
            
            map_list = []
            for i, map_key in enumerate(selected_maps, 1):
                map_info = VALORANT_MAPS[map_key]
                map_list.append(f"{i}. {map_info['emoji']} **{map_key}** ({map_info['name']})")
            
            embed.description = "\n".join(map_list)
            embed.set_footer(text="Good luck, have fun! 🎮")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"マップルーレットエラー: {e}")
        await ctx.send("❌ マップルーレットでエラーが発生しました。")

@bot.command(name='maplist', aliases=['マップ一覧', 'allmaps'], help='VALORANTの全マップ一覧を表示します')
@prevent_duplicate_execution
async def valorant_map_list(ctx):
    """VALORANTマップ一覧表示"""
    try:
        embed = discord.Embed(
            title="🗺️ VALORANT マップ一覧",
            description="現在のマッププール",
            color=0xff4655
        )
        
        # 全マップを一覧表示
        map_list = []
        for map_key, map_info in VALORANT_MAPS.items():
            map_text = f"{map_info['emoji']} **{map_key}** ({map_info['name']}) - {map_info['sites']}"
            map_list.append(map_text)
        
        # 全マップを一つのフィールドにまとめて表示
        embed.add_field(
            name="🗺️ 全マップ",
            value="\n".join(map_list),
            inline=False
        )
        
        embed.add_field(
            name="🎲 使用方法",
            value="`!map` - ランダムに1マップ選択\n`!map 3` - ランダムに3マップ選択",
            inline=False
        )
        
        embed.set_footer(text=f"総マップ数: {len(VALORANT_MAPS)}マップ")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"マップ一覧エラー: {e}")
        await ctx.send("❌ マップ一覧の表示でエラーが発生しました。")

@bot.command(name='mapinfo', aliases=['マップ情報'], help='特定のVALORANTマップの詳細情報を表示します')
@prevent_duplicate_execution
async def valorant_map_info(ctx, *, map_name=None):
    """特定マップの詳細情報表示"""
    try:
        if not map_name:
            await ctx.send("❌ マップ名を指定してください。例: `!mapinfo Ascent`")
            return
        
        # マップ名の検索（部分一致対応）
        found_map = None
        map_name_lower = map_name.lower()
        
        for map_key, map_info in VALORANT_MAPS.items():
            if (map_name_lower in map_key.lower() or 
                map_name_lower in map_info['name'].lower()):
                found_map = (map_key, map_info)
                break
        
        if not found_map:
            await ctx.send(f"❌ マップ「{map_name}」が見つかりません。`!maplist` で一覧を確認してください。")
            return
        
        map_key, map_info = found_map
        
        embed = discord.Embed(
            title=f"{map_info['emoji']} {map_key} ({map_info['name']})",
            description=map_info['description'],
            color=0xff4655
        )
        
        embed.add_field(name="📍 サイト構成", value=map_info['sites'], inline=True)
        embed.add_field(name="🎯 特徴", value=map_info['description'], inline=False)
        
        # マップ画像を表示
        if 'image_url' in map_info:
            embed.set_image(url=map_info['image_url'])
        
        embed.set_footer(text="!map でランダム選択 | !maplist で全マップ一覧")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"マップ情報エラー: {e}")
        await ctx.send("❌ マップ情報の表示でエラーが発生しました。")

# VALORANTランクシステム
VALORANT_RANKS = {
    "レディアント": {"tier": 9, "display": "🔥 レディアント", "value": 900, "color": 0xFFFFFF, "image_url": "https://picsum.photos/200/200?random=900"},
    "イモータル3": {"tier": 8, "display": "💎 イモータル 3", "value": 803, "color": 0xBA55D3, "image_url": "https://picsum.photos/200/200?random=803"},
    "イモータル2": {"tier": 8, "display": "💎 イモータル 2", "value": 802, "color": 0xBA55D3, "image_url": "https://picsum.photos/200/200?random=802"},
    "イモータル1": {"tier": 8, "display": "💎 イモータル 1", "value": 801, "color": 0xBA55D3, "image_url": "https://picsum.photos/200/200?random=801"},
    "アセンダント3": {"tier": 7, "display": "🔆 アセンダント 3", "value": 703, "color": 0x32CD32, "image_url": "https://picsum.photos/200/200?random=703"},
    "アセンダント2": {"tier": 7, "display": "🔆 アセンダント 2", "value": 702, "color": 0x32CD32, "image_url": "https://picsum.photos/200/200?random=702"},
    "アセンダント1": {"tier": 7, "display": "🔆 アセンダント 1", "value": 701, "color": 0x32CD32, "image_url": "https://picsum.photos/200/200?random=701"},
    "ダイヤ3": {"tier": 6, "display": "💠 ダイヤモンド 3", "value": 603, "color": 0x87CEEB, "image_url": "https://picsum.photos/200/200?random=603"},
    "ダイヤ2": {"tier": 6, "display": "💠 ダイヤモンド 2", "value": 602, "color": 0x87CEEB, "image_url": "https://picsum.photos/200/200?random=602"},
    "ダイヤ1": {"tier": 6, "display": "💠 ダイヤモンド 1", "value": 601, "color": 0x87CEEB, "image_url": "https://picsum.photos/200/200?random=601"},
    "プラチナ3": {"tier": 5, "display": "🔷 プラチナ 3", "value": 503, "color": 0x40E0D0, "image_url": "https://picsum.photos/200/200?random=503"},
    "プラチナ2": {"tier": 5, "display": "🔷 プラチナ 2", "value": 502, "color": 0x40E0D0, "image_url": "https://picsum.photos/200/200?random=502"},
    "プラチナ1": {"tier": 5, "display": "🔷 プラチナ 1", "value": 501, "color": 0x40E0D0, "image_url": "https://picsum.photos/200/200?random=501"},
    "ゴールド3": {"tier": 4, "display": "🟡 ゴールド 3", "value": 403, "color": 0xFFD700, "image_url": "https://picsum.photos/200/200?random=403"},
    "ゴールド2": {"tier": 4, "display": "🟡 ゴールド 2", "value": 402, "color": 0xFFD700, "image_url": "https://picsum.photos/200/200?random=402"},
    "ゴールド1": {"tier": 4, "display": "🟡 ゴールド 1", "value": 401, "color": 0xFFD700, "image_url": "https://picsum.photos/200/200?random=401"},
    "シルバー3": {"tier": 3, "display": "⚪ シルバー 3", "value": 303, "color": 0xC0C0C0, "image_url": "https://picsum.photos/200/200?random=303"},
    "シルバー2": {"tier": 3, "display": "⚪ シルバー 2", "value": 302, "color": 0xC0C0C0, "image_url": "https://picsum.photos/200/200?random=302"},
    "シルバー1": {"tier": 3, "display": "⚪ シルバー 1", "value": 301, "color": 0xC0C0C0, "image_url": "https://picsum.photos/200/200?random=301"},
    "ブロンズ3": {"tier": 2, "display": "🟤 ブロンズ 3", "value": 203, "color": 0xCD7F32, "image_url": "https://picsum.photos/200/200?random=203"},
    "ブロンズ2": {"tier": 2, "display": "🟤 ブロンズ 2", "value": 202, "color": 0xCD7F32, "image_url": "https://picsum.photos/200/200?random=202"},
    "ブロンズ1": {"tier": 2, "display": "🟤 ブロンズ 1", "value": 201, "color": 0xCD7F32, "image_url": "https://picsum.photos/200/200?random=201"},
    "アイアン3": {"tier": 1, "display": "⚫ アイアン 3", "value": 103, "color": 0x696969, "image_url": "https://picsum.photos/200/200?random=103"},
    "アイアン2": {"tier": 1, "display": "⚫ アイアン 2", "value": 102, "color": 0x696969, "image_url": "https://picsum.photos/200/200?random=102"},
    "アイアン1": {"tier": 1, "display": "⚫ アイアン 1", "value": 101, "color": 0x696969, "image_url": "https://picsum.photos/200/200?random=101"}
}

# ユーザーランク情報ストレージ
user_ranks = {}  # {user_id: {"current": "rank", "peak": "rank", "updated": datetime}}

def parse_rank_input(rank_input):
    """ランク入力をパース"""
    rank_input = rank_input.strip()
    
    # 前処理：スペース削除、全角数字を半角に変換
    rank_input = rank_input.replace(" ", "").replace("　", "")  # 半角・全角スペース削除
    rank_input = rank_input.replace("１", "1").replace("２", "2").replace("３", "3")  # 全角数字変換
    rank_input = rank_input.replace("ダイヤモンド", "ダイヤ")  # 「ダイヤモンド」→「ダイヤ」変換
    
    # 完全一致チェック
    for rank_key in VALORANT_RANKS.keys():
        if rank_input.lower() == rank_key.lower():
            return rank_key
    
    # 部分一致チェック（ランク名のみ）
    rank_mappings = {
        "レディアント": "レディアント",
        "radiant": "レディアント",
        "rad": "レディアント",
        "イモータル": ["イモータル3", "イモータル2", "イモータル1"],
        "immortal": ["イモータル3", "イモータル2", "イモータル1"],
        "imm": ["イモータル3", "イモータル2", "イモータル1"],
        "アセンダント": ["アセンダント3", "アセンダント2", "アセンダント1"],
        "ascendant": ["アセンダント3", "アセンダント2", "アセンダント1"],
        "asc": ["アセンダント3", "アセンダント2", "アセンダント1"],
        "ダイヤ": ["ダイヤ3", "ダイヤ2", "ダイヤ1"],
        "diamond": ["ダイヤ3", "ダイヤ2", "ダイヤ1"],
        "dia": ["ダイヤ3", "ダイヤ2", "ダイヤ1"],
        "プラチナ": ["プラチナ3", "プラチナ2", "プラチナ1"],
        "platinum": ["プラチナ3", "プラチナ2", "プラチナ1"],
        "plat": ["プラチナ3", "プラチナ2", "プラチナ1"],
        "ゴールド": ["ゴールド3", "ゴールド2", "ゴールド1"],
        "gold": ["ゴールド3", "ゴールド2", "ゴールド1"],
        "シルバー": ["シルバー3", "シルバー2", "シルバー1"],
        "silver": ["シルバー3", "シルバー2", "シルバー1"],
        "sil": ["シルバー3", "シルバー2", "シルバー1"],
        "ブロンズ": ["ブロンズ3", "ブロンズ2", "ブロンズ1"],
        "bronze": ["ブロンズ3", "ブロンズ2", "ブロンズ1"],
        "bro": ["ブロンズ3", "ブロンズ2", "ブロンズ1"],
        "アイアン": ["アイアン3", "アイアン2", "アイアン1"],
        "iron": ["アイアン3", "アイアン2", "アイアン1"]
    }
    
    # 数字付きランクチェック
    for base_name, ranks in rank_mappings.items():
        if isinstance(ranks, list):
            if rank_input.lower().startswith(base_name.lower()):
                # 数字を抽出
                for i in range(3, 0, -1):
                    if str(i) in rank_input:
                        return ranks[3-i]  # 3->0, 2->1, 1->2のインデックス
                # 数字がない場合は最高ランク（3）
                return ranks[0]
        else:
            if rank_input.lower().startswith(base_name.lower()):
                return ranks
    
    return None

@bot.command(name='rank', help='VALORANTランクを管理します（例: !rank set current ダイヤ2, !rank show）')
@prevent_duplicate_execution
async def rank_system(ctx, action=None, rank_type=None, *rank_input):
    """VALORANTランクシステム"""
    try:
        if not action:
            # ヘルプ表示
            embed = discord.Embed(
                title="🎯 VALORANTランクシステム",
                description="現在ランクと最高ランクを管理できます",
                color=0xff4655
            )
            
            embed.add_field(
                name="📝 ランク設定",
                value="`!rank set current [ランク]` - 現在ランク設定\n`!rank set peak [ランク]` - 最高ランク設定",
                inline=False
            )
            
            embed.add_field(
                name="📊 ランク表示",
                value="`!rank show` - 自分のランク表示\n`!rank show @ユーザー` - 他人のランク表示\n`!rank list` - サーバー内ランキング",
                inline=False
            )
            
            embed.add_field(
                name="🏆 ランク入力例",
                value="• `ダイヤ2`, `ダイヤモンド ２`\n• `イモータル3`, `imm3`\n• `プラチナ1`, `plat1`\n• `レディアント`, `radiant`\n※ スペースや全角数字も対応",
                inline=False
            )
            
            embed.set_footer(text="例: !rank set current ダイヤ2")
            await ctx.send(embed=embed)
            return
        
        if action.lower() == "set":
            if not rank_type or not rank_input:
                await ctx.send("❌ 使用方法: `!rank set current/peak [ランク名]`")
                return
            
            if rank_type.lower() not in ["current", "peak", "現在", "最高"]:
                await ctx.send("❌ ランクタイプは `current`（現在）または `peak`（最高）を指定してください")
                return
            
            # rank_inputをtupleから文字列に変換
            rank_input_str = " ".join(rank_input) if rank_input else ""
            
            # ランクをパース
            parsed_rank = parse_rank_input(rank_input_str)
            
            if not parsed_rank:
                rank_list = ", ".join(list(VALORANT_RANKS.keys())[:10]) + "..."
                await ctx.send(f"❌ 無効なランクです。利用可能なランク: {rank_list}")
                return
            
            user_id = ctx.author.id
            
            if user_id not in user_ranks:
                user_ranks[user_id] = {"current": None, "peak": None, "updated": datetime.now()}
            
            # ランクタイプを統一
            rank_type_key = "current" if rank_type.lower() in ["current", "現在"] else "peak"
            old_rank = user_ranks[user_id].get(rank_type_key)
            
            user_ranks[user_id][rank_type_key] = parsed_rank
            user_ranks[user_id]["updated"] = datetime.now()
            
            rank_info = VALORANT_RANKS[parsed_rank]
            type_display = "現在ランク" if rank_type_key == "current" else "最高ランク"
            
            embed = discord.Embed(
                title="✅ ランク設定完了",
                description=f"{type_display}を **{rank_info['display']}** に設定しました",
                color=rank_info['color']
            )
            
            # ランク画像を表示
            if 'image_url' in rank_info:
                embed.set_thumbnail(url=rank_info['image_url'])
            
            if old_rank and old_rank != parsed_rank:
                old_info = VALORANT_RANKS[old_rank]
                embed.add_field(
                    name="📈 変更",
                    value=f"{old_info['display']} → {rank_info['display']}",
                    inline=False
                )
            
            embed.set_footer(text=f"更新者: {ctx.author.display_name}")
            await ctx.send(embed=embed)
            
        elif action.lower() == "show":
            # ユーザー指定の確認
            target_user = ctx.author
            if rank_type:
                # メンション解析
                if ctx.message.mentions:
                    target_user = ctx.message.mentions[0]
                else:
                    await ctx.send("❌ ユーザーが見つかりません。`@ユーザー名` でメンションしてください。")
                    return
            
            user_id = target_user.id
            if user_id not in user_ranks or (not user_ranks[user_id]["current"] and not user_ranks[user_id]["peak"]):
                if target_user == ctx.author:
                    await ctx.send("❌ ランクが設定されていません。`!rank set current [ランク]` で設定してください。")
                else:
                    await ctx.send(f"❌ {target_user.display_name} のランクは設定されていません。")
                return
            
            user_data = user_ranks[user_id]
            current_rank = user_data.get("current")
            peak_rank = user_data.get("peak")
            
            # 表示色を決定（現在ランクがあればそれを、なければピークランクを使用）
            display_color = 0xff4655
            if current_rank:
                display_color = VALORANT_RANKS[current_rank]['color']
            elif peak_rank:
                display_color = VALORANT_RANKS[peak_rank]['color']
            
            embed = discord.Embed(
                title=f"🎯 {target_user.display_name} のVALORANTランク",
                color=display_color
            )
            
            # メインランクの画像を表示（現在ランク優先、なければピークランク）
            main_rank = current_rank if current_rank else peak_rank
            if main_rank and 'image_url' in VALORANT_RANKS[main_rank]:
                embed.set_image(url=VALORANT_RANKS[main_rank]['image_url'])
            
            if current_rank:
                current_info = VALORANT_RANKS[current_rank]
                embed.add_field(
                    name="📊 現在ランク",
                    value=current_info['display'],
                    inline=True
                )
            else:
                embed.add_field(
                    name="📊 現在ランク",
                    value="未設定",
                    inline=True
                )
            
            if peak_rank:
                peak_info = VALORANT_RANKS[peak_rank]
                embed.add_field(
                    name="🏆 最高ランク",
                    value=peak_info['display'],
                    inline=True
                )
            else:
                embed.add_field(
                    name="🏆 最高ランク",
                    value="未設定",
                    inline=True
                )
            
            # 最終更新日時
            if "updated" in user_data:
                embed.add_field(
                    name="📅 最終更新",
                    value=user_data["updated"].strftime("%Y/%m/%d %H:%M"),
                    inline=False
                )
            
            # ユーザーアバターはサムネイルに
            embed.set_thumbnail(url=target_user.display_avatar.url)
            await ctx.send(embed=embed)
            
        elif action.lower() == "list":
            # サーバー内ランキング表示
            guild_members = [member.id for member in ctx.guild.members if not member.bot]
            ranked_users = []
            
            for user_id in guild_members:
                if user_id in user_ranks:
                    user_data = user_ranks[user_id]
                    current_rank = user_data.get("current")
                    peak_rank = user_data.get("peak")
                    
                    # 現在ランクを優先、なければピークランク
                    display_rank = current_rank if current_rank else peak_rank
                    if display_rank:
                        user = ctx.guild.get_member(user_id)
                        if user:
                            rank_value = VALORANT_RANKS[display_rank]['value']
                            ranked_users.append((user, display_rank, rank_value, current_rank, peak_rank))
            
            if not ranked_users:
                await ctx.send("❌ このサーバーにはランクを設定したユーザーがいません。")
                return
            
            # ランクでソート（降順）
            ranked_users.sort(key=lambda x: x[2], reverse=True)
            
            embed = discord.Embed(
                title="🏆 サーバー内VALORANTランキング",
                description=f"登録者数: {len(ranked_users)}人",
                color=0xff4655
            )
            
            # 上位15人まで表示
            for i, (user, display_rank, rank_value, current, peak) in enumerate(ranked_users[:15], 1):
                rank_info = VALORANT_RANKS[display_rank]
                
                # メダル表示
                medal = ""
                if i == 1:
                    medal = "🥇 "
                elif i == 2:
                    medal = "🥈 "
                elif i == 3:
                    medal = "🥉 "
                else:
                    medal = f"{i}. "
                
                # ランク詳細
                rank_detail = rank_info['display']
                if current and peak and current != peak:
                    peak_info = VALORANT_RANKS[peak]
                    rank_detail += f" (最高: {peak_info['display']})"
                
                embed.add_field(
                    name=f"{medal}{user.display_name}",
                    value=rank_detail,
                    inline=False
                )
            
            if len(ranked_users) > 15:
                embed.set_footer(text=f"他 {len(ranked_users) - 15}人のランクユーザー")
            
            await ctx.send(embed=embed)
            
        else:
            await ctx.send("❌ 無効なアクション。利用可能: `set`, `show`, `list`")
            
    except Exception as e:
        print(f"ランクシステムエラー: {e}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ ランクシステムでエラーが発生しました: {str(e)}\n\n使用方法: `!rank set current/peak [ランク名]`\n例: `!rank set current ダイヤ2`")

@bot.command(name='ranklist', aliases=['ranks'], help='利用可能なVALORANTランク一覧を表示します')
@prevent_duplicate_execution
async def rank_list(ctx):
    """利用可能なランク一覧表示"""
    try:
        embed = discord.Embed(
            title="🎯 VALORANT ランク一覧",
            description="設定可能なランク（上位から順番）",
            color=0xff4655
        )
        
        # ランクを価値順にソート
        sorted_ranks = sorted(VALORANT_RANKS.items(), key=lambda x: x[1]['value'], reverse=True)
        
        rank_display = []
        current_tier = None
        
        for rank_key, rank_info in sorted_ranks:
            if current_tier != rank_info['tier']:
                if rank_display:  # 前のティアがある場合は改行追加
                    rank_display.append("")
                current_tier = rank_info['tier']
            
            rank_display.append(rank_info['display'])
        
        # 3つのフィールドに分けて表示
        chunks = [rank_display[i:i+9] for i in range(0, len(rank_display), 9)]
        
        for i, chunk in enumerate(chunks):
            field_name = f"🏆 ランク一覧 {i+1}" if len(chunks) > 1 else "🏆 ランク一覧"
            embed.add_field(
                name=field_name,
                value="\n".join(chunk),
                inline=True
            )
        
        embed.add_field(
            name="📝 使用方法",
            value="`!rank set current ダイヤ2`\n`!rank set peak レディアント`",
            inline=False
        )
        
        embed.set_footer(text="略語も使用可能: imm3, dia1, plat2, gold3など")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"ランク一覧エラー: {e}")
        await ctx.send("❌ ランク一覧の表示でエラーが発生しました。")

# Render.com Web Service対応のHTTPサーバー
async def handle_health(request):
    """ヘルスチェックエンドポイント"""
    uptime = datetime.now() - bot_stats['start_time']
    health_info = {
        "status": "healthy",
        "uptime_seconds": int(uptime.total_seconds()),
        "bot_ready": not bot.is_closed(),
        "commands_executed": bot_stats['commands_executed'],
        "messages_processed": bot_stats['messages_processed'],
        "errors_count": bot_stats['errors_count'],
        "last_heartbeat": bot_stats['last_heartbeat'].isoformat()
    }
    return web.json_response(health_info)

async def handle_root(request):
    """ルートエンドポイント"""
    return web.Response(text="Discord Bot is running! 🤖", content_type="text/plain")

async def handle_ping(request):
    """Pingエンドポイント"""
    return web.json_response({"message": "pong", "timestamp": datetime.now().isoformat()})

def create_app():
    """aiohttp Webアプリケーションを作成"""
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/ping', handle_ping)
    return app

async def start_web_server():
    """Webサーバーを起動"""
    try:
        app = create_app()
        port = int(os.environ.get('PORT', 8080))  # Render.comのポート
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        print(f"🌐 HTTPサーバーが起動しました: ポート {port}")
        print(f"📡 ヘルスチェック: http://localhost:{port}/health")
        
        return runner
    except Exception as e:
        print(f"❌ Webサーバー起動エラー: {e}")
        return None

# Botを起動
if __name__ == "__main__":
    import logging
    
    # ログレベルを調整（DEBUGは冗長すぎるため）
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # discord.py のログレベルを調整
    discord_logger = logging.getLogger('discord')
    discord_logger.setLevel(logging.WARNING)  # WARNINGレベル以上のみ
    
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("エラー: DISCORD_TOKENが設定されていません。")
        print(".envファイルを作成し、ボットトークンを設定してください。")
    else:
        max_retries = 5
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                print(f"🚀 Botを起動中... (試行 {retry_count + 1}/{max_retries})")
                bot.run(token, reconnect=True)
                break  # 正常終了した場合
                
            except discord.LoginFailure:
                print("❌ エラー: 無効なボットトークンです。")
                break  # 再試行しても無意味
                
            except discord.HTTPException as e:
                print(f"⚠️ Discord HTTPエラー: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = min(2 ** retry_count, 60)  # 指数バックオフ（最大60秒）
                    print(f"⏰ {wait_time}秒後に再試行します...")
                    import time
                    time.sleep(wait_time)
                
            except KeyboardInterrupt:
                print("🛑 Botを手動で停止しました。")
                break
                
            except Exception as e:
                print(f"❌ 予期しないエラー: {e}")
                import traceback
                traceback.print_exc()
                
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = min(2 ** retry_count, 60)
                    print(f"⏰ {wait_time}秒後に再試行します...")
                    import time
                    time.sleep(wait_time)
        
        if retry_count >= max_retries:
            print(f"❌ {max_retries}回の再試行後も起動に失敗しました。")
        
        print("👋 Botが終了しました。") 