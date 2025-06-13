import os
from dotenv import load_dotenv

# 環境変数を読み込み
load_dotenv()

# 設定確認
discord_token = os.getenv('DISCORD_TOKEN')
gemini_key = os.getenv('GEMINI_API_KEY')

print("=== 環境変数設定確認 ===")
print(f"DISCORD_TOKEN: {'✅ 設定済み' if discord_token else '❌ 未設定'}")
print(f"GEMINI_API_KEY: {'✅ 設定済み' if gemini_key else '❌ 未設定'}")
print()

if not discord_token:
    print("❌ DISCORD_TOKENが設定されていません。")
if not gemini_key:
    print("❌ GEMINI_API_KEYが設定されていません。")
    
if discord_token and gemini_key:
    print("✅ すべての環境変数が正しく設定されています！") 