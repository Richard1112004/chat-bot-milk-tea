import os
import logging
from typing import List, Optional
import pandas as pd

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
    from payos import PayOS
    from payos.types import ItemData, CreatePaymentLinkRequest as PaymentData
    payos_available = True
except Exception:
    payos_available = False
    logging.warning("payos library not installed; payment functionality will be disabled")

import motor.motor_asyncio

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
