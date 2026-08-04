import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "")

SOURCE_CHANNEL = "BLEACH | Bleach Thousand Year Blood War"
BOT_USERNAME = "@Markscans_bot"
TARGET_FRIENDS = ["@Jashuu", "@g_i_r_i_d_h_a_r_9","@prem3156", "me"] # 'me' sends to Saved Messages
