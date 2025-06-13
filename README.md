# Discord Bot

このプロジェクトは、Python と discord.py を使用したシンプルなDiscord botです。

## 機能

- **!hello** - 挨拶をします
- **!ping** - Botの応答速度を確認します
- **!info** - サーバー情報を表示します
- **!dice [面数]** - サイコロを振ります（デフォルト: 6面）
- **!userinfo [@ユーザー]** - ユーザー情報を表示します
- **!help** - コマンド一覧を表示します

## セットアップ手順

### 1. 必要なパッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセス
2. 「New Application」をクリックしてアプリケーションを作成
3. 左側メニューから「Bot」を選択
4. 「Add Bot」をクリック
5. 「Token」をコピー（このトークンは秘密にしてください）

### 3. Bot の権限設定

1. 左側メニューから「OAuth2」→「URL Generator」を選択
2. 「Scopes」で「bot」を選択
3. 「Bot Permissions」で以下を選択：
   - Send Messages
   - Use Slash Commands
   - Read Message History
   - Add Reactions
   - Embed Links
4. 生成されたURLでBotをサーバーに招待

### 4. 環境変数の設定

1. `.env.example` を `.env` にコピー
2. `.env` ファイルを編集してボットトークンを設定：
   ```
   DISCORD_TOKEN=your_actual_bot_token_here
   ```

### 5. Bot の起動

```bash
python bot.py
```

## 重要な注意事項

- **ボットトークンは絶対に公開しないでください**
- `.env` ファイルは `.gitignore` に追加してください
- Botには適切な権限のみを付与してください

## カスタマイズ

`bot.py` ファイルを編集して、新しいコマンドや機能を追加できます。discord.py の詳細なドキュメントは [こちら](https://discordpy.readthedocs.io/) を参照してください。

## サポート

問題が発生した場合は、以下を確認してください：

1. Python 3.8以上がインストールされているか
2. 必要なパッケージがインストールされているか
3. ボットトークンが正しく設定されているか
4. BotがDiscordサーバーに正しく招待されているか 