# Tracker.gg API Key 取得方法

## ステップ1: Tracker Network APIサイトにアクセス
1. [Tracker Network API](https://tracker.gg/developers) を開く
2. 右上の **「Sign Up」** または **「Log In」** をクリック

## ステップ2: アカウント作成/ログイン
- 既存のアカウントがある場合: ログイン
- 新規の場合: メールアドレスで新規登録

## ステップ3: API Key申請
1. ログイン後、**「Create Application」** をクリック
2. アプリケーション情報を入力：
   - **Application Name**: `Discord Bot` 
   - **Description**: `VALORANT stats for Discord bot`
   - **Website**: `https://discord.com` (任意)

## ステップ4: API Key取得
1. アプリケーション作成後、**API Key** が表示される
2. このKeyをコピーして保存

## ステップ5: .envファイルに追加
```env
TRACKER_API_KEY=your_actual_api_key_here
```

## 注意事項
- API Keyは機密情報として管理
- 1日のリクエスト制限があります（通常は充分）
- VALORANTプレイヤーの統計情報を取得可能

## 使用可能なコマンド
- `!valorant RiotID#Tag` - プレイヤー統計
- `!valorant_match RiotID#Tag` - 直近の試合履歴 