import os
from dotenv import load_dotenv
import aiohttp
import asyncio

# 環境変数を読み込み
load_dotenv()

async def check_tracker_api():
    api_key = os.getenv('TRACKER_API_KEY')
    
    print("🔍 Tracker.gg API 設定確認")
    print("=" * 50)
    
    if not api_key:
        print("❌ TRACKER_API_KEYが設定されていません。")
        print("\n📋 設定方法:")
        print("1. .envファイルに以下を追加:")
        print("   TRACKER_API_KEY=your_actual_api_key")
        print("2. tracker_api_setup.md を参照してAPI Key取得")
        return
    
    print(f"✅ TRACKER_API_KEY: 設定済み (長さ: {len(api_key)}文字)")
    
    # API接続テスト
    print("\n🔗 API接続テスト中...")
    
    headers = {
        "TRN-Api-Key": api_key,
        "User-Agent": "Discord Bot Test"
    }
    
    # テスト用エンドポイント（プレイヤー検索ではなくAPI状態確認）
    test_url = "https://api.tracker.gg/api/v2/valorant/standard/profile/riot/TenZ%23000"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, headers=headers) as response:
                print(f"📡 APIレスポンス: {response.status}")
                
                if response.status == 200:
                    print("✅ API接続成功！")
                elif response.status == 403:
                    print("❌ 403エラー: API Key認証失敗")
                    print("\n🛠️ 解決方法:")
                    print("1. API Keyが正しいか確認")
                    print("2. Tracker.gg でアプリケーションが有効か確認")
                    print("3. API Key を再生成")
                elif response.status == 404:
                    print("⚠️ 404エラー: テストプレイヤーが見つからない（API Keyは有効）")
                elif response.status == 429:
                    print("⚠️ 429エラー: レート制限に達しています")
                else:
                    print(f"⚠️ 予期しないエラー: {response.status}")
                
                # レスポンス内容を表示（エラーの場合）
                if response.status != 200:
                    try:
                        error_data = await response.text()
                        print(f"\n📄 エラー詳細:\n{error_data[:500]}")
                    except:
                        print("エラー詳細を取得できませんでした。")
                        
    except Exception as e:
        print(f"❌ 接続エラー: {e}")
    
    print("\n" + "=" * 50)
    print("💡 ヒント:")
    print("• API Keyは https://tracker.gg/developers で取得")
    print("• アプリケーション作成時に正しい情報を入力")
    print("• API Keyをコピー時に余分な文字が含まれていないか確認")

if __name__ == "__main__":
    asyncio.run(check_tracker_api()) 