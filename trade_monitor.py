import asyncio
import json
import traceback
import base58
import base64
from datetime import datetime
import itertools
from spl.token.instructions import transfer, TransferParams
import aiohttp
from telegram_utils import escape_markdown_v2, send_telegram_message  # No placeholder
import websockets

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.rpc.requests import GetBalance, GetTransaction, GetTokenSupply
from solders.message import Message
from solana.rpc.core import RPCException
from solders.transaction import VersionedTransaction
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer, create_account, CreateAccountParams
from solana.rpc.commitment import Confirmed
import struct
import aiofiles  # For asynchronous file I/O
from typing import Dict, List, Tuple
from httpx import HTTPStatusError, ConnectError, ReadTimeout
from solders.hash import Hash
from solders.instruction import Instruction, AccountMeta
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token._layouts import MINT_LAYOUT
import httpx
from config import RPC_ENDPOINT,HELIUS_API_KEY,TRADE_HISTORY,TRADE_PERCENTAGE,TRADE_WALLETS,TICK_ARRAY_SEED,TELEGRAM_BOT_TOKEN,COPY_WALLET_ADDRESS,WALLETS,MONITORING

# --- Configuration ---
class Config:  # Using a class for config
    RPC_ENDPOINT = RPC_ENDPOINT  # Or your preferred RPC
    HELIUS_API_KEY = HELIUS_API_KEY
    TRADE_WALLETS = TRADE_WALLETS  # Not really used.
    COPY_WALLET_ADDRESS = COPY_WALLET_ADDRESS  # The wallet to copy.
    TRADE_PERCENTAGE = TRADE_PERCENTAGE
    WALLETS = WALLETS  # Your bot's wallet(s) - Base58 encoded.

config = Config()

RPC_ENDPOINT = config.RPC_ENDPOINT
TRADE_WALLETS = config.TRADE_WALLETS
COPY_WALLET_ADDRESS = config.COPY_WALLET_ADDRESS
TRADE_PERCENTAGE = config.TRADE_PERCENTAGE
WALLETS = config.WALLETS
HELIUS_API_KEY = config.HELIUS_API_KEY

# --- Constants ---
PROCESSED_TRANSACTIONS_FILE = "processed_transactions.json"
MINIMUM_TRADE_AMOUNT = 0.0000000000000000000000000000000001  # SOL.  Make this configurable!
TELEGRAM_SEND_DELAY = 1  # seconds

# --- DEX Program IDs ---
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_CLMM_PROGRAM_ID = Pubkey.from_string("HjNfZaGbAoHqgjYeSnuEDpQ91yR6MqrAW9yhCaw1Lvau")
JUPITER_V6_PROGRAM_ID = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
POSITION_SEED = "position"
TICK_ARRAY_SEED = "tick_array"

# --- Global Variables ---
PROCESSED_TRANSACTIONS = set()
BOT_POSITIONS: Dict[str, List[str]] = {}
BOT_POSITIONS_FILE = "bot_positions.json"


monitor_task = None
helius_task = None
processed_tx_hashes = set()
_monitoring = False  # Use a private variable

# --- Utility Functions ---
async def load_processed_transactions():
    global PROCESSED_TRANSACTIONS
    try:
        async with aiofiles.open(PROCESSED_TRANSACTIONS_FILE, "r") as f:
            PROCESSED_TRANSACTIONS = set(json.loads(await f.read()))
    except FileNotFoundError:
        PROCESSED_TRANSACTIONS = set()
    except json.JSONDecodeError:
        print("Error decoding processed_transactions.json.  Starting with an empty set.")
        PROCESSED_TRANSACTIONS = set()

async def save_processed_transactions():
    try:
        async with aiofiles.open(PROCESSED_TRANSACTIONS_FILE, "w") as f:
            await f.write(json.dumps(list(PROCESSED_TRANSACTIONS)))
    except Exception as e:
        print(f"Error saving processed transactions: {e}")

async def load_bot_positions():
    global BOT_POSITIONS
    try:
        async with aiofiles.open(BOT_POSITIONS_FILE, "r") as f:
            BOT_POSITIONS = json.loads(await f.read())
    except FileNotFoundError:
        BOT_POSITIONS = {}  # Start with an empty dict
    except json.JSONDecodeError:
        print("Error decoding bot_positions.json. Starting with empty positions.")
        BOT_POSITIONS = {}

async def save_bot_positions():
    try:
        async with aiofiles.open(BOT_POSITIONS_FILE, "w") as f:
            await f.write(json.dumps(BOT_POSITIONS))
    except Exception as e:
        print(f"Error saving bot positions: {e}")

def load_keypair(private_key_base58: str) -> Keypair | None:
    try:
        secret_key_bytes = base58.b58decode(private_key_base58)
        keypair = Keypair.from_bytes(secret_key_bytes)
        return keypair
    except Exception as e:
        print(f"❌ Error loading keypair: {e}")
        return None

