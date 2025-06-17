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

# 会話履歴管理
conversation_history = {}  # チャンネルIDごとの会話履歴
MAX_HISTORY_LENGTH = 10   # 保存する会話数の上限

# Botの設定（メンバー情報取得対応）
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # メンバー情報取得に必要（Developer Portalで有効化済み前提）
# intents.presences = True  # ステータス情報取得に必要（要Developer Portal設定）
bot = commands.Bot(command_prefix='!', intents=intents)

# メンバー管理用のデータ構造
member_stats_dict = {}
welcome_messages_dict = {}
custom_commands_dict = {}
moderation_settings_dict = {}

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
    if any(keyword in message.content for keyword in team_keywords) and len(message.content) > 3:
        await handle_team_request(message)
        return
    
    # その他のメッセージ処理
    await bot.process_commands(message)

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
async def hello(ctx):
    """簡単な挨拶コマンド"""
    await ctx.send(f'こんにちは、{ctx.author.mention}さん！')

@bot.command(name='ping', help='Botの応答速度を確認します')
async def ping(ctx):
    """Pingコマンド - Botのレイテンシを表示"""
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! レイテンシ: {latency}ms')

@bot.command(name='commands', help='利用可能なコマンド一覧を表示')
async def show_commands(ctx):
    """利用可能なコマンドを表示"""
    embed = discord.Embed(title="🤖 リオンのコマンド一覧", color=0x00ff00)
    
    # チーム分けコマンド
    team_commands = [
        "!team - 自動チーム分け",
        "!team 2v1 - 2対1のチーム分け", 
        "!team 3v3 - 3対3のチーム分け",
        "!team 2v2 - 2対2のチーム分け",
        "!team 1v1 - 1対1のチーム分け",
        "!qt [形式] - クイックチーム分け",
        "!vc_team [形式] - VC内メンバーでチーム分け",
        "!vct [形式] - VC専用チーム分け（短縮版）"
    ]
    
    embed.add_field(
        name="🎯 チーム分けコマンド",
        value="\n".join(team_commands),
        inline=False
    )
    
    # 基本コマンド
    basic_commands = [
        "!ping - 応答速度確認",
        "!info - サーバー情報",
        "!members - メンバー統計",
        "!channels - チャンネル情報",
        "!commands - このコマンド一覧"
    ]
    
    embed.add_field(
        name="📝 基本コマンド",
        value="\n".join(basic_commands),
        inline=False
    )
    
    # AIコマンド
    ai_commands = [
        "!ai [質問] - AI会話",
        "!expert [質問] - 専門的な回答",
        "!creative [プロンプト] - 創作的な回答",
        "!translate [テキスト] - 翻訳",
        "!summarize [テキスト] - 要約"
    ]
    
    embed.add_field(
        name="🧠 AIコマンド",
        value="\n".join(ai_commands),
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
    embed.set_footer(text=f"登録済みコマンド数: {command_count}個")
    
    await ctx.send(embed=embed)

@bot.command(name='info', help='詳細なサーバー情報を表示します')
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
async def roll_dice(ctx, sides: int = 6):
    """サイコロを振るコマンド"""
    import random
    
    if sides < 2:
        await ctx.send("サイコロの面数は2以上である必要があります。")
        return
    
    result = random.randint(1, sides)
    await ctx.send(f'🎲 {sides}面サイコロの結果: **{result}**')

@bot.command(name='userinfo', help='ユーザー情報を表示します')
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
async def clear_history(ctx):
    """会話履歴をクリア"""
    channel_id = ctx.channel.id
    
    if channel_id in conversation_history:
        conversation_history[channel_id] = []
        await ctx.send("🗑️ このチャンネルの会話履歴をクリアしました。")
    else:
        await ctx.send("📝 このチャンネルには会話履歴がありません。")

@bot.command(name='usage', help='AI使用量と制限情報を表示します')
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
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                elif response.status == 404:
                    return None, "プレイヤーが見つかりません。Riot ID#Tagを確認してください。"
                else:
                    return None, f"API エラー: {response.status}"
    except Exception as e:
        return None, f"接続エラー: {str(e)}"

@bot.command(name='valorant', help='VALORANT統計を表示します（例: !valorant PlayerName#1234）')
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
            
            async with aiohttp.ClientSession() as session:
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
async def team_divide(ctx, format_type=None):
    """チーム分け機能"""
    try:
        # 実行中チェック
        if ctx.author.id in command_executing and command_executing[ctx.author.id] == 'team':
            await ctx.send("⚠️ チーム分けコマンドが既に実行中です。少しお待ちください。")
            return
        
        # 実行中フラグを設定
        command_executing[ctx.author.id] = 'team'
        
        # レート制限チェック
        allowed, wait_time = check_rate_limit(ctx.author.id)
        if not allowed:
            command_executing.pop(ctx.author.id, None)  # フラグをクリア
            await ctx.send(f"⏰ 少し待ってください。あと{wait_time:.1f}秒後に再度お試しください。")
            return
        
        # リクエスト時刻を記録
        user_last_request[ctx.author.id] = datetime.now()
        
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
            
            else:
                await ctx.send("❌ 対応していない形式です。使用可能: `2v1`, `3v3`, `2v2`, `1v1`")
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
    finally:
        # 実行中フラグをクリア
        command_executing.pop(ctx.author.id, None)

@bot.command(name='quick_team', aliases=['qt'], help='簡単チーム分け（例: !qt, !quick_team 2v1）')
async def quick_team(ctx, format_type=None):
    """簡単チーム分け（エイリアス）"""
    await team_divide(ctx, format_type)

@bot.command(name='vc_team', aliases=['vct'], help='VC内メンバーでチーム分けします（例: !vc_team, !vc_team 2v2）')
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
            
            else:
                await ctx.send("❌ 対応していない形式です。使用可能: `2v1`, `3v3`, `2v2`, `1v1`")
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

@bot.event
async def on_command_error(ctx, error):
    """エラーハンドリング"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("そのコマンドは存在しません。`!help`でコマンド一覧を確認してください。")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"必要な引数が不足しています。`!help {ctx.command}`で使い方を確認してください。")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("引数の形式が正しくありません。")
    else:
        print(f"エラーが発生しました: {error}")
        await ctx.send("予期しないエラーが発生しました。")

@bot.command(name='mystats', help='メンバーの統計情報を表示します')
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
        "type": "標準",
        "sites": "A・B",
        "description": "イタリア・ヴェネツィアをモチーフにした標準的なマップ",
        "emoji": "🏛️"
    },
    "Bind": {
        "name": "バインド",
        "type": "標準",
        "sites": "A・B",
        "description": "モロッコをモチーフにしたテレポーター付きマップ",
        "emoji": "🕌"
    },
    "Haven": {
        "name": "ヘイヴン",
        "type": "3サイト",
        "sites": "A・B・C",
        "description": "ブータンをモチーフにした3サイトマップ",
        "emoji": "🏔️"
    },
    "Split": {
        "name": "スプリット",
        "type": "標準",
        "sites": "A・B",
        "description": "日本・東京をモチーフにした縦長マップ",
        "emoji": "🏙️"
    },
    "Icebox": {
        "name": "アイスボックス",
        "type": "標準",
        "sites": "A・B",
        "description": "ロシア・シベリアをモチーフにした寒冷地マップ",
        "emoji": "🧊"
    },
    "Breeze": {
        "name": "ブリーズ",
        "type": "標準",
        "sites": "A・B",
        "description": "カリブ海の島をモチーフにした開放的なマップ",
        "emoji": "🏝️"
    },
    "Fracture": {
        "name": "フラクチャー",
        "type": "特殊",
        "sites": "A・B",
        "description": "アメリカをモチーフにした特殊構造マップ",
        "emoji": "⚡"
    },
    "Pearl": {
        "name": "パール",
        "type": "標準",
        "sites": "A・B",
        "description": "ポルトガル・リスボンをモチーフにした水中都市マップ",
        "emoji": "🐚"
    },
    "Lotus": {
        "name": "ロータス",
        "type": "3サイト",
        "sites": "A・B・C",
        "description": "インドをモチーフにした3サイトマップ",
        "emoji": "🪷"
    },
    "Sunset": {
        "name": "サンセット",
        "type": "標準",
        "sites": "A・B",
        "description": "アメリカ・ロサンゼルスをモチーフにしたマップ",
        "emoji": "🌅"
    },
    "Abyss": {
        "name": "アビス",
        "type": "標準",
        "sites": "A・B",
        "description": "OMEGA EARTHの実験施設をモチーフにしたマップ",
        "emoji": "🕳️"
    }
}

@bot.command(name='map', aliases=['マップ', 'valmap'], help='VALORANTのマップをランダムに選択します')
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
            
            embed.add_field(name="🗺️ マップタイプ", value=map_info['type'], inline=True)
            embed.add_field(name="📍 サイト", value=map_info['sites'], inline=True)
            embed.add_field(name="ℹ️ 説明", value=map_info['description'], inline=False)
            
            # マップ画像のURL（実際のゲーム画像は著作権の関係で使用しない）
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
async def valorant_map_list(ctx):
    """VALORANTマップ一覧表示"""
    try:
        embed = discord.Embed(
            title="🗺️ VALORANT マップ一覧",
            description="現在のマッププール",
            color=0xff4655
        )
        
        # マップタイプ別に分類
        standard_maps = []
        three_site_maps = []
        special_maps = []
        
        for map_key, map_info in VALORANT_MAPS.items():
            map_text = f"{map_info['emoji']} **{map_key}** ({map_info['name']})"
            
            if map_info['type'] == "標準":
                standard_maps.append(map_text)
            elif map_info['type'] == "3サイト":
                three_site_maps.append(map_text)
            else:
                special_maps.append(map_text)
        
        if standard_maps:
            embed.add_field(
                name="🏛️ 標準マップ (A・Bサイト)",
                value="\n".join(standard_maps),
                inline=False
            )
        
        if three_site_maps:
            embed.add_field(
                name="🔺 3サイトマップ (A・B・Cサイト)",
                value="\n".join(three_site_maps),
                inline=False
            )
        
        if special_maps:
            embed.add_field(
                name="⚡ 特殊マップ",
                value="\n".join(special_maps),
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
        
        embed.add_field(name="🗺️ マップタイプ", value=map_info['type'], inline=True)
        embed.add_field(name="📍 サイト構成", value=map_info['sites'], inline=True)
        embed.add_field(name="🎯 特徴", value=map_info['description'], inline=False)
        
        embed.set_footer(text="!map でランダム選択 | !maplist で全マップ一覧")
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        print(f"マップ情報エラー: {e}")
        await ctx.send("❌ マップ情報の表示でエラーが発生しました。")

# Botを起動
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("エラー: DISCORD_TOKENが設定されていません。")
        print(".envファイルを作成し、ボットトークンを設定してください。")
    else:
        try:
            bot.run(token)
        except discord.LoginFailure:
            print("エラー: 無効なボットトークンです。")
        except Exception as e:
            import traceback
            print(f"エラーが発生しました: {e}")
            traceback.print_exc() 