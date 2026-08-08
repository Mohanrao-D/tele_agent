import asyncio
import logging
import os
import threading
import urllib.request

from flask import Flask
from telethon import TelegramClient, events, types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.sessions import StringSession
from telethon.errors import MessageIdInvalidError

import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask Web Server to keep Render active
app = Flask('')

@app.route('/')
def home():
    return "Telegram Userbot is running!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# Client is created lazily inside the running event loop (required by Telethon on Python 3.14)
client = None

# In-memory dedup so edited/re-delivered messages are never processed twice
RECENT_IDS = set()
RECENT_DOC_IDS = set()

def mark_processed(key):
    if key in RECENT_IDS:
        return True
    RECENT_IDS.add(key)
    if len(RECENT_IDS) > 1000:
        RECENT_IDS.clear()
    return False

def mark_doc_processed(doc_id):
    if doc_id in RECENT_DOC_IDS:
        return True
    RECENT_DOC_IDS.add(doc_id)
    if len(RECENT_DOC_IDS) > 1000:
        RECENT_DOC_IDS.clear()
    return False

SOURCE_ENTITY_ID = None


async def visit_url(url):
    try:
        await asyncio.to_thread(urllib.request.urlopen, url, timeout=5)
        logger.info(f"Visited: {url}")
    except Exception as e:
        logger.warning(f"Failed to open URL {url}: {e}")


