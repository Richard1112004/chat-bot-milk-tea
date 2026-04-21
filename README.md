# Casso Milk Tea Bot 🍵

An intelligent Telegram bot for ordering milk tea with AI-powered conversation, payment integration, and delivery tracking.

## Overview

This Telegram bot helps customers:

- Browse and inquire about milk tea menu items
- Place orders through natural conversation
- Share delivery locations
- Complete secure payments
- Track their orders

The bot uses **Google Gemini AI** to understand natural Vietnamese language, **MongoDB** to track conversations and orders, and **PayOS** for payment processing.

---

## Quick Start (5 minutes)

### Prerequisites

- Python 3.9+
- A `.env` file with your API keys (see [Configuration](#configuration) below)

### Installation

```bash
# 1. Create virtual environment
python -m venv venv
venv\Scripts\activate  # On Windows
# source venv/bin/activate  # On macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the bot
python main.py
```

That's it! The bot will start listening for Telegram messages.

---

## Configuration

### 1. Create `.env` File

In the project root directory, create a `.env` file with these keys:

```env
# Required: Telegram Bot Token
# Get from: https://t.me/botfather
TELEGRAM_TOKEN=your_telegram_bot_token_here

# Required: Google Gemini API Key
# Get from: https://aistudio.google.com/app/apikeys
GOOGLE_GENAI_API_KEY=your_gemini_api_key_here

# Required: MongoDB Connection String
# MongoDB Atlas: https://www.mongodb.com/cloud/atlas
MONGODB_URI=mongodb+srv://username:password@cluster.mongodb.net/casso?retryWrites=true&w=majority

# Optional: PayOS Payment Gateway (for checkout feature)
PAYOS_CLIENT_ID=your_payos_client_id
PAYOS_API_KEY=your_payos_api_key
PAYOS_CHECKSUM_KEY=your_payos_checksum_key

# Optional: Server Port (for cloud deployment like Render)
PORT=8080
```

### 2. Prepare Menu Data

Ensure `Menu.csv` is in the project root with this format:

```csv
item_id,name,description,price_m,price_l,category
1,Trà Sữa Tây Ba,Milk tea with brown sugar,25000,30000,Trà Sữa
2,Trà Xanh Sữa,Green milk tea,24000,29000,Trà Sữa
3,Cà Phê Sữa,Coffee with milk,22000,27000,Cà Phê
```

---

## Running the Bot

### Local Development

```bash
# Activate virtual environment first
venv\Scripts\activate  # Windows
# source venv/bin/activate  # macOS/Linux

# Run the bot
python main.py
```

**Expected output:**

```
INFO:__main__:HTTP Server listening on port 8080
INFO:__main__:Bot started. Polling Telegram...
```

### Testing in Telegram

1. Open Telegram and find your bot (created via @botfather)
2. Send `/start` to initialize
3. Type a message, e.g., "Có loại trà nào?"
4. The bot will respond with menu options

### Cloud Deployment (Render, Heroku, Railway)

The bot automatically handles cloud deployment:

1. **HTTP Server**: A background thread listens on the `PORT` environment variable (default: 8080)
2. **Health Checks**: Responds to GET requests on `/` with "Bot is running"
3. **Polling**: Continues polling Telegram in the main thread

Set these environment variables in your cloud platform:

```
TELEGRAM_TOKEN=your_token
GOOGLE_GENAI_API_KEY=your_key
MONGODB_URI=your_mongodb_uri
PORT=8080
```

---

## Project Architecture

### File Structure

```
casso-bot/
├── main.py                 # Entry point (HTTP server + bot polling)
├── config.py              # Configuration & API initialization
├── handlers.py            # Telegram message handlers
├── services.py            # Business logic (Gemini, checkout)
├── bot.py                 # Legacy utilities (if needed)
├── Menu.csv               # Menu items database
├── requirements.txt       # Python dependencies
├── REFACTORING_GUIDE.md   # Detailed code structure
└── README.md              # This file
```

### Module Responsibilities

| Module          | Purpose                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------ |
| **main.py**     | Application entry point; starts HTTP server for cloud health checks; runs Telegram polling |
| **config.py**   | Environment variables, API clients (Gemini, MongoDB, PayOS), menu loading, logging         |
| **handlers.py** | Telegram command handlers; processes user messages; handles errors                         |
| **services.py** | Gemini API integration; checkout calculations; menu lookups                                |

### How It Works

```
User sends message on Telegram
         ↓
Telegram delivers to handlers.py
         ↓
handler_text() calls services.py
         ↓
Gemini API responds with order/question
         ↓
Response saved to MongoDB & sent to user
         ↓
If checkout: PayOS payment link generated
```

---

## Features

### ✨ Conversation Management

- Natural language understanding via Google Gemini
- Session-based chat history (stored in MongoDB)
- Vietnamese support with culturally appropriate responses

### 🛒 Order Processing

- Customers can order items by chatting naturally
- Order total calculated from menu database
- Receipt generated automatically

### 📍 Delivery Tracking

- Location sharing (GPS or text-based address)
- Delivery address storage in MongoDB
- Order status management

### 💳 Payment Integration

- PayOS integration for secure checkout
- Unique order codes per transaction
- Payment link generation and tracking

### 🛡️ Error Resilience

- **429 (Rate Limit)**: Graceful user message: "Cô đang có việc bận, cháu vui lòng chờ tới ngày mai nhé"
- **503 (Server Error)**: Friendly message: "Cô đang có việc bận, cháu chờ 5p sau thử lại nhé"
- Fallback model strategy for API quota exhaustion

### 🌐 Cloud-Ready

- Built-in HTTP server for Render/Heroku health checks
- Reads `PORT` environment variable
- Daemon thread for non-blocking server operations

---

## Development Guide

### Adding a New Telegram Command

**Example: Adding `/menu` command**

1. **In `handlers.py`:**

   ```python
   async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
       """Handle /menu command - show menu items."""
       await update.message.reply_text("Menu items available...")
   ```

2. **In `main.py`:**
   ```python
   app.add_handler(CommandHandler("menu", menu_command))
   ```

### Modifying System Prompt

Edit `SYSTEM_PROMPT` in `config.py` to change how Gemini responds.

### Debugging

Enable debug logging by editing `config.py`:

```python
logging.basicConfig(level=logging.DEBUG)  # Changed from INFO
```

Check logs for:

- Gemini API requests/responses
- MongoDB operations
- HTTP server requests
- Error stack traces

---

## Troubleshooting

### "telegram.error.Conflict"

**Cause**: Telegram detects duplicate bot instances  
**Solution**: Ensure only one instance of the bot is running

### "No open ports detected" (on Render)

**Cause**: HTTP server not starting  
**Solution**: Check that PORT environment variable is set; verify HTTP server logs

### "Failed to connect to MongoDB"

**Cause**: Invalid connection string or network issue  
**Solution**:

- Verify MONGODB_URI format
- Check IP whitelist in MongoDB Atlas
- Test connection with `mongosh` CLI

### "Gemini API rate limit (429)"

**Cause**: API quota exceeded  
**Cause**: User message: "Cô đang có việc bận, cháu vui lòng chờ tới ngày mai nhé"  
**Solution**: Wait until quota resets (usually next day for free tier)

### "No such file: Menu.csv"

**Cause**: Menu.csv not in project root  
**Solution**: Create Menu.csv in project root with proper format (see Configuration)

### Bot doesn't respond to messages

**Cause**:

- TELEGRAM_TOKEN invalid
- Bot not added to Telegram chat
- MongoDB not initialized

**Solution**:

- Verify TELEGRAM_TOKEN with @botfather
- Ensure chat_id in debug logs
- Check MongoDB connection

---

## Deployment Checklist

Before deploying to production:

- [ ] All secrets in environment variables (never commit `.env`)
- [ ] `.env` added to `.gitignore`
- [ ] MongoDB connection tested
- [ ] Telegram bot token verified
- [ ] Menu.csv uploaded
- [ ] Google Gemini API key confirmed
- [ ] PORT environment variable set (for cloud)
- [ ] Test bot conversation locally
- [ ] Check logs for any errors

---

## Project Structure (Detailed)

See [REFACTORING_GUIDE.md](REFACTORING_GUIDE.md) for:

- Detailed code structure
- Function descriptions
- Dependencies between modules
- How to extend the bot

---

## Dependencies

Core packages installed via `requirements.txt`:

| Package                     | Purpose                       |
| --------------------------- | ----------------------------- |
| `python-telegram-bot>=20.0` | Telegram bot framework        |
| `google-genai>=0.3.0`       | Google Gemini AI API          |
| `motor>=3.3.0`              | Async MongoDB driver          |
| `pandas>=2.0.0`             | Menu CSV parsing              |
| `python-dotenv>=1.0.0`      | Environment variable loading  |
| `payos-sdk>=1.0.0`          | PayOS payment integration     |
| `fastapi>=0.104.0`          | (Optional) REST API framework |
| `uvicorn>=0.24.0`           | (Optional) ASGI server        |

---

## Common Questions

**Q: Can I run the bot without MongoDB?**  
A: Not with the current setup. You need MongoDB to store conversations and orders. Use MongoDB Atlas free tier (512MB storage).

**Q: Can I run the bot without PayOS?**  
A: Yes. The bot will work, but the checkout/payment feature won't function. Remove or skip the PayOS-related code in `handlers.py`.

**Q: What's the difference between `run_polling()` and webhooks?**  
A: `run_polling()` is simpler (bot asks Telegram for new messages) but slower. Webhooks are faster but require a public URL. This bot uses polling, which is fine for small-scale use.

**Q: Can I add more AI models?**  
A: Yes. Edit `FREE_MODELS` in `config.py` to add model names from Google's API.

---

## Support & Contributing

For questions or issues:

1. Check the error logs: `python main.py` (debug mode)
2. Review [REFACTORING_GUIDE.md](REFACTORING_GUIDE.md) for code details
3. Verify all environment variables are set correctly

To improve the bot:

1. Test locally before changes
2. Follow Python PEP 8 style
3. Add error handling for new features
4. Update documentation

---

## License

This project is for Casso Milk Tea internal use.

---

**Last Updated**: April 2026  
**Python Version**: 3.9+  
**Status**: Production Ready