async def get_wallet_balance(http_client, wallet_pubkey, rpc_url):
    """Fetches the balance of a wallet (in SOL)."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(wallet_pubkey)],
        }
        response = await http_client.post(rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            print(f"⚠️ Failed to fetch balance (RPC): {data['error'].get('message', 'Unknown error')}")
            return None

        balance_lamports = data['result']['value']
        balance_sol = balance_lamports / 10**9  # Convert lamports to SOL
        return balance_sol

    except httpx.HTTPError as e:
        print(f"⚠️ HTTP error fetching balance: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Failed to fetch wallet balance: {e}")
        return None

async def fetch_token_decimals(token_mint: str, http_client: httpx.AsyncClient) -> int | None:
    """Fetches the decimals for a given token mint using the Solana RPC."""
    try:
        request = GetTokenSupply(Pubkey.from_string(token_mint))
        response = await http_client.post(RPC_ENDPOINT, json=request.to_json())
        response.raise_for_status()
        result = response.json()
        decimals = result['result']['value']['decimals']
        return decimals
    except httpx.HTTPError as e:
        print(f"HTTP error fetching token decimals: {e}")
        return None
    except Exception as e:
        print(f"Error fetching token decimals: {e}")
        return None

async def confirm_transaction(http_client: httpx.AsyncClient, tx_signature: str, rpc_url: str, max_retries: int = 3, delay: int = 5) -> bool:
    """Confirms a transaction with retries."""
    for attempt in range(max_retries):
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [tx_signature, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            }
            response = await http_client.post(rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                print(f"Error confirming transaction: {data['error']}")
                return False

            if data.get("result") is None or data["result"].get("transaction") is None:
                print(f"Attempt {attempt+1}: Transaction not found, retrying...")
                await asyncio.sleep(delay)
                continue

            if data["result"].get("meta") is not None and data["result"]["meta"].get("err") is None:
                print(f"✅ Transaction confirmed: {tx_signature}")
                return True
            else:
                print(f"❌ Transaction failed: {tx_signature}, Error: {data['result']['meta']['err']}")
                return False

        except httpx.HTTPError as e:
            print(f"⚠️ HTTP error confirming transaction on attempt {attempt+1}: {e}")
            await asyncio.sleep(delay)
        except Exception as e:
            print(f"⚠️ Error confirming transaction on attempt {attempt+1}: {e}")
            await asyncio.sleep(delay)

    print(f"❌ Transaction confirmation failed after {max_retries} attempts.")
    return False

async def subscribe_to_logs(ws, wallet_address):
    """Subscribes to the logs for a given wallet address."""
    subscription_message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [wallet_address]},
            {"commitment": "finalized"}
        ],
    }
    await ws.send(json.dumps(subscription_message))

def base64_decode_with_padding(data: str) -> bytes:
    """Decodes a base64 string, adding padding if necessary, and handling URL-safe variants."""
    data = data.replace('-', '+').replace('_', '/')
    missing_padding = len(data) % 4
    if missing_padding:
        data += '=' * (4 - missing_padding)
    return base64.b64decode(data)

async def extract_trade_data_no_app(txn_details: dict) -> list[dict]:
    try:
        trades = []
        user_account = None

        if 'signer' in txn_details.get('transaction', {}).get('message', {}):
            user_account = txn_details['transaction']['message']['signer']
            trades.append({"user_account": user_account})
            return trades

        account_data = txn_details.get('accountData', [])

        if not account_data:
            print("ERROR: extract_trade_data_no_app: No 'accountData' found in txn_details.")
            return []

        for account_info in account_data:
            if 'nativeBalanceChange' in account_info:
                user_account = account_info['account']
                break

        if user_account is None:
            print("ERROR: extract_trade_data_no_app: Could not find user account")
            return []

        trades.append({"user_account": user_account})
        return trades

    except Exception as e:
        print(f"ERROR in extract_trade_data_no_app: {e}")
        traceback.print_exc()
        return []

async def get_associated_token_address(wallet_address: Pubkey, mint_address: Pubkey) -> Pubkey:
    """Calculates the Associated Token Account address."""
    return Pubkey.find_program_address(
        [bytes(wallet_address), bytes(TOKEN_PROGRAM_ID), bytes(mint_address)],
        ASSOCIATED_TOKEN_PROGRAM_ID
    )[0]

async def create_associated_token_account_instruction(
    payer: Pubkey,
    wallet_address: Pubkey,
    mint_address: Pubkey
) -> Instruction:
    """Creates an instruction to create an Associated Token Account."""
    associated_token_address = await get_associated_token_address(wallet_address, mint_address)

    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
        accounts=[
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(associated_token_address, is_signer=False, is_writable=True),
            AccountMeta(wallet_address, is_signer=False, is_writable=False),
            AccountMeta(mint_address, is_signer=False, is_writable=False),
            AccountMeta(Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(Pubkey.from_string("SysvarRent111111111111111111111111111111111"), is_signer=False, is_writable=False),

        ],
        data=bytes(),
    )

async def check_and_create_ata(wallet: Keypair, mint: str, http_client: httpx.AsyncClient) -> None:
    """Checks if an ATA exists and creates it if not, with proper error handling."""
    ata_address = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(mint))
    print(f"DEBUG: Checking ATA for wallet: {wallet.pubkey()}, mint: {mint}, ATA: {ata_address}")

    try:
        # Check if the account exists
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [str(ata_address), {"encoding": "jsonParsed", "commitment": "confirmed"}],
        }
        response = await http_client.post(RPC_ENDPOINT, json=payload)
        response.raise_for_status()
        data = response.json()

        if data["result"]["value"] is None:  # Account doesn't exist
            print(f"DEBUG: ATA for {mint} does not exist. Creating...")
            # Create the ATA
            create_ata_instruction = await create_associated_token_account_instruction(
                wallet.pubkey(), wallet.pubkey(), Pubkey.from_string(mint)
            )
            message = Message([create_ata_instruction])

            # Get recent blockhash *before* creating the transaction
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
            response = await http_client.post(RPC_ENDPOINT, json=payload)
            response.raise_for_status()
            data = response.json()
            recent_blockhash = Hash.from_string(data["result"]["value"]["blockhash"])


            transaction = Transaction([wallet], message, recent_blockhash) # Corrected
            transaction.sign([wallet],recent_blockhash)  # Corrected
            serialized_tx = base64.b64encode(transaction.serialize()).decode("ascii")

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [serialized_tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
            }

            # Send and confirm ATA creation
            response = await http_client.post(RPC_ENDPOINT, json=payload)
            response.raise_for_status()
            tx_hash = response.json()['result']
            
            #Confirm transaction
            if await confirm_transaction(http_client, tx_hash, RPC_ENDPOINT):
                print(f"DEBUG: ATA for {mint} created: {tx_hash}")
            else:
                print(f"ERROR: ATA creation failed for {mint}") #We print the error.
                await send_telegram_message(None, f"❌ Failed to create ATA for mint: {mint} on wallet: {str(wallet.pubkey())}")



        else:
            print(f"DEBUG: ATA for {mint} exists.")

    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e}")
        if e.response:
            print(f"Response text: {e.response.text}")
        await send_telegram_message(None, f"❌ HTTP Error creating ATA: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"An error occurred: {e}")
        await send_telegram_message(None, f"❌ Error creating ATA: {e}")

async def fetch_transaction_details_helius(signature):
    """Fetches transaction details from Helius, with retries."""
    max_retries = 5
    retry_delay = 10

    for attempt in range(max_retries):
        try:
            url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "transactions": [signature],
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()

                if isinstance(data, list) and len(data) > 0:
                    transaction = data[0]
                    if "error" in transaction:
                        print(f"Transaction {signature} not found or error: {transaction.get('error')}")
                        print(f"  Error message: {transaction.get('errorMessage')}")
                        print(f"  Retrying in {retry_delay} seconds...")
                    elif "type" in transaction:
                        return transaction  # Successfully got the transaction
                    else:
                        print(f"Transaction {signature} found but missing expected fields (no 'type').")
                        print(transaction)
                else:
                    print(f"Transaction details not found for {signature} (empty list or not a list) on attempt {attempt + 1}. Retrying...")

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)

        except httpx.HTTPStatusError as e:
            print(f"HTTP error when fetching transaction details from Helius: {e}")
            if e.response and e.response.status_code in (429, 500, 502, 503, 504):
                print(f"Retrying after HTTP error {e.response.status_code} in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 60)
                continue
            else:
                return None  # Don't retry for other HTTP errors

        except httpx.RequestError as e:
            print(f"Network error fetching transaction from Helius: {e}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)
            continue

        except Exception as e:
            print(f"Error fetching transaction details from Helius: {e}")
            traceback.print_exc()
            return None

    print(f"Failed to fetch transaction details for {signature} after {max_retries} attempts.")
    return None

async def determine_trade_instructions(app, txn_details, wallet, trade_type, http_client):
    """Determines the trade instructions, handling inner instructions."""
    instructions_list = []
    account_info_dict = {
        "from_account": None,
        "to_account": None,
        "input_token_mint": None,
        "output_token_mint": None,
        "dex_name": None,
        "amount_in": None,
        "decimals": None
    }
    #Get trade data:
    trade_data = await extract_trade_data_no_app(txn_details)
    if not trade_data:
        return [], None  # No data found
    user_account_str = trade_data[0]["user_account"]

    # --- Pre-calculate EVERYTHING we need ---
    amount_in_human = 0
    input_token_mint = None
    output_token_mint = None
    from_account = None
    to_account = None
    transfer_decimals = 9  # Default to SOL

    print(f"DEBUG: determine_trade_instructions: Starting with trade_type={trade_type}")

    for transfer in txn_details.get("tokenTransfers", []):
        print(f"DEBUG: determine_trade_instructions: Processing token transfer: {transfer}")
        if trade_type == "SELL":
            if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                input_token_mint = transfer.get("mint")
                from_account = transfer.get("fromTokenAccount")
                token_amount = transfer.get("tokenAmount")
                if isinstance(token_amount, dict):
                    amount_in_human = float(token_amount.get("uiAmountString", "0"))
                    transfer_decimals = int(token_amount.get("decimals", 9))
                elif isinstance(token_amount, float):
                    amount_in_human = token_amount
                    transfer_decimals = 9
                if from_account and input_token_mint:
                    print(f"DEBUG: determine_trade_instructions: SELL - Found fromUserAccount. Breaking.")
                    break
            elif transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                to_account = transfer.get("toTokenAccount")
                output_token_mint = transfer.get("mint")
                if to_account and output_token_mint:
                    print(f"DEBUG: determine_trade_instructions: SELL - Found toUserAccount. Breaking.")
                    break
        elif trade_type == "BUY":
            if transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                input_token_mint = transfer.get("mint")
                to_account = transfer.get("toTokenAccount")
                token_amount = transfer.get("tokenAmount")
                if isinstance(token_amount, dict):
                    amount_in_human = float(token_amount.get("uiAmountString", "0"))
                    transfer_decimals = int(token_amount.get("decimals", 9))
                elif isinstance(token_amount, float):
                    amount_in_human = token_amount
                    transfer_decimals = 9
                if to_account and input_token_mint:
                    print(f"DEBUG: determine_trade_instructions: BUY - Found toUserAccount. Breaking.")
                    break
            elif transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                from_account = transfer.get("fromTokenAccount")
                output_token_mint = transfer.get("mint")
                if from_account and output_token_mint:
                    print(f"DEBUG: determine_trade_instructions: BUY - Found fromUserAccount. Breaking.")
                    break

    if not txn_details.get("tokenTransfers", []) or from_account is None or to_account is None: #Native
        print("DEBUG: determine_trade_instructions: Checking native transfers.")
        for transfer in txn_details.get("nativeTransfers", []):
            print(f"DEBUG: determine_trade_instructions: Processing native transfer: {transfer}")
            if trade_type == "SELL" and transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                input_token_mint = "So11111111111111111111111111111111111111112"
                from_account = transfer.get("fromUserAccount")
                to_account = transfer.get("toUserAccount")  # <-- Get to_account here!
                amount_in_human = float(transfer.get("amount", 0)) / (10**9)
                transfer_decimals = 9
                print(f"DEBUG: determine_trade_instructions: SELL - Found fromUserAccount in native. Breaking.")
                break  # Add this break
            elif trade_type == "BUY" and transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                input_token_mint = "So11111111111111111111111111111111111111112"
                to_account = transfer.get("toUserAccount")
                from_account = transfer.get("fromUserAccount") # <-- Get from_account here!
                amount_in_human = float(transfer.get("amount", 0)) / (10**9)
                transfer_decimals = 9
                print(f"DEBUG: determine_trade_instructions: BUY - Found toUserAccount in native. Breaking.")
                break  # Add this break

    print(f"DEBUG: determine_trade_instructions: Before check - from_account={from_account}, to_account={to_account}, input_token_mint={input_token_mint}")

    if from_account is None or to_account is None or input_token_mint is None:
        print("ERROR: Could not determine from/to accounts or mint.")
        return [], None

    # Find output token mint for SELL trades, if not already found in token transfers
    if trade_type == "SELL" and output_token_mint is None:
        for transfer in txn_details.get('tokenTransfers', []):
            if transfer.get('toUserAccount') != COPY_WALLET_ADDRESS: #The other token, the one received
                output_token_mint = transfer.get('mint')
                break
    # For BUY trades, output token is the same as input, if not found on token transfer.
    elif trade_type == "BUY" and output_token_mint is None:
        output_token_mint = input_token_mint

    if output_token_mint is None:
        print("ERROR: Could not determine output token mint.")
        return [], None


    account_info_dict["from_account"] = from_account
    account_info_dict["to_account"] = to_account
    account_info_dict["input_token_mint"] = input_token_mint
    account_info_dict["output_token_mint"] = output_token_mint
    account_info_dict["amount_in"] = amount_in_human
    account_info_dict["decimals"] = transfer_decimals

    for instruction_data in txn_details.get('instructions', []):
        program_id = instruction_data.get('programId')
        print(f"DEBUG: determine_trade_instructions processing instruction for program ID: {program_id}")

        if program_id == str(JUPITER_V6_PROGRAM_ID):
            print("DEBUG: determine_trade_instructions - Jupiter v6 instruction found.")
            instructions, updated_account_info = await handle_jupiter_v4(txn_details, wallet, trade_type, http_client, transfer_decimals, amount_in_human)
            if instructions:
                instructions_list.extend(instructions)
            if updated_account_info:
                account_info_dict.update(updated_account_info)
            break #Jupiter v6, we handled.

        # Directly call handle_raydium_clmm, it will dispatch further.
        handler_result = await handle_raydium_clmm(app, instruction_data, wallet, trade_type, http_client, account_info_dict)
        if handler_result:
            inner_instructions, inner_account_info = handler_result
            instructions_list.extend(inner_instructions)
            if inner_account_info:
                account_info_dict.update(inner_account_info)
            #If there is clmm trade, stop checking others.
            break

        # Recursively process *nested* inner instructions.
        if instruction_data.get("innerInstructions"):
            inner_result = await process_inner_instructions(app, instruction_data["innerInstructions"], wallet, trade_type, http_client, account_info_dict)
            if inner_result:
                inner_instructions, inner_account_info = inner_result
                instructions_list.extend(inner_instructions)  # Add inner instructions *first*
                # Update account info from inner instructions, if available.
                if inner_account_info:  # Ensure inner_account_info exists
                    account_info_dict.update(inner_account_info)

    return instructions_list, account_info_dict

async def process_transaction(app, txn_details):
    """Processes a single transaction log entry."""
    signature = txn_details.get("signature")
    if not signature or signature in PROCESSED_TRANSACTIONS:
        return

    txn_details = await fetch_transaction_details_helius(signature) # Fetch the full transaction details.
    if not txn_details:
        await send_telegram_message(app, f"❌ Could not fetch details for Tx `{signature}`.", markdown=True)
        return
    # --- FILTER Transactions ---
    if txn_details.get('type') not in ('SWAP', 'TRANSFER', 'UNKNOWN'): # Add types as needed
        return

    involved = False
    for transfer in txn_details.get("tokenTransfers", []):
      if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS or transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
          involved = True
          break

    if not involved:
      for transfer in txn_details.get("nativeTransfers", []):
          if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS or transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
              involved = True
              break
    if not involved:
      return
    # --- Determine trade type based on the COPIED WALLET'S transfers: ---
    trade_type = None

    for transfer in txn_details.get("tokenTransfers", []):
      if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
          trade_type = "SELL"
          break
      elif transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
          trade_type = "BUY"
          break

    if trade_type is None:
      for transfer in txn_details.get("nativeTransfers", []):
        if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
            trade_type = "SELL"
            break
        elif transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
            trade_type = "BUY"
            break
    if trade_type is None:
      return
    # --- Get Instructions and Account Info ---
    async with httpx.AsyncClient() as http_client:

      for private_key_hex in WALLETS:
          keypair = load_keypair(private_key_hex)
          if keypair:
              instructions, account_info = await determine_trade_instructions(app, txn_details, keypair, trade_type, http_client)

              if instructions:
                  await execute_trade(app, {}, str(keypair.pubkey()), keypair, trade_type, account_info, instructions, signature)
              else:
                  print(f"No instructions to execute")

    PROCESSED_TRANSACTIONS.add(signature)
    await save_processed_transactions()

async def execute_trade(app, amounts: dict, recipient: str, wallet: Keypair, trade_type: str, account_info: dict, instructions: list, signature:str):
    """Executes a trade based on the copied transaction."""
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    tx_signature = None
    async with httpx.AsyncClient() as http_client:
        try:

            if not instructions:
                print("No instructions returned from handler.")
                return False

            preview_message = "ℹ️ **Trade Preview** (What would have happened):\n\n"
            if account_info:
              if account_info['input_token_mint'] == "So11111111111111111111111111111111111111112":
                token_mint = "SOL"
              else:
                token_mint = account_info["input_token_mint"]
              preview_message += f" * **DEX:** {account_info.get('dex_name', 'Unknown DEX')}\n"
              preview_message += f" * **Action:** {trade_type}\n"
              preview_message += f" * **Amount In:** {account_info.get('amount_in', 'N/A'):.8f} {token_mint}\n"
              preview_message += f" * **From:** {account_info['from_account']}\n"
              preview_message += f" * **To:** {account_info['to_account']}\n"
              preview_message += f" * **Output Token Mint**: {account_info.get('output_token_mint', 'N/A')}\n"

            await send_telegram_message(app, preview_message, markdown=True)

            payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
            response = await http_client.post(rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
            recent_blockhash = Hash.from_string(data["result"]["value"]["blockhash"])
            print(f"DEBUG: execute_trade - Recent Blockhash: {recent_blockhash}")

            message = Message(instructions)
            transaction = Transaction([wallet], message, recent_blockhash)
            transaction.sign([wallet],recent_blockhash)
            serialized_tx = base64.b64encode(transaction.serialize()).decode("ascii")
            print(f"DEBUG: execute_trade - Serialized Transaction: {serialized_tx}")
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [serialized_tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
            }
            response = await http_client.post(rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
            print(f"DEBUG: execute_trade - sendTransaction Response: {data}")

            if "error" in data:
                error_msg = data["error"].get("message", "Unknown RPC error")
                await send_telegram_message(app, f"❌ Transaction Failed: {error_msg}", markdown=True)
                return False

            if "result" not in data:
                await send_telegram_message(app, f"❌ Transaction Failed: No result in RPC response.", markdown=True)
                return False

            tx_signature = data["result"]
            trade_action = "Bought" if trade_type == "BUY" else "Sold"
            await send_telegram_message(app, f"⏳ {trade_action} Transaction: `{tx_signature}`", markdown=True)

            # --- Check balance after trade (Optional) ---
            updated_balance = await get_wallet_balance(http_client, wallet.pubkey(), rpc_url)
            if updated_balance is not None:
                await send_telegram_message(app, f"💰 Updated balance: `{updated_balance:.6f}` SOL", markdown=True)

            # --- Confirm the transaction ---
            confirmed = await confirm_transaction(http_client, tx_signature, rpc_url)
            if confirmed:
                await send_telegram_message(app, "✅ Transaction Confirmed!", markdown=True)
                # --- Position Tracking (Open/Close) ---
                if account_info.get('dex_name') == 'raydium_clmm': # Only for CLMM
                    #Check to understand if it is open position or not.
                    if any(ix.program_id == RAYDIUM_CLMM_PROGRAM_ID and ix.data[:8] == bytes.fromhex("da58ea10799568b2") for ix in instructions):
                        # OpenPosition: Add NFT mint to BOT_POSITIONS
                        mint_address = str(instructions[0].accounts[2].pubkey) #Always at index to, and always created so, we take from instruction.
                        if str(wallet.pubkey()) not in BOT_POSITIONS:
                            BOT_POSITIONS[str(wallet.pubkey())] = []
                        BOT_POSITIONS[str(wallet.pubkey())].append(mint_address)
                        print(f"DEBUG: Added position NFT {mint_address} to tracking.")
                        await send_telegram_message(app, f"➕ Added position NFT to tracking: `{mint_address}`", markdown=True)
                        await save_bot_positions()
                    #Check if close position:
                    elif any(ix.program_id == RAYDIUM_CLMM_PROGRAM_ID and ix.data[:8] == bytes.fromhex("4bd71044f9eb6490") for ix in instructions):
                      #ClosePosition: Remove NFT mint from BOT_POSITIONS
                      nft_mint = str(instructions[0].accounts[1].pubkey) #Always index 1 in close position instruction
                      if str(wallet.pubkey()) in BOT_POSITIONS and nft_mint in BOT_POSITIONS[str(wallet.pubkey())]:
                          BOT_POSITIONS[str(wallet.pubkey())].remove(nft_mint)
                          print(f"DEBUG: Removed position NFT {nft_mint} from tracking.")
                          await send_telegram_message(app, f"➖ Removed position NFT from tracking: `{nft_mint}`", markdown=True)
                          await save_bot_positions()
                return True
            else:
                await send_telegram_message(app, f"❌ Transaction Failed to Confirm! Check explorer: https://explorer.solana.com/tx/{tx_signature}", markdown=True)
                return False

        except httpx.HTTPStatusError as e:
            await send_telegram_message(app, f"❌ HTTP Error: {e.response.status_code} - {e.response.text}", markdown=True)
            traceback.print_exc()
            return False
        except httpx.ConnectError as e:
            await send_telegram_message(app, f"❌ Connection Error: {e}", markdown=True)
            traceback.print_exc()
            return False
        except httpx.ReadTimeout as e:
            await send_telegram_message(app, f"❌ Read Timeout: {e}", markdown=True)
            traceback.print_exc()
            return False
        except Exception as e:
            await send_telegram_message(app, f"❌ Trade Failed! Error: `{e}`", markdown=True)
            traceback.print_exc()
            return False

async def main():
    class MockApp:
        def __init__(self):
            pass
    app = MockApp()

    await load_processed_transactions()
    await load_bot_positions()

    await monitor_trades(app)

async def process_inner_instructions(app, inner_instructions, wallet, trade_type, http_client, account_info_dict):
    """Recursively processes inner instructions."""
    instructions_list = []

    for inner_instruction in inner_instructions:
        program_id = inner_instruction.get('programId')
        handler = None

        if program_id == str(RAYDIUM_CLMM_PROGRAM_ID):
            handler = handle_raydium_clmm

        if handler:
            handler_result = await handler(app, inner_instruction, wallet, trade_type, http_client, account_info_dict)
            if handler_result:
                inner_instructions_list, _ = handler_result
                instructions_list.extend(inner_instructions_list)

        if inner_instruction.get("innerInstructions"):
            nested_result = await process_inner_instructions(app, inner_instruction["innerInstructions"], wallet, trade_type, http_client,  account_info_dict)
            if nested_result:
                nested_instructions, _ = nested_result
                instructions_list.extend(nested_instructions)

    return instructions_list, account_info_dict

class OpenPositionInstructionData:
    """Represents the data for the OpenPosition instruction."""
    def __init__(self, discriminator: bytes, tick_lower_index: int, tick_upper_index: int, liquidity: int, amount_0_max: int, amount_1_max: int):
        self.discriminator = discriminator
        self.tick_lower_index = tick_lower_index
        self.tick_upper_index = tick_upper_index
        self.liquidity = liquidity
        self.amount_0_max = amount_0_max
        self.amount_1_max = amount_1_max

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<iiQQQ",
                                                self.tick_lower_index,
                                                self.tick_upper_index,
                                                self.liquidity,
                                                self.amount_0_max,
                                                self.amount_1_max)

class ClosePositionInstructionData:
    """Represents the data for the ClosePosition instruction."""
    def __init__(self, discriminator: bytes):
        self.discriminator = discriminator

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator

class IncreaseLiquidityInstructionData:
    """Represents the data for the IncreaseLiquidity instruction."""
    def __init__(self, discriminator: bytes, liquidity: int, amount_0_max: int, amount_1_max: int):
        self.discriminator = discriminator
        self.liquidity = liquidity
        self.amount_0_max = amount_0_max
        self.amount_1_max = amount_1_max

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<QQQ", self.liquidity, self.amount_0_max, self.amount_1_max)

class DecreaseLiquidityInstructionData:
    """Represents the data for the DecreaseLiquidity instruction."""
    def __init__(self, discriminator: bytes, liquidity: int, amount_0_min: int, amount_1_min: int):
        self.discriminator = discriminator
        self.liquidity = liquidity
        self.amount_0_min = amount_0_min
        self.amount_1_min = amount_1_min

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<QQQ", self.liquidity, self.amount_0_min, self.amount_1_min)

class SwapBaseInInstructionData:
    def __init__(self, discriminator: bytes, amount_in: int, min_amount_out: int):
        self.discriminator = discriminator
        self.amount_in = amount_in
        self.min_amount_out = min_amount_out

    def serialize(self) -> bytes:
        return self.discriminator + struct.pack("<QQ", self.amount_in, self.min_amount_out)

class SwapBaseOutInstructionData:
    def __init__(self, discriminator: bytes, max_amount_in: int, amount_out: int):
        self.discriminator = discriminator
        self.max_amount_in = max_amount_in
        self.amount_out = amount_out

    def serialize(self) -> bytes:
        return self.discriminator + struct.pack("<QQ", self.max_amount_in, self.amount_out)

async def create_mint(http_client: httpx.AsyncClient, payer: Keypair, mint_authority: Pubkey, decimals: int = 0) -> Keypair | None:
    """Creates a new SPL Token mint."""
    mint = Keypair()
    account_len = MINT_LAYOUT.sizeof()
    lamports = await http_client.get_minimum_balance_for_rent_exemption(account_len)
    lamports = lamports.value

    create_mint_account_ix = create_account(
        CreateAccountParams(
            payer.pubkey(),
            mint.pubkey(),
            lamports,
            account_len,
            TOKEN_PROGRAM_ID
        )
    )

    initialize_mint_ix = Instruction(
        program_id=TOKEN_PROGRAM_ID,
        accounts=[
            AccountMeta(mint.pubkey(), True, False),
            AccountMeta(Pubkey.from_string("SysvarRent111111111111111111111111111111111"), False, False),
        ],
        data=struct.pack("<BB", 0, decimals),
    )

    #get recent blockhash:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
    response = await http_client.post(RPC_ENDPOINT, json=payload)
    response.raise_for_status()
    data = response.json()
    recent_blockhash = Hash.from_string(data["result"]["value"]["blockhash"])
    message = Message([create_mint_account_ix, initialize_mint_ix])
    transaction = Transaction([payer], message, recent_blockhash) #Corrected
    transaction.sign([payer, mint],recent_blockhash) # Signer

    return mint

def calculate_tick_array_start_index(tick_index: int, tick_spacing: int) -> int:
    """Calculates the tick array start index."""
    return (tick_index // (88 * tick_spacing)) * (88 * tick_spacing)

async def handle_clmm_open_position(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> Tuple[List[Instruction] | None, Dict]:
    """Handles the OpenPosition CLMM instruction."""
    print("DEBUG: Inside handle_clmm_open_position")
    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        tick_lower_index = struct.unpack("<i", data[8:12])[0]
        tick_upper_index = struct.unpack("<i", data[12:16])[0]
        liquidity = struct.unpack("<Q", data[16:24])[0]
        amount_0_max = struct.unpack("<Q", data[24:32])[0]
        amount_1_max = struct.unpack("<Q", data[32:40])[0]
        pool_state_account_info = await http_client.get_account_info(Pubkey.from_string(instruction_data.get("accounts", [])[5]))
        if pool_state_account_info.value is None:
            print(f"Pool state account not found")
            return [], None
        pool_state_data = pool_state_account_info.value.data
        tick_spacing = struct.unpack("<H", pool_state_data[104:106])[0]
        tick_array_lower_start_index = calculate_tick_array_start_index(tick_lower_index, tick_spacing)
        tick_array_upper_start_index = calculate_tick_array_start_index(tick_upper_index, tick_spacing)

        new_liquidity = int(liquidity * TRADE_PERCENTAGE)

        if liquidity > 0:
            original_ratio_0 = amount_0_max / liquidity
            original_ratio_1 = amount_1_max / liquidity
        else:
            print("ERROR: Original liquidity is zero in OpenPosition!")
            return [], None

        new_amount_0_max = int(new_liquidity * original_ratio_0 * 1.005)
        new_amount_1_max = int(new_liquidity * original_ratio_1 * 1.005)

        instruction_data_obj = OpenPositionInstructionData(discriminator, tick_lower_index, tick_upper_index, new_liquidity, new_amount_0_max, new_amount_1_max)
        modified_data = instruction_data_obj.serialize()

        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_owner = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True
            elif i == 1:
                position_nft_owner = pubkey
            elif i == 2:
                bot_nft_mint = await create_mint(http_client, wallet, wallet.pubkey())
                if not bot_nft_mint:
                    return [], None
                pubkey = bot_nft_mint.pubkey()
                is_writable = True
            elif i == 3:
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), pubkey)
                is_writable = True
            elif i == 5:
                pass
            elif i == 6:
                pool_state_key = dex_instruction_accounts[5]
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_lower_index.to_bytes(4, byteorder='little', signed=True),tick_upper_index.to_bytes(4, byteorder='little', signed=True)], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True
            elif i == 7 or i == 8:
                pool_state_key = dex_instruction_accounts[5]
                if i == 7:
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 8:
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True

            elif i == 9:
                nft_mint_key = dex_instruction_accounts[2]
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(nft_mint_key))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True

            elif i == 10:
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True
            elif i == 11:
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                is_writable = True
            elif i in (12, 13):
                is_writable = True

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))


        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data=modified_data
        )

        instructions = [instruction]
        if instruction_data.get("innerInstructions"):
            inner_instructions, _ = await process_inner_instructions(app, instruction_data["innerInstructions"], wallet, trade_type, http_client, account_info)
            instructions =  inner_instructions + instructions
        return instructions, account_info

    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_open_position: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_open_position: {e}")
        traceback.print_exc()
        return [], None

async def handle_clmm_close_position(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the ClosePosition CLMM instruction."""
    print("DEBUG: Inside handle_clmm_close_position")

    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        instruction_data_obj = ClosePositionInstructionData(discriminator)
        modified_data = instruction_data_obj.serialize()

        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True  # owner should be writable
            elif i == 1:  # position_nft_mint
                position_nft_mint_str = account_str
                is_writable = True # mint should be writable
            elif i == 2:  # position_nft_account, bot's position nft account
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str)
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint)
                pubkey = bot_nft_ata
                is_writable = True  # ATA should be writable

            elif i == 3: # personal_position
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True

            # Remaining accounts are read-only or have specific roles we don't modify
            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        # --- Verify Position Ownership ---
        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot close.")
            await send_telegram_message(app, f"❌ Cannot close position: Bot does not own NFT {position_nft_mint_str}.")
            return [], None

        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data= modified_data,
        )
        return [instruction], account_info

    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_close_position: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_close_position: {e}")
        traceback.print_exc()
        return [], None

