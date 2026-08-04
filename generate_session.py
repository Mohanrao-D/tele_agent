import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

async def main():
    if API_ID == 0 or not API_HASH:
        print("Please configure API_ID and API_HASH in your .env file first.")
        return
        
    print("Connecting to Telegram...")
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_str = client.session.save()
        print("\n" + "="*50)
        print("YOUR SESSION STRING (Copy everything below):")
        print("="*50)
        print(session_str)
        print("="*50 + "\n")
        print("Add this string to your .env file as: TELEGRAM_SESSION_STRING=your_string_here")

if __name__ == '__main__':
    asyncio.run(main())
