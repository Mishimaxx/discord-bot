import os
import discord
from discord.ext import commands
from discord import ui
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
        
        # 特定のフレーズに対する特別な応答
        ai_keywords = ['AIか本物', 'aiか本物', 'エーアイか本物', 'お前はAI', 'お前はai', 'お前はエーアイ', '君はAI', '君はai', 'あなたはAI', 'あなたはai']
        rion_keywords = ['りおん', 'リオン', 'rion', 'Rion', 'RION']
        
        # AIかりおんかを問われた場合の特別応答
        if any(ai_keyword in content for ai_keyword in ai_keywords) and any(rion_keyword in content for rion_keyword in rion_keywords):
            await message.reply("俺が本物のりおんやで")
            return
        
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
    
    embed = discord.Embed(
        title="🤖 リオンBot - コマンド一覧",
        description="各機能の詳細なコマンド一覧です。カテゴリ別に整理されています。",
        color=0x2ecc71
    )
    
    # 🎯 チーム分けシステム
    team_commands = [
        "`!team [形式]` - 自動チーム分け (2v1, 3v3, 5v5など)",
        "`!qt [形式]` - クイックチーム分け",
        "`!vc_team [形式]` - VC内メンバーでチーム分け",
        "`!rank_team [形式]` - ランクバランス調整チーム分け"
    ]
    
    embed.add_field(
        name="🎯 チーム分けシステム",
        value="\n".join(team_commands),
        inline=False
    )
    
    # 🏆 ランク管理システム
    rank_commands = [
        "`!rank set current [ランク]` - 現在ランクを設定",
        "`!rank set peak [ランク]` - 最高ランクを設定", 
        "`!rank show [@ユーザー]` - ランク情報を表示",
        "`!ranklist` - 設定可能ランク一覧を表示",
        "`!rank_team` - ランクバランスチーム分け"
    ]
    
    embed.add_field(
        name="🏆 ランク管理システム",
        value="\n".join(rank_commands),
        inline=False
    )
    
    # ⚔️ ゲーム募集システム
    recruit_commands = [
        "**カスタム募集**",
        "`!custom create [人数] [時間]` - カスタムゲーム募集開始",
        "`!custom join/leave/status` - 参加/離脱/状況確認",
        "`!custom add @ユーザー` - 🆕 メンバー手動追加",
        "`!custom kick @ユーザー` - メンバー除外",
        "",
        "**ランクマッチ募集**", 
        "`!ranked create [ランク帯] [時間]` - ランク募集開始",
        "`!ranked join/leave/status/check` - 参加/離脱/状況/ランク確認",
        "`!ranked add @ユーザー` - 🆕 メンバー手動追加（ランク条件チェック付き）",
        "`!ranked kick @ユーザー` - メンバー除外"
    ]
    
    embed.add_field(
        name="⚔️ ゲーム募集システム",
        value="\n".join(recruit_commands),
        inline=False
    )
    
    # 🎮 トーナメントシステム  
    tournament_commands = [
        "`!tournament create [形式]` - トーナメント作成",
        "`!tournament join/start/bracket` - 参加/開始/ブラケット表示",
        "`!tournament result [勝者]` - 試合結果入力",
        "`!tournament status/next/end` - 状況確認/次の試合/終了"
    ]
    
    embed.add_field(
        name="🎮 トーナメントシステム",
        value="\n".join(tournament_commands),
        inline=False
    )
    
    # 🌐 VALORANT機能
    valorant_commands = [
        "`!valorant [RiotID#Tag]` - VALORANT統計表示", 
        "`!valorant_match [RiotID#Tag]` - 試合履歴表示",
        "`!map [数]` - マップルーレット",
        "`!maplist` - 全マップ一覧",
        "`!mapinfo [マップ名]` - マップ詳細情報"
    ]
    
    embed.add_field(
        name="🌐 VALORANT機能",
        value="\n".join(valorant_commands),
        inline=False
    )
    
    # 🧠 AI機能
    ai_commands = [
        "`!ai [質問]` - AI会話",
        "`!expert [質問]` - 専門的な回答",
        "`!creative [プロンプト]` - 創作的な回答",
        "`!translate [テキスト]` - 翻訳",
        "`!summarize [テキスト]` - 要約"
    ]
    
    embed.add_field(
        name="🧠 AI機能",
        value="\n".join(ai_commands),
        inline=True
    )
    
    # 📊 情報・統計
    info_commands = [
        "`!info` - サーバー情報",
        "`!members` - メンバー統計",
        "`!userinfo [@ユーザー]` - ユーザー情報",
        "`!mystats [@ユーザー]` - メンバー統計",
        "`!ping` - 応答速度確認"
    ]
    
    embed.add_field(
        name="📊 情報・統計",
        value="\n".join(info_commands),
        inline=True
    )
    
    # 💡 使用例
    usage_examples = [
        "**基本例:**",
        "`!rank set current ダイヤ2` - ランク設定",
        "`!custom create 10人 20:00` - カスタム募集",
        "`!ranked create ダイヤ帯 21:00` - ランク募集",
        "",
        "**ランク条件例:**",
        "`プラチナ以上` `ダイヤ以下` `any`（問わず）",
        "",
        "**手動追加例:**",
        "`!custom add @user1 @user2` - 複数追加可能",
        "`!ranked add @user1` - ランク条件自動チェック"
    ]
    
    embed.add_field(
        name="💡 使用例",
        value="\n".join(usage_examples),
        inline=False
    )
    
    # ✨ 特殊機能
    special_features = [
        "🎯 **ランクバランス** - 自動でバランス調整されたチーム分け",
        "🔍 **自動チェック** - 参加時にランク条件を自動確認",
        "📊 **リアルタイム統計** - 参加者のランク分布と評価",
        "⏰ **自動リマインダー** - 開始5分前に自動通知",
        "🖱️ **ボタン操作** - 参加/離脱/チーム分けがワンクリック",
        "👥 **手動管理** - 作成者による参加者の追加・除外"
    ]
    
    embed.add_field(
        name="✨ 特殊機能",
        value="\n".join(special_features),
        inline=False
    )
    
    # 💬 その他
    other_features = [
        "• **@リオン + メッセージ** - 自然な会話",
        "• **「チーム分けして」** - 自動チーム分け実行",
        "• **ボタンUI** - クリックで簡単操作",
        "• **コマンド/ボタン両対応** - お好みの方法で操作可能"
    ]
    
    embed.add_field(
        name="💬 その他の機能",
        value="\n".join(other_features),
        inline=False
    )
    
    # フッター情報
    command_count = len(bot.commands)
    embed.set_footer(
        text=f"📝 登録コマンド数: {command_count}個 | 🆕 最新機能: 手動メンバー追加",
        icon_url=bot.user.avatar.url if bot.user.avatar else None
    )
    
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
    "レディアント": {"tier": 9, "display": "レディアント", "value": 900, "color": 0xFFFFFF, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/radiant.png"},
    "イモータル3": {"tier": 8, "display": "イモータル 3", "value": 803, "color": 0xBA55D3, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/immortal3.png"},
    "イモータル2": {"tier": 8, "display": "イモータル 2", "value": 802, "color": 0xBA55D3, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/immortal2.png"},
    "イモータル1": {"tier": 8, "display": "イモータル 1", "value": 801, "color": 0xBA55D3, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/immortal1.png"},
    "アセンダント3": {"tier": 7, "display": "アセンダント 3", "value": 703, "color": 0x32CD32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/ascendant3.png"},
    "アセンダント2": {"tier": 7, "display": "アセンダント 2", "value": 702, "color": 0x32CD32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/ascendant2.png"},
    "アセンダント1": {"tier": 7, "display": "アセンダント 1", "value": 701, "color": 0x32CD32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/ascendant1.png"},
    "ダイヤ3": {"tier": 6, "display": "ダイヤモンド 3", "value": 603, "color": 0x87CEEB, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/diamond3.png"},
    "ダイヤ2": {"tier": 6, "display": "ダイヤモンド 2", "value": 602, "color": 0x87CEEB, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/diamond2.png"},
    "ダイヤ1": {"tier": 6, "display": "ダイヤモンド 1", "value": 601, "color": 0x87CEEB, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/diamond1.png"},
    "プラチナ3": {"tier": 5, "display": "プラチナ 3", "value": 503, "color": 0x40E0D0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/platinum3.png"},
    "プラチナ2": {"tier": 5, "display": "プラチナ 2", "value": 502, "color": 0x40E0D0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/platinum2.png"},
    "プラチナ1": {"tier": 5, "display": "プラチナ 1", "value": 501, "color": 0x40E0D0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/platinum1.png"},
    "ゴールド3": {"tier": 4, "display": "ゴールド 3", "value": 403, "color": 0xFFD700, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/gold3.png"},
    "ゴールド2": {"tier": 4, "display": "ゴールド 2", "value": 402, "color": 0xFFD700, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/gold2.png"},
    "ゴールド1": {"tier": 4, "display": "ゴールド 1", "value": 401, "color": 0xFFD700, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/gold1.png"},
    "シルバー3": {"tier": 3, "display": "シルバー 3", "value": 303, "color": 0xC0C0C0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/silver3.png"},
    "シルバー2": {"tier": 3, "display": "シルバー 2", "value": 302, "color": 0xC0C0C0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/silver2.png"},
    "シルバー1": {"tier": 3, "display": "シルバー 1", "value": 301, "color": 0xC0C0C0, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/silver1.png"},
    "ブロンズ3": {"tier": 2, "display": "ブロンズ 3", "value": 203, "color": 0xCD7F32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/bronze3.png"},
    "ブロンズ2": {"tier": 2, "display": "ブロンズ 2", "value": 202, "color": 0xCD7F32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/bronze2.png"},
    "ブロンズ1": {"tier": 2, "display": "ブロンズ 1", "value": 201, "color": 0xCD7F32, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/bronze1.png"},
    "アイアン3": {"tier": 1, "display": "アイアン 3", "value": 103, "color": 0x696969, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/iron3.png"},
    "アイアン2": {"tier": 1, "display": "アイアン 2", "value": 102, "color": 0x696969, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/iron2.png"},
    "アイアン1": {"tier": 1, "display": "アイアン 1", "value": 101, "color": 0x696969, "image_url": "https://raw.githubusercontent.com/Mishimaxx/discord-bot/main/images/ranks/iron1.png"}
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

@bot.command(name='rank_team', aliases=['rt', 'vc_rank_team'], help='VC内メンバーをランクでバランス調整してチーム分けします')
@prevent_duplicate_execution
async def rank_based_team_divide(ctx, rank_type="current", format_type=None):
    """ランクベースでVC内メンバーをチーム分け"""
    try:
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ このコマンドはサーバー内でのみ使用できます。")
            return
        
        # ランクタイプのバリデーション
        if rank_type.lower() not in ["current", "peak", "現在", "最高"]:
            # 第一引数がフォーマットタイプの場合
            if rank_type.lower() in ['2v2', '3v3', '5v5', '2v1', '1v1', '4v4']:
                format_type = rank_type
                rank_type = "current"
            else:
                await ctx.send("❌ ランクタイプは `current`（現在）または `peak`（最高）を指定してください")
                return
        
        # ランクタイプを統一
        rank_key = "current" if rank_type.lower() in ["current", "現在"] else "peak"
        rank_display = "現在ランク" if rank_key == "current" else "最高ランク"
        
        # VC内メンバーを取得
        vc_members = []
        voice_channels_with_members = []
        
        for channel in guild.voice_channels:
            if channel.members:
                channel_members = [member for member in channel.members if not member.bot]
                if channel_members:
                    vc_members.extend(channel_members)
                    voice_channels_with_members.append(f"🔊 {channel.name} ({len(channel_members)}人)")
        
        # 重複除去
        vc_members = list(set(vc_members))
        
        if len(vc_members) < 2:
            embed = discord.Embed(
                title="❌ VC内メンバー不足", 
                color=discord.Color.red()
            )
            embed.add_field(
                name="現在の状況",
                value=f"VC内人間メンバー: {len(vc_members)}人\nランクチーム分けには最低2人必要です。",
                inline=False
            )
            await ctx.send(embed=embed)
            return
        
        # メンバーのランク情報を取得
        ranked_members = []
        unranked_members = []
        total_rank_value = 0
        rank_count = 0
        
        for member in vc_members:
            user_id = member.id
            if user_id in user_ranks and user_ranks[user_id].get(rank_key):
                rank_name = user_ranks[user_id][rank_key]
                rank_value = VALORANT_RANKS[rank_name]['value']
                ranked_members.append({
                    'member': member,
                    'rank': rank_name,
                    'value': rank_value
                })
                total_rank_value += rank_value
                rank_count += 1
            else:
                unranked_members.append(member)
        
        # 平均ランク値を計算（未設定者用）
        if rank_count > 0:
            avg_rank_value = total_rank_value / rank_count
        else:
            avg_rank_value = 300  # シルバー1レベル
        
        # 未ランクメンバーを平均ランクとして追加
        for member in unranked_members:
            ranked_members.append({
                'member': member,
                'rank': None,
                'value': avg_rank_value
            })
        
        if len(ranked_members) < 2:
            await ctx.send("❌ チーム分けには最低2人必要です。")
            return
        
        # ランクバランス調整アルゴリズム
        def balance_teams(members, team_size):
            """ランク値の合計ができるだけ均等になるようにチーム分け"""
            members = sorted(members, key=lambda x: x['value'], reverse=True)
            team1 = []
            team2 = []
            
            for member in members:
                # 現在のチーム合計値を計算
                team1_total = sum(m['value'] for m in team1)
                team2_total = sum(m['value'] for m in team2)
                
                # チームサイズ制限もチェック
                if len(team1) >= team_size:
                    team2.append(member)
                elif len(team2) >= team_size:
                    team1.append(member)
                else:
                    # より合計値が低いチームに追加
                    if team1_total <= team2_total:
                        team1.append(member)
                    else:
                        team2.append(member)
            
            return team1, team2
        
        # フォーマット別チーム分け
        embed = discord.Embed(title=f"🎯 ランクバランスチーム分け ({rank_display})", color=0xff4655)
        
        if format_type:
            format_type = format_type.lower()
            
            if format_type in ['2v2', '2対2']:
                if len(ranked_members) < 4:
                    await ctx.send("❌ 2v2には最低4人必要です。")
                    return
                
                team1, team2 = balance_teams(ranked_members[:4], 2)
                extras = ranked_members[4:] if len(ranked_members) > 4 else []
                
            elif format_type in ['3v3', '3対3']:
                if len(ranked_members) < 6:
                    await ctx.send(f"⚠️ 3v3には6人必要ですが、{len(ranked_members)}人しかいません。")
                    if len(ranked_members) >= 4:
                        team_size = len(ranked_members) // 2
                        team1, team2 = balance_teams(ranked_members, team_size)
                        extras = []
                    else:
                        await ctx.send("❌ チーム分けには最低4人必要です。")
                        return
                else:
                    team1, team2 = balance_teams(ranked_members[:6], 3)
                    extras = ranked_members[6:]
                
            elif format_type in ['5v5', '5対5']:
                if len(ranked_members) < 10:
                    await ctx.send(f"⚠️ 5v5には10人必要ですが、{len(ranked_members)}人しかいません。")
                    if len(ranked_members) >= 6:
                        team_size = len(ranked_members) // 2
                        team1, team2 = balance_teams(ranked_members, team_size)
                        extras = []
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1, team2 = balance_teams(ranked_members[:10], 5)
                    extras = ranked_members[10:]
                
            elif format_type in ['2v1', '2対1']:
                if len(ranked_members) < 3:
                    await ctx.send("❌ 2v1には最低3人必要です。")
                    return
                
                # 2v1は特別処理（最強者1人 vs 他2人）
                sorted_members = sorted(ranked_members, key=lambda x: x['value'], reverse=True)
                team1 = sorted_members[1:3]  # 2-3位
                team2 = [sorted_members[0]]   # 1位
                extras = sorted_members[3:] if len(sorted_members) > 3 else []
                
            elif format_type in ['1v1', '1対1']:
                if len(ranked_members) < 2:
                    await ctx.send("❌ 1v1には最低2人必要です。")
                    return
                
                # 1v1は最もランクが近い者同士
                sorted_members = sorted(ranked_members, key=lambda x: x['value'], reverse=True)
                team1 = [sorted_members[0]]
                team2 = [sorted_members[1]]
                extras = sorted_members[2:]
                
            elif format_type in ['4v4', '4対4']:
                if len(ranked_members) < 8:
                    await ctx.send(f"⚠️ 4v4には8人必要ですが、{len(ranked_members)}人しかいません。")
                    if len(ranked_members) >= 6:
                        team_size = len(ranked_members) // 2
                        team1, team2 = balance_teams(ranked_members, team_size)
                        extras = []
                    else:
                        await ctx.send("❌ チーム分けには最低6人必要です。")
                        return
                else:
                    team1, team2 = balance_teams(ranked_members[:8], 4)
                    extras = ranked_members[8:]
            else:
                await ctx.send("❌ 対応していない形式です。使用可能: `2v1`, `3v3`, `2v2`, `1v1`, `4v4`, `5v5`")
                return
        else:
            # 自動フォーマット選択
            member_count = len(ranked_members)
            
            if member_count >= 10:
                team1, team2 = balance_teams(ranked_members[:10], 5)
                extras = ranked_members[10:]
                format_type = "5v5"
            elif member_count >= 8:
                team1, team2 = balance_teams(ranked_members[:8], 4)
                extras = ranked_members[8:]
                format_type = "4v4"
            elif member_count >= 6:
                team1, team2 = balance_teams(ranked_members[:6], 3)
                extras = ranked_members[6:]
                format_type = "3v3"
            elif member_count >= 4:
                team1, team2 = balance_teams(ranked_members[:4], 2)
                extras = ranked_members[4:]
                format_type = "2v2"
            elif member_count == 3:
                sorted_members = sorted(ranked_members, key=lambda x: x['value'], reverse=True)
                team1 = sorted_members[1:3]
                team2 = [sorted_members[0]]
                extras = []
                format_type = "2v1"
            else:
                sorted_members = sorted(ranked_members, key=lambda x: x['value'], reverse=True)
                team1 = [sorted_members[0]]
                team2 = [sorted_members[1]]
                extras = []
                format_type = "1v1"
        
        # チーム情報を表示
        def format_team_info(team, team_name, team_color):
            if not team:
                return
            
            team_display = []
            team_total = 0
            rank_counts = {}
            
            for member_data in team:
                member = member_data['member']
                rank = member_data['rank']
                value = member_data['value']
                team_total += value
                
                if rank:
                    rank_info = VALORANT_RANKS[rank]
                    member_display = f"• {member.display_name} ({rank_info['display']})"
                    rank_counts[rank_info['display']] = rank_counts.get(rank_info['display'], 0) + 1
                else:
                    member_display = f"• {member.display_name} (ランク未設定)"
                    rank_counts['ランク未設定'] = rank_counts.get('ランク未設定', 0) + 1
                
                team_display.append(member_display)
            
            avg_rank = team_total / len(team) if team else 0
            
            embed.add_field(
                name=f"{team_color} {team_name} ({len(team)}人)",
                value="\n".join(team_display),
                inline=True
            )
            
            # チーム平均ランク値を表示
            embed.add_field(
                name=f"📊 {team_name} 平均値",
                value=f"{avg_rank:.0f}",
                inline=True
            )
            
            return avg_rank
        
        # チーム1の情報
        avg1 = format_team_info(team1, "チーム1", "🔴")
        
        # スペーサー（3列レイアウト用）
        embed.add_field(name="", value="", inline=True)
        
        # チーム2の情報
        avg2 = format_team_info(team2, "チーム2", "🔵")
        
        # バランス情報
        balance_diff = abs(avg1 - avg2) if avg1 and avg2 else 0
        balance_quality = "完璧" if balance_diff < 50 else "良好" if balance_diff < 100 else "やや偏り" if balance_diff < 150 else "偏りあり"
        
        embed.add_field(
            name="⚖️ バランス評価",
            value=f"{balance_quality} (差: {balance_diff:.0f})",
            inline=False
        )
        
        # 待機メンバー
        if extras:
            extras_display = []
            for member_data in extras:
                member = member_data['member']
                rank = member_data['rank']
                if rank:
                    rank_info = VALORANT_RANKS[rank]
                    extras_display.append(f"• {member.display_name} ({rank_info['display']})")
                else:
                    extras_display.append(f"• {member.display_name} (ランク未設定)")
            
            embed.add_field(
                name="⚪ 待機",
                value="\n".join(extras_display),
                inline=False
            )
        
        # 統計情報
        ranked_count = len([m for m in ranked_members if m['rank']])
        unranked_count = len(unranked_members)
        
        embed.add_field(
            name="📊 統計情報",
            value=f"基準: {rank_display}\n"
                  f"ランク設定済み: {ranked_count}人\n"
                  f"未設定: {unranked_count}人\n"
                  f"形式: {format_type}",
            inline=False
        )
        
        # VC情報
        if voice_channels_with_members:
            embed.add_field(
                name="🎤 対象VC", 
                value="\n".join(voice_channels_with_members), 
                inline=False
            )
        
        embed.set_footer(text=f"🎯 ランクバランス調整 | 未設定者は平均ランク({avg_rank_value:.0f})として計算")
        
        await ctx.send(embed=embed)
        
        # 追加メッセージ
        balance_msg = "⚖️ ランクバランスを考慮したチーム分けを行いました！"
        if unranked_count > 0:
            balance_msg += f"\n💡 {unranked_count}人がランク未設定のため、平均ランクで計算しています。"
        
        await ctx.send(balance_msg)
        
    except Exception as e:
        await ctx.send(f"❌ ランクベースチーム分けでエラーが発生しました: {str(e)}")
        print(f"ランクベースチーム分けエラー: {e}")
        import traceback
        traceback.print_exc()

# ===============================
# ゲーム管理機能のデータ構造
# ===============================

# スクリム/カスタムゲーム管理
active_scrims = {}  # {channel_id: scrim_data}
scrim_reminders = {}  # {scrim_id: reminder_task}

# ランクマッチ募集管理
active_rank_recruits = {}  # {channel_id: rank_recruit_data}
rank_recruit_reminders = {}  # {recruit_id: reminder_task}

# キュー管理（ランク別）



# トーナメント管理
active_tournaments = {}  # {guild_id: tournament_data}
tournament_matches = {}  # {tournament_id: [match_data]}

class TournamentView(discord.ui.View):
    """トーナメント用UIボタン"""
    
    def __init__(self, timeout=3600):  # 1時間でタイムアウト
        super().__init__(timeout=timeout)
    
    async def on_timeout(self):
        """タイムアウト時の処理"""
        try:
            # 全てのボタンを無効化
            for item in self.children:
                item.disabled = True
            
            # トーナメントが残っている場合はEmbed更新を試行
            for guild_id, tournament in list(active_tournaments.items()):
                if tournament.get('status') == 'registration':
                    try:
                        # トーナメントメッセージの更新を試行
                        embed = await create_tournament_embed(tournament, None)
                        embed.add_field(
                            name="⏰ タイムアウト", 
                            value="ボタンの有効期限が切れました。コマンドで操作してください。", 
                            inline=False
                        )
                        # メッセージ更新は context がないため、ログに記録のみ
                        print(f"TournamentView タイムアウト: guild_id={guild_id}")
                    except Exception as e:
                        print(f"TournamentView タイムアウト時のEmbed更新エラー: {e}")
        except Exception as e:
            print(f"TournamentView タイムアウト処理エラー: {e}")
            
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        """エラーハンドリング"""
        print(f"TournamentView エラー: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
        except:
            pass  # エラー通知に失敗しても継続
        
    @discord.ui.button(label='参加', emoji='✅', style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """参加ボタン"""
        await interaction.response.defer()
        
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        
        if guild_id not in active_tournaments:
            await interaction.followup.send("❌ アクティブなトーナメントがありません。", ephemeral=True)
            return
        
        tournament = active_tournaments[guild_id]
        
        if tournament['status'] != 'registration':
            await interaction.followup.send("❌ 現在参加登録を受け付けていません。", ephemeral=True)
            return
        
        if user_id in [p['user_id'] for p in tournament['participants']]:
            await interaction.followup.send("⚠️ 既に参加登録済みです。", ephemeral=True)
            return
        
        if len(tournament['participants']) >= tournament['max_participants']:
            await interaction.followup.send("❌ 参加者が満員です。", ephemeral=True)
            return
        
        # 参加登録
        participant = {
            'user_id': user_id,
            'user': interaction.user,
            'joined_at': datetime.now(),
            'wins': 0,
            'losses': 0
        }
        
        tournament['participants'].append(participant)
        
        current_count = len(tournament['participants'])
        
        # トーナメントメッセージを更新
        embed = await create_tournament_embed(tournament, interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)
        
        await interaction.followup.send(f"✅ {interaction.user.display_name} がトーナメントに参加しました！ ({current_count}/{tournament['max_participants']})", ephemeral=False)
    
    @discord.ui.button(label='離脱', emoji='❌', style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """離脱ボタン"""
        await interaction.response.defer()
        
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        
        if guild_id not in active_tournaments:
            await interaction.followup.send("❌ アクティブなトーナメントがありません。", ephemeral=True)
            return
        
        tournament = active_tournaments[guild_id]
        
        if tournament['status'] != 'registration':
            await interaction.followup.send("❌ 既に開始されているため離脱できません。", ephemeral=True)
            return
        
        # 参加者から削除
        for i, participant in enumerate(tournament['participants']):
            if participant['user_id'] == user_id:
                del tournament['participants'][i]
                
                # トーナメントメッセージを更新
                embed = await create_tournament_embed(tournament, interaction.guild)
                await interaction.edit_original_response(embed=embed, view=self)
                
                await interaction.followup.send(f"✅ {interaction.user.display_name} がトーナメントから離脱しました。", ephemeral=False)
                return
        
        await interaction.followup.send("❌ トーナメントに参加していません。", ephemeral=True)
    
    @discord.ui.button(label='ステータス確認', emoji='📊', style=discord.ButtonStyle.secondary)
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ステータス確認ボタン"""
        await interaction.response.defer()
        
        guild_id = interaction.guild.id
        
        if guild_id not in active_tournaments:
            await interaction.followup.send("❌ アクティブなトーナメントがありません。", ephemeral=True)
            return
        
        tournament = active_tournaments[guild_id]
        
        status_map = {
            'registration': '📝 参加者募集中',
            'ongoing': '⚔️ 進行中',
            'ended': '🏁 終了'
        }
        
        embed = discord.Embed(
            title="📊 トーナメント詳細ステータス",
            color=0x00aaff
        )
        
        embed.add_field(
            name="基本情報",
            value=f"**ステータス:** {status_map.get(tournament['status'], tournament['status'])}\n"
                  f"**形式:** {tournament['tournament_type']}\n"
                  f"**参加者:** {len(tournament['participants'])}/{tournament['max_participants']}人\n"
                  f"**作成者:** {tournament['creator'].display_name}",
            inline=True
        )
        
        # 参加者リスト
        if tournament['participants']:
            participants_list = []
            for i, participant in enumerate(tournament['participants'], 1):
                participants_list.append(f"{i}. {participant['user'].display_name}")
            
            embed.add_field(
                name="👥 参加者一覧",
                value="\n".join(participants_list[:10]) + ("..." if len(participants_list) > 10 else ""),
                inline=True
            )
        
        if tournament['status'] == 'ongoing':
            current_round_matches = [m for m in tournament['bracket'] if m['round'] == tournament['current_round']]
            pending_matches = [m for m in current_round_matches if m['status'] == 'pending']
            
            embed.add_field(
                name="進行状況",
                value=f"**現在ラウンド:** {tournament['current_round']}\n"
                      f"**待機中試合:** {len(pending_matches)}試合\n"
                      f"**完了試合:** {len([m for m in tournament['bracket'] if m['status'] == 'completed'])}試合",
                inline=False
            )
        
        embed.set_footer(text=f"ID: {tournament['id'][:8]} | 作成: {tournament['created_at'].strftime('%m/%d %H:%M')}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label='開始', emoji='🏁', style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """開始ボタン（作成者のみ）"""
        await interaction.response.defer()
        
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        
        if guild_id not in active_tournaments:
            await interaction.followup.send("❌ アクティブなトーナメントがありません。", ephemeral=True)
            return
        
        tournament = active_tournaments[guild_id]
        
        # 権限チェック
        if user_id != tournament['creator'].id and not interaction.user.guild_permissions.manage_messages:
            await interaction.followup.send("❌ トーナメント作成者または管理者のみ開始できます。", ephemeral=True)
            return
        
        if tournament['status'] != 'registration':
            await interaction.followup.send("❌ 既に開始されているか、終了しています。", ephemeral=True)
            return
        
        participants = tournament['participants']
        
        if len(participants) < 4:
            await interaction.followup.send("❌ トーナメント開始には最低4人必要です。", ephemeral=True)
            return
        
        # ブラケット生成処理（start_tournament関数と同じロジック）
        import math
        
        # 2の累乗に調整
        bracket_size = 2 ** math.ceil(math.log2(len(participants)))
        
        # 参加者をシャッフル
        shuffled_participants = participants.copy()
        random.shuffle(shuffled_participants)
        
        # 不戦勝者（BYE）を追加
        while len(shuffled_participants) < bracket_size:
            shuffled_participants.append(None)  # BYE
        
        # 第1ラウンドの試合を作成
        matches = []
        match_id = 1
        
        for i in range(0, len(shuffled_participants), 2):
            player1 = shuffled_participants[i]
            player2 = shuffled_participants[i + 1] if i + 1 < len(shuffled_participants) else None
            
            match_data = {
                'id': match_id,
                'round': 1,
                'player1': player1,
                'player2': player2,
                'winner': None,
                'status': 'pending'  # pending, completed
            }
            
            # BYE の処理
            if player1 and not player2:
                match_data['winner'] = player1
                match_data['status'] = 'completed'
            elif player2 and not player1:
                match_data['winner'] = player2
                match_data['status'] = 'completed'
            
            matches.append(match_data)
            match_id += 1
        
        tournament['bracket'] = matches
        tournament['status'] = 'ongoing'
        tournament['current_round'] = 1
        
        embed = discord.Embed(
            title="🏁 トーナメント開始！",
            description=f"**{tournament['tournament_type']}** トーナメントが開始されました",
            color=0xffd700
        )
        
        embed.add_field(
            name="📊 情報",
            value=f"**参加者数:** {len([p for p in participants if p])}人\n"
                  f"**第1ラウンド試合数:** {len([m for m in matches if m['status'] == 'pending'])}試合\n"
                  f"**形式:** シングルエリミネーション",
            inline=False
        )
        
        embed.add_field(
            name="🎯 次のステップ",
            value="`!tournament bracket` - ブラケット確認\n"
                  "`!tournament next` - 次の試合確認\n"
                  "`!tournament result @勝者` - 結果入力",
            inline=False
        )
        
        # ボタンを無効化
        for item in self.children:
            item.disabled = True
        
        await interaction.edit_original_response(embed=embed, view=self)
        await interaction.followup.send("🎉 トーナメントが開始されました！", ephemeral=False)

async def create_tournament_embed(tournament, guild):
    """トーナメント募集のEmbed作成"""
    # 参加者リスト作成
    participants_list = []
    
    for participant in tournament['participants']:
        participants_list.append(f"• {participant['user'].display_name}")
    
    status_map = {
        'registration': '📝 参加者募集中',
        'ongoing': '⚔️ 進行中',
        'ended': '🏁 終了'
    }
    
    current_count = len(tournament['participants'])
    max_participants = tournament['max_participants']
    
    title = "🏆 トーナメント募集"
    if current_count >= 4:
        title = "🎉 トーナメント募集（開始可能）"
    if current_count >= max_participants:
        title = "🔥 トーナメント募集（満員）"
    
    embed = discord.Embed(
        title=title,
        description=f"**{tournament['tournament_type']}** の参加者を募集中",
        color=0xffd700 if current_count >= 4 else 0x4a90e2
    )
    
    embed.add_field(
        name="📊 募集情報",
        value=f"**形式:** {tournament['tournament_type']}\n"
              f"**最大人数:** {max_participants}人\n"
              f"**最小開始人数:** 4人\n"
              f"**現在の参加者:** {current_count}/{max_participants}人\n"
              f"**ステータス:** {status_map.get(tournament['status'], tournament['status'])}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list[:8]) + ("..." if len(participants_list) > 8 else "") if participants_list else "なし",
        inline=True
    )
    
    if tournament.get('description'):
        embed.add_field(
            name="📝 詳細",
            value=tournament['description'],
            inline=False
        )
    
    if current_count >= 4:
        embed.add_field(
            name="🎯 開始可能",
            value="開始ボタンまたは `!tournament start` で開始できます！",
            inline=False
        )
    
    embed.set_footer(text=f"作成者: {tournament['creator'].display_name} | ID: {tournament['id'][:8]}")
    
    return embed

# ===============================
# メインコントロールパネル
# ===============================

class MainControlPanel(discord.ui.View):
    """メイン機能コントロールパネル"""
    
    def __init__(self):
        super().__init__(timeout=None)  # 永続的なパネル
    
    @discord.ui.button(label='🎯 ゲーム募集', style=discord.ButtonStyle.primary, row=0)
    async def game_recruit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ゲーム募集パネル"""
        view = GameRecruitPanel()
        embed = discord.Embed(
            title="🎮 ゲーム募集パネル",
            description="各種ゲーム募集を作成できます",
            color=0x00aaff
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label='🎲 ゲーム機能', style=discord.ButtonStyle.success, row=0)
    async def game_tools_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ゲーム機能パネル"""
        view = GameToolsPanel()
        embed = discord.Embed(
            title="🎲 ゲーム機能パネル",
            description="チーム分け、マップ選択、統計などの機能",
            color=0x00ff88
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label='🏆 ランク管理', style=discord.ButtonStyle.secondary, row=0)
    async def rank_management_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ランク管理パネル"""
        view = RankManagementPanel()
        embed = discord.Embed(
            title="🏆 ランク管理パネル",
            description="VALORANTランクの設定と確認",
            color=0xffd700
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label='🤖 AI機能', style=discord.ButtonStyle.danger, row=1)
    async def ai_tools_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """AI機能パネル"""
        view = AIToolsPanel()
        embed = discord.Embed(
            title="🤖 AI機能パネル",
            description="AI会話、翻訳、要約などの機能",
            color=0xff6b6b
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label='📊 情報・統計', style=discord.ButtonStyle.primary, row=1)
    async def info_stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """情報・統計パネル"""
        view = InfoStatsPanel()
        embed = discord.Embed(
            title="📊 情報・統計パネル",
            description="サーバー情報、ユーザー統計、Bot状態",
            color=0x4a90e2
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label='⚙️ 管理機能', style=discord.ButtonStyle.secondary, row=1)
    async def admin_tools_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """管理機能パネル"""
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ 管理者権限が必要です。", ephemeral=True)
            return
        
        view = AdminToolsPanel()
        embed = discord.Embed(
            title="⚙️ 管理機能パネル",
            description="管理者専用機能",
            color=0x666666
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class GameRecruitPanel(discord.ui.View):
    """ゲーム募集専用パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='🎯 カスタムゲーム', style=discord.ButtonStyle.primary)
    async def custom_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomGameModal())
    
    @discord.ui.button(label='🏆 ランクマッチ', style=discord.ButtonStyle.success)
    async def ranked_match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RankedMatchModal())
    
    @discord.ui.button(label='🏅 トーナメント', style=discord.ButtonStyle.danger)
    async def tournament_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TournamentModal())

class GameToolsPanel(discord.ui.View):
    """ゲーム機能パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='🎯 チーム分け', style=discord.ButtonStyle.primary, row=0)
    async def team_divide_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        try:
            # 疑似的なctxオブジェクトを作成（team_divide関数用）
            class PseudoCtx:
                def __init__(self, interaction):
                    self.channel = interaction.channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                    # prevent_duplicate_executionデコレータ用の属性追加
                    self.id = interaction.id
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    try:
                        await self._interaction.followup.send(content=content, embed=embed, view=view)
                    except Exception as e:
                        print(f"チーム分けボタンの送信エラー: {e}")
                        # フォールバック：エフェメラルメッセージで送信
                        try:
                            await self._interaction.followup.send(
                                content="⚠️ 一時的なエラーが発生しました。",
                                ephemeral=True
                            )
                        except:
                            pass
            
            pseudo_ctx = PseudoCtx(interaction)
            
            print(f"チーム分けボタン: ユーザーID {interaction.user.id}, チャンネル {interaction.channel.name}")
            
            # コマンド版と同じteam_divide関数を呼び出し
            await team_divide(pseudo_ctx, None)
            
        except Exception as e:
            print(f"チーム分けボタンエラー詳細: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ チーム分けでエラーが発生しました: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label='🗺️ マップ選択', style=discord.ButtonStyle.success, row=0)
    async def map_select_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            
            # 疑似的なctxオブジェクトを作成（valorant_map_roulette関数用）
            class PseudoCtx:
                def __init__(self, interaction):
                    self.channel = interaction.channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                    # prevent_duplicate_executionデコレータ用の属性追加
                    self.id = interaction.id
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    try:
                        await self._interaction.followup.send(content=content, embed=embed, view=view)
                    except Exception as e:
                        print(f"マップ選択ボタンの送信エラー: {e}")
                        # フォールバック：エフェメラルメッセージで送信
                        try:
                            await self._interaction.followup.send(
                                content="⚠️ 一時的なエラーが発生しました。",
                                ephemeral=True
                            )
                        except:
                            pass
            
            pseudo_ctx = PseudoCtx(interaction)
            
            # コマンド版と同じvaloranta_map_roulette関数を呼び出し
            await valorant_map_roulette(pseudo_ctx, 1)
            
        except Exception as e:
            print(f"マップ選択ボタンエラー: {e}")
            import traceback
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ マップ選択でエラーが発生しました。", ephemeral=True)
                else:
                    await interaction.followup.send("❌ マップ選択でエラーが発生しました。", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(label='📊 統計確認', style=discord.ButtonStyle.secondary, row=0)
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StatsModal())
    
    @discord.ui.button(label='🎲 サイコロ', style=discord.ButtonStyle.primary, row=1)
    async def dice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            
            # 疑似的なctxオブジェクトを作成（roll_dice関数用）
            class PseudoCtx:
                def __init__(self, interaction):
                    self.channel = interaction.channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                    # prevent_duplicate_executionデコレータ用の属性追加
                    self.id = interaction.id
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    try:
                        await self._interaction.followup.send(content=content, embed=embed, view=view)
                    except Exception as e:
                        print(f"サイコロボタンの送信エラー: {e}")
                        # フォールバック：エフェメラルメッセージで送信
                        try:
                            await self._interaction.followup.send(
                                content="⚠️ 一時的なエラーが発生しました。",
                                ephemeral=True
                            )
                        except:
                            pass
            
            pseudo_ctx = PseudoCtx(interaction)
            
            # コマンド版と同じroll_dice関数を呼び出し
            await roll_dice(pseudo_ctx, 6)
            
        except Exception as e:
            print(f"サイコロボタンエラー: {e}")
            import traceback
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ サイコロでエラーが発生しました。", ephemeral=True)
                else:
                    await interaction.followup.send("❌ サイコロでエラーが発生しました。", ephemeral=True)
            except:
                pass

class RankManagementPanel(discord.ui.View):
    """ランク管理パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='📝 ランク設定', style=discord.ButtonStyle.primary)
    async def rank_set_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RankSetModal())
    
    @discord.ui.button(label='👀 ランク確認', style=discord.ButtonStyle.success)
    async def rank_show_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（rank_system関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じrank_system関数を呼び出し（show）
        await rank_system(pseudo_ctx, "show")
    
    @discord.ui.button(label='📋 ランク一覧', style=discord.ButtonStyle.secondary)
    async def rank_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（rank_list関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じrank_list関数を呼び出し
        await rank_list(pseudo_ctx)

class AIToolsPanel(discord.ui.View):
    """AI機能パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='💬 AI会話', style=discord.ButtonStyle.primary)
    async def ai_chat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AIChatModal())
    
    @discord.ui.button(label='🌍 翻訳', style=discord.ButtonStyle.success)
    async def translate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TranslateModal())
    
    @discord.ui.button(label='📝 要約', style=discord.ButtonStyle.secondary)
    async def summarize_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SummarizeModal())

class InfoStatsPanel(discord.ui.View):
    """情報・統計パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='🏠 サーバー情報', style=discord.ButtonStyle.primary, row=0)
    async def server_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（server_info関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じserver_info関数を呼び出し
        await server_info(pseudo_ctx)
    
    @discord.ui.button(label='👤 ユーザー情報', style=discord.ButtonStyle.success, row=0)
    async def user_info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（user_info関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じuser_info関数を呼び出し
        await user_info(pseudo_ctx, interaction.user)
    
    @discord.ui.button(label='🤖 Bot状態', style=discord.ButtonStyle.secondary, row=0)
    async def bot_status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（bot_status関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じbot_status関数を呼び出し
        await bot_status(pseudo_ctx)

class AdminToolsPanel(discord.ui.View):
    """管理機能パネル"""
    
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label='🧹 クリーンアップ', style=discord.ButtonStyle.danger)
    async def cleanup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（manual_cleanup関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じmanual_cleanup関数を呼び出し
        await manual_cleanup(pseudo_ctx)
    
    @discord.ui.button(label='📊 使用量確認', style=discord.ButtonStyle.primary)
    async def usage_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（show_usage関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じshow_usage関数を呼び出し
        await show_usage(pseudo_ctx)

# ===== モーダルクラス =====
# 統計確認モーダル（VALORANT統計とユーザー統計の両方に対応）
class StatsModal(discord.ui.Modal, title='📊 統計確認'):
    def __init__(self):
        super().__init__()
    
    riot_id = discord.ui.TextInput(
        label='Riot ID',
        placeholder='例: PlayerName#1234（空白で自分のDiscord統計）',
        required=False,
        max_length=50
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        if self.riot_id.value:
            # 疑似的なctxオブジェクトを作成（valorant_stats関数用）
            class PseudoCtx:
                def __init__(self, interaction):
                    self.channel = interaction.channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
            
            pseudo_ctx = PseudoCtx(interaction)
            
            # コマンド版と同じvalorant_stats関数を呼び出し
            await valorant_stats(pseudo_ctx, riot_id=self.riot_id.value)
        else:
            # 疑似的なctxオブジェクトを作成（show_member_stats関数用）
            class PseudoCtx:
                def __init__(self, interaction):
                    self.channel = interaction.channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
            
            pseudo_ctx = PseudoCtx(interaction)
            
            # コマンド版と同じshow_member_stats関数を呼び出し
            await show_member_stats(pseudo_ctx, interaction.user)

class RankSetModal(discord.ui.Modal, title='📝 ランク設定'):
    def __init__(self):
        super().__init__()
    
    rank_type = discord.ui.TextInput(
        label='ランクタイプ',
        placeholder='current または peak',
        default='current',
        max_length=10
    )
    
    rank_value = discord.ui.TextInput(
        label='ランク',
        placeholder='例: ダイヤ2, プラチナ3, アイアン1',
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        user_id = interaction.user.id
        rank_type = self.rank_type.value.lower()
        rank_input = self.rank_value.value
        
        if rank_type not in ['current', 'peak']:
            await interaction.followup.send("❌ ランクタイプは 'current' または 'peak' を指定してください。", ephemeral=True)
            return
        
        # ランク解析
        parsed_rank = parse_rank_input([rank_input])
        if not parsed_rank:
            await interaction.followup.send("❌ 無効なランク形式です。例: ダイヤ2, プラチナ3", ephemeral=True)
            return
        
        # ユーザーランクデータの初期化
        if user_id not in user_ranks:
            user_ranks[user_id] = {}
        
        user_ranks[user_id][rank_type] = parsed_rank
        rank_info = VALORANT_RANKS[parsed_rank]
        
        embed = discord.Embed(
            title="✅ ランク設定完了",
            color=0x00ff88
        )
        
        embed.add_field(
            name=f"📊 {rank_type.title()}ランク",
            value=f"**{rank_info['display']}**",
            inline=True
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)

class AIChatModal(discord.ui.Modal, title='💬 AI会話'):
    def __init__(self):
        super().__init__()
    
    question = discord.ui.TextInput(
        label='質問',
        placeholder='AIに聞きたいことを入力してください',
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（ask_ai関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じask_ai関数を呼び出し
        await ask_ai(pseudo_ctx, question=self.question.value)

class TranslateModal(discord.ui.Modal, title='🌍 翻訳'):
    def __init__(self):
        super().__init__()
    
    text = discord.ui.TextInput(
        label='翻訳したいテキスト',
        placeholder='翻訳したいテキストを入力',
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（translate_text関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じtranslate_text関数を呼び出し
        await translate_text(pseudo_ctx, text=self.text.value)

class SummarizeModal(discord.ui.Modal, title='📝 要約'):
    def __init__(self):
        super().__init__()
    
    text = discord.ui.TextInput(
        label='要約したいテキスト',
        placeholder='要約したいテキストを入力',
        style=discord.TextStyle.paragraph,
        max_length=1000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # 疑似的なctxオブジェクトを作成（summarize_text関数用）
        class PseudoCtx:
            def __init__(self, interaction):
                self.channel = interaction.channel
                self.author = interaction.user
                self.guild = interaction.guild
                self._interaction = interaction
                self.send = self._send_wrapper
            
            async def _send_wrapper(self, content=None, embed=None, view=None):
                await self._interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        
        pseudo_ctx = PseudoCtx(interaction)
        
        # コマンド版と同じsummarize_text関数を呼び出し
        await summarize_text(pseudo_ctx, text=self.text.value)

class CustomGameModal(discord.ui.Modal, title='🎯 カスタムゲーム募集作成'):
    """カスタムゲーム募集作成モーダル"""
    
    def __init__(self):
        super().__init__()
        
    target_channel = discord.ui.TextInput(
        label='募集チャンネル（任意）',
        placeholder='例: general, ゲーム募集（空白で現在のチャンネル）',
        required=False,
        max_length=50
    )
        
    max_players = discord.ui.TextInput(
        label='最大人数',
        placeholder='例: 10',
        default='10',
        min_length=1,
        max_length=2
    )
    
    game_mode = discord.ui.TextInput(
        label='ゲームモード',
        placeholder='例: 5v5, 3v3, カスタム',
        default='5v5',
        min_length=1,
        max_length=10
    )
    
    start_time = discord.ui.TextInput(
        label='開始時間',
        placeholder='例: 20:00, 今から, 30分後',
        default='今から',
        min_length=1,
        max_length=20
    )
    
    description = discord.ui.TextInput(
        label='説明（任意）',
        placeholder='例: アンレート カジュアル',
        required=False,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理 - コマンド版と完全に統一"""
        await interaction.response.defer()
        
        try:
            # チャンネル選択の処理
            target_channel = interaction.channel  # デフォルトは現在のチャンネル
            if self.target_channel.value:
                # チャンネル名で検索
                channel_name = self.target_channel.value.lower().replace('#', '')
                found_channel = None
                for ch in interaction.guild.text_channels:
                    if ch.name.lower() == channel_name or channel_name in ch.name.lower():
                        found_channel = ch
                        break
                
                if found_channel:
                    target_channel = found_channel
                else:
                    await interaction.followup.send(f"❌ チャンネル '{self.target_channel.value}' が見つかりません。現在のチャンネルで作成します。", ephemeral=True)
            
            # モーダル入力を引数形式に変換
            args = []
            
            # 最大人数の処理
            max_players_value = self.max_players.value.strip()
            if max_players_value:
                args.append(f"{max_players_value}人")
            
            # ゲームモードの処理
            game_mode_value = self.game_mode.value.strip()
            if game_mode_value and game_mode_value != "カスタム":
                args.append(game_mode_value)
            
            # 開始時間の処理
            start_time_value = self.start_time.value.strip()
            if start_time_value and start_time_value != "未設定":
                args.append(start_time_value)
            
            # 説明の処理
            description_value = self.description.value.strip()
            if description_value:
                args.append(description_value)
            
            # 疑似的なctxオブジェクトを作成（create_scrim関数用）
            class PseudoCtx:
                def __init__(self, interaction, target_channel):
                    self.channel = target_channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    # @everyoneメッセージの場合は実際のチャンネルに送信
                    if content == "@everyone":
                        message = await self.channel.send(content=content, embed=embed, view=view)
                        return message
                    else:
                        # 通常のメッセージはスキップ（create_scrim内の通知は不要）
                        return None
            
            pseudo_ctx = PseudoCtx(interaction, target_channel)
            
            # コマンド版と同じcreate_scrim関数を呼び出し
            await create_scrim(pseudo_ctx, args)
            
            # 作成完了通知
            if target_channel.id != interaction.channel.id:
                await interaction.followup.send(f"✅ {target_channel.mention} でカスタムゲーム募集を作成しました！", ephemeral=True)
            else:
                await interaction.followup.send("✅ カスタムゲーム募集を作成しました！", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ カスタムゲーム作成中にエラーが発生しました: {str(e)}", ephemeral=True)
            print(f"カスタムゲーム作成エラー: {e}")

class RankedMatchModal(discord.ui.Modal, title='🏆 ランクマッチ募集作成'):
    """ランクマッチ募集作成モーダル"""
    
    def __init__(self):
        super().__init__()
        
    target_channel = discord.ui.TextInput(
        label='募集チャンネル（任意）',
        placeholder='例: general, ゲーム募集（空白で現在のチャンネル）',
        required=False,
        max_length=50
    )
        
    rank_requirement = discord.ui.TextInput(
        label='ランク条件',
        placeholder='例: ダイヤ帯, プラチナ以上, any',
        default='any',
        min_length=1,
        max_length=20
    )
    
    max_players = discord.ui.TextInput(
        label='最大人数',
        placeholder='例: 10',
        default='10',
        min_length=1,
        max_length=2
    )
    
    start_time = discord.ui.TextInput(
        label='開始時間',
        placeholder='例: 20:00, 今から, 30分後',
        default='今から',
        min_length=1,
        max_length=20
    )
    
    description = discord.ui.TextInput(
        label='説明（任意）',
        placeholder='例: 真剣勝負 ランクマ',
        required=False,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理 - コマンド版と完全に統一"""
        await interaction.response.defer()
        
        try:
            # チャンネル選択の処理
            target_channel = interaction.channel  # デフォルトは現在のチャンネル
            if self.target_channel.value:
                # チャンネル名で検索
                channel_name = self.target_channel.value.lower().replace('#', '')
                found_channel = None
                for ch in interaction.guild.text_channels:
                    if ch.name.lower() == channel_name or channel_name in ch.name.lower():
                        found_channel = ch
                        break
                
                if found_channel:
                    target_channel = found_channel
                else:
                    await interaction.followup.send(f"❌ チャンネル '{self.target_channel.value}' が見つかりません。現在のチャンネルで作成します。", ephemeral=True)
            
            # モーダル入力を引数形式に変換
            args = []
            
            # ランク条件の処理
            rank_requirement_value = self.rank_requirement.value.strip()
            if rank_requirement_value and rank_requirement_value != "any":
                args.append(rank_requirement_value)
            
            # 最大人数の処理
            max_players_value = self.max_players.value.strip()
            if max_players_value:
                args.append(f"{max_players_value}人")
            
            # 開始時間の処理
            start_time_value = self.start_time.value.strip()
            if start_time_value and start_time_value != "未設定":
                args.append(start_time_value)
            
            # 説明の処理
            description_value = self.description.value.strip()
            if description_value:
                args.append(description_value)
            
            # 疑似的なctxオブジェクトを作成（create_ranked_recruit関数用）
            class PseudoCtx:
                def __init__(self, interaction, target_channel):
                    self.channel = target_channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    # @everyoneメッセージの場合は実際のチャンネルに送信
                    if content == "@everyone":
                        message = await self.channel.send(content=content, embed=embed, view=view)
                        return message
                    else:
                        # 通常のメッセージはスキップ（create_ranked_recruit内の通知は不要）
                        return None
            
            pseudo_ctx = PseudoCtx(interaction, target_channel)
            
            # コマンド版と同じcreate_ranked_recruit関数を呼び出し
            await create_ranked_recruit(pseudo_ctx, args)
            
            # 作成完了通知
            if target_channel.id != interaction.channel.id:
                await interaction.followup.send(f"✅ {target_channel.mention} でランクマッチ募集を作成しました！", ephemeral=True)
            else:
                await interaction.followup.send("✅ ランクマッチ募集を作成しました！", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ ランクマッチ募集作成中にエラーが発生しました: {str(e)}", ephemeral=True)
            print(f"ランクマッチ募集作成エラー: {e}")

class TournamentModal(discord.ui.Modal, title='🏅 トーナメント作成'):
    """トーナメント作成モーダル"""
    
    def __init__(self):
        super().__init__()
        
    target_channel = discord.ui.TextInput(
        label='募集チャンネル（任意）',
        placeholder='例: general, ゲーム募集（空白で現在のチャンネル）',
        required=False,
        max_length=50
    )
        
    tournament_type = discord.ui.TextInput(
        label='トーナメント形式',
        placeholder='例: シングル戦, ダブル戦, チーム戦',
        default='シングル戦',
        min_length=1,
        max_length=20
    )
    
    max_participants = discord.ui.TextInput(
        label='最大参加者数',
        placeholder='例: 16',
        default='16',
        min_length=1,
        max_length=2
    )
    
    description = discord.ui.TextInput(
        label='説明（任意）',
        placeholder='例: 優勝者には特典あり',
        required=False,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理 - コマンド版と完全に統一"""
        await interaction.response.defer()
        
        try:
            # チャンネル選択の処理
            target_channel = interaction.channel  # デフォルトは現在のチャンネル
            if self.target_channel.value:
                # チャンネル名で検索
                channel_name = self.target_channel.value.lower().replace('#', '')
                found_channel = None
                for ch in interaction.guild.text_channels:
                    if ch.name.lower() == channel_name or channel_name in ch.name.lower():
                        found_channel = ch
                        break
                
                if found_channel:
                    target_channel = found_channel
                else:
                    await interaction.followup.send(f"❌ チャンネル '{self.target_channel.value}' が見つかりません。現在のチャンネルで作成します。", ephemeral=True)
            
            # モーダル入力を引数形式に変換
            args = []
            
            # トーナメント形式の処理
            tournament_type_value = self.tournament_type.value.strip()
            if tournament_type_value:
                args.append(tournament_type_value)
            
            # 最大参加者数の処理
            max_participants_value = self.max_participants.value.strip()
            if max_participants_value:
                args.append(f"{max_participants_value}人")
            
            # 説明の処理
            description_value = self.description.value.strip()
            if description_value:
                args.append(description_value)
            
            # 疑似的なctxオブジェクトを作成（create_tournament関数用）
            class PseudoCtx:
                def __init__(self, interaction, target_channel):
                    self.channel = target_channel
                    self.author = interaction.user
                    self.guild = interaction.guild
                    self._interaction = interaction
                    self.send = self._send_wrapper
                
                async def _send_wrapper(self, content=None, embed=None, view=None):
                    # @everyoneメッセージの場合は実際のチャンネルに送信
                    if content == "@everyone":
                        message = await self.channel.send(content=content, embed=embed, view=view)
                        return message
                    else:
                        # 通常のメッセージはスキップ（create_tournament内の通知は不要）
                        return None
            
            pseudo_ctx = PseudoCtx(interaction, target_channel)
            
            # コマンド版と同じcreate_tournament関数を呼び出し
            await create_tournament(pseudo_ctx, args)
            
            # 作成完了通知
            if target_channel.id != interaction.channel.id:
                await interaction.followup.send(f"✅ {target_channel.mention} でトーナメントを作成しました！", ephemeral=True)
            else:
                await interaction.followup.send("✅ トーナメントを作成しました！", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ トーナメント作成中にエラーが発生しました: {str(e)}", ephemeral=True)
            print(f"トーナメント作成エラー: {e}")

# 削除: モーダル版は従来のコマンド版関数を使用するため、専用関数は不要

@bot.command(name='panel', help='メイン機能コントロールパネルを表示します')
@prevent_duplicate_execution
async def show_control_panel(ctx):
    """メインコントロールパネル表示 - 全コマンドとの完全統一"""
    embed = discord.Embed(
        title="🎮 メイン機能コントロールパネル",
        description="全ての機能をボタンで簡単操作！コマンドと完全同等の機能を提供",
        color=0x00aaff
    )
    
    embed.add_field(
        name="🎯 ゲーム募集",
        value="**カスタムゲーム:** `!custom create` と同等\n"
              "**ランクマッチ:** `!ranked create` と同等\n"
              "**トーナメント:** `!tournament create` と同等",
        inline=True
    )
    
    embed.add_field(
        name="🎲 ゲーム機能",
        value="**チーム分け:** `!team` と同等\n"
              "**マップ選択:** `!map` と同等\n"
              "**統計確認:** `!valorant` と同等\n"
              "**サイコロ:** `!dice` と同等",
        inline=True
    )
    
    embed.add_field(
        name="🏆 ランク管理",
        value="**ランク設定:** `!rank set` と同等\n"
              "**ランク確認:** `!rank show` と同等\n"
              "**ランク一覧:** `!ranklist` と同等",
        inline=True
    )
    
    embed.add_field(
        name="🤖 AI機能",
        value="**AI会話:** `!ai` と同等\n"
              "**翻訳:** `!translate` と同等\n"
              "**要約:** `!summarize` と同等",
        inline=True
    )
    
    embed.add_field(
        name="📊 情報・統計",
        value="**サーバー情報:** `!info` と同等\n"
              "**ユーザー情報:** `!userinfo` と同等\n"
              "**Bot状態:** `!botstatus` と同等",
        inline=True
    )
    
    embed.add_field(
        name="⚙️ 管理機能",
        value="**クリーンアップ:** `!cleanup` と同等\n"
              "**使用量確認:** `!usage` と同等\n"
              "（管理者権限必要）",
        inline=True
    )
    
    embed.add_field(
        name="🔄 完全統一された機能",
        value="• ボタン操作とコマンド操作で**全く同じ結果**\n"
              "• チャンネル選択機能付き\n"
              "• 従来のコマンドも引き続き利用可能\n"
              "• 同じバリデーション、同じリマインダー機能",
        inline=False
    )
    
    embed.set_footer(text="ボタンクリック = コマンド入力と同等の機能を提供")
    
    view = MainControlPanel()
    await ctx.send(embed=embed, view=view)

# ===============================
# スクリム/カスタムゲーム機能
# ===============================

class CustomGameView(discord.ui.View):
    """カスタムゲーム募集のボタンUI"""
    
    def __init__(self, timeout=3600):  # 1時間でタイムアウト
        super().__init__(timeout=timeout)
        
    async def on_timeout(self):
        """タイムアウト時の処理"""
        try:
            # ボタンを無効化
            for item in self.children:
                item.disabled = True
            
            # メッセージを更新してタイムアウトを通知
            embed = discord.Embed(
                title="⏰ カスタムゲーム募集タイムアウト",
                description="ボタンの有効期限が切れました。\nコマンドでの操作は引き続き可能です。",
                color=0xffa500
            )
            
            # メッセージを更新（可能な場合のみ）
            if hasattr(self, 'message') and self.message:
                try:
                    await self.message.edit(view=self, embed=embed)
                except:
                    pass  # メッセージ更新に失敗しても継続
        except Exception as e:
            print(f"CustomGameView タイムアウト処理エラー: {e}")
            
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        """エラーハンドリング"""
        print(f"CustomGameView エラー: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
        except:
            pass  # エラー通知に失敗しても継続
        
    @discord.ui.button(label='参加', emoji='✅', style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """参加ボタン"""
        try:
            await interaction.response.defer()
            
            channel_id = interaction.channel.id
            user_id = interaction.user.id
            
            if channel_id not in active_scrims:
                await interaction.followup.send("❌ アクティブなカスタムゲームがありません。", ephemeral=True)
                return
            
            scrim = active_scrims[channel_id]
            
            if user_id in scrim['participants']:
                await interaction.followup.send("⚠️ 既に参加済みです。", ephemeral=True)
                return
            
            if len(scrim['participants']) >= scrim['max_players']:
                await interaction.followup.send("❌ 参加者が満員です。", ephemeral=True)
                return
            
            # 参加処理
            scrim['participants'].append(user_id)
            
            current_count = len(scrim['participants'])
            max_players = scrim['max_players']
            
            if current_count >= max_players:
                scrim['status'] = 'ready'
            
            # 募集メッセージを更新
            embed = await create_custom_embed(scrim, interaction.guild)
            await interaction.edit_original_response(embed=embed, view=self)
            
            await interaction.followup.send(f"✅ {interaction.user.display_name} が参加しました！ ({current_count}/{max_players})", ephemeral=False)
        except Exception as e:
            print(f"join_button エラー: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ 参加処理中にエラーが発生しました。", ephemeral=True)
                else:
                    await interaction.followup.send("❌ 参加処理中にエラーが発生しました。", ephemeral=True)
            except:
                pass
    
    @discord.ui.button(label='離脱', emoji='❌', style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """離脱ボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        user_id = interaction.user.id
        
        if channel_id not in active_scrims:
            await interaction.followup.send("❌ アクティブなカスタムゲームがありません。", ephemeral=True)
            return
        
        scrim = active_scrims[channel_id]
        
        if user_id not in scrim['participants']:
            await interaction.followup.send("⚠️ カスタムゲームに参加していません。", ephemeral=True)
            return
        
        # 作成者の場合は特別処理
        if user_id == scrim['creator'].id:
            if len(scrim['participants']) > 1:
                await interaction.followup.send("⚠️ 作成者は他の参加者がいる間は離脱できません。終了ボタンで募集を終了してください。", ephemeral=True)
                return
        
        # 離脱処理
        scrim['participants'].remove(user_id)
        scrim['status'] = 'recruiting'
        
        # 募集メッセージを更新
        embed = await create_custom_embed(scrim, interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)
        
        await interaction.followup.send(f"✅ {interaction.user.display_name} が離脱しました。", ephemeral=False)
    
    @discord.ui.button(label='チーム分け', emoji='🎯', style=discord.ButtonStyle.primary)
    async def team_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """チーム分けボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        
        if channel_id not in active_scrims:
            await interaction.followup.send("❌ アクティブなカスタムゲームがありません。", ephemeral=True)
            return
        
        scrim = active_scrims[channel_id]
        
        if len(scrim['participants']) < 2:
            await interaction.followup.send("❌ チーム分けには最低2人必要です。", ephemeral=True)
            return
        
        guild = interaction.guild
        members = []
        for participant_id in scrim['participants']:
            member = guild.get_member(participant_id)
            if member:
                members.append(member)
        
        # チーム分けロジック
        random.shuffle(members)
        
        if scrim['game_mode'] in ['5v5', '5V5']:
            team_size = 5
        elif scrim['game_mode'] in ['3v3', '3V3']:
            team_size = 3
        elif scrim['game_mode'] in ['2v2', '2V2']:
            team_size = 2
        else:
            team_size = len(members) // 2
        
        team1 = members[:team_size]
        team2 = members[team_size:team_size*2]
        extras = members[team_size*2:] if len(members) > team_size*2 else []
        
        # チーム情報を保存
        scrim['teams'] = {
            'team1': [m.id for m in team1],
            'team2': [m.id for m in team2],
            'extras': [m.id for m in extras]
        }
        
        embed = discord.Embed(
            title="🎯 カスタムゲームチーム分け結果",
            color=0x00ff88
        )
        
        embed.add_field(
            name="🔴 チーム1",
            value="\n".join([f"• {m.display_name}" for m in team1]) if team1 else "なし",
            inline=True
        )
        
        embed.add_field(
            name="🔵 チーム2",
            value="\n".join([f"• {m.display_name}" for m in team2]) if team2 else "なし",
            inline=True
        )
        
        if extras:
            embed.add_field(
                name="⚪ 待機",
                value="\n".join([f"• {m.display_name}" for m in extras]),
                inline=False
            )
        
        embed.set_footer(text=f"ゲームモード: {scrim['game_mode']} | 頑張って！")
        
        await interaction.followup.send(embed=embed)
    
    @discord.ui.button(label='終了', emoji='🏁', style=discord.ButtonStyle.secondary)
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """終了ボタン（作成者のみ）"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        user_id = interaction.user.id
        
        if channel_id not in active_scrims:
            await interaction.followup.send("❌ アクティブなカスタムゲームがありません。", ephemeral=True)
            return
        
        scrim = active_scrims[channel_id]
        
        # 作成者または管理者のみ終了可能
        if user_id != scrim['creator'].id and not interaction.user.guild_permissions.manage_messages:
            await interaction.followup.send("❌ カスタムゲームの作成者または管理者のみ終了できます。", ephemeral=True)
            return
        
        # リマインダーキャンセル
        scrim_id = scrim['id']
        if scrim_id in scrim_reminders:
            scrim_reminders[scrim_id].cancel()
            del scrim_reminders[scrim_id]
        
        # スクリム削除
        del active_scrims[channel_id]
        
        embed = discord.Embed(
            title="🏁 カスタムゲーム募集終了",
            description=f"**{scrim['game_mode']}** の募集を終了しました。",
            color=0xff6b6b
        )
        
        embed.add_field(
            name="📊 最終統計",
            value=f"**参加者数:** {len(scrim['participants'])}人\n"
                  f"**募集時間:** {(datetime.now() - scrim['created_at']).seconds // 60}分間",
            inline=False
        )
        
        # ボタンを無効化
        for item in self.children:
            item.disabled = True
        
        await interaction.edit_original_response(embed=embed, view=self)
        await interaction.followup.send("カスタムゲーム募集が終了されました。", ephemeral=False)

async def create_custom_embed(scrim, guild):
    """カスタムゲーム募集のEmbed作成"""
    # 参加者リスト作成
    participants_list = []
    for participant_id in scrim['participants']:
        member = guild.get_member(participant_id)
        if member:
            participants_list.append(f"• {member.display_name}")
    
    status_map = {
        'recruiting': '📢 募集中',
        'ready': '✅ 準備完了',
        'in_progress': '🎮 進行中',
        'ended': '🏁 終了'
    }
    
    current_count = len(scrim['participants'])
    max_players = scrim['max_players']
    
    title = "🎯 カスタムゲーム募集"
    if current_count >= max_players:
        title = "🎉 カスタムゲーム募集（満員）"
    
    embed = discord.Embed(
        title=title,
        description=f"**{scrim['game_mode']}** のメンバーを募集中",
        color=0x00ff88 if current_count < max_players else 0xffd700
    )
    
    embed.add_field(
        name="📊 募集情報",
        value=f"**ゲームモード:** {scrim['game_mode']}\n"
              f"**最大人数:** {max_players}人\n"
              f"**開始時間:** {scrim['scheduled_time']}\n"
              f"**現在の参加者:** {current_count}/{max_players}人\n"
              f"**ステータス:** {status_map.get(scrim['status'], scrim['status'])}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list) if participants_list else "なし",
        inline=True
    )
    
    if scrim.get('description'):
        embed.add_field(
            name="📝 詳細",
            value=scrim['description'],
            inline=False
        )
    
    if scrim.get('teams'):
        embed.add_field(
            name="🎯 チーム分け",
            value="チーム分け済み（チーム分けボタンで再確認）",
            inline=False
        )
    
    embed.set_footer(text=f"作成者: {scrim['creator'].display_name} | ID: {scrim['id'][:8]}")
    
    return embed

@bot.command(name='custom', help='カスタムゲーム募集（例: !custom create 10人 20:00, !custom join, !custom status）')
@prevent_duplicate_execution
async def scrim_manager(ctx, action=None, *args):
    """スクリム/カスタムゲーム管理システム"""
    try:
        if not action:
            # ヘルプ表示
            embed = discord.Embed(
                title="🎯 カスタムゲーム機能",
                description="カスタムゲームの募集・管理システム",
                color=0x00ff88
            )
            
            embed.add_field(
                name="📝 基本コマンド",
                value="`!custom create [人数] [時間]` - 募集開始\n"
                      "`!custom join` - 参加\n"
                      "`!custom leave` - 離脱\n"
                      "`!custom status` - 現在の状況\n"
                      "`!custom end` - 募集終了",
                inline=False
            )
            
            embed.add_field(
                name="⚙️ 管理コマンド",
                value="`!custom add @ユーザー` - メンバー追加\n"
                      "`!custom kick @ユーザー` - 除外\n"
                      "`!custom remind` - リマインダー送信\n"
                      "`!custom team` - チーム分け実行\n"
                      "`!custom info` - 詳細情報",
                inline=False
            )
            
            embed.add_field(
                name="💡 使用例",
                value="`!custom create 10人 20:00` - 10人で20時スタート\n"
                      "`!custom create 5v5 今から` - 5v5を今すぐ開始",
                inline=False
            )
            
            await ctx.send(embed=embed)
            return
        
        channel_id = ctx.channel.id
        user = ctx.author
        
        if action.lower() in ['create', 'start', '作成', '開始']:
            await create_scrim(ctx, args)
            
        elif action.lower() in ['join', 'j', '参加']:
            await join_scrim(ctx)
            
        elif action.lower() in ['leave', 'l', '離脱']:
            await leave_scrim(ctx)
            
        elif action.lower() in ['status', 's', '状況', '確認']:
            await show_scrim_status(ctx)
            
        elif action.lower() in ['end', 'close', '終了', '解散']:
            await end_scrim(ctx)
            
        elif action.lower() in ['add', 'invite', '追加', '招待']:
            await add_to_scrim(ctx, args)
            
        elif action.lower() in ['kick', 'remove', '除外']:
            await kick_from_scrim(ctx, args)
            
        elif action.lower() in ['remind', 'reminder', 'リマインド']:
            await send_scrim_reminder(ctx)
            
        elif action.lower() in ['team', 'teams', 'チーム分け']:
            await scrim_team_divide(ctx)
            
        elif action.lower() in ['info', 'detail', '詳細']:
            await show_scrim_info(ctx)
            
        else:
            await ctx.send("❌ 不明なアクション。`!custom` でヘルプを確認してください。")
            
    except Exception as e:
        await ctx.send(f"❌ スクリム機能でエラーが発生しました: {str(e)}")
        print(f"スクリム機能エラー: {e}")

async def create_scrim(ctx, args):
    """スクリム作成"""
    channel_id = ctx.channel.id
    
    # 既存のスクリムチェック
    if channel_id in active_scrims:
        await ctx.send("❌ このチャンネルで既にカスタムゲームが進行中です。`!custom end` で終了してください。")
        return
    
    # 引数解析
    max_players = 10  # デフォルト
    scheduled_time = "未設定"
    game_mode = "カスタム"
    description = ""
    
    for arg in args:
        if '人' in arg or 'v' in arg.lower():
            # 人数またはフォーマット指定
            if 'v' in arg.lower():
                game_mode = arg.upper()
                if '5v5' in arg.lower():
                    max_players = 10
                elif '3v3' in arg.lower():
                    max_players = 6
                elif '2v2' in arg.lower():
                    max_players = 4
                elif '1v1' in arg.lower():
                    max_players = 2
            else:
                try:
                    max_players = int(arg.replace('人', ''))
                except:
                    pass
        elif ':' in arg or '時' in arg:
            # 時間指定
            scheduled_time = arg
        elif arg in ['今から', 'now', 'すぐ']:
            scheduled_time = "今すぐ"
        else:
            # 説明文
            if description:
                description += f" {arg}"
            else:
                description = arg
    
    # スクリムデータ作成
    scrim_data = {
        'id': f"{channel_id}_{int(datetime.now().timestamp())}",
        'channel_id': channel_id,
        'creator': ctx.author,
        'created_at': datetime.now(),
        'max_players': max_players,
        'scheduled_time': scheduled_time,
        'game_mode': game_mode,
        'description': description,
        'participants': [ctx.author.id],
        'status': 'recruiting',  # recruiting, ready, in_progress, ended
        'teams': None
    }
    
    active_scrims[channel_id] = scrim_data
    
    # ボタン付き募集メッセージ作成
    embed = await create_custom_embed(scrim_data, ctx.guild)
    
    # 操作方法を追加（ボタンとコマンド両方）
    embed.add_field(
        name="🔧 操作方法",
        value="**ボタン操作:** 下のボタンをクリック\n"
              "**コマンド操作:** `!custom join/leave/status`",
        inline=False
    )
    
    view = CustomGameView()
    message = await ctx.send(content="@everyone", embed=embed, view=view)
    scrim_data['message_id'] = message.id
    view.message = message  # ビューにメッセージオブジェクトを保存
    
    # 自動リマインダー設定（開始時間が指定されている場合）
    if scheduled_time != "未設定" and scheduled_time != "今すぐ":
        await schedule_scrim_reminder(ctx, scrim_data)

async def join_scrim(ctx):
    """カスタムゲーム参加"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    if user_id in scrim['participants']:
        await ctx.send("⚠️ 既に参加済みです。")
        return
    
    if len(scrim['participants']) >= scrim['max_players']:
        await ctx.send("❌ 参加者が満員です。")
        return
    
    # 参加処理
    scrim['participants'].append(user_id)
    
    current_count = len(scrim['participants'])
    max_players = scrim['max_players']
    
    # ステータス更新
    if current_count >= max_players:
        scrim['status'] = 'ready'
    
    # 参加者リスト作成
    guild = ctx.guild
    participants_list = []
    for participant_id in scrim['participants']:
        member = guild.get_member(participant_id)
        if member:
            participants_list.append(f"• {member.display_name}")
    
    # 更新メッセージ
    embed = discord.Embed(
        title="✅ カスタムゲーム参加完了！" if current_count < max_players else "🎉 カスタムゲーム参加者満員！",
        color=0x00ff88 if current_count < max_players else 0xffd700
    )
    
    embed.add_field(
        name="📊 現在の状況",
        value=f"**参加者:** {current_count}/{max_players}人\n"
              f"**ゲームモード:** {scrim['game_mode']}\n"
              f"**開始予定:** {scrim['scheduled_time']}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list),
        inline=True
    )
    
    if current_count >= max_players:
        embed.add_field(
            name="🎯 次のステップ",
            value="`!custom team` - チーム分け\n`!custom remind` - 全員に通知",
            inline=False
        )
    
    await ctx.send(embed=embed)

async def leave_scrim(ctx):
    """カスタムゲーム離脱"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    if user_id not in scrim['participants']:
        await ctx.send("⚠️ カスタムゲームに参加していません。")
        return
    
    # 作成者の場合は特別処理
    if user_id == scrim['creator'].id:
        if len(scrim['participants']) > 1:
            await ctx.send("⚠️ 作成者は他の参加者がいる間は離脱できません。`!custom end` で募集を終了してください。")
            return
    
    # 離脱処理
    scrim['participants'].remove(user_id)
    scrim['status'] = 'recruiting'
    
    await ctx.send(f"✅ {ctx.author.display_name} がカスタムゲームから離脱しました。")

async def show_scrim_status(ctx):
    """カスタムゲーム状況表示"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    # 参加者リスト作成
    guild = ctx.guild
    participants_list = []
    for participant_id in scrim['participants']:
        member = guild.get_member(participant_id)
        if member:
            participants_list.append(f"• {member.display_name}")
    
    status_map = {
        'recruiting': '📢 募集中',
        'ready': '✅ 準備完了',
        'in_progress': '🎮 進行中',
        'ended': '🏁 終了'
    }
    
    embed = discord.Embed(
        title="📊 カスタムゲーム状況",
        color=0x00ff88
    )
    
    embed.add_field(
        name="基本情報",
        value=f"**ステータス:** {status_map.get(scrim['status'], scrim['status'])}\n"
              f"**ゲームモード:** {scrim['game_mode']}\n"
              f"**参加者:** {len(scrim['participants'])}/{scrim['max_players']}人\n"
              f"**開始予定:** {scrim['scheduled_time']}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list) if participants_list else "なし",
        inline=True
    )
    
    if scrim.get('teams'):
        embed.add_field(
            name="🎯 チーム分け",
            value="チーム分け済み (詳細は `!custom team` で確認)",
            inline=False
        )
    
    embed.set_footer(text=f"作成者: {scrim['creator'].display_name} | 作成時刻: {scrim['created_at'].strftime('%H:%M')}")
    
    await ctx.send(embed=embed)

async def end_scrim(ctx):
    """カスタムゲーム終了"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    # 作成者または管理者のみ終了可能
    if user_id != scrim['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ カスタムゲームの作成者または管理者のみ終了できます。")
        return
    
    # リマインダーキャンセル
    scrim_id = scrim['id']
    if scrim_id in scrim_reminders:
        scrim_reminders[scrim_id].cancel()
        del scrim_reminders[scrim_id]
    
    # スクリム削除
    del active_scrims[channel_id]
    
    embed = discord.Embed(
        title="🏁 カスタムゲーム募集終了",
        description=f"**{scrim['game_mode']}** の募集を終了しました。",
        color=0xff6b6b
    )
    
    embed.add_field(
        name="📊 最終統計",
        value=f"**参加者数:** {len(scrim['participants'])}人\n"
              f"**募集時間:** {(datetime.now() - scrim['created_at']).seconds // 60}分間",
        inline=False
    )
    
    await ctx.send(embed=embed)

# ===============================
# ランクマッチ募集機能
# ===============================

class RankedRecruitView(discord.ui.View):
    """ランクマッチ募集のボタンUI"""
    
    def __init__(self, timeout=3600):  # 1時間でタイムアウト
        super().__init__(timeout=timeout)
        
    async def on_timeout(self):
        """タイムアウト時の処理"""
        try:
            # ボタンを無効化
            for item in self.children:
                item.disabled = True
            
            # メッセージを更新してタイムアウトを通知
            embed = discord.Embed(
                title="⏰ ランクマッチ募集タイムアウト",
                description="ボタンの有効期限が切れました。\nコマンドでの操作は引き続き可能です。",
                color=0xffa500
            )
            
            # メッセージを更新（可能な場合のみ）
            if hasattr(self, 'message') and self.message:
                try:
                    await self.message.edit(view=self, embed=embed)
                except:
                    pass  # メッセージ更新に失敗しても継続
        except Exception as e:
            print(f"RankedRecruitView タイムアウト処理エラー: {e}")
            
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        """エラーハンドリング"""
        print(f"RankedRecruitView エラー: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 操作中にエラーが発生しました。しばらく待ってから再試行してください。", ephemeral=True)
        except:
            pass  # エラー通知に失敗しても継続
        
    @discord.ui.button(label='参加', emoji='✅', style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """参加ボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        user_id = interaction.user.id
        
        if channel_id not in active_rank_recruits:
            await interaction.followup.send("❌ アクティブなランクマッチ募集がありません。", ephemeral=True)
            return
        
        recruit = active_rank_recruits[channel_id]
        
        if user_id in recruit['participants']:
            await interaction.followup.send("⚠️ 既に参加済みです。", ephemeral=True)
            return
        
        if len(recruit['participants']) >= recruit['max_players']:
            await interaction.followup.send("❌ 参加者が満員です。", ephemeral=True)
            return
        
        # ランク条件チェック
        if not check_rank_eligibility(user_id, recruit):
            rank_req = recruit['rank_requirement']
            await interaction.followup.send(f"❌ ランク条件（{rank_req}）を満たしていません。\n💡 `!rank set current [ランク]` でランクを設定してください。", ephemeral=True)
            return
        
        # 参加処理
        recruit['participants'].append(user_id)
        
        current_count = len(recruit['participants'])
        max_players = recruit['max_players']
        
        if current_count >= max_players:
            recruit['status'] = 'ready'
        
        # 募集メッセージを更新
        embed = await create_ranked_embed(recruit, interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)
        
        user_rank = get_user_rank_display(user_id)
        await interaction.followup.send(f"✅ {interaction.user.display_name} {user_rank} が参加しました！ ({current_count}/{max_players})", ephemeral=False)
    
    @discord.ui.button(label='離脱', emoji='❌', style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """離脱ボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        user_id = interaction.user.id
        
        if channel_id not in active_rank_recruits:
            await interaction.followup.send("❌ アクティブなランクマッチ募集がありません。", ephemeral=True)
            return
        
        recruit = active_rank_recruits[channel_id]
        
        if user_id not in recruit['participants']:
            await interaction.followup.send("⚠️ ランクマッチ募集に参加していません。", ephemeral=True)
            return
        
        # 作成者の場合は特別処理
        if user_id == recruit['creator'].id:
            if len(recruit['participants']) > 1:
                await interaction.followup.send("⚠️ 作成者は他の参加者がいる間は離脱できません。終了ボタンで募集を終了してください。", ephemeral=True)
                return
        
        # 離脱処理
        recruit['participants'].remove(user_id)
        recruit['status'] = 'recruiting'
        
        # 募集メッセージを更新
        embed = await create_ranked_embed(recruit, interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)
        
        await interaction.followup.send(f"✅ {interaction.user.display_name} が離脱しました。", ephemeral=False)
    
    @discord.ui.button(label='ランクチーム分け', emoji='🎯', style=discord.ButtonStyle.primary)
    async def team_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ランクバランスチーム分けボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        
        if channel_id not in active_rank_recruits:
            await interaction.followup.send("❌ アクティブなランクマッチ募集がありません。", ephemeral=True)
            return
        
        recruit = active_rank_recruits[channel_id]
        
        if len(recruit['participants']) < 2:
            await interaction.followup.send("❌ チーム分けには最低2人必要です。", ephemeral=True)
            return
        
        # ランクバランスチーム分けの実行（既存の関数を使用）
        await execute_ranked_team_divide_logic(recruit, interaction)
    
    @discord.ui.button(label='ランク確認', emoji='🔍', style=discord.ButtonStyle.secondary)
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ランク確認ボタン"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        
        if channel_id not in active_rank_recruits:
            await interaction.followup.send("❌ アクティブなランクマッチ募集がありません。", ephemeral=True)
            return
        
        recruit = active_rank_recruits[channel_id]
        
        guild = interaction.guild
        rank_check_results = []
        eligible_count = 0
        ineligible_count = 0
        
        for participant_id in recruit['participants']:
            member = guild.get_member(participant_id)
            if member:
                is_eligible = check_rank_eligibility(participant_id, recruit)
                rank_display = get_user_rank_display(participant_id)
                
                if is_eligible:
                    status = "✅"
                    eligible_count += 1
                else:
                    status = "❌"
                    ineligible_count += 1
                
                rank_check_results.append(f"{status} {member.display_name} {rank_display}")
        
        embed = discord.Embed(
            title="🔍 参加者ランク確認",
            color=0x00ff88 if ineligible_count == 0 else 0xff6b6b
        )
        
        embed.add_field(
            name="📊 確認結果",
            value=f"**適格者:** {eligible_count}人\n"
                  f"**不適格者:** {ineligible_count}人\n"
                  f"**ランク条件:** {recruit['rank_requirement']}",
            inline=True
        )
        
        embed.add_field(
            name="👥 詳細結果",
            value="\n".join(rank_check_results) if rank_check_results else "参加者なし",
            inline=False
        )
        
        if ineligible_count > 0:
            embed.add_field(
                name="⚠️ 注意",
                value="ランク条件を満たしていない参加者がいます。",
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label='終了', emoji='🏁', style=discord.ButtonStyle.secondary)
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """終了ボタン（作成者のみ）"""
        await interaction.response.defer()
        
        channel_id = interaction.channel.id
        user_id = interaction.user.id
        
        if channel_id not in active_rank_recruits:
            await interaction.followup.send("❌ アクティブなランクマッチ募集がありません。", ephemeral=True)
            return
        
        recruit = active_rank_recruits[channel_id]
        
        # 作成者または管理者のみ終了可能
        if user_id != recruit['creator'].id and not interaction.user.guild_permissions.manage_messages:
            await interaction.followup.send("❌ ランクマッチ募集の作成者または管理者のみ終了できます。", ephemeral=True)
            return
        
        # リマインダーキャンセル
        recruit_id = recruit['id']
        if recruit_id in rank_recruit_reminders:
            rank_recruit_reminders[recruit_id].cancel()
            del rank_recruit_reminders[recruit_id]
        
        # 募集削除
        del active_rank_recruits[channel_id]
        
        embed = discord.Embed(
            title="🏁 ランクマッチ募集終了",
            description=f"**{recruit['rank_requirement']}** の募集を終了しました。",
            color=0xff6b6b
        )
        
        embed.add_field(
            name="📊 最終統計",
            value=f"**参加者数:** {len(recruit['participants'])}人\n"
                  f"**募集時間:** {(datetime.now() - recruit['created_at']).seconds // 60}分間",
            inline=False
        )
        
        # ボタンを無効化
        for item in self.children:
            item.disabled = True
        
        await interaction.edit_original_response(embed=embed, view=self)
        await interaction.followup.send("ランクマッチ募集が終了されました。", ephemeral=False)

async def create_ranked_embed(recruit, guild):
    """ランクマッチ募集のEmbed作成"""
    # 参加者リスト作成（ランク情報付き）
    participants_list = []
    rank_stats = {}
    
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            rank_info = get_user_rank_display(participant_id)
            participants_list.append(f"• {member.display_name} {rank_info}")
            
            # ランク統計
            if participant_id in user_ranks and user_ranks[participant_id].get('current'):
                rank = user_ranks[participant_id]['current']
                tier = VALORANT_RANKS[rank]['tier']
                rank_stats[tier] = rank_stats.get(tier, 0) + 1
    
    status_map = {
        'recruiting': '📢 募集中',
        'ready': '✅ 準備完了',
        'in_progress': '🎮 進行中',
        'ended': '🏁 終了'
    }
    
    current_count = len(recruit['participants'])
    max_players = recruit['max_players']
    
    title = "🏆 ランクマッチ募集"
    if current_count >= max_players:
        title = "🎉 ランクマッチ募集（満員）"
    
    embed = discord.Embed(
        title=title,
        description=f"**{recruit['rank_requirement']}** のメンバーを募集中",
        color=0x4a90e2 if current_count < max_players else 0xffd700
    )
    
    embed.add_field(
        name="📊 募集情報",
        value=f"**ランク条件:** {recruit['rank_requirement']}\n"
              f"**最大人数:** {max_players}人\n"
              f"**開始時間:** {recruit['scheduled_time']}\n"
              f"**現在の参加者:** {current_count}/{max_players}人\n"
              f"**ステータス:** {status_map.get(recruit['status'], recruit['status'])}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list) if participants_list else "なし",
        inline=True
    )
    
    # ランク分布（参加者がいる場合）
    if rank_stats:
        tier_names = {9: "レディアント", 8: "イモータル", 7: "アセンダント", 6: "ダイヤ", 5: "プラチナ", 4: "ゴールド", 3: "シルバー", 2: "ブロンズ", 1: "アイアン"}
        rank_distribution = []
        for tier in sorted(rank_stats.keys(), reverse=True):
            tier_name = tier_names.get(tier, f"ティア{tier}")
            rank_distribution.append(f"{tier_name}: {rank_stats[tier]}人")
        
        embed.add_field(
            name="🏆 ランク分布",
            value="\n".join(rank_distribution),
            inline=False
        )
    
    if recruit.get('description'):
        embed.add_field(
            name="📝 詳細",
            value=recruit['description'],
            inline=False
        )
    
    if recruit.get('teams'):
        embed.add_field(
            name="🎯 チーム分け",
            value="チーム分け済み（チーム分けボタンで再確認）",
            inline=False
        )
    
    embed.set_footer(text=f"作成者: {recruit['creator'].display_name} | ID: {recruit['id'][:8]}")
    
    return embed

async def execute_ranked_team_divide_logic(recruit, interaction):
    """ランクバランスチーム分けのロジック実行"""
    guild = interaction.guild
    members = []
    ranked_members = []
    
    # 参加者のランク情報を取得
    total_rank_value = 0
    rank_count = 0
    
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            members.append(member)
            
            # ランク情報取得
            if participant_id in user_ranks and user_ranks[participant_id].get('current'):
                rank_name = user_ranks[participant_id]['current']
                rank_value = VALORANT_RANKS[rank_name]['value']
                ranked_members.append({
                    'member': member,
                    'rank': rank_name,
                    'value': rank_value
                })
                total_rank_value += rank_value
                rank_count += 1
            else:
                # ランク未設定者は平均ランクで計算
                ranked_members.append({
                    'member': member,
                    'rank': None,
                    'value': 400  # ゴールド1レベル
                })
    
    # 平均ランク値を計算
    if rank_count > 0:
        avg_rank_value = total_rank_value / rank_count
    else:
        avg_rank_value = 400
    
    # 未ランクメンバーに平均値を適用
    for member_data in ranked_members:
        if member_data['rank'] is None:
            member_data['value'] = avg_rank_value
    
    # ランクバランス調整チーム分け
    def balance_teams_by_rank(members_data, team_size):
        members_data = sorted(members_data, key=lambda x: x['value'], reverse=True)
        team1 = []
        team2 = []
        
        for member_data in members_data:
            team1_total = sum(m['value'] for m in team1)
            team2_total = sum(m['value'] for m in team2)
            
            if len(team1) >= team_size:
                team2.append(member_data)
            elif len(team2) >= team_size:
                team1.append(member_data)
            else:
                if team1_total <= team2_total:
                    team1.append(member_data)
                else:
                    team2.append(member_data)
        
        return team1, team2
    
    # チーム分けの実行
    team_size = len(ranked_members) // 2
    team1, team2 = balance_teams_by_rank(ranked_members, team_size)
    
    # チーム情報を保存
    recruit['teams'] = {
        'team1': [m['member'].id for m in team1],
        'team2': [m['member'].id for m in team2]
    }
    
    embed = discord.Embed(
        title="🎯 ランクマッチ チーム分け結果",
        description="ランクバランスを考慮したチーム分け",
        color=0x4a90e2
    )
    
    # チーム1の情報
    team1_display = []
    team1_total = 0
    for member_data in team1:
        member = member_data['member']
        rank = member_data['rank']
        value = member_data['value']
        team1_total += value
        
        if rank:
            rank_info = VALORANT_RANKS[rank]
            team1_display.append(f"• {member.display_name} ({rank_info['display']})")
        else:
            team1_display.append(f"• {member.display_name} (ランク未設定)")
    
    embed.add_field(
        name="🔴 チーム1",
        value="\n".join(team1_display),
        inline=True
    )
    
    # チーム2の情報
    team2_display = []
    team2_total = 0
    for member_data in team2:
        member = member_data['member']
        rank = member_data['rank']
        value = member_data['value']
        team2_total += value
        
        if rank:
            rank_info = VALORANT_RANKS[rank]
            team2_display.append(f"• {member.display_name} ({rank_info['display']})")
        else:
            team2_display.append(f"• {member.display_name} (ランク未設定)")
    
    embed.add_field(
        name="🔵 チーム2",
        value="\n".join(team2_display),
        inline=True
    )
    
    # バランス情報
    avg1 = team1_total / len(team1) if team1 else 0
    avg2 = team2_total / len(team2) if team2 else 0
    balance_diff = abs(avg1 - avg2)
    balance_quality = "完璧" if balance_diff < 50 else "良好" if balance_diff < 100 else "やや偏り" if balance_diff < 150 else "偏りあり"
    
    embed.add_field(
        name="⚖️ バランス評価",
        value=f"{balance_quality} (差: {balance_diff:.0f})",
        inline=False
    )
    
    embed.add_field(
        name="📊 平均ランク値",
        value=f"チーム1: {avg1:.0f} | チーム2: {avg2:.0f}",
        inline=False
    )
    
    embed.set_footer(text=f"ランク条件: {recruit['rank_requirement']} | 頑張って！")
    
    await interaction.followup.send(embed=embed)

@bot.command(name='ranked', aliases=['ランク募集', 'rank_recruit'], help='ランクマッチ募集（例: !ranked create ダイヤ帯 20:00, !ranked join, !ranked status）')
@prevent_duplicate_execution
async def ranked_recruit_manager(ctx, action=None, *args):
    """ランクマッチ募集管理システム"""
    try:
        if not action:
            # ヘルプ表示
            embed = discord.Embed(
                title="🏆 ランクマッチ募集機能",
                description="ランク帯別のマッチング募集システム",
                color=0x4a90e2
            )
            
            embed.add_field(
                name="📝 基本コマンド",
                value="`!ranked create [ランク帯] [時間]` - 募集開始\n"
                      "`!ranked join` - 参加\n"
                      "`!ranked leave` - 離脱\n"
                      "`!ranked status` - 現在の状況\n"
                      "`!ranked end` - 募集終了",
                inline=False
            )
            
            embed.add_field(
                name="⚙️ 管理コマンド",
                value="`!ranked add @ユーザー` - メンバー追加\n"
                      "`!ranked kick @ユーザー` - 除外\n"
                      "`!ranked remind` - リマインダー送信\n"
                      "`!ranked team` - ランクバランスチーム分け\n"
                      "`!ranked check` - 参加者ランク確認",
                inline=False
            )
            
            embed.add_field(
                name="💡 使用例",
                value="`!ranked create ダイヤ帯 20:00` - ダイヤ帯で20時スタート\n"
                      "`!ranked create プラチナ以上 今から` - プラチナ以上で今すぐ\n"
                      "`!ranked create any 21:30` - ランク問わず21:30",
                inline=False
            )
            
            await ctx.send(embed=embed)
            return
        
        channel_id = ctx.channel.id
        user = ctx.author
        
        if action.lower() in ['create', 'start', '作成', '開始']:
            await create_ranked_recruit(ctx, args)
            
        elif action.lower() in ['join', 'j', '参加']:
            await join_ranked_recruit(ctx)
            
        elif action.lower() in ['leave', 'l', '離脱']:
            await leave_ranked_recruit(ctx)
            
        elif action.lower() in ['status', 's', '状況', '確認']:
            await show_ranked_recruit_status(ctx)
            
        elif action.lower() in ['end', 'close', '終了', '解散']:
            await end_ranked_recruit(ctx)
            
        elif action.lower() in ['add', 'invite', '追加', '招待']:
            await add_to_ranked_recruit(ctx, args)
            
        elif action.lower() in ['kick', 'remove', '除外']:
            await kick_from_ranked_recruit(ctx, args)
            
        elif action.lower() in ['remind', 'reminder', 'リマインド']:
            await send_ranked_recruit_reminder(ctx)
            
        elif action.lower() in ['team', 'teams', 'チーム分け']:
            await ranked_recruit_team_divide(ctx)
            
        elif action.lower() in ['check', 'verify', 'ランク確認']:
            await check_ranked_recruit_ranks(ctx)
            
        else:
            await ctx.send("❌ 不明なアクション。`!ranked` でヘルプを確認してください。")
            
    except Exception as e:
        await ctx.send(f"❌ ランクマッチ募集機能でエラーが発生しました: {str(e)}")
        print(f"ランクマッチ募集機能エラー: {e}")

async def create_ranked_recruit(ctx, args):
    """ランクマッチ募集作成"""
    channel_id = ctx.channel.id
    
    # 既存の募集チェック
    if channel_id in active_rank_recruits:
        await ctx.send("❌ このチャンネルで既にランクマッチ募集が進行中です。`!ranked end` で終了してください。")
        return
    
    # 引数解析
    rank_requirement = "any"  # デフォルト
    scheduled_time = "未設定"
    max_players = 5  # デフォルト5人（ランクマッチは5人）
    description = ""
    min_rank = None
    max_rank = None
    
    for arg in args:
        # ランク指定の解析
        if any(rank_word in arg for rank_word in ['ダイヤ', 'プラチナ', 'ゴールド', 'シルバー', 'ブロンズ', 'アイアン', 'イモータル', 'アセンダント', 'レディアント']):
            if '以上' in arg:
                rank_requirement = arg.replace('以上', '').strip() + "以上"
                min_rank = parse_rank_requirement(arg.replace('以上', '').strip())
            elif '以下' in arg:
                rank_requirement = arg.replace('以下', '').strip() + "以下"
                max_rank = parse_rank_requirement(arg.replace('以下', '').strip())
            elif '帯' in arg:
                rank_requirement = arg
                base_rank = parse_rank_requirement(arg.replace('帯', '').strip())
                if base_rank:
                    min_rank, max_rank = get_rank_tier_range(base_rank)
            else:
                rank_requirement = arg
                min_rank = parse_rank_requirement(arg)
        elif ':' in arg or '時' in arg:
            # 時間指定
            scheduled_time = arg
        elif arg in ['今から', 'now', 'すぐ']:
            scheduled_time = "今すぐ"
        elif arg.lower() == 'any':
            rank_requirement = "ランク問わず"
        elif arg.isdigit():
            # 人数指定
            max_players = min(int(arg), 10)  # 最大10人
        else:
            # 説明文
            if description:
                description += f" {arg}"
            else:
                description = arg
    
    # ランクマッチ募集データ作成
    recruit_data = {
        'id': f"{channel_id}_{int(datetime.now().timestamp())}",
        'channel_id': channel_id,
        'creator': ctx.author,
        'created_at': datetime.now(),
        'max_players': max_players,
        'scheduled_time': scheduled_time,
        'rank_requirement': rank_requirement,
        'min_rank': min_rank,
        'max_rank': max_rank,
        'description': description,
        'participants': [ctx.author.id],
        'status': 'recruiting',  # recruiting, ready, in_progress, ended
        'teams': None,
        'type': 'ranked_match'
    }
    
    active_rank_recruits[channel_id] = recruit_data
    
    # ボタン付き募集メッセージ作成
    embed = await create_ranked_embed(recruit_data, ctx.guild)
    
    # 操作方法を追加（ボタンとコマンド両方）
    embed.add_field(
        name="🔧 操作方法",
        value="**ボタン操作:** 下のボタンをクリック\n"
              "**コマンド操作:** `!ranked join/leave/status`",
        inline=False
    )
    
    # ランク条件の詳細表示
    if min_rank or max_rank:
        rank_details = []
        if min_rank:
            rank_details.append(f"最低ランク: {VALORANT_RANKS[min_rank]['display']}")
        if max_rank:
            rank_details.append(f"最高ランク: {VALORANT_RANKS[max_rank]['display']}")
        
        embed.add_field(
            name="🎯 ランク詳細",
            value="\n".join(rank_details),
            inline=False
        )
    
    view = RankedRecruitView()
    message = await ctx.send(content="@everyone", embed=embed, view=view)
    recruit_data['message_id'] = message.id
    view.message = message  # ビューにメッセージオブジェクトを保存
    
    # 自動リマインダー設定
    if scheduled_time != "未設定" and scheduled_time != "今すぐ":
        await schedule_ranked_recruit_reminder(ctx, recruit_data)

def parse_rank_requirement(rank_text):
    """ランク要求をパース"""
    if not rank_text:
        return None
    
    # 既存のparse_rank_input関数を使用
    return parse_rank_input(rank_text)

def get_rank_tier_range(base_rank):
    """ランク帯の範囲を取得（例：ダイヤ1-3）"""
    if not base_rank or base_rank not in VALORANT_RANKS:
        return None, None
    
    base_tier = VALORANT_RANKS[base_rank]['tier']
    
    # 同じティアのランクを検索
    tier_ranks = []
    for rank_key, rank_info in VALORANT_RANKS.items():
        if rank_info['tier'] == base_tier:
            tier_ranks.append((rank_key, rank_info['value']))
    
    # 値でソート
    tier_ranks.sort(key=lambda x: x[1])
    
    if tier_ranks:
        min_rank = tier_ranks[0][0]  # 最低ランク
        max_rank = tier_ranks[-1][0]  # 最高ランク
        return min_rank, max_rank
    
    return base_rank, base_rank

async def join_ranked_recruit(ctx):
    """ランクマッチ募集参加"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    if user_id in recruit['participants']:
        await ctx.send("⚠️ 既に参加済みです。")
        return
    
    if len(recruit['participants']) >= recruit['max_players']:
        await ctx.send("❌ 参加者が満員です。")
        return
    
    # ランク条件チェック
    if not check_rank_eligibility(user_id, recruit):
        rank_req = recruit['rank_requirement']
        await ctx.send(f"❌ ランク条件（{rank_req}）を満たしていません。\n"
                      f"💡 `!rank set current [ランク]` でランクを設定してください。")
        return
    
    # 参加処理
    recruit['participants'].append(user_id)
    
    current_count = len(recruit['participants'])
    max_players = recruit['max_players']
    
    # ステータス更新
    if current_count >= max_players:
        recruit['status'] = 'ready'
    
    # 参加者リスト作成
    guild = ctx.guild
    participants_list = []
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            # ランク情報を追加
            rank_info = get_user_rank_display(participant_id)
            participants_list.append(f"• {member.display_name} {rank_info}")
    
    # 更新メッセージ
    embed = discord.Embed(
        title="✅ ランクマッチ募集参加完了！" if current_count < max_players else "🎉 ランクマッチ募集参加者満員！",
        color=0x4a90e2 if current_count < max_players else 0xffd700
    )
    
    embed.add_field(
        name="📊 現在の状況",
        value=f"**参加者:** {current_count}/{max_players}人\n"
              f"**ランク条件:** {recruit['rank_requirement']}\n"
              f"**開始予定:** {recruit['scheduled_time']}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list),
        inline=True
    )
    
    if current_count >= max_players:
        embed.add_field(
            name="🎯 次のステップ",
            value="`!ranked team` - ランクバランスチーム分け\n`!ranked remind` - 全員に通知",
            inline=False
        )
    
    await ctx.send(embed=embed)

def check_rank_eligibility(user_id, recruit):
    """ランク条件をチェック"""
    if recruit['rank_requirement'] in ["any", "ランク問わず"]:
        return True
    
    if user_id not in user_ranks:
        return False
    
    user_rank_data = user_ranks[user_id]
    current_rank = user_rank_data.get('current')
    
    if not current_rank:
        return False
    
    user_rank_value = VALORANT_RANKS[current_rank]['value']
    
    # 最低ランクチェック
    if recruit['min_rank']:
        min_value = VALORANT_RANKS[recruit['min_rank']]['value']
        if user_rank_value < min_value:
            return False
    
    # 最高ランクチェック
    if recruit['max_rank']:
        max_value = VALORANT_RANKS[recruit['max_rank']]['value']
        if user_rank_value > max_value:
            return False
    
    return True

def get_user_rank_display(user_id):
    """ユーザーのランク表示を取得"""
    if user_id not in user_ranks:
        return "(ランク未設定)"
    
    user_rank_data = user_ranks[user_id]
    current_rank = user_rank_data.get('current')
    
    if not current_rank:
        return "(ランク未設定)"
    
    return f"({VALORANT_RANKS[current_rank]['display']})"

async def leave_ranked_recruit(ctx):
    """ランクマッチ募集離脱"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    if user_id not in recruit['participants']:
        await ctx.send("⚠️ ランクマッチ募集に参加していません。")
        return
    
    # 作成者の場合は特別処理
    if user_id == recruit['creator'].id:
        if len(recruit['participants']) > 1:
            await ctx.send("⚠️ 作成者は他の参加者がいる間は離脱できません。`!ranked end` で募集を終了してください。")
            return
    
    # 離脱処理
    recruit['participants'].remove(user_id)
    recruit['status'] = 'recruiting'
    
    await ctx.send(f"✅ {ctx.author.display_name} がランクマッチ募集から離脱しました。")

async def show_ranked_recruit_status(ctx):
    """ランクマッチ募集状況表示"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    # 参加者リスト作成（ランク情報付き）
    guild = ctx.guild
    participants_list = []
    rank_stats = {}
    
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            rank_info = get_user_rank_display(participant_id)
            participants_list.append(f"• {member.display_name} {rank_info}")
            
            # ランク統計
            if participant_id in user_ranks and user_ranks[participant_id].get('current'):
                rank = user_ranks[participant_id]['current']
                tier = VALORANT_RANKS[rank]['tier']
                rank_stats[tier] = rank_stats.get(tier, 0) + 1
    
    status_map = {
        'recruiting': '📢 募集中',
        'ready': '✅ 準備完了',
        'in_progress': '🎮 進行中',
        'ended': '🏁 終了'
    }
    
    embed = discord.Embed(
        title="📊 ランクマッチ募集状況",
        color=0x4a90e2
    )
    
    embed.add_field(
        name="基本情報",
        value=f"**ステータス:** {status_map.get(recruit['status'], recruit['status'])}\n"
              f"**ランク条件:** {recruit['rank_requirement']}\n"
              f"**参加者:** {len(recruit['participants'])}/{recruit['max_players']}人\n"
              f"**開始予定:** {recruit['scheduled_time']}",
        inline=True
    )
    
    embed.add_field(
        name="👥 参加者一覧",
        value="\n".join(participants_list) if participants_list else "なし",
        inline=True
    )
    
    # ランク分布
    if rank_stats:
        tier_names = {9: "レディアント", 8: "イモータル", 7: "アセンダント", 6: "ダイヤ", 5: "プラチナ", 4: "ゴールド", 3: "シルバー", 2: "ブロンズ", 1: "アイアン"}
        rank_distribution = []
        for tier in sorted(rank_stats.keys(), reverse=True):
            tier_name = tier_names.get(tier, f"ティア{tier}")
            rank_distribution.append(f"{tier_name}: {rank_stats[tier]}人")
        
        embed.add_field(
            name="🏆 ランク分布",
            value="\n".join(rank_distribution),
            inline=False
        )
    
    if recruit.get('teams'):
        embed.add_field(
            name="🎯 チーム分け",
            value="チーム分け済み (詳細は `!ranked team` で確認)",
            inline=False
        )
    
    embed.set_footer(text=f"作成者: {recruit['creator'].display_name} | 作成時刻: {recruit['created_at'].strftime('%H:%M')}")
    
    await ctx.send(embed=embed)

async def end_ranked_recruit(ctx):
    """ランクマッチ募集終了"""
    channel_id = ctx.channel.id
    user_id = ctx.author.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    # 作成者または管理者のみ終了可能
    if user_id != recruit['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ ランクマッチ募集の作成者または管理者のみ終了できます。")
        return
    
    # リマインダーキャンセル
    recruit_id = recruit['id']
    if recruit_id in rank_recruit_reminders:
        rank_recruit_reminders[recruit_id].cancel()
        del rank_recruit_reminders[recruit_id]
    
    # 募集削除
    del active_rank_recruits[channel_id]
    
    embed = discord.Embed(
        title="🏁 ランクマッチ募集終了",
        description=f"**{recruit['rank_requirement']}** の募集を終了しました。",
        color=0xff6b6b
    )
    
    embed.add_field(
        name="📊 最終統計",
        value=f"**参加者数:** {len(recruit['participants'])}人\n"
              f"**募集時間:** {(datetime.now() - recruit['created_at']).seconds // 60}分間",
        inline=False
    )
    
    await ctx.send(embed=embed)

async def add_to_ranked_recruit(ctx, args):
    """ランクマッチ募集にユーザーを追加"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    # 権限チェック
    if ctx.author.id != recruit['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ ランクマッチ募集の作成者または管理者のみメンバーを追加できます。")
        return
    
    if not args:
        await ctx.send("❌ 追加するユーザーを指定してください。例: `!ranked add @ユーザー`")
        return
    
    # メンションされたユーザーを取得
    mentioned_users = []
    for arg in args:
        if arg.startswith('<@') and arg.endswith('>'):
            try:
                user_id = int(arg.strip('<@!>'))
                member = ctx.guild.get_member(user_id)
                if member:
                    mentioned_users.append(member)
            except ValueError:
                pass
    
    if not mentioned_users:
        await ctx.send("❌ 有効なユーザーメンションが見つかりません。例: `!ranked add @ユーザー`")
        return
    
    added_users = []
    already_joined = []
    max_capacity = []
    rank_ineligible = []
    
    for member in mentioned_users:
        if member.id in recruit['participants']:
            already_joined.append(member.display_name)
        elif len(recruit['participants']) >= recruit['max_players']:
            max_capacity.append(member.display_name)
        elif not check_rank_eligibility(member.id, recruit):
            rank_ineligible.append(member.display_name)
        else:
            # 追加処理
            recruit['participants'].append(member.id)
            added_users.append(member.display_name)
            
            # ステータス更新
            if len(recruit['participants']) >= recruit['max_players']:
                recruit['status'] = 'ready'
    
    # 既存の募集メッセージを更新
    try:
        if 'message_id' in recruit:
            channel = ctx.channel
            message = await channel.fetch_message(recruit['message_id'])
            
            # 最新の募集情報でembedを再作成
            updated_embed = await create_ranked_embed(recruit, ctx.guild)
            
            # 操作方法を追加（元のメッセージと同じ形式）
            updated_embed.add_field(
                name="🔧 操作方法",
                value="**ボタン操作:** 下のボタンをクリック\n"
                      "**コマンド操作:** `!ranked join/leave/status`",
                inline=False
            )
            
            # ランク条件の詳細表示（元のメッセージと同じ形式）
            if recruit.get('min_rank') or recruit.get('max_rank'):
                rank_details = []
                if recruit.get('min_rank'):
                    rank_details.append(f"最低ランク: {VALORANT_RANKS[recruit['min_rank']]['display']}")
                if recruit.get('max_rank'):
                    rank_details.append(f"最高ランク: {VALORANT_RANKS[recruit['max_rank']]['display']}")
                
                updated_embed.add_field(
                    name="🎯 ランク詳細",
                    value="\n".join(rank_details),
                    inline=False
                )
            
            # メッセージを更新（ボタンは維持）
            await message.edit(embed=updated_embed)
    except:
        pass  # メッセージ更新に失敗した場合はスキップ
    
    # 簡潔な確認メッセージ
    result_messages = []
    
    if added_users:
        result_messages.append(f"✅ **追加完了:** {', '.join(added_users)}")
    
    if already_joined:
        result_messages.append(f"⚠️ **既に参加済み:** {', '.join(already_joined)}")
    
    if max_capacity:
        result_messages.append(f"❌ **満員のため追加不可:** {', '.join(max_capacity)}")
    
    if rank_ineligible:
        result_messages.append(f"❌ **ランク条件不適合:** {', '.join(rank_ineligible)}")
    
    if result_messages:
        current_count = len(recruit['participants'])
        status_text = f"📊 現在 {current_count}/{recruit['max_players']}人"
        
        final_message = "\n".join(result_messages) + f"\n{status_text}"
        await ctx.send(final_message)
    else:
        await ctx.send("ℹ️ 処理するユーザーがありませんでした。")

async def kick_from_ranked_recruit(ctx, args):
    """ランクマッチ募集からユーザーをキック"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    # 権限チェック
    if ctx.author.id != recruit['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ ランクマッチ募集の作成者または管理者のみキックできます。")
        return
    
    if not args:
        await ctx.send("❌ キックするユーザーを指定してください。例: `!ranked kick @ユーザー`")
        return
    
    # メンションされたユーザーを取得
    if ctx.message.mentions:
        target_user = ctx.message.mentions[0]
        if target_user.id in recruit['participants']:
            recruit['participants'].remove(target_user.id)
            recruit['status'] = 'recruiting'
            await ctx.send(f"✅ {target_user.display_name} をランクマッチ募集からキックしました。")
        else:
            await ctx.send("❌ そのユーザーはランクマッチ募集に参加していません。")
    else:
        await ctx.send("❌ 有効なユーザーメンションが見つかりません。")

async def send_ranked_recruit_reminder(ctx):
    """ランクマッチ募集リマインダー送信"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    # 権限チェック
    if ctx.author.id != recruit['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ ランクマッチ募集の作成者または管理者のみリマインダーを送信できます。")
        return
    
    # 参加者にメンション
    guild = ctx.guild
    mentions = []
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            mentions.append(member.mention)
    
    embed = discord.Embed(
        title="🔔 ランクマッチ募集リマインダー",
        description=f"**{recruit['rank_requirement']}** の時間です！",
        color=0xffaa00
    )
    
    embed.add_field(
        name="📊 情報",
        value=f"**参加者:** {len(recruit['participants'])}/{recruit['max_players']}人\n"
              f"**開始予定:** {recruit['scheduled_time']}\n"
              f"**ランク条件:** {recruit['rank_requirement']}",
        inline=False
    )
    
    if len(recruit['participants']) >= recruit['max_players']:
        embed.add_field(
            name="🎯 準備完了",
            value="参加者が揃いました！ランクマッチを開始してください。",
            inline=False
        )
    
    mention_text = " ".join(mentions) if mentions else "参加者なし"
    await ctx.send(f"{mention_text}\n", embed=embed)

async def ranked_recruit_team_divide(ctx):
    """ランクマッチ募集でランクバランスチーム分け実行"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    if len(recruit['participants']) < 2:
        await ctx.send("❌ チーム分けには最低2人必要です。")
        return
    
    guild = ctx.guild
    members = []
    ranked_members = []
    
    # 参加者のランク情報を取得
    total_rank_value = 0
    rank_count = 0
    
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            members.append(member)
            
            # ランク情報取得
            if participant_id in user_ranks and user_ranks[participant_id].get('current'):
                rank_name = user_ranks[participant_id]['current']
                rank_value = VALORANT_RANKS[rank_name]['value']
                ranked_members.append({
                    'member': member,
                    'rank': rank_name,
                    'value': rank_value
                })
                total_rank_value += rank_value
                rank_count += 1
            else:
                # ランク未設定者は平均ランクで計算
                ranked_members.append({
                    'member': member,
                    'rank': None,
                    'value': 400  # ゴールド1レベル
                })
    
    # 平均ランク値を計算
    if rank_count > 0:
        avg_rank_value = total_rank_value / rank_count
    else:
        avg_rank_value = 400
    
    # 未ランクメンバーに平均値を適用
    for member_data in ranked_members:
        if member_data['rank'] is None:
            member_data['value'] = avg_rank_value
    
    # ランクバランス調整チーム分け
    def balance_teams_by_rank(members_data, team_size):
        """ランク値の合計ができるだけ均等になるようにチーム分け"""
        members_data = sorted(members_data, key=lambda x: x['value'], reverse=True)
        team1 = []
        team2 = []
        
        for member_data in members_data:
            # 現在のチーム合計値を計算
            team1_total = sum(m['value'] for m in team1)
            team2_total = sum(m['value'] for m in team2)
            
            # チームサイズ制限もチェック
            if len(team1) >= team_size:
                team2.append(member_data)
            elif len(team2) >= team_size:
                team1.append(member_data)
            else:
                # より合計値が低いチームに追加
                if team1_total <= team2_total:
                    team1.append(member_data)
                else:
                    team2.append(member_data)
        
        return team1, team2
    
    # チーム分けの実行
    team_size = len(ranked_members) // 2
    team1, team2 = balance_teams_by_rank(ranked_members, team_size)
    
    # チーム情報を保存
    recruit['teams'] = {
        'team1': [m['member'].id for m in team1],
        'team2': [m['member'].id for m in team2]
    }
    
    embed = discord.Embed(
        title="🎯 ランクマッチ チーム分け結果",
        description="ランクバランスを考慮したチーム分け",
        color=0x4a90e2
    )
    
    # チーム1の情報
    team1_display = []
    team1_total = 0
    for member_data in team1:
        member = member_data['member']
        rank = member_data['rank']
        value = member_data['value']
        team1_total += value
        
        if rank:
            rank_info = VALORANT_RANKS[rank]
            team1_display.append(f"• {member.display_name} ({rank_info['display']})")
        else:
            team1_display.append(f"• {member.display_name} (ランク未設定)")
    
    embed.add_field(
        name="🔴 チーム1",
        value="\n".join(team1_display),
        inline=True
    )
    
    # チーム2の情報
    team2_display = []
    team2_total = 0
    for member_data in team2:
        member = member_data['member']
        rank = member_data['rank']
        value = member_data['value']
        team2_total += value
        
        if rank:
            rank_info = VALORANT_RANKS[rank]
            team2_display.append(f"• {member.display_name} ({rank_info['display']})")
        else:
            team2_display.append(f"• {member.display_name} (ランク未設定)")
    
    embed.add_field(
        name="🔵 チーム2",
        value="\n".join(team2_display),
        inline=True
    )
    
    # バランス情報
    avg1 = team1_total / len(team1) if team1 else 0
    avg2 = team2_total / len(team2) if team2 else 0
    balance_diff = abs(avg1 - avg2)
    balance_quality = "完璧" if balance_diff < 50 else "良好" if balance_diff < 100 else "やや偏り" if balance_diff < 150 else "偏りあり"
    
    embed.add_field(
        name="⚖️ バランス評価",
        value=f"{balance_quality} (差: {balance_diff:.0f})",
        inline=False
    )
    
    embed.add_field(
        name="📊 平均ランク値",
        value=f"チーム1: {avg1:.0f} | チーム2: {avg2:.0f}",
        inline=False
    )
    
    embed.set_footer(text=f"ランク条件: {recruit['rank_requirement']} | 頑張って！")
    
    await ctx.send(embed=embed)

async def check_ranked_recruit_ranks(ctx):
    """ランクマッチ募集参加者のランク確認"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_rank_recruits:
        await ctx.send("❌ このチャンネルにアクティブなランクマッチ募集がありません。")
        return
    
    recruit = active_rank_recruits[channel_id]
    
    guild = ctx.guild
    rank_check_results = []
    eligible_count = 0
    ineligible_count = 0
    
    for participant_id in recruit['participants']:
        member = guild.get_member(participant_id)
        if member:
            # ランク適格性チェック
            is_eligible = check_rank_eligibility(participant_id, recruit)
            rank_display = get_user_rank_display(participant_id)
            
            if is_eligible:
                status = "✅"
                eligible_count += 1
            else:
                status = "❌"
                ineligible_count += 1
            
            rank_check_results.append(f"{status} {member.display_name} {rank_display}")
    
    embed = discord.Embed(
        title="🔍 参加者ランク確認",
        color=0x00ff88 if ineligible_count == 0 else 0xff6b6b
    )
    
    embed.add_field(
        name="📊 確認結果",
        value=f"**適格者:** {eligible_count}人\n"
              f"**不適格者:** {ineligible_count}人\n"
              f"**ランク条件:** {recruit['rank_requirement']}",
        inline=True
    )
    
    embed.add_field(
        name="👥 詳細結果",
        value="\n".join(rank_check_results) if rank_check_results else "参加者なし",
        inline=False
    )
    
    if ineligible_count > 0:
        embed.add_field(
            name="⚠️ 注意",
            value="ランク条件を満たしていない参加者がいます。\n"
                  "適切にランクを設定するか、募集から除外してください。",
            inline=False
        )
    
    await ctx.send(embed=embed)

async def schedule_ranked_recruit_reminder(ctx, recruit_data):
    """ランクマッチ募集リマインダーのスケジュール設定"""
    # 簡単な時間解析（scrimと同じロジック）
    scheduled_time = recruit_data['scheduled_time']
    
    # "20:00" 形式の解析
    if ':' in scheduled_time:
        try:
            time_parts = scheduled_time.split(':')
            target_hour = int(time_parts[0])
            target_minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            
            now = datetime.now()
            target_time = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            
            # 過去の時間の場合は翌日に設定
            if target_time <= now:
                target_time += timedelta(days=1)
            
            # リマインダータスク作成
            delay = (target_time - now).total_seconds() - 300  # 5分前に通知
            
            if delay > 0:
                async def reminder_task():
                    await asyncio.sleep(delay)
                    if recruit_data['id'] in rank_recruit_reminders:
                        channel = bot.get_channel(ctx.channel.id)
                        if channel:
                            await channel.send(f"🔔 **リマインダー**: 5分後にランクマッチ開始予定です！")
                
                task = asyncio.create_task(reminder_task())
                rank_recruit_reminders[recruit_data['id']] = task
                
        except ValueError:
            pass  # 時間解析に失敗した場合はスキップ

async def add_to_scrim(ctx, args):
    """カスタムゲームにユーザーを追加"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    # 権限チェック
    if ctx.author.id != scrim['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ カスタムゲームの作成者または管理者のみメンバーを追加できます。")
        return
    
    if not args:
        await ctx.send("❌ 追加するユーザーを指定してください。例: `!custom add @ユーザー`")
        return
    
    # メンションされたユーザーを取得
    mentioned_users = []
    for arg in args:
        if arg.startswith('<@') and arg.endswith('>'):
            try:
                user_id = int(arg.strip('<@!>'))
                member = ctx.guild.get_member(user_id)
                if member:
                    mentioned_users.append(member)
            except ValueError:
                pass
    
    if not mentioned_users:
        await ctx.send("❌ 有効なユーザーメンションが見つかりません。例: `!custom add @ユーザー`")
        return
    
    added_users = []
    already_joined = []
    max_capacity = []
    
    for member in mentioned_users:
        if member.id in scrim['participants']:
            already_joined.append(member.display_name)
        elif len(scrim['participants']) >= scrim['max_players']:
            max_capacity.append(member.display_name)
        else:
            # 追加処理
            scrim['participants'].append(member.id)
            added_users.append(member.display_name)
            
            # ステータス更新
            if len(scrim['participants']) >= scrim['max_players']:
                scrim['status'] = 'ready'
    
    # 既存の募集メッセージを更新
    try:
        if 'message_id' in scrim:
            channel = ctx.channel
            message = await channel.fetch_message(scrim['message_id'])
            
            # 最新の募集情報でembedを再作成
            updated_embed = await create_custom_embed(scrim, ctx.guild)
            
            # 操作方法を追加（元のメッセージと同じ形式）
            updated_embed.add_field(
                name="🔧 操作方法",
                value="**ボタン操作:** 下のボタンをクリック\n"
                      "**コマンド操作:** `!custom join/leave/status`",
                inline=False
            )
            
            # メッセージを更新（ボタンは維持）
            await message.edit(embed=updated_embed)
    except:
        pass  # メッセージ更新に失敗した場合はスキップ
    
    # 簡潔な確認メッセージ
    result_messages = []
    
    if added_users:
        result_messages.append(f"✅ **追加完了:** {', '.join(added_users)}")
    
    if already_joined:
        result_messages.append(f"⚠️ **既に参加済み:** {', '.join(already_joined)}")
    
    if max_capacity:
        result_messages.append(f"❌ **満員のため追加不可:** {', '.join(max_capacity)}")
    
    if result_messages:
        current_count = len(scrim['participants'])
        status_text = f"📊 現在 {current_count}/{scrim['max_players']}人"
        
        final_message = "\n".join(result_messages) + f"\n{status_text}"
        await ctx.send(final_message)
    else:
        await ctx.send("ℹ️ 処理するユーザーがありませんでした。")

async def kick_from_scrim(ctx, args):
    """カスタムゲームからユーザーをキック"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    # 権限チェック
    if ctx.author.id != scrim['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ カスタムゲームの作成者または管理者のみキックできます。")
        return
    
    if not args:
        await ctx.send("❌ キックするユーザーを指定してください。例: `!custom kick @ユーザー`")
        return
    
    # メンションされたユーザーを取得
    if ctx.message.mentions:
        target_user = ctx.message.mentions[0]
        if target_user.id in scrim['participants']:
            scrim['participants'].remove(target_user.id)
            scrim['status'] = 'recruiting'
            await ctx.send(f"✅ {target_user.display_name} をカスタムゲームからキックしました。")
        else:
            await ctx.send("❌ そのユーザーはカスタムゲームに参加していません。")
    else:
        await ctx.send("❌ 有効なユーザーメンションが見つかりません。")

async def send_scrim_reminder(ctx):
    """カスタムゲームリマインダー送信"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    # 権限チェック
    if ctx.author.id != scrim['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ カスタムゲームの作成者または管理者のみリマインダーを送信できます。")
        return
    
    # 参加者にメンション
    guild = ctx.guild
    mentions = []
    for participant_id in scrim['participants']:
        member = guild.get_member(participant_id)
        if member:
            mentions.append(member.mention)
    
    embed = discord.Embed(
        title="🔔 カスタムゲームリマインダー",
        description=f"**{scrim['game_mode']}** の時間です！",
        color=0xffaa00
    )
    
    embed.add_field(
        name="📊 情報",
        value=f"**参加者:** {len(scrim['participants'])}/{scrim['max_players']}人\n"
              f"**開始予定:** {scrim['scheduled_time']}",
        inline=False
    )
    
    if len(scrim['participants']) >= scrim['max_players']:
        embed.add_field(
            name="🎯 準備完了",
            value="参加者が揃いました！ゲームを開始してください。",
            inline=False
        )
    
    mention_text = " ".join(mentions) if mentions else "参加者なし"
    await ctx.send(f"{mention_text}\n", embed=embed)

async def scrim_team_divide(ctx):
    """カスタムゲームでチーム分け実行"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    if len(scrim['participants']) < 2:
        await ctx.send("❌ チーム分けには最低2人必要です。")
        return
    
    guild = ctx.guild
    members = []
    for participant_id in scrim['participants']:
        member = guild.get_member(participant_id)
        if member:
            members.append(member)
    
    # チーム分けロジック
    random.shuffle(members)
    
    if scrim['game_mode'] in ['5v5', '5V5']:
        team_size = 5
    elif scrim['game_mode'] in ['3v3', '3V3']:
        team_size = 3
    elif scrim['game_mode'] in ['2v2', '2V2']:
        team_size = 2
    else:
        team_size = len(members) // 2
    
    team1 = members[:team_size]
    team2 = members[team_size:team_size*2]
    extras = members[team_size*2:] if len(members) > team_size*2 else []
    
    # チーム情報を保存
    scrim['teams'] = {
        'team1': [m.id for m in team1],
        'team2': [m.id for m in team2],
        'extras': [m.id for m in extras]
    }
    
    embed = discord.Embed(
        title="🎯 カスタムゲームチーム分け結果",
        color=0x00ff88
    )
    
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
    
    if extras:
        embed.add_field(
            name="⚪ 待機",
            value="\n".join([f"• {m.display_name}" for m in extras]),
            inline=False
        )
    
    embed.set_footer(text=f"ゲームモード: {scrim['game_mode']} | 頑張って！")
    
    await ctx.send(embed=embed)

async def show_scrim_info(ctx):
    """カスタムゲーム詳細情報表示"""
    channel_id = ctx.channel.id
    
    if channel_id not in active_scrims:
        await ctx.send("❌ このチャンネルにアクティブなカスタムゲームがありません。")
        return
    
    scrim = active_scrims[channel_id]
    
    embed = discord.Embed(
        title="📋 カスタムゲーム詳細情報",
        color=0x00aaff
    )
    
    embed.add_field(
        name="基本情報",
        value=f"**ID:** {scrim['id'][:8]}\n"
              f"**作成者:** {scrim['creator'].display_name}\n"
              f"**作成時刻:** {scrim['created_at'].strftime('%m/%d %H:%M')}\n"
              f"**ゲームモード:** {scrim['game_mode']}",
        inline=True
    )
    
    embed.add_field(
        name="募集状況",
        value=f"**最大人数:** {scrim['max_players']}人\n"
              f"**現在:** {len(scrim['participants'])}人\n"
              f"**開始予定:** {scrim['scheduled_time']}\n"
              f"**ステータス:** {scrim['status']}",
        inline=True
    )
    
    if scrim.get('description'):
        embed.add_field(
            name="📝 説明",
            value=scrim['description'],
            inline=False
        )
    
    if scrim.get('teams'):
        embed.add_field(
            name="🎯 チーム状況",
            value="チーム分け完了済み",
            inline=False
        )
    
    await ctx.send(embed=embed)

async def schedule_scrim_reminder(ctx, scrim_data):
    """スクリムリマインダーのスケジュール設定"""
    # 簡単な時間解析（実装は基本的なもの）
    scheduled_time = scrim_data['scheduled_time']
    
    # "20:00" 形式の解析
    if ':' in scheduled_time:
        try:
            time_parts = scheduled_time.split(':')
            target_hour = int(time_parts[0])
            target_minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            
            now = datetime.now()
            target_time = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            
            # 過去の時間の場合は翌日に設定
            if target_time <= now:
                target_time += timedelta(days=1)
            
            # リマインダータスク作成
            delay = (target_time - now).total_seconds() - 300  # 5分前に通知
            
            if delay > 0:
                async def reminder_task():
                    await asyncio.sleep(delay)
                    if scrim_data['id'] in scrim_reminders:
                        channel = bot.get_channel(ctx.channel.id)
                        if channel:
                            await channel.send(f"🔔 **リマインダー**: 5分後にカスタムゲーム開始予定です！")
                
                task = asyncio.create_task(reminder_task())
                scrim_reminders[scrim_data['id']] = task
                
        except ValueError:
            pass  # 時間解析に失敗した場合はスキップ



# ===============================
# トーナメント機能
# ===============================

@bot.command(name='tournament', aliases=['tourney'], help='ミニトーナメント開催（例: !tournament create シングル戦, !tournament join, !tournament bracket）')
@prevent_duplicate_execution
async def tournament_manager(ctx, action=None, *args):
    """ミニトーナメント管理システム"""
    try:
        if not action:
            # ヘルプ表示
            embed = discord.Embed(
                title="🏆 トーナメント機能",
                description="ミニトーナメント開催・管理システム",
                color=0xffd700
            )
            
            embed.add_field(
                name="📝 基本コマンド",
                value="`!tournament create [形式]` - トーナメント作成\n"
                      "`!tournament join` - 参加登録\n"
                      "`!tournament leave` - 参加取消\n"
                      "`!tournament start` - 開始\n"
                      "`!tournament bracket` - ブラケット表示",
                inline=False
            )
            
            embed.add_field(
                name="⚔️ 試合管理",
                value="`!tournament result [勝者]` - 結果入力\n"
                      "`!tournament next` - 次の試合\n"
                      "`!tournament status` - 進行状況\n"
                      "`!tournament end` - 終了",
                inline=False
            )
            
            embed.add_field(
                name="⚙️ 管理コマンド",
                value="`!tournament add @ユーザー` - メンバー手動追加",
                inline=False
            )
            
            embed.add_field(
                name="🎯 形式例",
                value="`!tournament create シングル戦` - シングル戦\n"
                      "`!tournament create ダブル戦` - ダブル戦\n"
                      "`!tournament create チーム戦` - チーム戦",
                inline=False
            )
            
            await ctx.send(embed=embed)
            return
        
        guild_id = ctx.guild.id
        
        if action.lower() in ['create', 'new', '作成']:
            await create_tournament(ctx, args)
            
        elif action.lower() in ['join', 'j', '参加']:
            await join_tournament(ctx)
            
        elif action.lower() in ['leave', 'l', '離脱']:
            await leave_tournament(ctx)
            
        elif action.lower() in ['start', 'begin', '開始']:
            await start_tournament(ctx)
            
        elif action.lower() in ['bracket', 'br', 'ブラケット']:
            await show_tournament_bracket(ctx)
            
        elif action.lower() in ['status', 's', '状況']:
            await show_tournament_status(ctx)
            
        elif action.lower() in ['result', 'res', '結果']:
            await input_match_result(ctx, args)
            
        elif action.lower() in ['next', 'n', '次']:
            await show_next_matches(ctx)
            
        elif action.lower() in ['end', 'finish', '終了']:
            await end_tournament(ctx)
            
        elif action.lower() in ['add', 'invite', '追加', '招待']:
            await add_to_tournament(ctx, args)
            
        else:
            await ctx.send("❌ 不明なアクション。`!tournament` でヘルプを確認してください。")
            
    except Exception as e:
        await ctx.send(f"❌ トーナメント機能でエラーが発生しました: {str(e)}")
        print(f"トーナメント機能エラー: {e}")

async def create_tournament(ctx, args):
    """トーナメント作成"""
    guild_id = ctx.guild.id
    
    # 既存のトーナメントチェック
    if guild_id in active_tournaments:
        tournament = active_tournaments[guild_id]
        if tournament['status'] != 'ended':
            await ctx.send(f"❌ 既にトーナメントが進行中です。`!tournament end` で終了してください。")
            return
    
    # 形式解析
    tournament_type = "シングル戦"
    max_participants = 16
    description = ""
    
    if args:
        format_input = " ".join(args)
        if "ダブル" in format_input or "double" in format_input.lower():
            tournament_type = "ダブル戦"
        elif "チーム" in format_input or "team" in format_input.lower():
            tournament_type = "チーム戦"
        elif "シングル" in format_input or "single" in format_input.lower():
            tournament_type = "シングル戦"
        else:
            description = format_input
    
    # トーナメントデータ作成
    tournament_data = {
        'id': f"{guild_id}_{int(datetime.now().timestamp())}",
        'guild_id': guild_id,
        'creator': ctx.author,
        'created_at': datetime.now(),
        'tournament_type': tournament_type,
        'max_participants': max_participants,
        'description': description,
        'participants': [],
        'status': 'registration',  # registration, ongoing, ended
        'bracket': [],
        'current_round': 0,
        'matches': {}
    }
    
    active_tournaments[guild_id] = tournament_data
    
    embed = discord.Embed(
        title="🏆 トーナメント作成完了！",
        description=f"**{tournament_type}** トーナメントの参加者募集を開始",
        color=0xffd700
    )
    
    embed.add_field(
        name="📊 基本情報",
        value=f"**形式:** {tournament_type}\n"
              f"**最大参加者:** {max_participants}人\n"
              f"**現在の参加者:** 0人",
        inline=True
    )
    
    embed.add_field(
        name="📝 詳細",
        value=description if description else "なし",
        inline=True
    )
    
    embed.add_field(
        name="🔧 操作方法",
        value="`!tournament join` - 参加登録\n"
              "`!tournament start` - 開始\n"
              "`!tournament bracket` - ブラケット確認",
        inline=False
    )
    
    # ボタン付き募集メッセージ作成
    embed = await create_tournament_embed(tournament_data, ctx.guild)
    
    # 操作方法を追加（ボタンとコマンド両方）
    embed.add_field(
        name="🔧 操作方法",
        value="**ボタン操作:** 下のボタンをクリック\n"
              "**コマンド操作:** `!tournament join/leave/status`",
        inline=False
    )
    
    view = TournamentView()
    message = await ctx.send(content="@everyone", embed=embed, view=view)
    tournament_data['message_id'] = message.id
    view.message = message  # ビューにメッセージオブジェクトを保存

async def join_tournament(ctx):
    """トーナメント参加"""
    guild_id = ctx.guild.id
    user_id = ctx.author.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] != 'registration':
        await ctx.send("❌ 現在参加登録を受け付けていません。")
        return
    
    if user_id in [p['user_id'] for p in tournament['participants']]:
        await ctx.send("⚠️ 既に参加登録済みです。")
        return
    
    if len(tournament['participants']) >= tournament['max_participants']:
        await ctx.send("❌ 参加者が満員です。")
        return
    
    # 参加登録
    participant = {
        'user_id': user_id,
        'user': ctx.author,
        'joined_at': datetime.now(),
        'wins': 0,
        'losses': 0
    }
    
    tournament['participants'].append(participant)
    
    embed = discord.Embed(
        title="✅ トーナメント参加登録完了",
        color=0x00ff88
    )
    
    current_count = len(tournament['participants'])
    
    embed.add_field(
        name="📊 現在の状況",
        value=f"**参加者:** {current_count}/{tournament['max_participants']}人\n"
              f"**形式:** {tournament['tournament_type']}\n"
              f"**必要最小人数:** 4人",
        inline=True
    )
    
    # 参加者リスト（最新5人のみ表示）
    recent_participants = tournament['participants'][-5:]
    participant_list = [f"• {p['user'].display_name}" for p in recent_participants]
    
    embed.add_field(
        name="👥 最新参加者",
        value="\n".join(participant_list),
        inline=True
    )
    
    if current_count >= 4:
        embed.add_field(
            name="🎯 開始可能",
            value=f"最小人数に達しました！\n`!tournament start` で開始できます。",
            inline=False
        )
    
    await ctx.send(embed=embed)

async def leave_tournament(ctx):
    """トーナメント離脱"""
    guild_id = ctx.guild.id
    user_id = ctx.author.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] != 'registration':
        await ctx.send("❌ 既に開始されているため離脱できません。")
        return
    
    # 参加者から削除
    for i, participant in enumerate(tournament['participants']):
        if participant['user_id'] == user_id:
            del tournament['participants'][i]
            await ctx.send(f"✅ {ctx.author.display_name} がトーナメントから離脱しました。")
            return
    
    await ctx.send("❌ トーナメントに参加していません。")

async def start_tournament(ctx):
    """トーナメント開始"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    # 権限チェック
    if ctx.author.id != tournament['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ トーナメント作成者または管理者のみ開始できます。")
        return
    
    if tournament['status'] != 'registration':
        await ctx.send("❌ 既に開始されているか、終了しています。")
        return
    
    participants = tournament['participants']
    
    if len(participants) < 4:
        await ctx.send("❌ トーナメント開始には最低4人必要です。")
        return
    
    # ブラケット生成
    import math
    
    # 2の累乗に調整
    bracket_size = 2 ** math.ceil(math.log2(len(participants)))
    
    # 参加者をシャッフル
    shuffled_participants = participants.copy()
    random.shuffle(shuffled_participants)
    
    # 不戦勝者（BYE）を追加
    while len(shuffled_participants) < bracket_size:
        shuffled_participants.append(None)  # BYE
    
    # 第1ラウンドの試合を作成
    matches = []
    match_id = 1
    
    for i in range(0, len(shuffled_participants), 2):
        player1 = shuffled_participants[i]
        player2 = shuffled_participants[i + 1] if i + 1 < len(shuffled_participants) else None
        
        match_data = {
            'id': match_id,
            'round': 1,
            'player1': player1,
            'player2': player2,
            'winner': None,
            'status': 'pending'  # pending, completed
        }
        
        # BYE の処理
        if player1 and not player2:
            match_data['winner'] = player1
            match_data['status'] = 'completed'
        elif player2 and not player1:
            match_data['winner'] = player2
            match_data['status'] = 'completed'
        
        matches.append(match_data)
        match_id += 1
    
    tournament['bracket'] = matches
    tournament['status'] = 'ongoing'
    tournament['current_round'] = 1
    
    embed = discord.Embed(
        title="🏁 トーナメント開始！",
        description=f"**{tournament['tournament_type']}** トーナメントが開始されました",
        color=0xffd700
    )
    
    embed.add_field(
        name="📊 情報",
        value=f"**参加者数:** {len([p for p in participants if p])}人\n"
              f"**第1ラウンド試合数:** {len([m for m in matches if m['status'] == 'pending'])}試合\n"
              f"**形式:** シングルエリミネーション",
        inline=False
    )
    
    embed.add_field(
        name="🎯 次のステップ",
        value="`!tournament bracket` - ブラケット確認\n"
              "`!tournament next` - 次の試合確認\n"
              "`!tournament result @勝者` - 結果入力",
        inline=False
    )
    
    await ctx.send(embed=embed)

async def show_tournament_bracket(ctx):
    """ブラケット表示"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] == 'registration':
        await ctx.send("❌ まだトーナメントが開始されていません。")
        return
    
    embed = discord.Embed(
        title="🏆 トーナメントブラケット",
        color=0xffd700
    )
    
    # ラウンド別に試合を整理
    rounds = {}
    for match in tournament['bracket']:
        round_num = match['round']
        if round_num not in rounds:
            rounds[round_num] = []
        rounds[round_num].append(match)
    
    for round_num in sorted(rounds.keys()):
        round_matches = rounds[round_num]
        
        match_list = []
        for match in round_matches:
            p1_name = match['player1']['user'].display_name if match['player1'] else "BYE"
            p2_name = match['player2']['user'].display_name if match['player2'] else "BYE"
            
            if match['status'] == 'completed':
                winner_name = match['winner']['user'].display_name if match['winner'] else "TBD"
                match_text = f"**{p1_name}** vs **{p2_name}** → 🏆 {winner_name}"
            else:
                match_text = f"{p1_name} vs {p2_name}"
            
            match_list.append(match_text)
        
        embed.add_field(
            name=f"🔥 第{round_num}ラウンド",
            value="\n".join(match_list) if match_list else "試合なし",
            inline=False
        )
    
    # 進行状況
    total_matches = len(tournament['bracket'])
    completed_matches = len([m for m in tournament['bracket'] if m['status'] == 'completed'])
    
    embed.add_field(
        name="📊 進行状況",
        value=f"完了試合: {completed_matches}/{total_matches}\n"
              f"現在ラウンド: {tournament['current_round']}",
        inline=False
    )
    
    await ctx.send(embed=embed)

async def show_tournament_status(ctx):
    """トーナメント状況表示"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    status_map = {
        'registration': '📝 参加者募集中',
        'ongoing': '⚔️ 進行中',
        'ended': '🏁 終了'
    }
    
    embed = discord.Embed(
        title="📊 トーナメント状況",
        color=0x00aaff
    )
    
    embed.add_field(
        name="基本情報",
        value=f"**ステータス:** {status_map.get(tournament['status'], tournament['status'])}\n"
              f"**形式:** {tournament['tournament_type']}\n"
              f"**参加者:** {len(tournament['participants'])}人\n"
              f"**作成者:** {tournament['creator'].display_name}",
        inline=True
    )
    
    if tournament['status'] == 'ongoing':
        current_round_matches = [m for m in tournament['bracket'] if m['round'] == tournament['current_round']]
        pending_matches = [m for m in current_round_matches if m['status'] == 'pending']
        
        embed.add_field(
            name="進行状況",
            value=f"**現在ラウンド:** {tournament['current_round']}\n"
                  f"**待機中試合:** {len(pending_matches)}試合\n"
                  f"**完了試合:** {len([m for m in tournament['bracket'] if m['status'] == 'completed'])}試合",
            inline=True
        )
    
    embed.set_footer(text=f"ID: {tournament['id'][:8]} | 作成: {tournament['created_at'].strftime('%m/%d %H:%M')}")
    
    await ctx.send(embed=embed)

async def input_match_result(ctx, args):
    """試合結果入力"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] != 'ongoing':
        await ctx.send("❌ 現在進行中のトーナメントがありません。")
        return
    
    # 勝者の特定
    winner = None
    if ctx.message.mentions:
        winner_user = ctx.message.mentions[0]
        # 参加者から検索
        for participant in tournament['participants']:
            if participant['user_id'] == winner_user.id:
                winner = participant
                break
    
    if not winner:
        await ctx.send("❌ 有効な勝者を指定してください。例: `!tournament result @勝者`")
        return
    
    # 該当する試合を検索
    current_round = tournament['current_round']
    pending_matches = [m for m in tournament['bracket'] 
                      if m['round'] == current_round and m['status'] == 'pending']
    
    target_match = None
    for match in pending_matches:
        if (match['player1'] and match['player1']['user_id'] == winner['user_id']) or \
           (match['player2'] and match['player2']['user_id'] == winner['user_id']):
            target_match = match
            break
    
    if not target_match:
        await ctx.send("❌ 該当する試合が見つかりません。")
        return
    
    # 結果を記録
    target_match['winner'] = winner
    target_match['status'] = 'completed'
    
    # 勝者の統計更新
    winner['wins'] += 1
    
    # 敗者の統計更新
    loser = None
    if target_match['player1'] and target_match['player1']['user_id'] != winner['user_id']:
        loser = target_match['player1']
    elif target_match['player2'] and target_match['player2']['user_id'] != winner['user_id']:
        loser = target_match['player2']
    
    if loser:
        loser['losses'] += 1
    
    embed = discord.Embed(
        title="✅ 試合結果入力完了",
        color=0x00ff88
    )
    
    p1_name = target_match['player1']['user'].display_name if target_match['player1'] else "BYE"
    p2_name = target_match['player2']['user'].display_name if target_match['player2'] else "BYE"
    winner_name = winner['user'].display_name
    
    embed.add_field(
        name="試合結果",
        value=f"**{p1_name}** vs **{p2_name}**\n🏆 勝者: **{winner_name}**",
        inline=False
    )
    
    # 次ラウンドの生成をチェック
    current_round_matches = [m for m in tournament['bracket'] if m['round'] == current_round]
    pending_current = [m for m in current_round_matches if m['status'] == 'pending']
    
    if not pending_current:
        # 現在ラウンド完了、次ラウンド生成
        await generate_next_round(ctx, tournament)
    
    await ctx.send(embed=embed)

async def generate_next_round(ctx, tournament):
    """次ラウンド生成"""
    current_round = tournament['current_round']
    current_round_matches = [m for m in tournament['bracket'] if m['round'] == current_round]
    winners = [m['winner'] for m in current_round_matches if m['winner']]
    
    if len(winners) <= 1:
        # トーナメント終了
        if winners:
            champion = winners[0]
            embed = discord.Embed(
                title="🏆 トーナメント終了！",
                description=f"**優勝者: {champion['user'].display_name}**",
                color=0xffd700
            )
            
            embed.add_field(
                name="🎊 結果",
                value=f"🥇 優勝: {champion['user'].display_name}\n"
                      f"勝利数: {champion['wins']}勝",
                inline=False
            )
            
            tournament['status'] = 'ended'
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ トーナメント処理中にエラーが発生しました。")
        return
    
    # 次ラウンドの試合を生成
    next_round = current_round + 1
    match_id = max([m['id'] for m in tournament['bracket']]) + 1
    
    next_matches = []
    for i in range(0, len(winners), 2):
        player1 = winners[i]
        player2 = winners[i + 1] if i + 1 < len(winners) else None
        
        match_data = {
            'id': match_id,
            'round': next_round,
            'player1': player1,
            'player2': player2,
            'winner': None,
            'status': 'pending'
        }
        
        # BYE処理
        if player1 and not player2:
            match_data['winner'] = player1
            match_data['status'] = 'completed'
        
        next_matches.append(match_data)
        match_id += 1
    
    tournament['bracket'].extend(next_matches)
    tournament['current_round'] = next_round
    
    embed = discord.Embed(
        title="🔥 次ラウンド開始！",
        description=f"第{next_round}ラウンドが開始されました",
        color=0xff6b6b
    )
    
    match_list = []
    for match in next_matches:
        if match['status'] == 'pending':
            p1_name = match['player1']['user'].display_name if match['player1'] else "BYE"
            p2_name = match['player2']['user'].display_name if match['player2'] else "BYE"
            match_list.append(f"{p1_name} vs {p2_name}")
    
    embed.add_field(
        name=f"第{next_round}ラウンド 対戦カード",
        value="\n".join(match_list) if match_list else "全てBYE",
        inline=False
    )
    
    await ctx.send(embed=embed)

async def show_next_matches(ctx):
    """次の試合表示"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] != 'ongoing':
        await ctx.send("❌ 現在進行中のトーナメントがありません。")
        return
    
    current_round = tournament['current_round']
    pending_matches = [m for m in tournament['bracket'] 
                      if m['round'] == current_round and m['status'] == 'pending']
    
    if not pending_matches:
        await ctx.send("❌ 待機中の試合がありません。")
        return
    
    embed = discord.Embed(
        title="🎯 次の試合",
        description=f"第{current_round}ラウンド 待機中の試合",
        color=0xff6b6b
    )
    
    for match in pending_matches:
        p1_name = match['player1']['user'].display_name if match['player1'] else "BYE"
        p2_name = match['player2']['user'].display_name if match['player2'] else "BYE"
        
        embed.add_field(
            name=f"試合 #{match['id']}",
            value=f"{p1_name} vs {p2_name}",
            inline=True
        )
    
    embed.add_field(
        name="📝 結果入力",
        value="`!tournament result @勝者` で結果を入力してください",
        inline=False
    )
    
    await ctx.send(embed=embed)

async def end_tournament(ctx):
    """トーナメント終了"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    # 権限チェック
    if ctx.author.id != tournament['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ トーナメント作成者または管理者のみ終了できます。")
        return
    
    tournament['status'] = 'ended'
    
    embed = discord.Embed(
        title="🏁 トーナメント終了",
        description=f"**{tournament['tournament_type']}** トーナメントを終了しました",
        color=0xff6b6b
    )
    
    # 最終結果
    if tournament['status'] == 'ongoing':
        completed_matches = [m for m in tournament['bracket'] if m['status'] == 'completed']
        embed.add_field(
            name="📊 最終統計",
            value=f"完了試合数: {len(completed_matches)}\n"
                  f"参加者数: {len(tournament['participants'])}人",
            inline=False
        )
    
    await ctx.send(embed=embed)

async def add_to_tournament(ctx, args):
    """トーナメントにメンバーを手動追加"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_tournaments:
        await ctx.send("❌ アクティブなトーナメントがありません。")
        return
    
    tournament = active_tournaments[guild_id]
    
    if tournament['status'] != 'registration':
        await ctx.send("❌ 現在参加登録を受け付けていません。")
        return
    
    # 権限チェック（作成者または管理者のみ）
    if ctx.author.id != tournament['creator'].id and not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ トーナメント作成者または管理者のみメンバーを追加できます。")
        return
    
    # メンションされたユーザーを取得
    mentioned_users = ctx.message.mentions
    if not mentioned_users:
        await ctx.send("❌ 追加するユーザーをメンションしてください。例: `!tournament add @ユーザー1 @ユーザー2`")
        return
    
    added_users = []
    already_joined = []
    tournament_full = []
    
    for user in mentioned_users:
        user_id = user.id
        
        # 既に参加しているかチェック
        if user_id in [p['user_id'] for p in tournament['participants']]:
            already_joined.append(user.display_name)
            continue
        
        # 満員チェック
        if len(tournament['participants']) >= tournament['max_participants']:
            tournament_full.append(user.display_name)
            continue
        
        # 参加者として追加
        participant = {
            'user_id': user_id,
            'user': user,
            'joined_at': datetime.now(),
            'wins': 0,
            'losses': 0
        }
        
        tournament['participants'].append(participant)
        added_users.append(user.display_name)
    
    # 結果の報告
    embed = discord.Embed(
        title="👥 トーナメントメンバー追加結果",
        color=0x00ff88
    )
    
    if added_users:
        embed.add_field(
            name="✅ 追加成功",
            value="\n".join([f"• {name}" for name in added_users]),
            inline=False
        )
    
    if already_joined:
        embed.add_field(
            name="⚠️ 既に参加済み",
            value="\n".join([f"• {name}" for name in already_joined]),
            inline=False
        )
    
    if tournament_full:
        embed.add_field(
            name="❌ 満員のため追加不可",
            value="\n".join([f"• {name}" for name in tournament_full]),
            inline=False
        )
    
    current_count = len(tournament['participants'])
    max_participants = tournament['max_participants']
    
    embed.add_field(
        name="📊 現在の状況",
        value=f"**参加者数:** {current_count}/{max_participants}人\n"
              f"**開始可能:** {'はい' if current_count >= 4 else 'いいえ（最低4人必要）'}",
        inline=False
    )
    
    await ctx.send(embed=embed)
    
    # トーナメントメッセージを更新（ボタン付きメッセージがある場合）
    if 'message_id' in tournament:
        try:
            channel = ctx.channel
            message = await channel.fetch_message(tournament['message_id'])
            updated_embed = await create_tournament_embed(tournament, ctx.guild)
            view = TournamentView()
            await message.edit(embed=updated_embed, view=view)
            view.message = message
        except Exception as e:
            print(f"トーナメントメッセージ更新エラー: {e}")

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