import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

import pandas as pd
import re
# Try new google.genai first, fall back to deprecated google.generativeai
genai_version = None
genai = None
try:
    import google.genai
    from google.genai import types
    from google.genai import errors as genai_errors
    genai = google.genai
    genai_version = "new"
except Exception:
    try:
        import google.generativeai
        genai = google.generativeai
        genai_version = "old"
        logging.warning("google.generativeai is deprecated; please install google-genai when available")
    except Exception:
        genai = None
        genai_version = None
        logging.warning("No Google GenAI client available; LLM calls will be disabled")

# PayOS imports
try:
    from payos import PayOS; from payos.types import ItemData, CreatePaymentLinkRequest as PaymentData
    payos_available = True
except Exception:
    payos_available = False
    logging.warning("payos library not installed; payment functionality will be disabled")

import motor.motor_asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables required:
# - GEMINI_API_KEY
# - TELEGRAM_TOKEN
# - MONGODB_URI
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    logger.info("python-dotenv not installed or no .env file; skipping .env load")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

# PayOS configuration
PAYOS_CLIENT_ID = os.getenv("PAYOS_CLIENT_ID")
PAYOS_API_KEY = os.getenv("PAYOS_API_KEY")
PAYOS_CHECKSUM_KEY = os.getenv("PAYOS_CHECKSUM_KEY")

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not set in environment; LLM calls will likely fail")
if not MONGODB_URI:
    logger.warning("MONGODB_URI not set in environment; MongoDB features will be disabled")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set in environment")
    raise SystemExit("TELEGRAM_TOKEN environment variable is required to start the bot. Set it in your environment or a .env file.")

# Initialize PayOS client
payos = None
if payos_available and PAYOS_CLIENT_ID and PAYOS_API_KEY and PAYOS_CHECKSUM_KEY:
    try:
        payos = PayOS(client_id=PAYOS_CLIENT_ID, api_key=PAYOS_API_KEY, checksum_key=PAYOS_CHECKSUM_KEY)
        logger.info("PayOS client initialized successfully")
    except Exception:
        logger.exception("Failed to initialize PayOS client")
        payos = None
else:
    if not payos_available:
        logger.warning("payos library not available; payment functionality disabled")
    elif not all([PAYOS_CLIENT_ID, PAYOS_API_KEY, PAYOS_CHECKSUM_KEY]):
        logger.warning("PayOS credentials not fully configured; payment functionality disabled")

# Configure genai client if available and GEMINI_API_KEY provided
genai_client = None
if genai is not None and GEMINI_API_KEY:
    try:
        if genai_version == "new":
            # New google.genai uses client-based API
            genai_client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            # Old google.generativeai uses configure
            genai.configure(api_key=GEMINI_API_KEY)
    except Exception:
        logger.exception("Failed to configure genai client")
        genai_client = None

# Free models fallback list for handling quota exhaustion (429 errors)
FREE_MODELS = [
    'gemini-3.1-flash-lite-preview',
    'gemini-3-flash-preview',
    'gemini-2.5-pro',
    'gemini-2.5-flash'
]

# Defer model initialization until after GEMINI_FUNCTIONS is defined
model = None
model_name = None
# Global DB client (Motor) and collections
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client.casso_milktea if mongo_client is not None else None
sessions_coll = db.sessions if db is not None else None
orders_coll = db.orders if db is not None else None

# Load Menu.csv on startup using pandas and convert to a clean string for the system prompt
MENU_CSV_PATH = "Menu.csv"
try:
    menu_df = pd.read_csv(MENU_CSV_PATH, dtype=str)
except Exception as e:
    logger.error(f"Failed to read {MENU_CSV_PATH}: {e}")
    menu_df = pd.DataFrame(columns=["category", "item_id", "name", "description", "price_m", "price_l", "available"])


def build_menu_text(df: pd.DataFrame) -> str:
    lines: List[str] = []
    for _, row in df.iterrows():
        lines.append(
            f"Category: {row.get('category','')}; ID: {row.get('item_id','')}; Name: {row.get('name','')}"
            + f"; Desc: {row.get('description','')}" 
            + f"; Price M: {row.get('price_m','')}; Price L: {row.get('price_l','')}; Available: {row.get('available','')}")
    return "\n".join(lines)


