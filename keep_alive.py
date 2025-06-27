import asyncio
import aiohttp
import os
from datetime import datetime

async def keep_alive():
    """Render.comでのスリープを防ぐためのkeep-alive機能"""
    url = "https://discord-bot-uk5s.onrender.com"  # あなたのRender URL
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    print(f"Keep-alive ping: {response.status} at {datetime.now()}")
        except Exception as e:
            print(f"Keep-alive error: {e}")
        
        # 25分ごとに実行（スリープの30分より短く）
        await asyncio.sleep(1500)

if __name__ == "__main__":
    asyncio.run(keep_alive()) 