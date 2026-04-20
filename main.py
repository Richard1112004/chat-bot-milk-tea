"""
Casso Milk Tea Bot
Main entry point for the Telegram bot application.

This bot handles:
- Menu queries and item information
- Order placement and tracking
- Checkout and payment integration with PayOS
- AI-powered conversation using Google Gemini
- Location-based delivery tracking
"""

import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import TELEGRAM_TOKEN, logger
from handlers import start_command, handle_text, handle_location

logger = logging.getLogger(__name__)


def main() -> None:
    """Build and run the Telegram bot."""
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Register command and message handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started. Polling Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
