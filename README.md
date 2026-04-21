# Casso Milk Tea Bot - Refactored Structure

## Overview

The monolithic `bot.py` file has been refactored into 4 modular files for better organization and maintainability.

## File Structure

### 1. **config.py** (~280 lines)

**Purpose**: All configurations, environment setup, and initializations

- Environment variables loading (GEMINI_API_KEY, TELEGRAM_TOKEN, MONGODB_URI, PayOS credentials)
- Client initialization (GenAI, PayOS, MongoDB Motor)
- Menu CSV loading and system prompt definition
- Gemini functions definitions
- Global variables and model initialization
- Exports: `genai_version`, `genai_client`, `menu_df`, `SYSTEM_PROMPT`, `FREE_MODELS`, `sessions_coll`, `orders_coll`, `payos`, `types`, `logger`

### 2. **services.py** (~180 lines)

**Purpose**: Business logic and service functions

- `find_menu_row_by_id()` - Menu item lookup
- `process_checkout()` - Order total calculation
- `calculate_and_checkout()` - Tool wrapper for Gemini
- `call_gemini_with_history()` - Main LLM integration with 429 fallback strategy
- Exports: All functions for handlers to use

### 3. **handlers.py** (~520 lines)

**Purpose**: Telegram message handlers

- `start_command()` - Handle /start command
- `handle_text()` - Process user messages and Gemini responses
- `handle_location()` - Process GPS location sharing
- `handle_description_text()` - Finalize orders with PayOS integration
- Imports from config and services, handles all Telegram interactions

### 4. **main.py** (~30 lines)

**Purpose**: Application entry point

- Imports handlers and config
- Sets up Telegram bot with handlers
- Runs the bot polling
- **Run the bot with: `python main.py`**

## How to Use

### Running the Bot

```bash
python main.py
```

### File Dependencies

```
main.py
  ├── imports from config.py
  ├── imports from handlers.py
  │   ├── imports from config.py
  │   └── imports from services.py
  │       └── imports from config.py
```

## Key Changes

✅ **No code logic changed** - All functionality is identical to the original  
✅ **Better organization** - Each file has a clear responsibility  
✅ **Easier maintenance** - Smaller files are easier to navigate and modify  
✅ **Improved testability** - Functions are now more isolated  
✅ **Cleaner imports** - Dependencies are explicit and clear

## Original File

The original `bot.py` file still exists and can be deleted once you confirm the refactored version works correctly.

## Common Tasks

### Add a new Telegram command

1. Create handler function in `handlers.py`
2. Register it in `main.py` with `app.add_handler()`

### Modify the system prompt

1. Edit `SYSTEM_PROMPT` in `config.py`

### Adjust Gemini fallback models

1. Modify `FREE_MODELS` list in `config.py`

### Add new checkout logic

1. Create function in `services.py`
2. Import and use in `handlers.py`
