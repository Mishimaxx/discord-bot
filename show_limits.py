import google.generativeai as genai
import os
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# Gemini APIキーを設定
api_key = os.getenv('GEMINI_API_KEY')
if not api_key:
    print("GEMINI_API_KEYが設定されていません。")
    exit()

genai.configure(api_key=api_key)

print("🔍 Gemini API 制限情報")
print("=" * 50)

print("\n📈 gemini-1.5-flash (現在使用中)")
print("✅ 15 RPM (リクエスト/分)")
print("✅ 1,500 RPD (リクエスト/日)")
print("✅ 1,000,000 TPM (トークン/分)")
print("✅ 制限が緩い、無料プランに最適")

print("\n📊 gemini-1.5-pro (高性能だが制限厳しい)")
print("❌ 2 RPM (リクエスト/分)")
print("❌ 50 RPD (リクエスト/日)")
print("❌ 32,000 TPM (トークン/分)")
print("❌ 制限が厳しい、すぐに上限に到達")

print("\n🛡️ 実装している制限対策")
print("• 1ユーザーあたり10秒間隔のレート制限")
print("• 軽量モデル (gemini-1.5-flash) の使用")
print("• エラーハンドリングと待機メッセージ")
print("• 使用量チェック機能 (!usage)")

print("\n💡 推奨使用方法")
print("• 短時間で連続使用を避ける")
print("• 質問は簡潔にまとめる")
print("• 長文は分割して送信")
print("• !usage コマンドで状況確認")

print("\n🚀 制限を回避する方法")
print("1. Google Cloud Platform (有料) でクレジット使用")
print("2. 複数のAPIキーを用意して分散")
print("3. より軽量なモデルを使用 (現在適用済み)")
print("4. 使用頻度を制限 (現在適用済み)")

# 現在利用可能なモデルを表示
print("\n🔧 利用可能なモデル:")
try:
    for model in genai.list_models():
        if 'generateContent' in model.supported_generation_methods:
            print(f"• {model.name.split('/')[-1]}")
except Exception as e:
    print(f"モデル一覧取得エラー: {e}")

print("\n" + "=" * 50)
print("💬 Discord bot での実際の制限")
print("• AIコマンド: 10秒間隔")
print("• 1日の推奨使用回数: 100-200回程度")
print("• 同時使用ユーザー数: 複数人でも問題なし") 