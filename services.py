import asyncio
import logging
from typing import Dict, Any, List

from config import (
    genai_version, genai, genai_client, menu_df, SYSTEM_PROMPT,
    FREE_MODELS, types, logger
)

# --- Function calling flow explanation (in comments) ---
# 1) The model is provided the `GEMINI_FUNCTIONS` schema above in the chat call.
# 2) When the assistant decides the user has finished ordering, it should return a function call
#    with name `calculate_and_checkout` and a JSON argument `items` (array of {item_id,size,quantity,note}).
# 3) The bot code detects the function call, parses the arguments, and executes the local
#    `process_checkout(items)` function to compute the exact totals using the authoritative menu.
# 4) Instead of finishing the order immediately, the code updates the user's session to "awaiting_location",
#    asks the user to share their GPS location, then collects a description before finalizing.


def find_menu_row_by_id(item_id: str) -> Dict[str, Any]:
    """Find menu item by ID from the loaded menu dataframe."""
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
            
            # For 503 errors, don't try next model - raise immediately
            if hasattr(e, '__class__') and e.__class__.__name__ == 'ServerError':
                error_code = getattr(e, 'status_code', None)
                if error_code == 503:
                    logger.error(f"Server error (503) from model {model_name}: {str(e)}")
                    raise
            
            # For other errors, also continue to try next model
            logger.warning(f"Model {model_name} failed with error: {str(e)}")
            continue
    
    # If all models in FREE_MODELS returned 429 or other errors, raise the last error
    if last_error:
        logger.error(f"All models exhausted. Last error: {str(last_error)}")
        raise last_error
    
    raise RuntimeError("No genai client available or no models to try")
