import json
import logging
from typing import Dict, Any
from datetime import datetime, timezone

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

from config import (
    genai_version, sessions_coll, orders_coll, payos, SYSTEM_PROMPT, logger
)
from services import call_gemini_with_history, process_checkout

try:
    from payos.types import ItemData
except Exception:
    ItemData = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command - initialize user session."""
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

    await update.message.reply_text("Chào con! Cô là chủ quán. Cô có thể giúp gì cho con hôm nay? Con có thể hỏi về menu hoặc đặt trà nhé.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages - process chat and call Gemini."""
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
    """Handle location sharing."""
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
    """Handle delivery description and finalize order."""
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
    if ItemData:
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
