#!/bin/bash
# Discord Bot Start Script for Render.com
# エラー時に停止するように設定
set -e

echo "🚀 Discord Bot Starting..."
echo "⏰ Start Time: $(date)"
echo "📂 Working Directory: $(pwd)"
echo "🐍 Python Version: $(python3 --version)"

# 環境変数の確認
echo "🔧 Checking Environment Variables..."
if [ -z "$DISCORD_TOKEN" ]; then
    echo "❌ ERROR: DISCORD_TOKEN is not set"
    exit 1
fi

if [ -z "$GEMINI_API_KEY" ]; then
    echo "⚠️ WARNING: GEMINI_API_KEY is not set"
fi

echo "✅ Environment check completed"

# Pythonパッケージの確認
echo "📦 Checking required packages..."
python3 -c "import discord; print('✅ discord.py installed')" || exit 1
python3 -c "import google.generativeai; print('✅ google-generativeai installed')" || exit 1

echo "🎯 All dependencies verified"
echo "🤖 Starting Discord Bot..."

# Botを起動（ログ出力を有効化）
export PYTHONUNBUFFERED=1
python3 bot.py 