from telegram import constants
import re

def escape_markdown_v2(text):
    """Escapes problematic characters in MarkdownV2 while keeping readability."""
    if not isinstance(text, str):
        return text  # Return as-is if it's not a string

    # Escape all special characters required by MarkdownV2
    text = re.sub(r"([\\*\[\]\(\)~`>#+=|{}!\-])", r"\\\1", text)  # Added `-` here

    # Escape underscores if they are not part of a bold (`__bold__`) or italic (`_italic_`) sequence
    text = re.sub(r"(?<!_)_(?!_)", r"\\_", text)

    # Escape all dots (.) to prevent Telegram formatting issues
    text = re.sub(r"\.", r"\\.", text)

    return text

async def send_telegram_message(app, text, markdown=False):
    """Sends a message to all allowed users."""
    from config import ALLOWED_USERS  # Assuming ALLOWED_USERS is defined in config.py

    if app is None:
        print("⚠️ Telegram app not initialized. Cannot send message.")
        return

    for user_id in ALLOWED_USERS:
        try:
            if markdown:
                escaped_text = escape_markdown_v2(text)
                print(f"Sending to {user_id}:")
                print(f"  Original Text:\n{text}")
                print(f"  Escaped Text:\n{escaped_text}")
                await app.bot.send_message(chat_id=user_id, text=escaped_text, parse_mode=constants.ParseMode.MARKDOWN_V2)
                print(f"  Message sent successfully to {user_id}.")
            else:
                print(f"Sending to {user_id}:")
                print(f"  Original Text:\n{text}")
                await app.bot.send_message(chat_id=user_id, text=text)
                print(f"  Message sent successfully to {user_id}.")
        except Exception as e:
            print(f"⚠️ Failed to send message to {user_id}: {e}")
            print(f"  Text that caused error:\n{text}")  # Print the text that caused the error