async def forward_media_to_friends(message):
    """Forward a bot media message with fresh re-fetch + retries to survive stale references."""
    doc = getattr(message.media, 'document', None)
    doc_id = getattr(doc, 'id', None)
    if doc_id is not None and mark_doc_processed(doc_id):
        logger.info(f"Document {doc_id} already forwarded; skipping duplicate.")
        return

    chat_id = message.chat_id
    msg_id = message.id

    # Give the bot a moment to finish registering the file before forwarding
    await asyncio.sleep(1.5)

    for friend in config.TARGET_FRIENDS:
        for attempt in range(4):
            try:
                fresh = await client.get_messages(chat_id, ids=msg_id)
                if fresh is None:
                    logger.info(f"Message {msg_id} is no longer available; cannot forward to {friend}.")
                    return
                await client.forward_messages(friend, fresh)
                logger.info(f"Forwarded to {friend}")
                break
            except Exception as e:
                logger.warning(f"Forward to {friend} failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(3)
        # Stagger forwards to avoid Telegram flood waits
        await asyncio.sleep(2)


async def click_button_safely(message, button_text=None, button_data=None):
    """Re-fetch the message and click a button, retrying and ignoring stale/invalid references."""
    chat_id = message.chat_id
    msg_id = message.id
    for attempt in range(3):
        try:
            fresh = await client.get_messages(chat_id, ids=msg_id)
            if fresh is None or not fresh.reply_markup:
                logger.info("Button message was edited/removed; skipping click.")
                return
            target = None
            for row in fresh.reply_markup.rows:
                for b in row.buttons:
                    if button_data is not None and isinstance(b, types.KeyboardButtonCallback) and b.data == button_data:
                        target = b
                    elif button_text is not None and b.text == button_text:
                        target = b
            if target is None:
                logger.info("Button no longer present on the message; skipping click.")
                return
            if isinstance(target, types.KeyboardButtonCallback):
                await fresh.click(data=target.data)
            else:
                await fresh.click(text=target.text)
            logger.info(f"Clicked button '{getattr(target, 'text', 'callback')}'")
            return
        except MessageIdInvalidError:
            logger.info("Message ID became invalid before the click; ignoring.")
            return
        except Exception as e:
            logger.warning(f"Button click attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(2)


async def process_message(message):
    """Core logic to process an episode message and click the 1080p button."""
    if mark_processed(('post', message.chat_id, message.id)):
        logger.info(f"Skipping already-processed post {message.id}")
        return
    logger.info(f"Processing message: {message.text[:50] if message.text else '[No Text]'}...")
    if message.reply_markup:
        for row in message.reply_markup.rows:
            for button in row.buttons:
                if '1080' in button.text:
                    logger.info(f"Found 1080p button: '{button.text}'")
                    if isinstance(button, types.KeyboardButtonUrl) and button.url:
                        logger.info(f"Button has URL: {button.url}")
                        if 'start=' in button.url:
                            start_param = button.url.split('start=')[1]
                            bot_username = config.BOT_USERNAME
                            if 't.me/' in button.url or 'telegram.me/' in button.url:
                                url_path = button.url.split('?')[0]
                                bot_part = url_path.split('/')[-1]
                                if bot_part:
                                    bot_username = f"@{bot_part}"
                            logger.info(f"Deep link detected. Sending /start {start_param} to {bot_username}...")
                            await client.send_message(bot_username, f"/start {start_param}")
                        else:
                            logger.info("Non-deep-link URL button. Attempting to click...")
                            await click_button_safely(message, button_text=button.text)
                    else:
                        logger.info("Direct callback button. Clicking...")
                        await click_button_safely(
                            message,
                            button_text=button.text,
                            button_data=button.data if isinstance(button, types.KeyboardButtonCallback) else None,
                        )
                    return


async def handler_bot_messages(event):
    """Listens for messages from the bot to handle joining channels or forwarding files."""
    message = event.message
    logger.info(f"Received message from bot: '{message.text[:100] if message.text else '[No Text]'}'")

    # Forward media (video/document) to friends
    if message.media and (hasattr(message.media, 'document') or hasattr(message.media, 'video')):
        logger.info("Received media file from bot. Forwarding to friends...")
        await forward_media_to_friends(message)
        return

    if message.reply_markup:
        buttons = [b for row in message.reply_markup.rows for b in row.buttons]
        logger.info(f"Message has buttons: {[b.text for b in buttons]}")
        join_urls = [b for b in buttons if isinstance(b, types.KeyboardButtonUrl) and ('t.me/' in b.url or 'telegram.me/' in b.url)]
        ad_urls = [b for b in buttons if isinstance(b, types.KeyboardButtonUrl) and not ('t.me/' in b.url or 'telegram.me/' in b.url)]
        check_button = next((b for b in buttons if isinstance(b, types.KeyboardButtonCallback)), None)

        if join_urls:
            logger.info("Bot requires joining channels.")
            for button in join_urls:
                url = button.url
                if 't.me/' in url:
                    channel_username = url.split('t.me/')[1].split('/')[0]
                    if '+' in channel_username or 'joinchat' in channel_username:
                        logger.info(f"Join via invite link: {url}")
                        hash_part = url.split('/')[-1].lstrip('+')
                        try:
                            await client(ImportChatInviteRequest(hash_part))
                        except Exception as e:
                            logger.error(f"Failed to join via invite link: {e}")
                    else:
                        logger.info(f"Joining channel: {channel_username}")
                        try:
                            await client(JoinChannelRequest(channel_username))
                        except Exception as e:
                            logger.error(f"Failed to join channel: {e}")
            if check_button:
                logger.info("Clicking the check/joined button...")
                await click_button_safely(message, button_data=check_button.data)

        elif ad_urls:
            logger.info("Bot requires watching ads/clicking links.")
            for button in ad_urls:
                logger.info(f"Simulating ad click by visiting: {button.url}")
                await visit_url(button.url)

            # Wait 7 seconds to satisfy the 5-second timer limit on the bot
            logger.info("Waiting 7 seconds to bypass the ad timer...")
            await asyncio.sleep(7)

            if check_button:
                logger.info(f"Clicking verification button: '{check_button.text}'")
                await click_button_safely(message, button_data=check_button.data)


async def handler_global_messages(event):
    """Global handler that filters source-channel posts and bot messages, never crashing the loop."""
    try:
        # Check if the message is from our source channel
        # We compare both the raw event.chat_id and the cleaned ID (removing -100 prefix if present)
        event_raw_id = abs(event.chat_id)
        clean_chat_id = event_raw_id
        if str(event_raw_id).startswith('100'):
            try:
                clean_chat_id = int(str(event_raw_id)[3:])
            except Exception:
                pass

        if SOURCE_ENTITY_ID and (event.chat_id == SOURCE_ENTITY_ID or clean_chat_id == SOURCE_ENTITY_ID):
            logger.info("New message received in source channel.")
            await process_message(event.message)
            return

        # If not the source channel, check if it's from the bot
        sender = await event.get_sender()
        if not sender:
            return
        username = (getattr(sender, 'username', '') or '').lower()

        if username == 'markscans_bot':
            logger.info("Intercepted message from @Markscans_bot globally.")
            await handler_bot_messages(event)
    except Exception as e:
        logger.exception(f"Unhandled error in global handler: {e}")


async def main():
    global SOURCE_ENTITY_ID
    logger.info("Starting Telegram Userbot...")
    await client.start()
    logger.info("Userbot is logged in.")

    # Check and process the latest message in the channel on startup
    try:
        logger.info(f"Checking the recent messages in '{config.SOURCE_CHANNEL}' to find the latest episode...")
        entity = None
        async for dialog in client.iter_dialogs():
            if dialog.name == config.SOURCE_CHANNEL:
                entity = dialog.entity
                break

        if not entity:
            entity = await client.get_input_entity(config.SOURCE_CHANNEL)

        SOURCE_ENTITY_ID = getattr(entity, 'id', None)
        logger.info(f"Source channel resolved with id={SOURCE_ENTITY_ID}")

        async for message in client.iter_messages(entity, limit=10):
            has_1080 = False
            if message.reply_markup:
                for row in message.reply_markup.rows:
                    for button in row.buttons:
                        if '1080' in button.text:
                            has_1080 = True
                            break
            if has_1080:
                logger.info("Found the latest episode post in history. Processing...")
                await process_message(message)
                break
    except Exception as e:
        logger.error(f"Failed to fetch/process the latest message on startup: {e}")

    logger.info("Userbot is running and listening for new episodes!")
    await client.run_until_disconnected()


async def setup_client():
    """Create the TelegramClient inside the running event loop and register handlers."""
    global client
    if config.TELEGRAM_SESSION_STRING:
        logger.info("Using StringSession for Telegram Client...")
        _session = StringSession(config.TELEGRAM_SESSION_STRING)
    else:
        logger.info("Using local session file for Telegram Client...")
        _session = 'userbot_session'

    client = TelegramClient(
        _session,
        config.API_ID,
        config.API_HASH,
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
    )
    client.add_event_handler(handler_global_messages, events.NewMessage())
    client.add_event_handler(handler_global_messages, events.MessageEdited())
    return client


async def run_forever():
    """Run the bot forever, restarting gracefully if the Telegram connection is lost."""
    await setup_client()
    while True:
        try:
            await main()
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f"Telegram connection failed: {e}. Retrying in 30 seconds...")
        except Exception as e:
            logger.exception(f"Unexpected error: {e}. Restarting in 30 seconds...")
        await asyncio.sleep(30)


if __name__ == '__main__':
    if config.API_ID == 0 or not config.API_HASH:
        logger.error("Please set API_ID and API_HASH in your .env file")
    else:
        logger.info("Starting Flask web server on background thread...")
        threading.Thread(target=run_web, daemon=True).start()
        try:
            asyncio.run(run_forever())
        except KeyboardInterrupt:
            logger.info("Shutting down...")