async def handle_clmm_increase_liquidity(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the IncreaseLiquidity CLMM instruction."""
    print("DEBUG: Inside handle_clmm_increase_liquidity")
    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        liquidity = struct.unpack("<Q", data[8:16])[0]
        amount_0_max = struct.unpack("<Q", data[16:24])[0]
        amount_1_max = struct.unpack("<Q", data[24:32])[0]

        new_liquidity = int(liquidity * TRADE_PERCENTAGE)
        new_amount_0_max = int(amount_0_max * TRADE_PERCENTAGE * 1.005)
        new_amount_1_max = int(amount_1_max * TRADE_PERCENTAGE * 1.005)
        instruction_data_obj = IncreaseLiquidityInstructionData(discriminator, new_liquidity, new_amount_0_max, new_amount_1_max)
        modified_data = instruction_data_obj.serialize()


        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True
            elif i == 1:  # nft_account
                position_nft_mint_str = str((await http_client.get_account_info(pubkey)).value.mint)

                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str)
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint)
                pubkey = bot_nft_ata
                is_writable = True

            elif i == 2: #pool state
                pass #readonly
            elif i == 3: # protocol_position
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[2]
                personal_position_key = dex_instruction_accounts[4]
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_lower_index,tick_upper_index], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True

            elif i == 4: #personal_position
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True
            elif i == 5 or i == 6: #tick_array_lower and tick_array_upper
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[2]
                personal_position_key = dex_instruction_accounts[4]
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                tick_spacing = (await http_client.get_account_info(pool_state_key)).value.data[104:106]
                tick_array_lower_start_index = calculate_tick_array_start_index(int.from_bytes(tick_lower_index, byteorder='little', signed=True), int.from_bytes(tick_spacing, byteorder='little', signed=False))
                tick_array_upper_start_index = calculate_tick_array_start_index(int.from_bytes(tick_upper_index, byteorder='little', signed=True),  int.from_bytes(tick_spacing, byteorder='little', signed=False))

                if i == 5:
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 6:
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True
            elif i == 7:  # token_account_0 (user's token A account)
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True
            elif i == 8:  # token_account_1 (user's token B account)
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                is_writable = True
            elif i in (9, 10):  # token_vault_0 and token_vault_1 (pool vaults)
                is_writable = True

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot increase liquidity.")
            await send_telegram_message(app, f"❌ Cannot increase liquidity: Bot does not own NFT {position_nft_mint_str}.")
            return [], None

        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data= modified_data
        )
        instructions = [instruction]

        if instruction_data.get("innerInstructions"):
            inner_instructions, _ = await process_inner_instructions(app, instruction_data["innerInstructions"], wallet, trade_type, http_client, account_info)
            instructions = inner_instructions + instructions


        return instructions, account_info
    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_increase_liquidity: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_increase_liquidity: {e}")
        traceback.print_exc()
        return [], None

async def handle_clmm_decrease_liquidity(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the DecreaseLiquidity CLMM instruction."""
    print("DEBUG: Inside handle_clmm_decrease_liquidity")

    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        liquidity = struct.unpack("<Q", data[8:16])[0]
        amount_0_min = struct.unpack("<Q", data[16:24])[0]
        amount_1_min = struct.unpack("<Q", data[24:32])[0]

        new_liquidity = int(liquidity * TRADE_PERCENTAGE)
        new_amount_0_min = int(amount_0_min * TRADE_PERCENTAGE * 0.995)  # 0.5% tolerance
        new_amount_1_min = int(amount_1_min * TRADE_PERCENTAGE * 0.995)  # 0.5% tolerance

        instruction_data_obj = DecreaseLiquidityInstructionData(discriminator, new_liquidity, new_amount_0_min, new_amount_1_min)
        modified_data = instruction_data_obj.serialize()


        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True
            elif i == 1:  # nft_account, bot's nft account.
                position_nft_mint_str = str((await http_client.get_account_info(pubkey)).value.mint)
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str)
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint)
                pubkey = bot_nft_ata
                is_writable = True
            elif i == 2: # personal_position
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True
            elif i == 3: #pool state
                pass  # Readonly
            elif i == 4: # protocol_position
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[3]
                personal_position_key = dex_instruction_accounts[2]
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_lower_index,tick_upper_index], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True

            elif i == 5 or i == 6:  # token_vault_0 and token_vault_1 (pool vaults)
                is_writable = True
            elif i == 7 or i == 8: #tick_array_lower and tick_array_upper
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[3]
                personal_position_key = dex_instruction_accounts[2]
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                tick_spacing = (await http_client.get_account_info(pool_state_key)).value.data[104:106]
                tick_array_lower_start_index = calculate_tick_array_start_index(int.from_bytes(tick_lower_index, byteorder='little', signed=True), int.from_bytes(tick_spacing, byteorder='little', signed=False))
                tick_array_upper_start_index = calculate_tick_array_start_index(int.from_bytes(tick_upper_index, byteorder='little', signed=True),  int.from_bytes(tick_spacing, byteorder='little', signed=False))

                if i == 7:
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 8:
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True
            elif i == 9:  # recipient_token_account_0 (user's token A account)
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                is_writable = True
            elif i == 10:  # recipient_token_account_1 (user's token B account)
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot decrease liquidity.")
            await send_telegram_message(app, f"❌ Cannot decrease liquidity: Bot does not own NFT {position_nft_mint_str}.")
            return [], None


        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data=modified_data,
        )

        return [instruction], account_info

    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_decrease_liquidity: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_decrease_liquidity: {e}")
        traceback.print_exc()
        return [], None