MENU_TEXT = build_menu_text(menu_df)

# System prompt describing the bot persona and explicit rules
SYSTEM_PROMPT = f"""
You are "Cô chủ quán", a friendly middle-aged Vietnamese female shop owner for a milk tea shop.
- Always be polite, warm, and use conversational Vietnamese when appropriate.
- You MUST consult the provided menu (below) when answering questions about items, prices, availability, or descriptions.
- When a customer places an order, ask for: item (by `item_id` or name), size (M or L), and any notes (ice level, sugar level).
- Do NOT compute or present the final total price yourself in chat. Instead, when the user explicitly finishes ordering and asks to checkout, CALL the provided tool `calculate_and_checkout` to compute the accurate bill.
- The tool `calculate_and_checkout` will accept a JSON argument `items` which is an array of objects with fields: `item_id`, `size` ("M" or "L"), `quantity` (integer), and `note` (string).
- After the tool runs, present the resulting receipt and the payment link returned by the tool to the user.

Menu data (consult this for availability and unit prices):
{MENU_TEXT}
"""


# --- Gemini function (tool) definition that the model can call ---
# We define `calculate_and_checkout` as a function the model can call when user finishes ordering.
GEMINI_FUNCTIONS = [
    {
        "name": "calculate_and_checkout",
        "description": "Calculate exact total price and return a detailed bill and a mock payment link. Trigger only when the user finishes their order.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string"},
                            "size": {"type": "string", "enum": ["M", "L"]},
                            "quantity": {"type": "integer", "minimum": 1},
                            "note": {"type": "string"}
                        },
                        "required": ["item_id", "size", "quantity"]
                    }
                }
            },
            "required": ["items"]
        }
    }
]

# Initialize model now that GEMINI_FUNCTIONS exists
if genai is not None and GEMINI_API_KEY:
    try:
        # Build preferred model list, allow override via env GEMINI_MODEL
        preferred = []
        env_model = os.getenv("GEMINI_MODEL")
        if env_model:
            preferred.append(env_model)
            logger.info(f"Using GEMINI_MODEL env override: {env_model}")
        preferred.extend(["gemini-2.0-flash", "gemini-1.5-flash", "gemini-pro", "gemini-pro-vision"])

        available_models = []
        model_name = None
        
        # Try to list available models
        if genai_client is not None:
            try:
                logger.info("Attempting to list available Gemini models...")
                models_resp = genai_client.models.list()
                for m in models_resp:
                    try:
                        name = m.name if hasattr(m, 'name') else str(m)
                        # Clean up model name format (e.g., "models/gemini-pro" -> "gemini-pro")
                        if name.startswith("models/"):
                            name = name.replace("models/", "")
                        available_models.append(name)
                        logger.info(f"  Available model: {name}")
                    except Exception:
                        pass
            except Exception as e:
                logger.info(f"Could not list models with new SDK: {e}")
                available_models = []

        # Choose first preferred that exists in available, else try first preferred
        if available_models:
            for p in preferred:
                if p in available_models:
                    model_name = p
                    logger.info(f"Selected model from available: {model_name}")
                    break
        
        # If still no match, use first preferred
        if not model_name:
            model_name = preferred[0] if preferred else "gemini-pro"
            logger.info(f"Using fallback model: {model_name}")

        if genai_version == "new":
            # New google.genai uses client-based approach; store model name for later use
            model = model_name
            logger.info(f"Model initialized for new SDK: {model}")
        else:
            # Old google.generativeai: use GenerativeModel directly
            model = genai.GenerativeModel(model=model_name, tools=GEMINI_FUNCTIONS)
            logger.info(f"Model initialized for old SDK: {model_name}")
    except Exception:
        logger.exception("Model initialization failed; will use fallback chat approach")
        model = None
        model_name = None


def find_menu_row_by_id(item_id: str) -> Dict[str, Any]:
    # item_id in CSV may be non-str; compare as string
    matches = menu_df[menu_df['item_id'].astype(str) == str(item_id)]
    if matches.empty:
        return {}
    return matches.iloc[0].to_dict()


