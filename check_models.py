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

# 利用可能なモデルを一覧表示
print("利用可能なGeminiモデル:")
for model in genai.list_models():
    if 'generateContent' in model.supported_generation_methods:
        print(f"- {model.name}")
        print(f"  説明: {model.description}")
        print() 