async def handle_raydium_amm(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles Raydium AMM swaps."""
    print("DEBUG: Inside handle_raydium_amm")

    try:
        if instruction_data.get('programId') == str(RAYDIUM_AMM_PROGRAM_ID):
            print("DEBUG: Found Raydium AMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]
            discriminator_hex = discriminator.hex()

            if discriminator_hex == "5416a48589ca4b5f":  # swap_base_in
                print("DEBUG: Found swap_base_in instruction")

                amount_in = struct.unpack("<Q", data[8:16])[0]
                min_amount_out = struct.unpack("<Q", data[16:24])[0]

                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                original_amount_in = int(amount_in)
                original_amount_out = int(min_amount_out)

                if original_amount_in > 0:
                    expected_output_ratio = original_amount_out / original_amount_in
                else:
                    expected_output_ratio = 0

                new_expected_output = int(new_amount_in * expected_output_ratio)
                new_min_amount_out = int(new_expected_output * 0.995)  # 0.5% slippage

                instruction_data_obj = SwapBaseInInstructionData(discriminator, new_amount_in, new_min_amount_out)
                modified_data = instruction_data_obj.serialize()

            elif discriminator_hex == "f25f7d241885dd76":  # swap_base_out
                print("DEBUG: Found swap_base_out instruction")

                max_amount_in = struct.unpack("<Q", data[8:16])[0]
                amount_out = struct.unpack("<Q", data[16:24])[0]

                amount_in = int(amount_out * (10 ** account_info["decimals"])) #Use output amount.
                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                new_max_amount_in = int(new_amount_in * 1.005)

                instruction_data_obj = SwapBaseOutInstructionData(discriminator, new_max_amount_in, new_amount_in) #Use calculated amount in.
                modified_data = instruction_data_obj.serialize()
            else:
                print(f"DEBUG: Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                return [], None

            accounts = []
            dex_instruction_accounts = instruction_data.get("accounts", [])

            for i, account_str in enumerate(dex_instruction_accounts):
                pubkey = Pubkey.from_string(account_str)
                is_signer = False
                is_writable = False

                if account_str == COPY_WALLET_ADDRESS:
                    pubkey = wallet.pubkey()
                    is_signer = True
                    is_writable = True

                elif account_str == account_info["from_account"]:
                    print(f"DEBUG: Creating/Checking ATA for input token: {account_info['input_token_mint']}")
                    await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                    if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                    is_writable = True

                elif account_str == account_info["to_account"]:
                    print(f"DEBUG: Creating/Checking ATA for output token: {account_info['output_token_mint']}")
                    await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                    if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                    is_writable = True

                elif i in (1, 2, 3, 6, 7):  # Correct writable account indices for AMM
                    is_writable = True

                accounts.append(AccountMeta(pubkey, is_signer, is_writable))

            instruction = Instruction(program_id=RAYDIUM_AMM_PROGRAM_ID, accounts=accounts, data=bytes(modified_data))
            return [instruction], account_info

    except Exception as e:
        print(f"Error in handle_raydium_amm: {e}")
        traceback.print_exc()
        return [], None

async def handle_raydium_clmm(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles Raydium CLMM instructions, dispatching to specific handlers."""
    print("DEBUG: Inside handle_raydium_clmm")

    CLMM_DISCRIMINATORS = {
        "4f5917759e1421b7": "create_pool",
        "da58ea10799568b2": "open_position",
        "d85e1d3d0469799c": "open_position_v2",
        "d8c747194d7b9b6e": "open_position_with_token22_nft",
        "4bd71044f9eb6490": "close_position",
        "e441f24085726917": "increase_liquidity",
        "97fe9a236d189432": "increase_liquidity_v2",
        "c4750e39b889c540": "decrease_liquidity",
        "802d76d7fb28b28d": "decrease_liquidity_v2",
        "28c710985441885d": "swap",
        "4424459b7b027205": "swap_v2",
        "81b623c1c849f74d": "swap_router_base_in",
        "06e1a439579b45b8": "update_reward_info",
        "a6b2b3f89629c8b4": "initialize_reward",
        "8b84801d5a976ea9": "set_reward_params",
        "7a45a2b07e6bca19": "collect_remaining_rewards",
        "808e54b16f951b4c": "admin",
    }

    AMM_DISCRIMINATORS = {
        "41888098e941d738": "initialize",
        "d7d1e53979557c5b": "initialize2",
        "f0c5b7c4259696d4": "pre_initialize",
        "65b165056fd051cf": "monitor_step",
        "ccb810d5d2591357": "deposit",
        "9104e2c36895db72": "withdraw",
        "6bd781d3c5f3f234": "set_params",
        "3d9e44f879b9e6a2": "withdraw_srm",
        "5416a48589ca4b5f": "swap_base_in",
        "f25f7d241885dd76": "swap_base_out",
        "26fa468b681cd25b": "simulate_get_pool_info",
        "a8b549087793e4d5": "simulate_swap_base_in",
        "5f649b6293c21f16": "simulate_swap_base_out",
        "53f3b35d910a3120": "simulate_run_crank",
        "1b7395576fd96b55": "admin_cancel_orders",
        "b22fdc78d9536cad": "create_config_account",
        "68ff6c91814a1f57": "update_config_account",
        "7c6bb28406ab1ffc": "config",
    }
    try:
        instructions_list = []

        if instruction_data.get('programId') == str(RAYDIUM_CLMM_PROGRAM_ID):
            print("DEBUG: Found Raydium CLMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]
            discriminator_hex = discriminator.hex()

            clmm_instruction_name = CLMM_DISCRIMINATORS.get(discriminator_hex)

            if clmm_instruction_name:
                print(f"DEBUG: CLMM Instruction: {clmm_instruction_name}")
                await send_telegram_message(app, f"ℹ️ Raydium CLMM Instruction: {clmm_instruction_name}")

                if clmm_instruction_name == "open_position":
                    instructions, updated_account_info = await handle_clmm_open_position(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                        account_info.update(updated_account_info)
                    return instructions_list, account_info

                elif clmm_instruction_name == "close_position":
                    instructions, updated_account_info = await handle_clmm_close_position(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                        account_info.update(updated_account_info)
                    return instructions_list, account_info

                elif clmm_instruction_name == "increase_liquidity":
                    instructions, updated_account_info = await handle_clmm_increase_liquidity(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                        account_info.update(updated_account_info)
                    return instructions_list, account_info

                elif clmm_instruction_name == "decrease_liquidity":
                    instructions, updated_account_info = await handle_clmm_decrease_liquidity(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                        account_info.update(updated_account_info)
                    return instructions_list, account_info
                elif clmm_instruction_name == 'swap':
                    await send_telegram_message(app, f"CLMM swap instruction not implemented")
                    return [], None


                else:
                    await send_telegram_message(app, f"⏩ Skipping Raydium CLMM instruction {clmm_instruction_name} ({discriminator_hex}).  Not yet supported.")
                    return [], None

            else:
                await send_telegram_message(app, f"⏩ Skipping Raydium CLMM instruction with discriminator {discriminator_hex}.  Not recognized.")
                return [], None


        elif instruction_data.get('programId') == str(RAYDIUM_AMM_PROGRAM_ID):
            print("DEBUG: Found Raydium AMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]
            discriminator_hex = discriminator.hex()

            amm_instruction_name = AMM_DISCRIMINATORS.get(discriminator_hex)

            if amm_instruction_name:
                print(f"DEBUG: AMM Instruction: {amm_instruction_name}")
                await send_telegram_message(app, f"ℹ️ Raydium AMM Instruction: {amm_instruction_name}")
                if amm_instruction_name == "swap_base_in" or amm_instruction_name == "swap_base_out":
                    instructions, updated_account_info = await handle_raydium_amm(app, instruction_data, wallet, trade_type, http_client, account_info)
                    instructions_list.extend(instructions)
                    if updated_account_info:
                            account_info.update(updated_account_info)
                    return instructions_list, account_info

                else:
                    await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction {amm_instruction_name} ({discriminator_hex}).  Not yet supported.")
                    return [], None

            else:
                await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Admin instruction cannot be copied .")
                return [], None
        else:
            return [], None


    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_raydium_clmm: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_raydium_clmm: {e}")
        traceback.print_exc()
        return [], None
    
async def handle_jupiter_v4(txn_details: dict, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, decimals: int, amount_in_human: float) -> tuple[list[Instruction] | None, dict]:
    """Handles Jupiter v4 swaps."""
    print("DEBUG: Inside handle_jupiter_v4")

    try:
        trade_data = await extract_trade_data_no_app(txn_details)
        if not trade_data:
            raise ValueError("Could not extract trade data.")
        user_account_str = trade_data[0]["user_account"]
        print(f"DEBUG: User Account: {user_account_str}")

        from_account = None
        to_account = None
        input_token_mint = None
        output_token_mint = None

        for transfer in txn_details.get("tokenTransfers", []):
            if trade_type == "SELL":
                if transfer.get("fromUserAccount") == user_account_str:
                    input_token_mint = transfer.get("mint")
                    from_account = transfer.get("fromTokenAccount")
                elif transfer.get("toUserAccount") == user_account_str:
                    to_account = transfer.get("toTokenAccount")
                    output_token_mint = transfer.get("mint")
            elif trade_type == "BUY":
                if transfer.get("toUserAccount") == user_account_str:
                    input_token_mint = transfer.get("mint")
                    to_account = transfer.get("toTokenAccount")
                elif transfer.get("fromUserAccount") == user_account_str:
                    from_account = transfer.get("fromTokenAccount")
                    output_token_mint = transfer.get("mint")
            if from_account is not None and to_account is not None:
                break

        if not txn_details.get("tokenTransfers", []) or from_account is None or to_account is None:
            print("DEBUG: Checking native transfers.")
            native_transfers = txn_details.get("nativeTransfers", [])
            for transfer in native_transfers:
                print(f"DEBUG: Processing native transfer: {transfer}")
                if trade_type == "SELL":
                    if transfer.get("fromUserAccount") == user_account_str:
                        input_token_mint = "So11111111111111111111111111111111111111112"
                        from_account = user_account_str
                    elif transfer.get("toUserAccount") == user_account_str:
                        to_account = user_account_str
                elif trade_type == "BUY":
                    if transfer.get("toUserAccount") == user_account_str:
                        input_token_mint = "So11111111111111111111111111111111111111112"
                        to_account = user_account_str
                    elif transfer.get("fromUserAccount") == user_account_str:
                        from_account = user_account_str
                if from_account is not None and to_account is not None:
                    print("DEBUG: Both from_account and to_account found (native). Breaking loop.")
                    break
        if from_account is None or to_account is None or input_token_mint is None:
            print("ERROR: Could not determine from/to accounts or mint.")
            return [], {"from_account": None, "to_account": None, "input_token_mint": None, "output_token_mint":None}
        if trade_type == "SELL":
            output_mints = [t['mint'] for t in txn_details.get('tokenTransfers', []) if t['toUserAccount'] == user_account_str]
            if not output_mints:
                output_mints = [t['mint'] for t in txn_details.get('nativeTransfers', []) if t['toUserAccount'] == user_account_str]
            if not output_mints:
                print("Could not retreive output mint")
                return [], {"from_account": None, "to_account": None, "input_token_mint": None, "output_token_mint": None}
            output_mint = output_mints[0]
        else:
            output_mint = input_token_mint

        amount_in = int(amount_in_human * (10 ** decimals))
        new_amount_in = int(amount_in * TRADE_PERCENTAGE)

        quote_url = (
            f"https://quote-api.jup.ag/v6/quote?inputMint={input_token_mint}"
            f"&outputMint={output_mint}"
            f"&amount={new_amount_in}"
            f"&swapMode=ExactIn"
            f"&slippageBps=50"
        )
        print(f"DEBUG: Quote URL: {quote_url}")

        quote_response = await http_client.get(quote_url, timeout=10.0)
        quote_response.raise_for_status()
        quote_data = quote_response.json()

        print(f"DEBUG: Quote Data: {quote_data}")

        routes = quote_data.get('data')
        if not routes or not isinstance(routes, list):
            print("ERROR: No valid routes found in Jupiter API response.")
            return [], {"from_account": None, "to_account": None, "input_token_mint": None, "output_token_mint": None}

        best_route = routes[0]
        print(f"DEBUG: Selected route: {best_route}")

        swap_url = "https://quote-api.jup.ag/v6/swap"
        swap_payload = {
            "quoteResponse": best_route,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
        }
        print(f"DEBUG: Swap Payload: {swap_payload}")
        swap_response = await http_client.post(swap_url, json=swap_payload, timeout=10.0)
        swap_response.raise_for_status()
        swap_data = swap_response.json()
        print(f"DEBUG: Swap Data: {swap_data}")

        swap_transaction_bytes = base64.b64decode(swap_data['swapTransaction'])
        swap_transaction = Transaction.from_bytes(swap_transaction_bytes)

        instructions = []
        for ix in swap_transaction.message.instructions:
            accounts = []
            for account_key in ix.account_keys:
                is_signer = False
                is_writable = False
                if account_key == wallet.pubkey():
                    is_signer = True
                    is_writable = True

                accounts.append(AccountMeta(pubkey=account_key, is_signer=is_signer, is_writable=is_writable))
            instructions.append(Instruction(program_id = ix.program_id, data = bytes(ix.data), accounts=accounts))

        account_info_dict = {
            "from_account": from_account,
            "to_account": to_account,
            "input_token_mint": input_token_mint,
            "amount_in": new_amount_in / (10**decimals),
            "output_token_mint": output_mint,
        }
        return instructions, account_info_dict

    except httpx.HTTPError as e:
        print(f"HTTP error in handle_jupiter_v4: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response text: {e.response.text}")
        traceback.print_exc()
        return [], {"from_account": None, "to_account": None, "input_token_mint": None, 'output_token_mint': None}

    except (ValueError, IndexError, struct.error, KeyError) as e:
        print(f"Error in handle_jupiter_v4: {e}")
        traceback.print_exc()
        return [], {"from_account": None, "to_account": None, "input_token_mint": None, 'output_token_mint': None}

    except Exception as e:
        print(f"Unexpected error in handle_jupiter_v4: {e}")
        traceback.print_exc()
        return [], {"from_account": None, "to_account": None, "input_token_mint": None, 'output_token_mint': None}

async def monitor_trades(app):
    await asyncio.gather(listen_to_helius(app))

async def listen_to_helius(app):
    url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    while is_monitoring_active():  # Use the getter function
        try:
            async with websockets.connect(url) as ws:
                await subscribe_to_logs(ws, COPY_WALLET_ADDRESS)
                await send_telegram_message(
                    app,
                    "✅ Subscribed to Helius WebSocket for logs mentioning: "
                    + escape_markdown_v2(COPY_WALLET_ADDRESS),
                    markdown=True,
                )
                async for message in ws:
                    if not is_monitoring_active():  # Use the getter
                        break
                    data = json.loads(message)
                    if "result" in data:
                        continue

                    params = data.get("params", {})
                    result = params.get("result", {})
                    log_entry = result.get("value", {})

                    if log_entry:
                        await process_transaction(app, log_entry)

        except websockets.exceptions.ConnectionClosedError as e:
            await send_telegram_message(
                app,
                f"⚠️ WebSocket connection closed: `{escape_markdown_v2(str(e))}`. Reconnecting...",
                markdown=True
            )
        except Exception as e:
            await send_telegram_message(
                app,
                f"⚠️ WebSocket error: `{escape_markdown_v2(str(e))}`",
                markdown=True,
            )
        finally:
            if is_monitoring_active():  # Use the getter
                await asyncio.sleep(5)


    
async def execute_trade(app, amounts: dict, recipient: str, wallet: Keypair, trade_type: str, account_info: dict, instructions: list, signature:str):
    """Executes a trade based on the copied transaction."""
    rpc_url = config.RPC_ENDPOINT  # Use the config
    tx_signature = None  # Initialize tx_signature
    async with httpx.AsyncClient() as http_client:
        try:

            if not instructions:
                print("No instructions returned from handler.")
                return False

            # --- Construct "What Would Have Happened" Message ---
            preview_message = "ℹ️ **Trade Preview** (What would have happened):\n\n"
            if account_info: #Check if exists
              if account_info['input_token_mint'] == "So11111111111111111111111111111111111111112":
                token_mint_in = "SOL"
              else:
                token_mint_in = account_info["input_token_mint"]

              if account_info['output_token_mint'] == "So11111111111111111111111111111111111111112":
                token_mint_out = "SOL"
              else:
                token_mint_out = account_info["output_token_mint"]
              preview_message += f" * **DEX:** {account_info.get('dex_name', 'Unknown DEX')}\n"  # Use get()
              preview_message += f" * **Action:** {trade_type}\n"
              preview_message += f" * **Amount In:** {account_info.get('amount_in', 'N/A'):.8f} {token_mint_in}\n"
              preview_message += f" * **From:** {account_info['from_account']}\n"
              preview_message += f" * **To:** {account_info['to_account']}\n"
              preview_message += f" * **Output Token Mint**: {token_mint_out}\n"

            await send_telegram_message(app, preview_message, markdown=True)

            # --- Get recent blockhash ---
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
            response = await http_client.post(rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
            recent_blockhash = Hash.from_string(data["result"]["value"]["blockhash"])
            print(f"DEBUG: execute_trade - Recent Blockhash: {recent_blockhash}")

            # --- Create the transaction ---
            message = Message(instructions)  # Correct: Pass instructions directly
            transaction = Transaction([wallet], message, recent_blockhash) # Correct: fee_payer, instructions, recent_blockhash.
            transaction.sign([wallet],recent_blockhash) # Correct: Sign with a list containing the Keypair

            # --- Send and Confirm Transaction ---
            serialized_tx = base64.b64encode(transaction.serialize()).decode("ascii")
            print(f"DEBUG: execute_trade - Serialized Transaction: {serialized_tx}")  # Helpful for debugging
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [serialized_tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
            }
            response = await http_client.post(rpc_url, json=payload)
            response.raise_for_status() #Correct.
            data = response.json()
            print(f"DEBUG: execute_trade - sendTransaction Response: {data}") # Helpful for debugging


            if "error" in data:
                error_msg = data["error"].get("message", "Unknown RPC error")
                await send_telegram_message(app, f"❌ Transaction Failed: {error_msg}", markdown=True)
                return False

            if "result" not in data:
                await send_telegram_message(app, f"❌ Transaction Failed: No result in RPC response.", markdown=True)
                return False


            tx_signature = data["result"]  #Get tx signature.
            trade_action = "Bought" if trade_type == "BUY" else "Sold" #For telegram message
            await send_telegram_message(app, f"⏳ {trade_action} Transaction: `{tx_signature}`", markdown=True)



            # --- Check balance after trade (Optional) ---
            updated_balance = await get_wallet_balance(http_client, wallet.pubkey(), rpc_url)
            if updated_balance is not None:
                await send_telegram_message(app, f"💰 Updated balance: `{updated_balance:.6f}` SOL", markdown=True)

            # --- Confirm the transaction ---
            confirmed = await confirm_transaction(http_client, tx_signature, rpc_url)
            if confirmed:
                await send_telegram_message(app, "✅ Transaction Confirmed!", markdown=True)
                # --- Position Tracking (Open/Close) ---
                if account_info.get('dex_name') == 'raydium_clmm': # Only for CLMM
                    #Check to understand if it is open position or not.
                    if any(ix.program_id == RAYDIUM_CLMM_PROGRAM_ID and ix.data[:8] == bytes.fromhex("da58ea10799568b2") for ix in instructions):
                        # OpenPosition: Add NFT mint to BOT_POSITIONS
                        # Find the CreateAccount instruction for the NFT mint (index 2 in OpenPosition accounts)
                        mint_address = str(instructions[0].accounts[2].pubkey)  #Correct
                        if str(wallet.pubkey()) not in BOT_POSITIONS:
                            BOT_POSITIONS[str(wallet.pubkey())] = []
                        BOT_POSITIONS[str(wallet.pubkey())].append(mint_address)
                        print(f"DEBUG: Added position NFT {mint_address} to tracking.")
                        await send_telegram_message(app, f"➕ Added position NFT to tracking: `{mint_address}`", markdown=True)
                        await save_bot_positions() # ADDED: Save positions
                    #Check if close position:
                    elif any(ix.program_id == RAYDIUM_CLMM_PROGRAM_ID and ix.data[:8] == bytes.fromhex("4bd71044f9eb6490") for ix in instructions):
                      #ClosePosition: Remove NFT mint from BOT_POSITIONS
                      nft_mint = str(instructions[0].accounts[1].pubkey) #Correct
                      if str(wallet.pubkey()) in BOT_POSITIONS and nft_mint in BOT_POSITIONS[str(wallet.pubkey())]:
                          BOT_POSITIONS[str(wallet.pubkey())].remove(nft_mint)
                          print(f"DEBUG: Removed position NFT {nft_mint} from tracking.")
                          await send_telegram_message(app, f"➖ Removed position NFT from tracking: `{nft_mint}`", markdown=True)
                          await save_bot_positions() # ADDED: Save positions
                return True  #Transaction successful.

            else:
                await send_telegram_message(app, f"❌ Transaction Failed to Confirm! Check explorer: https://explorer.solana.com/tx/{tx_signature}", markdown=True)
                return False


        except httpx.HTTPStatusError as e:
            await send_telegram_message(app, f"❌ HTTP Error: {e.response.status_code} - {e.response.text}", markdown=True)
            traceback.print_exc()
            return False
        except httpx.ConnectError as e:
            await send_telegram_message(app, f"❌ Connection Error: {e}", markdown=True)
            traceback.print_exc()
            return False
        except httpx.ReadTimeout as e:
            await send_telegram_message(app, f"❌ Read Timeout: {e}", markdown=True)
            traceback.print_exc()
            return False

        except Exception as e:
            await send_telegram_message(app, f"❌ Trade Failed! Error: `{e}`", markdown=True)
            traceback.print_exc()
            return False
    
async def main():
    # Mock app object for testing - you may have a real app object in your actual application
    class MockApp:
        def __init__(self):
            pass
    app = MockApp()

    await load_processed_transactions()
    await load_bot_positions()  # Load bot positions

    await monitor_trades(app) # Start the main loop

async def process_inner_instructions(app, inner_instructions, wallet, trade_type, http_client, account_info_dict):
    """Recursively processes inner instructions."""
    instructions_list = []
    # No need to re-initialize account_info, we receive it.

    for inner_instruction in inner_instructions:
        # Determine the handler based on programId *within* the inner instruction
        program_id = inner_instruction.get('programId')
        handler = None
        # if program_id == str(RAYDIUM_AMM_PROGRAM_ID): #Removed, we check inside clmm handler.
        #     handler = handle_raydium_amm # We handle inside clmm
        if program_id == str(RAYDIUM_CLMM_PROGRAM_ID):  # Added CLMM
            handler = handle_raydium_clmm
        # Critically, there is no `else`. We skip unsupported inner instructions.

        if handler:  # Make sure a handler is defined.
            # Pass the ENTIRE inner_instruction, not wrapped
            handler_result = await handler(app, inner_instruction, wallet, trade_type, http_client, account_info_dict)
            if handler_result:
                inner_instructions_list, _ = handler_result  # We already have account_info
                instructions_list.extend(inner_instructions_list)  # Add the inner instructions

        # Recursively process *nested* inner instructions.
        if inner_instruction.get("innerInstructions"):
            nested_result = await process_inner_instructions(app, inner_instruction["innerInstructions"], wallet, trade_type, http_client,  account_info_dict) # Pass existing account_info, REMOVE handler
            if nested_result:
                nested_instructions, _ = nested_result # We already have account_info
                instructions_list.extend(nested_instructions)

    return instructions_list, account_info_dict  # Return the original account_info_dict

# --- Instruction Data Classes (for CLMM and AMM) ---

class OpenPositionInstructionData:
    """Represents the data for the OpenPosition instruction."""
    def __init__(self, discriminator: bytes, tick_lower_index: int, tick_upper_index: int, liquidity: int, amount_0_max: int, amount_1_max: int):
        self.discriminator = discriminator
        self.tick_lower_index = tick_lower_index
        self.tick_upper_index = tick_upper_index
        self.liquidity = liquidity
        self.amount_0_max = amount_0_max
        self.amount_1_max = amount_1_max

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<iiQQQ",
                                                self.tick_lower_index,
                                                self.tick_upper_index,
                                                self.liquidity,
                                                self.amount_0_max,
                                                self.amount_1_max)

class ClosePositionInstructionData:
    """Represents the data for the ClosePosition instruction."""
    def __init__(self, discriminator: bytes):
        self.discriminator = discriminator

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator

class IncreaseLiquidityInstructionData:
    """Represents the data for the IncreaseLiquidity instruction."""
    def __init__(self, discriminator: bytes, liquidity: int, amount_0_max: int, amount_1_max: int):
        self.discriminator = discriminator
        self.liquidity = liquidity
        self.amount_0_max = amount_0_max
        self.amount_1_max = amount_1_max

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<QQQ", self.liquidity, self.amount_0_max, self.amount_1_max)

class DecreaseLiquidityInstructionData:
    """Represents the data for the DecreaseLiquidity instruction."""
    def __init__(self, discriminator: bytes, liquidity: int, amount_0_min: int, amount_1_min: int):
        self.discriminator = discriminator
        self.liquidity = liquidity
        self.amount_0_min = amount_0_min
        self.amount_1_min = amount_1_min

    def serialize(self) -> bytes:
        """Serializes the instruction data into bytes."""
        return self.discriminator + struct.pack("<QQQ", self.liquidity, self.amount_0_min, self.amount_1_min)

class SwapBaseInInstructionData: #for amm
    def __init__(self, discriminator: bytes, amount_in: int, min_amount_out: int):
        self.discriminator = discriminator
        self.amount_in = amount_in
        self.min_amount_out = min_amount_out
    def serialize(self) -> bytes:
        return self.discriminator + struct.pack("<QQ", self.amount_in, self.min_amount_out)

class SwapBaseOutInstructionData: #For amm
    def __init__(self, discriminator: bytes, max_amount_in: int, amount_out: int):
        self.discriminator = discriminator
        self.max_amount_in = max_amount_in
        self.amount_out = amount_out

    def serialize(self) -> bytes:
        return self.discriminator + struct.pack("<QQ", self.max_amount_in, self.amount_out)

def calculate_tick_array_start_index(tick_index: int, tick_spacing: int) -> int:
    """Calculates the tick array start index.  This is pool specific

    Args:
        tick_index: The tick index.
        tick_spacing: The tick spacing of the pool.

    Returns:
        The starting index of the tick array that contains the given tick.
    """
    return (tick_index // (88 * tick_spacing)) * (88 * tick_spacing)

async def handle_clmm_open_position(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> Tuple[List[Instruction] | None, Dict]:
    """Handles the OpenPosition CLMM instruction."""
    print("DEBUG: Inside handle_clmm_open_position")
    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        tick_lower_index = struct.unpack("<i", data[8:12])[0]
        tick_upper_index = struct.unpack("<i", data[12:16])[0]
        liquidity = struct.unpack("<Q", data[16:24])[0]
        amount_0_max = struct.unpack("<Q", data[24:32])[0]
        amount_1_max = struct.unpack("<Q", data[32:40])[0]
        pool_state_account_info = await http_client.get_account_info(Pubkey.from_string(instruction_data.get("accounts", [])[5]))
        if pool_state_account_info.value is None:
            print(f"Pool state account not found")
            return [], None
        pool_state_data = pool_state_account_info.value.data
        tick_spacing = struct.unpack("<H", pool_state_data[104:106])[0]
        tick_array_lower_start_index = calculate_tick_array_start_index(tick_lower_index, tick_spacing)
        tick_array_upper_start_index = calculate_tick_array_start_index(tick_upper_index, tick_spacing)

        new_liquidity = int(liquidity * TRADE_PERCENTAGE)

        if liquidity > 0:
            original_ratio_0 = amount_0_max / liquidity
            original_ratio_1 = amount_1_max / liquidity
        else:
            print("ERROR: Original liquidity is zero in OpenPosition!")
            return [], None

        new_amount_0_max = int(new_liquidity * original_ratio_0 * 1.005)
        new_amount_1_max = int(new_liquidity * original_ratio_1 * 1.005)

        instruction_data_obj = OpenPositionInstructionData(discriminator, tick_lower_index, tick_upper_index, new_liquidity, new_amount_0_max, new_amount_1_max)
        modified_data = instruction_data_obj.serialize()

        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_owner = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # payer
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True
            elif i == 1: # position_nft_owner
                position_nft_owner = pubkey #We dont modifiy.
            elif i == 2: # position_nft_mint (create)
                bot_nft_mint = await create_mint(http_client, wallet, wallet.pubkey()) #Create the mint.
                if not bot_nft_mint:
                    return [], None
                pubkey = bot_nft_mint.pubkey()
                is_writable = True
            elif i == 3:   #position_nft_account (create ATA)
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), pubkey)  # Use *new* mint, and bot wallet
                is_writable = True
            elif i == 5: # pool_state
                pass #Readonly
            elif i == 6: # protocol_position (PDA)
                pool_state_key = dex_instruction_accounts[5] #index 5 is pool
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED, bytes(Pubkey.from_string(pool_state_key)), tick_lower_index.to_bytes(4, byteorder='little', signed=True),tick_upper_index.to_bytes(4, byteorder='little', signed=True)], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True
            elif i == 7 or i == 8: #tick_array_lower and tick_array_upper
                pool_state_key = dex_instruction_accounts[5] #index 5 is pool.
                if i == 7: #lower
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED, bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 8: #upper.
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED, bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True

            elif i == 9: #personal_position (PDA)
                nft_mint_key = dex_instruction_accounts[2] #nft mint is index 2.
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED, bytes(Pubkey.from_string(nft_mint_key))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True

            elif i == 10:  # token_account_0 (bot's token A account)
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client) #Create if not exists.
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey() #Native sol
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True
            elif i == 11:  # token_account_1 (bot's token B account)
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                is_writable = True
            elif i in (12, 13):  # token_vault_0 and token_vault_1 (pool vaults)
                is_writable = True

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))


        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data=modified_data
        )

        instructions = [instruction] # clmm instruction.
        # Handle Inner Instructions (AMM Swaps)
        if instruction_data.get("innerInstructions"):
            inner_instructions, _ = await process_inner_instructions(app, instruction_data["innerInstructions"], wallet, trade_type, http_client, account_info)
            instructions =  inner_instructions + instructions #amm instructions first.
        return instructions, account_info

    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_open_position: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_open_position: {e}")
        traceback.print_exc()
        return [], None

async def handle_clmm_close_position(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the ClosePosition CLMM instruction."""
    print("DEBUG: Inside handle_clmm_close_position")

    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        instruction_data_obj = ClosePositionInstructionData(discriminator)  # Only the discriminator is needed
        modified_data = instruction_data_obj.serialize()

        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None # We will get it
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()  # Bot's wallet is the new owner
                is_signer = True
                is_writable = True
            elif i == 1:  # position_nft_mint
                #This is the NFT mint we close, so get it.
                position_nft_mint_str = account_str
                is_writable = True
            elif i == 2:  # position_nft_account, bot's position nft account
                # We have created when opening position.
                if not position_nft_mint_str: #double check
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str) # Convert
                # Get the bot's associated token account for the NFT mint
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint) #bot ata and nft mint.
                pubkey = bot_nft_ata
                is_writable = True

            elif i == 3: # personal_position
                if not position_nft_mint_str: # Double check
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True # It will be closed, so, writable

            else:
                # Remaining accounts, we keep how they are, false, false
                pass
            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        # --- Verify Position Ownership --- (Before creating the instruction)
        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot close.")
            await send_telegram_message(app, f"❌ Cannot close position: Bot does not own NFT {position_nft_mint_str}.")
            return [], None  # Don't proceed if the bot doesn't own the position


        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data= modified_data
        )

        return [instruction], account_info #return instruction.

    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_close_position: {e}")
        traceback.print_exc()
        return [], None  # Return empty instruction list on error
    except Exception as e:
        print(f"Unexpected error in handle_clmm_close_position: {e}")
        traceback.print_exc()
        return [], None  # Return empty instruction list on error
async def handle_clmm_increase_liquidity(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the IncreaseLiquidity CLMM instruction."""
    print("DEBUG: Inside handle_clmm_increase_liquidity")
    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        liquidity = struct.unpack("<Q", data[8:16])[0]
        amount_0_max = struct.unpack("<Q", data[16:24])[0]
        amount_1_max = struct.unpack("<Q", data[24:32])[0]

        # Modify amounts.
        new_liquidity = int(liquidity * TRADE_PERCENTAGE)
        new_amount_0_max = int(amount_0_max * TRADE_PERCENTAGE * 1.005)  # Add 0.5% slippage
        new_amount_1_max = int(amount_1_max * TRADE_PERCENTAGE * 1.005)
        instruction_data_obj = IncreaseLiquidityInstructionData(discriminator, new_liquidity, new_amount_0_max, new_amount_1_max) # Pass to class.
        modified_data = instruction_data_obj.serialize() # Convert to bytes.


        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()
                is_signer = True
                is_writable = True
            elif i == 1:  # nft_account
                # We need to get nft mint from this.
                position_nft_mint_str = str((await http_client.get_account_info(pubkey)).value.mint)

                # Get the bot's associated token account for the NFT mint
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str)
                # Get the bot's associated token account for the NFT mint
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint)
                pubkey = bot_nft_ata
                is_writable = True

            elif i == 2: #pool state
                pass
            elif i == 3: # protocol_position
                #Find pda.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[2] #Pool is 2
                personal_position_key = dex_instruction_accounts[4] #personal position is 4
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45] #Get lower and upper indexes from personal position data.
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49] # 4 byte signed integer.
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_lower_index,tick_upper_index], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True

            elif i == 4: #personal_position
                # Find PDA.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True
            elif i == 5 or i == 6: #tick_array_lower and tick_array_upper
                #Find the pda.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[2] #Pool is 2
                personal_position_key = dex_instruction_accounts[4] #personal position is 4
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                tick_spacing = (await http_client.get_account_info(pool_state_key)).value.data[104:106] #2 byte, tick spacing
                tick_array_lower_start_index = calculate_tick_array_start_index(int.from_bytes(tick_lower_index, byteorder='little', signed=True), int.from_bytes(tick_spacing, byteorder='little', signed=False))
                tick_array_upper_start_index = calculate_tick_array_start_index(int.from_bytes(tick_upper_index, byteorder='little', signed=True),  int.from_bytes(tick_spacing, byteorder='little', signed=False))

                if i == 5: #Lower
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 6: #Upper:
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True
            elif i == 7:  # token_account_0 (user's token A account)
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()  # Replace with bot's wallet (for native SOL)
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True
            elif i == 8:  # token_account_1 (user's token B account)
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                is_writable = True
            elif i in (9, 10):  # token_vault_0 and token_vault_1 (pool vaults)
                is_writable = True  # Vaults are always writable

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        # --- Verify Position Ownership ---
        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot increase liquidity.")
            await send_telegram_message(app, f"❌ Cannot increase liquidity: Bot does not own NFT {position_nft_mint_str}.")
            return [], None
        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data= modified_data
        )
        instructions = [instruction]

        # --- Handle Inner Instructions (AMM Swaps) ---
        # If there are inner AMM swap instructions, process them *before* the main instruction
        if instruction_data.get("innerInstructions"):
            inner_instructions, _ = await process_inner_instructions(app, instruction_data["innerInstructions"], wallet, trade_type, http_client, account_info)
            instructions = inner_instructions + instructions


        return instructions, account_info
    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_increase_liquidity: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_increase_liquidity: {e}")
        traceback.print_exc()
        return [], None

