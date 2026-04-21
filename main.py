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
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import TELEGRAM_TOKEN, logger
from handlers import start_command, handle_text, handle_location

logger = logging.getLogger(__name__)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP request handler for health checks."""
    
    def do_GET(self):
        """Respond to GET requests."""
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Bot is running')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        logger.debug(f"HTTP Server: {format % args}")


def start_http_server(port: int) -> threading.Thread:
    """
    Start a simple HTTP server in a background thread for Render health checks.
    
    Args:
        port: The port to listen on
        
    Returns:
        The thread object (started and running)
    """
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"HTTP Server listening on port {port}")
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main() -> None:
    """Build and run the Telegram bot with HTTP server for health checks."""
    # Get port from environment variable, default to 8080
    port = int(os.getenv('PORT', '8080'))
    
    # Start HTTP server in background thread
    logger.info(f"Starting HTTP server on port {port}")
    start_http_server(port)
    
    # Build and configure Telegram bot
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Register command and message handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started. Polling Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