def process_checkout(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Mock checkout processor that uses the menu dataframe to compute exact totals.
    Returns a dict: {"receipt": str, "total": float}
    """
    lines: List[str] = []
    total = 0.0
    lines.append("---- HÓA ĐƠN TẠM TÍNH ----")
    for it in items:
        item_id = str(it.get("item_id"))
        size = it.get("size", "M")
        try:
            qty = int(it.get("quantity", 1))
        except Exception:
            qty = 1
        note = it.get("note", "") or "-"

        row = find_menu_row_by_id(item_id)
        if not row:
            lines.append(f"Item ID {item_id} not found; skipped.")
            continue

        name = row.get('name', 'Unknown')
        price_field = 'price_m' if size == 'M' else 'price_l'
        try:
            unit_price = float(str(row.get(price_field, '0')).replace(',', '').strip() or 0)
        except Exception:
            unit_price = 0.0

        subtotal = unit_price * qty
        total += subtotal
        lines.append(f"{name} (ID:{item_id}) - Size: {size} - Qty: {qty} - Unit: {unit_price:.2f} - Subtotal: {subtotal:.2f}")
        lines.append(f"  Note: {note}")

    lines.append(f"TOTAL: {total:.2f} VND")
    receipt = "\n".join(lines)
    return {"receipt": receipt, "total": total}


def calculate_and_checkout(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Tool wrapper intended to be callable by the model. Uses the authoritative
    pandas `menu_df` to compute exact totals and returns a dict with `receipt` and `total`.
    """
    return process_checkout(items)


# --- Function calling flow explanation (in comments) ---
# 1) The model is provided the `GEMINI_FUNCTIONS` schema above in the chat call.
# 2) When the assistant decides the user has finished ordering, it should return a function call
#    with name `calculate_and_checkout` and a JSON argument `items` (array of {item_id,size,quantity,note}).
# 3) The bot code detects the function call, parses the arguments, and executes the local
#    `process_checkout(items)` function to compute the exact totals using the authoritative menu.
# 4) Instead of finishing the order immediately, the code updates the user's session to "awaiting_location",
#    asks the user to share their GPS location, then collects a description before finalizing.


async def call_gemini_with_history(history: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Send chat history to Gemini using the appropriate API based on genai_version.
    Implements a fallback strategy for handling 429 RESOURCE_EXHAUSTED errors.
    
    For the new google-genai SDK (2026+):
    - Filters out 'system' role messages
    - Maps 'assistant' role to 'model' role
    - Transforms messages into types.Content objects
    - Uses system_instruction and tools in the config
    - Iterates through FREE_MODELS list on 429 errors
    """
    if genai_version != "new" or genai_client is None:
        raise RuntimeError("call_gemini_with_history requires the new google-genai SDK")
    
    # Transform history: filter out system messages and map assistant -> model
    def _transform_history():
        transformed = []
        for msg in history:
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            
            # Skip system messages - they go into system_instruction
            if role == 'system':
                continue
            
            # Map 'assistant' to 'model' for Gemini API
            if role == 'assistant':
                role = 'model'
            
            # Create types.Content with text wrapped in types.Part
            msg_content = types.Content(
                role=role,
                parts=[types.Part.from_text(text=content)]
            )
            transformed.append(msg_content)
        return transformed
    
    # Define tool with JSON schema format (compatible with new SDK)
    def _build_checkout_tool():
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="calculate_and_checkout",
                    description="Calculate exact total price and return a detailed bill and a mock payment link.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "size": {"type": "string"},
                                        "quantity": {"type": "integer"},
                                        "note": {"type": "string"}
                                    },
                                    "required": ["item_id", "size", "quantity"]
                                }
                            }
                        },
                        "required": ["items"]
                    }
                )
            ]
        )
    
    # Create config with system_instruction and tools
    def _build_config():
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[_build_checkout_tool()],
            temperature=0.2,
        )
    
    # Iterate through FREE_MODELS for 429 handling
    models_to_try = FREE_MODELS.copy()
    last_error = None
    
    for model_name in models_to_try:
        try:
            logger.info(f"Attempting Gemini call with model: {model_name}")
            
            # Transform contents and build config for each attempt
            transformed_contents = _transform_history()
            config = _build_config()
            
            # Call the API
            response = await asyncio.to_thread(
                lambda: genai_client.models.generate_content(
                    model=model_name,
                    contents=transformed_contents,
                    config=config,
                )
            )
            
            logger.info(f"Successfully received response from model: {model_name}")
            return response
            
        except Exception as e:
            last_error = e
            
            # Check if it's a ClientError with 429 status
            if hasattr(e, '__class__') and e.__class__.__name__ == 'ClientError':
                # Try to extract status code
                error_code = getattr(e, 'status_code', None)
                if error_code == 429:
                    logger.warning(f"Model {model_name} exhausted, trying next...")
                    continue
            
            # For other errors, also continue to try next model
            logger.warning(f"Model {model_name} failed with error: {str(e)}")
            continue
    
    # If all models in FREE_MODELS returned 429 or other errors
    if last_error:
        logger.error(f"All models exhausted. Last error: {str(last_error)}")
        # Return a friendly Vietnamese message asking user to wait
        return {
            "error": True,
            "message": "Cô chủ quán đang quá bận rộn. Vui lòng đợi cho đến khi hết giờ Pacific Time (nửa đêm) để thử lại. Xin lỗi vì sự bất tiện!"
        }
    
    raise RuntimeError("No genai client available or no models to try")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Initialize the session document in MongoDB for this chat_id
    try:
        if sessions_coll is not None:
            await sessions_coll.update_one(
                {"telegram_id": chat_id},
                {"$setOnInsert": {"telegram_id": chat_id, "chat_history": [{"role": "system", "content": SYSTEM_PROMPT}], "status": "ordering", "cart": []}},
                upsert=True,
            )
    except Exception:
        logger.exception("Failed to initialize session in MongoDB")

    await update.message.reply_text("Chào bạn! Mình là Cô chủ quán. Mình có thể giúp gì cho bạn hôm nay? Bạn có thể hỏi về menu hoặc đặt trà nhé.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text

    # Fetch or create session from MongoDB
    try:
        session_doc = await sessions_coll.find_one({"telegram_id": chat_id}) if sessions_coll is not None else None
    except Exception:
        logger.exception("Failed to read session from MongoDB")
        session_doc = None

    if not session_doc:
        # initialize
        session_doc = {"telegram_id": chat_id, "chat_history": [{"role": "system", "content": SYSTEM_PROMPT}], "status": "ordering", "cart": []}
        try:
            if sessions_coll is not None:
                await sessions_coll.insert_one(session_doc)
        except Exception:
            logger.exception("Failed to insert new session")

    status = session_doc.get('status', 'ordering')

    # If we are awaiting location, treat text as delivery address (for users without GPS)
    if status == "awaiting_location":
        address = text
        try:
            await sessions_coll.update_one(
                {"telegram_id": chat_id},
                {"$set": {"address": address, "status": "awaiting_description"}}
            )
        except Exception:
            logger.exception("Failed to save address to session")

        await update.message.reply_text("Cảm ơn cháu! Cháu ghi chú thêm giúp cô số nhà, tên tòa nhà hoặc số tầng để shipper dễ tìm nha.")
        return

    # If we are awaiting description, route to finalize flow
    if status == "awaiting_description":
        await handle_description_text(update, context, session_doc, text)
        return

    # Otherwise normal ordering chat: append user's message to chat_history and call Gemini
    try:
        await sessions_coll.update_one({"telegram_id": chat_id}, {"$push": {"chat_history": {"role": "user", "content": text}}})
    except Exception:
        logger.exception("Failed to append user message to chat_history")

    # Load updated history for Gemini
    try:
        session_doc = await sessions_coll.find_one({"telegram_id": chat_id})
        history = session_doc.get('chat_history', [{"role": "system", "content": SYSTEM_PROMPT}])
    except Exception:
        logger.exception("Failed to load chat_history for Gemini call")
        history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]

    # Call Gemini
    try:
        resp = await call_gemini_with_history(history)
    except Exception as e:
        logger.exception("Gemini call failed")
        text_err = str(e)
        if "429" in text_err or "ResourceExhausted" in text_err or "Resource exhausted" in text_err:
            await update.message.reply_text("Cô đang bận xíu, cháu đợi 10 giây rồi nhắn lại nhé")
            return
        await update.message.reply_text("Xin lỗi, có lỗi khi kết nối LLM.")
        return
    
    # Parse response based on SDK version
    try:
        if genai_version == "new":
            # New google-genai SDK: use response.text and response.function_calls
            
            # Check for function calls first
            if hasattr(resp, 'function_calls') and resp.function_calls:
                func_call = resp.function_calls[0] if resp.function_calls else None
                if func_call:
                    func_name = func_call.name
                    # Parse function arguments from the FunctionCall object
                    func_args = {}
                    if hasattr(func_call, 'args'):
                        func_args = dict(func_call.args)
                    
                    if func_name == 'calculate_and_checkout':
                        items = func_args.get('items', [])
                        receipt_data = process_checkout(items)

                        # Update session in MongoDB and request location
                        try:
                            await sessions_coll.update_one(
                                {"telegram_id": chat_id},
                                {"$set": {"cart": items, "total_price": receipt_data['total'], "status": "awaiting_location"}}
                            )
                            await sessions_coll.update_one(
                                {"telegram_id": chat_id},
                                {"$push": {"chat_history": {"role": "assistant", "content": receipt_data['receipt']}}}
                            )
                        except Exception:
                            logger.exception("Failed to update session with cart/total")

                        kb = ReplyKeyboardMarkup(
                            [[KeyboardButton(text="Chia sẻ vị trí", request_location=True)]],
                            one_time_keyboard=True,
                            resize_keyboard=True
                        )
                        try:
                            await update.message.reply_text(
                                f"{receipt_data['receipt']}\n\nVui lòng chia sẻ vị trí giao hàng để cô gửi shipper nhé.",
                                reply_markup=kb
                            )
                        except Exception:
                            logger.exception("Failed to send receipt or keyboard")
                    else:
                        await update.message.reply_text("Tool call requested unknown tool.")
            else:
                # No tool call; send assistant text
                assistant_text = resp.text if hasattr(resp, 'text') else None
                
                if assistant_text:
                    try:
                        await sessions_coll.update_one(
                            {"telegram_id": chat_id},
                            {"$push": {"chat_history": {"role": "assistant", "content": assistant_text}}}
                        )
                    except Exception:
                        logger.exception("Failed to append assistant reply to chat_history")
                    await update.message.reply_text(assistant_text)
                else:
                    await update.message.reply_text("Xin lỗi, không nhận được phản hồi từ model.")
        else:
            # Old google.generativeai SDK: parse legacy response format
            candidate = None
            if isinstance(resp, dict):
                candidate = resp.get('candidates', [None])[0]
            else:
                candidate = getattr(resp, 'candidates', [None])[0]

            # Try to detect a tool/function call
            tool_call = None
            if candidate:
                tool_call = candidate.get('tool_call') if isinstance(candidate, dict) else None

            if tool_call:
                func_name = tool_call.get('name')
                func_args = tool_call.get('arguments') or {}

                if func_name == 'calculate_and_checkout':
                    items = func_args.get('items', [])
                    receipt_data = process_checkout(items)

                    # Update session in MongoDB and request location
                    try:
                        await sessions_coll.update_one(
                            {"telegram_id": chat_id},
                            {"$set": {"cart": items, "total_price": receipt_data['total'], "status": "awaiting_location"}}
                        )
                        await sessions_coll.update_one(
                            {"telegram_id": chat_id},
                            {"$push": {"chat_history": {"role": "assistant", "content": None, "function_call": {"name": func_name, "arguments": json.dumps(func_args)}}}}
                        )
                        await sessions_coll.update_one(
                            {"telegram_id": chat_id},
                            {"$push": {"chat_history": {"role": "function", "name": func_name, "content": receipt_data['receipt']}}}
                        )
                    except Exception:
                        logger.exception("Failed to update session with cart/total")

                    kb = ReplyKeyboardMarkup(
                        [[KeyboardButton(text="Chia sẻ vị trí", request_location=True)]],
                        one_time_keyboard=True,
                        resize_keyboard=True
                    )
                    try:
                        await update.message.reply_text(
                            f"{receipt_data['receipt']}\n\nVui lòng chia sẻ vị trí giao hàng để cô gửi shipper nhé.",
                            reply_markup=kb
                        )
                    except Exception:
                        logger.exception("Failed to send receipt or keyboard")
                else:
                    await update.message.reply_text("Tool call requested unknown tool.")
            else:
                # No tool; send assistant text
                assistant_text = None
                if candidate:
                    if isinstance(candidate, dict):
                        assistant_text = candidate.get('content') or candidate.get('message') or None
                        if isinstance(assistant_text, list):
                            assistant_text = "\n".join([p.get('text') if isinstance(p, dict) else str(p) for p in assistant_text])
                    else:
                        assistant_text = str(candidate)

                if not assistant_text:
                    assistant_text = resp.get('output', {}).get('content', '') if isinstance(resp, dict) else None

                if assistant_text:
                    try:
                        await sessions_coll.update_one(
                            {"telegram_id": chat_id},
                            {"$push": {"chat_history": {"role": "assistant", "content": assistant_text}}}
                        )
                    except Exception:
                        logger.exception("Failed to append assistant reply to chat_history")
                    await update.message.reply_text(assistant_text)
                else:
                    await update.message.reply_text("Xin lỗi, không nhận được phản hồi từ model.")
                    
    except Exception:
        logger.exception("Failed to parse Gemini response")
        await update.message.reply_text("Xin lỗi, có lỗi khi xử lý phản hồi từ LLM.")


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Không nhận được vị trí, vui lòng thử lại.")
        return

    lat = loc.latitude
    lon = loc.longitude

    try:
        session_doc = await sessions_coll.find_one({"telegram_id": chat_id})
    except Exception:
        logger.exception("Failed to read session on location handler")
        session_doc = None

    if not session_doc:
        await update.message.reply_text("Phiên đặt hàng không tìm thấy. Hãy gửi /start để bắt đầu.")
        return

    if session_doc.get('status') != 'awaiting_location':
        await update.message.reply_text("Hiện không cần vị trí. Nếu bạn đang muốn đặt hàng, hãy bắt đầu trước.")
        return

    # Save lat/long and move to awaiting_description
    try:
        await sessions_coll.update_one({"telegram_id": chat_id}, {"$set": {"lat": lat, "lon": lon, "status": "awaiting_description"}})
    except Exception:
        logger.exception("Failed to save location to session")

    await update.message.reply_text("Cảm ơn cháu! Cháu ghi chú thêm giúp cô số nhà, tên tòa nhà hoặc số tầng để shipper dễ tìm nha.")


async def handle_description_text(update: Update, context: ContextTypes.DEFAULT_TYPE, session_doc: Dict[str, Any], text: str) -> None:
    chat_id = update.effective_chat.id
    # Save description and finalize order: move session -> orders
    description = text
    try:
        # refresh session in case updated
        session_doc = await sessions_coll.find_one({"telegram_id": chat_id})
    except Exception:
        logger.exception("Failed to refresh session before finalizing")
        session_doc = session_doc

    cart = session_doc.get('cart', [])
    total_price = session_doc.get('total_price', 0.0)
    lat = session_doc.get('lat')
    lon = session_doc.get('lon')
    address = session_doc.get('address')  # Address from text instead of GPS

    if not cart:
        await update.message.reply_text("Không tìm thấy giỏ hàng. Vui lòng đặt lại.")
        return

    # Generate unique order code using timestamp (integer, within JS safe integer range)
    order_code_int = int(datetime.now().timestamp())
    # Ensure order_code fits PayOS constraints (positive integer, <= 9007199254740991)
    MAX_SAFE = 9007199254740991
    if order_code_int <= 0:
        order_code_int = abs(order_code_int) + 1
    if order_code_int > MAX_SAFE:
        order_code_int = order_code_int % MAX_SAFE or 1
    # String form for human-visible messages
    order_code = str(order_code_int)

    # Convert total_price to integer (PayOS requirement - smallest currency unit)
    # Assuming total_price is in VND and already a whole number; cast to int
    amount = int(total_price)
    
    # Create ItemData list from cart
    items_data = []
    for cart_item in cart:
        item_id = cart_item.get("item_id", "unknown")
        size = cart_item.get("size", "M")
        quantity = cart_item.get("quantity", 1)
        note = cart_item.get("note", "")
        
        # Construct item name with details
        item_name = f"{item_id} ({size}) x{quantity}"
        if note:
            item_name += f" - {note}"
        
        # Create ItemData; price can be 0 since we have amount in PaymentData
        item_data = ItemData(
            name=item_name,
            quantity=quantity,
            price=0  # Total amount is set in PaymentData
        )
        items_data.append(item_data)
    
    # Create payment data for PayOS
    payment_data = None
    checkout_url = None
    
    if payos is not None:
        try:
            # Use new PayOS API: payment_requests.create() instead of deprecated createPaymentLink()
            # PayOS requires a short description (<=25 chars)
            desc = f"Casso #{order_code}"
            if len(desc) > 25:
                desc = desc[:25]

            payment_data = {
                "orderCode": order_code_int,
                "amount": amount,
                "description": desc,
                "items": [{"name": item.name, "quantity": item.quantity, "price": item.price} for item in items_data],
                "cancelUrl": "https://google.com",
                "returnUrl": "https://google.com",
                "buyerName": "Casso Customer",
                "buyerPhone": str(chat_id),
                "buyerEmail": "noreply@casso.vn"
            }
            
            # Create payment link using new API
            result = payos.payment_requests.create(payment_data)
            logger.info(f"PayOS response type: {type(result)}, response: {result}")
            
            # Try multiple ways to extract checkout URL
            checkout_url = None
            if isinstance(result, dict):
                checkout_url = result.get("checkoutUrl") or result.get("checkout_url") or result.get("link")
            else:
                # Try object attributes
                checkout_url = getattr(result, "checkoutUrl", None) or getattr(result, "checkout_url", None) or getattr(result, "link", None)
            
            if not checkout_url:
                logger.warning(f"PayOS did not return checkout URL for order {order_code}. Full response: {result}")
                checkout_url = None
        except Exception as e:
            logger.exception(f"Failed to create PayOS payment link for order {order_code}")
            logger.error(f"Error details: {str(e)}, Type: {type(e)}")
            checkout_url = None
    else:
        logger.warning("PayOS not available; using fallback payment link")
    
    # If PayOS failed or unavailable, use fallback
    if not checkout_url:
        checkout_url = "https://google.com"  # Fallback link
    
    # Create order document
    order_doc = {
        "telegram_id": chat_id,
        "order_code": order_code_int,
        "items": cart,
        "total_price": total_price,
        "lat": lat,
        "lon": lon,
        "address": address,  # Include address if provided
        "description": description,
        "status": "pending",  # Changed from 'unpaid' to 'pending'
        "payment_link": checkout_url,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        await orders_coll.insert_one(order_doc)
        # Clear session: remove cart, reset status and chat_history
        await sessions_coll.delete_one({"telegram_id": chat_id})
    except Exception:
        logger.exception("Failed to finalize order into orders collection")
        await update.message.reply_text("Lỗi khi lưu đơn. Vui lòng thử lại sau.")
        return

    # Format total price with comma separator for Vietnamese currency
    formatted_price = f"{int(total_price):,}".replace(",", ".")
    
    # Send final payment link and summary with Vietnamese persona
    summary_lines = [
        "Đơn hàng đã được ghi nhận! 🎉",
        f"Tổng tiền: {formatted_price} VND",
        f"\nLink thanh toán: {checkout_url}",
        "\nCảm ơn cháu đã tin tưởng cô! 💕"
    ]
    await update.message.reply_text("\n".join(summary_lines))


def main() -> None:
    # Build and run the Telegram bot
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started. Polling Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