async def handle_clmm_decrease_liquidity(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles the DecreaseLiquidity CLMM instruction."""
    print("DEBUG: Inside handle_clmm_decrease_liquidity")

    try:
        data = base64_decode_with_padding(instruction_data.get('data'))
        discriminator = data[:8]
        liquidity = struct.unpack("<Q", data[8:16])[0]
        amount_0_min = struct.unpack("<Q", data[16:24])[0]
        amount_1_min = struct.unpack("<Q", data[24:32])[0]

        new_liquidity = int(liquidity * TRADE_PERCENTAGE)
        new_amount_0_min = int(amount_0_min * TRADE_PERCENTAGE * 0.995)  # 0.5% tolerance
        new_amount_1_min = int(amount_1_min * TRADE_PERCENTAGE * 0.995)  # 0.5% tolerance

        instruction_data_obj = DecreaseLiquidityInstructionData(discriminator, new_liquidity, new_amount_0_min, new_amount_1_min)
        modified_data = instruction_data_obj.serialize()


        dex_instruction_accounts = instruction_data.get("accounts", [])
        accounts = []
        position_nft_mint_str = None
        for i, account_str in enumerate(dex_instruction_accounts):
            pubkey = Pubkey.from_string(account_str)
            is_signer = False
            is_writable = False

            if i == 0:  # nft_owner
                pubkey = wallet.pubkey()  # Bot's wallet is the new owner
                is_signer = True
                is_writable = True
            elif i == 1:  # nft_account, bot's nft account.
                #We need to get nft mint from this.
                position_nft_mint_str = str((await http_client.get_account_info(pubkey)).value.mint)
                # Get the bot's associated token account for the NFT mint
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                position_nft_mint = Pubkey.from_string(position_nft_mint_str)
                # Get the bot's associated token account for the NFT mint
                bot_nft_ata = await get_associated_token_address(wallet.pubkey(), position_nft_mint)
                pubkey = bot_nft_ata
                is_writable = True
            elif i == 2: # personal_position
                # Find PDA.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                personal_position , _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(position_nft_mint_str))], RAYDIUM_CLMM_PROGRAM_ID)
                pubkey = personal_position
                is_writable = True
            elif i == 3: #pool state
                pass  # Readonly
            elif i == 4: # protocol_position
                #Find pda.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[3] #Pool is 3
                personal_position_key = dex_instruction_accounts[2] #personal position is 2
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45] #Get lower and upper indexes from personal position data.
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49] # 4 byte signed integer.
                protocol_position, _ = Pubkey.find_program_address([POSITION_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_lower_index,tick_upper_index], RAYDIUM_CLMM_PROGRAM_ID,)
                pubkey = protocol_position
                is_writable = True

            elif i == 5 or i == 6:  # token_vault_0 and token_vault_1 (pool vaults)
                is_writable = True  # Vaults are always writable
            elif i == 7 or i == 8: #tick_array_lower and tick_array_upper
                #Find the pda.
                if not position_nft_mint_str:
                    print("Position nft mint not found.")
                    return [], None
                pool_state_key = dex_instruction_accounts[3] #Pool is 3
                personal_position_key = dex_instruction_accounts[2] #personal position is 2
                tick_lower_index = (await http_client.get_account_info(personal_position_key)).value.data[41:45]
                tick_upper_index = (await http_client.get_account_info(personal_position_key)).value.data[45:49]
                tick_spacing = (await http_client.get_account_info(pool_state_key)).value.data[104:106] #2 byte, tick spacing
                tick_array_lower_start_index = calculate_tick_array_start_index(int.from_bytes(tick_lower_index, byteorder='little', signed=True), int.from_bytes(tick_spacing, byteorder='little', signed=False))
                tick_array_upper_start_index = calculate_tick_array_start_index(int.from_bytes(tick_upper_index, byteorder='little', signed=True),  int.from_bytes(tick_spacing, byteorder='little', signed=False))

                if i == 7: #Lower
                    tick_array_lower_start_index_bytes = tick_array_lower_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_lower, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_lower_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_lower
                if i == 8: #Upper:
                    tick_array_upper_start_index_bytes = tick_array_upper_start_index.to_bytes(4, byteorder='little', signed=True)
                    tick_array_upper, _ = Pubkey.find_program_address([TICK_ARRAY_SEED.encode(), bytes(Pubkey.from_string(pool_state_key)), tick_array_upper_start_index_bytes], RAYDIUM_CLMM_PROGRAM_ID)
                    pubkey = tick_array_upper
                is_writable = True
            elif i == 9:  # recipient_token_account_0 (user's token A account)
                await check_and_create_ata(wallet, account_info["output_token_mint"], http_client) #We are receiving, so output.
                if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()  # Replace with bot's wallet (for native SOL)
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"])) # Bot receives, so, output.
                is_writable = True
            elif i == 10:  # recipient_token_account_1 (user's token B account)
                await check_and_create_ata(wallet, account_info["input_token_mint"], http_client) #we are receciving so input.
                if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                    pubkey = wallet.pubkey()
                else:
                    pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                is_writable = True

            accounts.append(AccountMeta(pubkey, is_signer, is_writable))

        # --- Verify Position Ownership ---
        if position_nft_mint_str not in BOT_POSITIONS.get(str(wallet.pubkey()), []):
            print(f"ERROR: Bot does not own position NFT {position_nft_mint_str}. Cannot decrease liquidity.")
            await send_telegram_message(app, f"❌ Cannot decrease liquidity: Bot does not own NFT {position_nft_mint_str}.")
            return [], None


        instruction = Instruction(
            program_id=RAYDIUM_CLMM_PROGRAM_ID,
            accounts=accounts,
            data=modified_data,
        )

        #No inner instructions.

        return [instruction], account_info


    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_clmm_decrease_liquidity: {e}")
        traceback.print_exc()
        return [], None
    except Exception as e:
        print(f"Unexpected error in handle_clmm_decrease_liquidity: {e}")
        traceback.print_exc()
        return [], None
async def handle_raydium_amm(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles Raydium AMM swaps, supporting both swap_base_in and swap_base_out."""
    print("DEBUG: Inside handle_raydium_amm")

    try:
        if instruction_data.get('programId') == str(RAYDIUM_AMM_PROGRAM_ID):
            print("DEBUG: Found Raydium AMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]  # Full 8-byte discriminator
            discriminator_hex = discriminator.hex()

            if discriminator_hex == "5416a48589ca4b5f":  # swap_base_in
                print("DEBUG: Found swap_base_in instruction")

                try:
                    # Data structure for swap_base_in (from Raydium SDK and source)
                    # instruction: u8 (discriminator)
                    # amount_in: u64
                    # min_amount_out: u64
                    amount_in = struct.unpack("<Q", data[8:16])[0]  # Correct offset after discriminator
                    min_amount_out = struct.unpack("<Q", data[16:24])[0]
                except struct.error:
                    print("ERROR: Raydium AMM swap_base_in instruction data is too short.")
                    return [], None

                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                original_amount_in = int(amount_in)
                original_amount_out = int(min_amount_out)

                if original_amount_in > 0:
                    expected_output_ratio = original_amount_out / original_amount_in
                else:
                    expected_output_ratio = 0

                new_expected_output = int(new_amount_in * expected_output_ratio)
                new_min_amount_out = int(new_expected_output * 0.995)  # 0.5% slippage


                modified_data = bytearray(data)
                modified_data[8:16] = struct.pack("<Q", new_amount_in)  # Correct offset
                modified_data[16:24] = struct.pack("<Q", new_min_amount_out) # Correct offset
                # Use InstructionData class
                instruction_data_obj = SwapBaseInInstructionData(discriminator, new_amount_in, new_min_amount_out)
                modified_data = instruction_data_obj.serialize()

            elif discriminator_hex == "f25f7d241885dd76":  # swap_base_out
                print("DEBUG: Found swap_base_out instruction")

                try:
                    # Data structure for swap_base_out (from Raydium SDK and source):
                    # instruction: u8 (discriminator)
                    # max_amount_in: u64
                    # amount_out: u64
                    max_amount_in = struct.unpack("<Q", data[8:16])[0]
                    amount_out = struct.unpack("<Q", data[16:24])[0]

                except struct.error:
                    print("ERROR: Raydium AMM swap_base_out instruction data is too short.")
                    return [], None
                #amount in is amount_out in this case.
                amount_in = int(amount_out * (10 ** account_info["decimals"]))
                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                new_max_amount_in = int(new_amount_in * 1.005)  # Allow up to 0.5% more input

                modified_data = bytearray(data)
                modified_data[8:16] = struct.pack("<Q", new_max_amount_in) # Correct Offset
                modified_data[16:24] = struct.pack("<Q", new_amount_in)  #Use calculated not copied

                # Use InstructionData class
                instruction_data_obj = SwapBaseOutInstructionData(discriminator, new_max_amount_in, new_amount_in)
                modified_data = instruction_data_obj.serialize()
            else:
                print(f"DEBUG: Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                return [], None

            # --- Account Handling (Common to both swap types) ---
            accounts = []
            dex_instruction_accounts = instruction_data.get("accounts", [])

            for i, account_str in enumerate(dex_instruction_accounts):
                pubkey = Pubkey.from_string(account_str)
                is_signer = False
                is_writable = False

                if account_str == COPY_WALLET_ADDRESS:
                    pubkey = wallet.pubkey()
                    is_signer = True
                    is_writable = True

                elif account_str == account_info["from_account"]:
                    print(f"DEBUG: Creating/Checking ATA for input token: {account_info['input_token_mint']}")
                    await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                    if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                    is_writable = True

                elif account_str == account_info["to_account"]:
                    print(f"DEBUG: Creating/Checking ATA for output token: {account_info['output_token_mint']}")
                    await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                    if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                    is_writable = True

                elif i in (1, 2, 3, 6, 7):  # Correct writable account indices for AMM
                    is_writable = True

                accounts.append(AccountMeta(pubkey, is_signer, is_writable))

            instruction = Instruction(program_id=RAYDIUM_AMM_PROGRAM_ID, accounts=accounts, data=bytes(modified_data))
            return [instruction], account_info

    except Exception as e:
        print(f"Error in handle_raydium_amm: {e}")
        traceback.print_exc()
        return [], None

async def handle_raydium_clmm(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles Raydium CLMM instructions, dispatching to specific handlers."""
    print("DEBUG: Inside handle_raydium_clmm")

    # --- Raydium CLMM Discriminators (From previous calculations) ---
    CLMM_DISCRIMINATORS = {
        "4f5917759e1421b7": "create_pool",
        "da58ea10799568b2": "open_position",
        "d85e1d3d0469799c": "open_position_v2",
        "d8c747194d7b9b6e": "open_position_with_token22_nft",
        "4bd71044f9eb6490": "close_position",
        "e441f24085726917": "increase_liquidity",
        "97fe9a236d189432": "increase_liquidity_v2",
        "c4750e39b889c540": "decrease_liquidity",
        "802d76d7fb28b28d": "decrease_liquidity_v2",
        "28c710985441885d": "swap",
        "4424459b7b027205": "swap_v2",
        "81b623c1c849f74d": "swap_router_base_in",  # Added
        "06e1a439579b45b8": "update_reward_info",
        "a6b2b3f89629c8b4": "initialize_reward",
        "8b84801d5a976ea9": "set_reward_params",
        "7a45a2b07e6bca19": "collect_remaining_rewards",
        "808e54b16f951b4c": "admin",  # Could be multiple admin instructions
    }

    # --- Raydium AMM Discriminators (From previous calculations) ---
    AMM_DISCRIMINATORS = {  # These are for the *inner* AMM instructions
        "41888098e941d738": "initialize",
        "d7d1e53979557c5b": "initialize2",
        "f0c5b7c4259696d4": "pre_initialize",
        "65b165056fd051cf": "monitor_step",
        "ccb810d5d2591357": "deposit",
        "9104e2c36895db72": "withdraw",
        "6bd781d3c5f3f234": "set_params",
        "3d9e44f879b9e6a2": "withdraw_srm",
        "5416a48589ca4b5f": "swap_base_in",  # AMM swap
        "f25f7d241885dd76": "swap_base_out",  # AMM swap
        "26fa468b681cd25b": "simulate_get_pool_info",
        "a8b549087793e4d5": "simulate_swap_base_in",
        "5f649b6293c21f16": "simulate_swap_base_out",
        "53f3b35d910a3120": "simulate_run_crank",
        "1b7395576fd96b55": "admin_cancel_orders",
        "b22fdc78d9536cad": "create_config_account",
        "68ff6c91814a1f57": "update_config_account",
        "7c6bb28406ab1ffc": "config",
    }
    try:
        instructions_list = []

        if instruction_data.get('programId') == str(RAYDIUM_CLMM_PROGRAM_ID):
            print("DEBUG: Found Raydium CLMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]
            discriminator_hex = discriminator.hex()

            clmm_instruction_name = CLMM_DISCRIMINATORS.get(discriminator_hex)

            if clmm_instruction_name:
                print(f"DEBUG: CLMM Instruction: {clmm_instruction_name}")
                await send_telegram_message(app, f"ℹ️ Raydium CLMM Instruction: {clmm_instruction_name}")

                if clmm_instruction_name == "open_position":
                    instructions, updated_account_info = await handle_clmm_open_position(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                         account_info.update(updated_account_info)
                    return instructions_list, account_info # Return after handling

                elif clmm_instruction_name == "close_position":
                    instructions, updated_account_info = await handle_clmm_close_position(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                         account_info.update(updated_account_info)
                    return instructions_list, account_info # Return after handling

                elif clmm_instruction_name == "increase_liquidity":
                    instructions, updated_account_info = await handle_clmm_increase_liquidity(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                         account_info.update(updated_account_info)
                    return instructions_list, account_info # Return after handling

                elif clmm_instruction_name == "decrease_liquidity":
                    instructions, updated_account_info = await handle_clmm_decrease_liquidity(app, instruction_data, wallet, trade_type, http_client, account_info)
                    if instructions:
                        instructions_list.extend(instructions)
                    if updated_account_info:
                        account_info.update(updated_account_info)
                    return instructions_list, account_info # Return after handling
                elif clmm_instruction_name == 'swap':
                    await send_telegram_message(app, f"CLMM swap instruction not implemented")
                    return [], None
                else:
                    await send_telegram_message(app, f"⏩ Skipping Raydium CLMM instruction {clmm_instruction_name} ({discriminator_hex}).  Not yet supported.")
                    return [], None

            else:
                # await send_telegram_message(app, f"⏩ Skipping Raydium CLMM instruction with discriminator {discriminator_hex}.  An ADMIN request cannot be copied .")
                return [], None

        # Handle AMM instructions *within* the CLMM handler (inner instructions)
        elif instruction_data.get('programId') == str(RAYDIUM_AMM_PROGRAM_ID):
            print("DEBUG: Found Raydium AMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]
            discriminator_hex = discriminator.hex()
            amm_instruction_name = AMM_DISCRIMINATORS.get(discriminator_hex)
            if amm_instruction_name:
              print(f"DEBUG: AMM Instruction: {amm_instruction_name}")
              await send_telegram_message(app, f"ℹ️ Raydium AMM Instruction: {amm_instruction_name}") #telegram message

              if amm_instruction_name == "swap_base_in" or amm_instruction_name == "swap_base_out":
                instructions, updated_account_info = await handle_raydium_amm(app, instruction_data, wallet, trade_type, http_client, account_info)
                if instructions:
                    instructions_list.extend(instructions)
                if updated_account_info:
                    account_info.update(updated_account_info)  # Keep updating
                return instructions_list, account_info # Return after handling

              else: #If isn't swap
                await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction {amm_instruction_name} ({discriminator_hex}).  Not yet supported.")
                return [], None #Skip
            else: # Unrecognized AMM instruction
              await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not recognized.")
              return [], None #Skip
        else: #if isn't clmm, nor amm, we are not interested.
          return [], None



    except (ValueError, IndexError, struct.error) as e:
        print(f"Error in handle_raydium_clmm: {e}")
        traceback.print_exc()
        return [], None  # Return empty list and None on error
    except Exception as e:
        print(f"Unexpected error in handle_raydium_clmm: {e}")
        traceback.print_exc()
        return [], None  # Return empty list and None on error
async def handle_raydium_amm(app, instruction_data, wallet: Keypair, trade_type: str, http_client: httpx.AsyncClient, account_info: dict) -> tuple[list[Instruction] | None, dict]:
    """Handles Raydium AMM swaps, supporting both swap_base_in and swap_base_out."""
    print("DEBUG: Inside handle_raydium_amm")

    try:
        if instruction_data.get('programId') == str(RAYDIUM_AMM_PROGRAM_ID):
            print("DEBUG: Found Raydium AMM instruction.")
            data = base64_decode_with_padding(instruction_data.get('data'))
            discriminator = data[:8]  # Full 8-byte discriminator
            discriminator_hex = discriminator.hex()

            if discriminator_hex == "5416a48589ca4b5f":  # swap_base_in
                print("DEBUG: Found swap_base_in instruction")

                try:
                    # Data structure for swap_base_in (from Raydium SDK and source)
                    # instruction: u8 (discriminator)
                    # amount_in: u64
                    # min_amount_out: u64
                    amount_in = struct.unpack("<Q", data[8:16])[0]  # Correct offset after discriminator
                    min_amount_out = struct.unpack("<Q", data[16:24])[0]
                except struct.error:
                    print("ERROR: Raydium AMM swap_base_in instruction data is too short.")
                    return [], None

                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                original_amount_in = int(amount_in)
                original_amount_out = int(min_amount_out)

                if original_amount_in > 0:
                    expected_output_ratio = original_amount_out / original_amount_in
                else:
                    expected_output_ratio = 0

                new_expected_output = int(new_amount_in * expected_output_ratio)
                new_min_amount_out = int(new_expected_output * 0.995)  # 0.5% slippage


                modified_data = bytearray(data)
                modified_data[8:16] = struct.pack("<Q", new_amount_in)  # Correct offset
                modified_data[16:24] = struct.pack("<Q", new_min_amount_out) # Correct offset
                # Use InstructionData class
                instruction_data_obj = SwapBaseInInstructionData(discriminator, new_amount_in, new_min_amount_out)
                modified_data = instruction_data_obj.serialize()

            elif discriminator_hex == "f25f7d241885dd76":  # swap_base_out
                print("DEBUG: Found swap_base_out instruction")

                try:
                    # Data structure for swap_base_out (from Raydium SDK and source):
                    # instruction: u8 (discriminator)
                    # max_amount_in: u64
                    # amount_out: u64
                    max_amount_in = struct.unpack("<Q", data[8:16])[0]
                    amount_out = struct.unpack("<Q", data[16:24])[0]

                except struct.error:
                    print("ERROR: Raydium AMM swap_base_out instruction data is too short.")
                    return [], None
                #amount in is amount_out in this case.
                amount_in = int(amount_out * (10 ** account_info["decimals"]))
                new_amount_in = int(amount_in * TRADE_PERCENTAGE)
                new_max_amount_in = int(new_amount_in * 1.005)  # Allow up to 0.5% more input

                modified_data = bytearray(data)
                modified_data[8:16] = struct.pack("<Q", new_max_amount_in) # Correct Offset
                modified_data[16:24] = struct.pack("<Q", new_amount_in)  #Use calculated not copied

                # Use InstructionData class
                instruction_data_obj = SwapBaseOutInstructionData(discriminator, new_max_amount_in, new_amount_in)
                modified_data = instruction_data_obj.serialize()
            else:
                print(f"DEBUG: Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                await send_telegram_message(app, f"⏩ Skipping Raydium AMM instruction with discriminator {discriminator_hex}. Not a supported swap.")
                return [], None

            # --- Account Handling (Common to both swap types) ---
            accounts = []
            dex_instruction_accounts = instruction_data.get("accounts", [])

            for i, account_str in enumerate(dex_instruction_accounts):
                pubkey = Pubkey.from_string(account_str)
                is_signer = False
                is_writable = False

                if account_str == COPY_WALLET_ADDRESS:
                    pubkey = wallet.pubkey()
                    is_signer = True
                    is_writable = True

                elif account_str == account_info["from_account"]:
                    print(f"DEBUG: Creating/Checking ATA for input token: {account_info['input_token_mint']}")
                    await check_and_create_ata(wallet, account_info["input_token_mint"], http_client)
                    if account_info["input_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["input_token_mint"]))
                    is_writable = True

                elif account_str == account_info["to_account"]:
                    print(f"DEBUG: Creating/Checking ATA for output token: {account_info['output_token_mint']}")
                    await check_and_create_ata(wallet, account_info["output_token_mint"], http_client)
                    if account_info["output_token_mint"] == "So11111111111111111111111111111111111111112":
                        pubkey = wallet.pubkey()
                    else:
                        pubkey = await get_associated_token_address(wallet.pubkey(), Pubkey.from_string(account_info["output_token_mint"]))
                    is_writable = True

                elif i in (1, 2, 3, 6, 7):  # Correct writable account indices for AMM
                    is_writable = True

                accounts.append(AccountMeta(pubkey, is_signer, is_writable))

            instruction = Instruction(program_id=RAYDIUM_AMM_PROGRAM_ID, accounts=accounts, data=bytes(modified_data))
            return [instruction], account_info

    except Exception as e:
        print(f"Error in handle_raydium_amm: {e}")
        traceback.print_exc()
        return [], None
      
def is_monitoring_active():
    """Getter function for the monitoring state."""
    global _monitoring
    return _monitoring

def set_monitoring(value: bool):
    """Setter function for the monitoring state."""
    global _monitoring
    _monitoring = value
    
async def reset_monitor():
    """Resets the state of the monitor."""
    global processed_tx_hashes, monitor_task, helius_task
    if 'processed_tx_hashes' not in globals():
        global processed_tx_hashes
        processed_tx_hashes = set()
    else:
        processed_tx_hashes.clear()
    print("Monitoring state reset.")
    
################################################################################################################    
async def extract_trade_data(app, txn):
    print("Extracting trade data...")
    try:
        signature = txn["signature"]
        timestamp = txn.get("timestamp", 0)
        trade_time = (
            datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S UTC")
            if timestamp
            else "Unknown"
        )

        trades = []
        decimals = {}  # Track decimals, keyed by mint

        # Find the entry for COPY_WALLET_ADDRESS in accountData
        copy_wallet_data = None
        for account_info in txn.get("accountData", []):
            if account_info.get("account") == COPY_WALLET_ADDRESS:
                copy_wallet_data = account_info
                break

        if copy_wallet_data is None:
            print(f"COPY_WALLET_ADDRESS ({COPY_WALLET_ADDRESS}) not found in accountData. Skipping.")
            return []

        # --- 1. Handle SOL Balance Change ---
        sol_balance_change = copy_wallet_data.get("nativeBalanceChange", 0) / 1e9  # Convert lamports to SOL
        if sol_balance_change != 0:
          sol_amount = abs(sol_balance_change)
          if sol_balance_change > 0:
              trade_type = "SELL"
          elif sol_balance_change < 0:
              trade_type = "BUY"

          decimals["So11111111111111111111111111111111111111112"] = 9
          trades.append({
              "amounts": {"So11111111111111111111111111111111111111112": sol_amount},
              "trade_type": trade_type,
              "trade_time": trade_time,
              "decimals": decimals
          })

        # --- 2. Handle Token Balance Changes ---
        for token_change in copy_wallet_data.get("tokenBalanceChanges", []):
            mint = token_change.get("mint")
            if not mint:
                print("Token change entry missing mint. Skipping.")
                continue

            token_decimals = await fetch_token_decimals_helius(mint)
            if token_decimals is None:
                print(f"Could not fetch decimals for mint: {mint}. Skipping.")
                continue
            decimals[mint] = token_decimals

            raw_amount = int(token_change.get("rawTokenAmount", {}).get("tokenAmount", 0))

            # Determine BUY/SELL based on the *token* balance change
            if raw_amount > 0:
                trade_type = "BUY"
            elif raw_amount < 0:
                trade_type = "SELL"
            else:
                continue #Zero change

            #Use sol_balance_change.
            amount = abs(sol_balance_change)

            trades.append({
                "amounts": {mint: amount},  # SOL equivalent
                "trade_type": trade_type,
                "trade_time": trade_time,
                "decimals": decimals,
            })

        print(f"Extracted Trades: {trades}")
        return trades

    except Exception as e:
        print(f"Error in extract_trade_data: {e}")
        traceback.print_exc()
        return None
    
    

async def fetch_token_decimals_helius(token_mint):
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    headers={"Content-Type":"application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [token_mint]
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url,headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if "result" in data and "value" in data["result"] and "decimals" in data["result"]["value"]:
                        return data["result"]["value"]["decimals"]
                    else:
                        print(f"[DEBUG] Helius API response did not contain decimals for {token_mint}: {data}")
                else:
                    print(f"[DEBUG] Helius API error {response.status} for {token_mint}")
        except Exception as e:
            print(f"[DEBUG] Error fetching decimals from Helius: {e}")
    return None

async def process_transaction(app, txn_details):
    signature = txn_details.get("signature")
    if not signature or signature in PROCESSED_TRANSACTIONS:
        return

    txn_details = await fetch_transaction_details_helius(signature)
    if not txn_details:
        await send_telegram_message(app, f"❌ Could not fetch details for Tx `{signature}`.", markdown=True)
        return
    if txn_details.get('type') not in ('SWAP', 'TRANSFER', 'UNKNOWN'):  # Consider any of these types.
        return

    involved = False
    for transfer in txn_details.get("tokenTransfers", []):
        if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS or transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
            involved = True
            break

    if not involved:
        for transfer in txn_details.get("nativeTransfers", []):
            if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS or transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                involved = True
                break
    if not involved:
        return

    trade_type = None
    toaccountitsasell = None  # Initialize to None
    fromaccountitsabuy = None

    for transfer in txn_details.get("tokenTransfers", []):
        if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
            toaccountitsasell = transfer.get("toUserAccount")
            trade_type = "SELL"
            break
        elif transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
            fromaccountitsabuy = transfer.get("fromUserAccount")
            trade_type = "BUY"
            break

    if trade_type is None:
        for transfer in txn_details.get("nativeTransfers", []):
            if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                toaccountitsasell = transfer.get("toUserAccount")
                trade_type = "SELL"
                break
            elif transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                fromaccountitsabuy = transfer.get("fromUserAccount")
                trade_type = "BUY"
                break

    trade_amount_to_mirror = 0  # Initialize
    token_mint = "So11111111111111111111111111111111111111112"  # Default to SOL.
    token_decimals = 9

    if trade_type == "SELL":
        for transfer in txn_details.get("tokenTransfers", []):
            if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                trade_amount_to_mirror = float(transfer.get("rawTokenAmount", 0)) / (10 ** transfer.get("decimals", 9))
                token_mint = transfer.get("mint")  # Get the correct mint
                token_decimals = transfer.get("decimals", 9)  # for token transfers.
                break
        if trade_type is not None:  #Native Sol Check for SELL
            for transfer in txn_details.get("nativeTransfers", []):
               if transfer.get("fromUserAccount") == COPY_WALLET_ADDRESS:
                    trade_amount_to_mirror = float(transfer.get("amount", 0)) / (10**9)  # SOL decimals
                    break #Break here since we found the correct one.

    elif trade_type == "BUY":
        for transfer in txn_details.get("tokenTransfers", []):
            if transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                trade_amount_to_mirror = float(transfer.get("rawTokenAmount", 0)) / (10 ** transfer.get("decimals", 9))
                token_mint = transfer.get("mint")
                token_decimals = transfer.get("decimals", 9)
                break
        if trade_type is not None: #Native Check for BUY
            for transfer in txn_details.get("nativeTransfers", []):
                if transfer.get("toUserAccount") == COPY_WALLET_ADDRESS:
                    trade_amount_to_mirror = float(transfer.get("amount", 0)) / (10**9)
                    break



    # ... (rest of your process_transaction code before calling execute_trade_v0) ...

    trade_details = await extract_trade_data(app, txn_details)  # Always extract trade details.
    if not trade_details:
        print(f"No trade details extracted for Tx `{signature}`.")
        # We will proceed to instruction determination regardless

    # Telegram message for detected trade (combined into a single message)
    trade_msg = f"🚀 Trade Detected! Signature: `{signature}`\n"

    # Handle missing trade_type
    if trade_type is None:
        trade_msg += "⚠️ Could not determine trade type (BUY/SELL).\n"

    if trade_details:  # Only add details if they were successfully extracted
        for trade in trade_details:
            for mint, amount in trade["amounts"].items():
                trade_msg += f"📌 Type: `{(trade.get('trade_type', 'UNKNOWN'))}`\n" \
                             f"💰 Amount: `{amount}`\n" \
                             f"🪙 Token: `{mint}`\n" \
                             f"⏳ Time: `{trade.get('trade_time', 'N/A')}`\n\n"
    else:  # No trade details. Still construct a basic message
        trade_msg += "⚠️ Could not extract detailed trade information.\n"

    await send_telegram_message(app, trade_msg, markdown=True)


    async with httpx.AsyncClient() as http_client:
        for private_key_hex in WALLETS:
            keypair = load_keypair(private_key_hex)
            if keypair:
                # ALWAYS attempt to get instructions, even if trade_details is None
                instructions, account_info = await determine_trade_instructions(app, txn_details, keypair, trade_type, http_client)  # Pass trade_type.

                if instructions:
                    # If we have instructions, execute the trade, REPLACING accounts.
                    await execute_trade(app, {}, str(keypair.pubkey()), keypair, trade_type, account_info, instructions, signature)  # Pass account_info
                else:
                    # --- MIRRORING LOGIC (Simplified) ---
                    print(trade_amount_to_mirror)

                    if trade_type == "BUY":
                        # We BUY *from* the same address they bought from.  Pass decimals.
                        await execute_trade_v0(app, {token_mint: amount}, fromaccountitsabuy, keypair, "BUY", token_decimals)
                    elif trade_type == "SELL":
                        # We SELL *to* the same address they sold to. Pass decimals.
                        await execute_trade_v0(app, {token_mint: amount}, toaccountitsasell, keypair, "SELL", token_decimals)
                    else:
                        print(f"No instructions and could not determine BUY/SELL for mirroring.")

    PROCESSED_TRANSACTIONS.add(signature)
    await save_processed_transactions()

async def execute_trade_v0(app, amounts, recipient, wallet, trade_type, decimals):
    """
    Executes a trade (buy or sell) on the Solana blockchain.
    """
    try:
        trade_action = "Bought" if trade_type == "BUY" else "Sold"
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

        try:
            recipient_pubkey = Pubkey.from_string(recipient)
            wallet_pubkey = wallet.pubkey()
            print(f"execute_trade: Recipient Pubkey: {recipient_pubkey}")
            print(f"execute_trade: Wallet Pubkey: {wallet_pubkey}")
        except Exception as e:
            await send_telegram_message(app, f"❌ Invalid recipient or wallet address: {e}", markdown=True)
            return False

        async with httpx.AsyncClient() as http_client:
            # --- Fetch wallet balance ---
            try:
                print(f"execute_trade: Fetching balance for: {wallet_pubkey}")
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [str(wallet_pubkey)],
                }
                response = await http_client.post(rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()
                print(f"execute_trade: Balance Response: {data}")

                if "error" in data:
                    error_message = data["error"].get("message", "Unknown RPC error")
                    await send_telegram_message(app, f"⚠️ Failed to fetch balance (RPC): {error_message}", markdown=True)

                current_balance = data['result']['value'] / 10**9
                print(f"Wallet Balance for {wallet_pubkey}: {current_balance} SOL")

            except Exception as e:
                print(f"Balance check failed (not critical): {e}")

            # --- Fetch latest blockhash ---
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getLatestBlockhash",
                    "params": [{"commitment": "finalized"}],
                }
                response = await http_client.post(rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()

                if "error" in data:
                    error_message = data["error"].get("message", "Unknown RPC error")
                    await send_telegram_message(app, f"❌ Failed to get latest blockhash (RPC): {error_message}", markdown=True)
                    return False

                recent_blockhash_str = data["result"]["value"]["blockhash"]
                print(f"execute_trade: Recent Blockhash: {recent_blockhash_str}")

                recent_blockhash = Hash.from_string(recent_blockhash_str)

            except Exception as e:
                print(f"Blockhash fetch failed (not critical): {e}")
                return False

            # --- Process each trade ---
            transaction = Transaction()
            for mint, amount in amounts.items():
                trade_amount = amount * TRADE_PERCENTAGE

                # Calculate lamports
                if mint == "So11111111111111111111111111111111111111112":  # SOL
                    required_lamports = int(trade_amount * (10 ** 9))
                    if trade_amount < MINIMUM_TRADE_AMOUNT:
                        await send_telegram_message(app, f"⚠️ Trade amount too small: {trade_amount:.6f} SOL (minimum is {MINIMUM_TRADE_AMOUNT} SOL)", markdown=True)
                        continue

                    from solders.system_program import transfer, TransferParams
                    instruction = transfer(
                        TransferParams(
                            from_pubkey=wallet_pubkey,
                            to_pubkey=recipient_pubkey,
                            lamports=required_lamports,
                        )
                    )
                else:  # SPL Token
                    required_lamports = int(trade_amount * (10 ** decimals))
                    if required_lamports == 0:
                        await send_telegram_message(app, f"⚠️ Trade amount too small for: {mint}", markdown=True)
                        continue

                    from_token_account = Pubkey.find_program_address(
                        [bytes(wallet.pubkey()), bytes(Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")), bytes(Pubkey.from_string(mint))],
                        Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
                    )[0]

                    to_token_account = Pubkey.find_program_address(
                        [bytes(Pubkey.from_string(recipient)), bytes(Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")), bytes(Pubkey.from_string(mint))],
                        Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
                    )[0]

                    from spl.token.instructions import transfer, TransferParams
                    instruction = transfer(
                        TransferParams(
                            program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                            source=from_token_account,
                            dest=to_token_account,
                            owner=wallet_pubkey,
                            amount=required_lamports,
                            signers=[],
                        )
                    )

                transaction.add(instruction)

            transaction.recent_blockhash = recent_blockhash
            transaction.sign(wallet)

            # --- Send Transaction ---
            try:
                encoded_tx = base58.b58encode(bytes(transaction)).decode("utf-8")

                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [encoded_tx, {"encoding": "base58", "preflightCommitment": "confirmed"}],
                }
                response = await http_client.post(rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()
                print(f"execute_trade: Transaction Response: {data}")

                if "error" in data:
                    error_message = data["error"].get("message", "Unknown RPC error")
                    await send_telegram_message(app, f"❌ Transaction Failed (RPC)!: {error_message}", markdown=True)
                    return False

                tx_signature = data["result"]

                await send_telegram_message(
                    app,
                    f"⏳ {trade_action} `{trade_amount:.6f}` SOL (or equivalent token amount). Transaction submitted: `{tx_signature}`. Waiting for confirmation...",
                    markdown=True,
                )

                await asyncio.sleep(5)
                confirmed = await confirm_transaction(http_client, tx_signature, rpc_url)
                if confirmed:
                    await send_telegram_message(app, f"✅ Transaction Confirmed!", markdown=True)
                else:
                    await send_telegram_message(app, f"❌ Transaction Failed to Confirm! Check explorer: https://explorer.solana.com/tx/{tx_signature}", markdown=True)

                return True

            except Exception as e:
                print(f"Transaction sending failed: {e}")
                await send_telegram_message(app, f"❌ Transaction Failed: {type(e).__name__} - {e}", markdown=True)
                return False

    except Exception as e:
        error_message = f"Unexpected error during trade execution: {type(e).__name__} - {e}\nTraceback:\n{traceback.format_exc()}"
        await send_telegram_message(app, f"❌ Trade Failed! Unexpected Error: `{error_message}`", markdown=True)
        return False
