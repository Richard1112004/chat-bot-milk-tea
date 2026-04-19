import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime

import pandas as pd
# Try new google.genai first, fall back to deprecated google.generativeai
genai_version = None
genai = None
try:
    import google.genai
    from google.genai import types
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

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not set in environment; LLM calls will likely fail")
if not MONGODB_URI:
    logger.warning("MONGODB_URI not set in environment; MongoDB features will be disabled")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set in environment")
    raise SystemExit("TELEGRAM_TOKEN environment variable is required to start the bot. Set it in your environment or a .env file.")

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

# Defer model initialization until after GEMINI_FUNCTIONS is defined
model = None

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
        if genai_version == "new":
            # New google.genai uses client-based approach; store model name for later use
            model = "gemini-3-flash-preview"
        else:
            # Old google.generativeai: use GenerativeModel directly
            model = genai.GenerativeModel(model="gemini-3-flash-preview", tools=GEMINI_FUNCTIONS)
    except Exception:
        model = None
        logger.info("Model initialization failed; will use fallback chat approach")


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
    For the new google-genai SDK (2026+):
    - Filters out 'system' role messages
    - Maps 'assistant' role to 'model' role
    - Transforms messages into types.Content objects
    - Uses system_instruction and tools in the config
    """
    def _call_with_old_api():
        # Old google.generativeai API
        if genai_version == "old" and isinstance(model, type(genai.GenerativeModel)):
            chat = model.start_chat(system_instruction=SYSTEM_PROMPT)
            for m in history:
                role = m.get('role', 'user')
                content = m.get('content', '')
                if role == 'user':
                    chat.send_message(content=content)
            return chat.get_response()
        else:
            # Fallback to genai.chat.create for old API
            return genai.chat.create(
                model="gemini-3-flash-preview",
                messages=history,
                tools=GEMINI_FUNCTIONS,
                temperature=0.2,
            )

    def _call_with_new_api():
        """Call the new google-genai SDK with proper message transformation."""
        if genai_client is None:
            raise RuntimeError("genai_client not initialized")
        
        # Transform history: filter out system messages and map assistant -> model
        transformed_contents = []
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
            transformed_contents.append(msg_content)
        
        # Define tool with JSON schema format (compatible with new SDK)
        calculate_checkout_tool = types.Tool(
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
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[calculate_checkout_tool],
            temperature=0.2,
        )
        
        # Call the API
        response = genai_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=transformed_contents,
            config=config,
        )
        
        return response

    if genai_version == "new" and genai_client is not None:
        return await asyncio.to_thread(_call_with_new_api)
    elif genai_version == "old" and genai is not None:
        return await asyncio.to_thread(_call_with_old_api)
    else:
        raise RuntimeError("No valid genai client available")


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

    order_doc = {
        "telegram_id": chat_id,
        "items": cart,
        "total_price": total_price,
        "lat": lat,
        "lon": lon,
        "address": address,  # Include address if provided
        "description": description,
        "status": "unpaid",
        "created_at": datetime.utcnow(),
    }

    try:
        await orders_coll.insert_one(order_doc)
        # Clear session: remove cart, reset status and chat_history
        await sessions_coll.delete_one({"telegram_id": chat_id})
    except Exception:
        logger.exception("Failed to finalize order into orders collection")
        await update.message.reply_text("Lỗi khi lưu đơn. Vui lòng thử lại sau.")
        return

    # Send final mock payment link and summary
    payment_link = "https://payos.demo/123"
    summary_lines = [f"Đơn hàng đã được ghi nhận. Tổng: {total_price:.2f} VND", f"Link thanh toán: {payment_link}"]
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
