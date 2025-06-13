# Discord Bot 招待ガイド

## 🤖 リオンボット サーバー招待手順

### ステップ1: Discord Developer Portal にアクセス
1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. あなたのアプリケーション（ボット）を選択

### ステップ2: 必要なIntentsを有効化
1. 左メニューの「**Bot**」をクリック
2. 「**Privileged Gateway Intents**」セクションで以下を有効化：
   - ✅ **SERVER MEMBERS INTENT** ← 必須！メンバー情報取得に必要
   - ✅ **MESSAGE CONTENT INTENT** ← 必須！メッセージ読み取りに必要

### ステップ3: 招待URL生成
1. 左メニューの「**OAuth2**」→「**URL Generator**」をクリック
2. **Scopes** で以下を選択：
   - ✅ `bot`
   - ✅ `applications.commands`

3. **Bot Permissions** で以下を選択：

#### 📋 必須権限リスト
```
General Permissions:
✅ View Channels

Text Permissions:
✅ Send Messages
✅ Send Messages in Threads  
✅ Create Public Threads
✅ Create Private Threads
✅ Embed Links
✅ Attach Files
✅ Read Message History
✅ Mention @everyone, @here, and All Roles
✅ Use External Emojis
✅ Use External Stickers
✅ Add Reactions
✅ Use Slash Commands

Voice Permissions:
✅ Connect
✅ Speak
```

### ステップ4: 招待URLをコピー
- 下部に表示される**Generated URL**をコピー
- URLは以下のような形式になります：
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_BOT_ID&permissions=2147871744&scope=bot%20applications.commands
```

### ステップ5: サーバーに招待
1. コピーしたURLをブラウザで開く
2. 招待先のサーバーを選択
3. 権限を確認して「**認証**」をクリック
4. 「**私はロボットではありません**」をチェック

### ステップ6: 動作確認
招待完了後、以下のコマンドでテスト：
```
!ping
!hello  
!commands
!info
```

## ❌ よくあるエラーと解決法

### エラー1: "Bot missing permissions"
**解決法**: 招待URLを再生成し、全ての必要権限をチェックしてから招待

### エラー2: "Missing Access"  
**解決法**: サーバーの管理者権限を持つアカウントで招待を実行

### エラー3: メンバー情報が取得できない
**解決法**: Developer Portal で SERVER MEMBERS INTENT を有効化し、ボットを再起動

### エラー4: "Application not found"
**解決法**: 
- Bot IDが正しいか確認
- Developer Portal で Application ID をチェック
- 新しく招待URLを生成

## 🔧 権限の詳細説明

このボットが使用する主な機能と必要権限：

| 機能 | 必要権限 | 理由 |
|------|----------|------|
| メッセージ送信 | Send Messages | コマンド応答 |
| 埋め込み送信 | Embed Links | 統計表示、情報表示 |
| リアクション | Add Reactions | 処理状況表示 |
| メンバー情報 | View Members | チーム分け、統計 |
| 履歴読み取り | Read Message History | 会話履歴機能 |
| メンション | Mention Everyone | 緊急通知機能 |

## 📞 サポート

問題が解決しない場合：
1. ボットのログを確認（`python bot.py`実行時の出力）
2. 権限設定を再確認
3. 招待URLを完全に新しく生成し直す
4. サーバー管理者に招待を依頼

## ⚠️ 重要な注意事項
- **SERVER MEMBERS INTENT** は必ず有効化してください
- サーバー管理者権限が必要な場合があります
- ボットトークンは絶対に公開しないでください 