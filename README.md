# 🐾 GGQT Bot

A feature-rich, high-performance Telegram AI chat companion powered by **Pyrogram**, **OpenRouter**, **MongoDB**, and custom **OpenAI-compatible APIs**.

---

## ✨ Features

- **🐾 Natural Cat Girl Persona**: Sweet, playful, and expressive neko companion (*nya~*, cute emojis 🐾✨) that feels like a real friend.
- **🌐 Live Web Search**: Integrated multi-tier DuckDuckGo web search engine (`duckduckgo-search` library + HTML & API fallbacks) providing real-time facts and latest news.
- **⚡ Instant Startup**: Starts up in < 1 second using `openrouter/free` as immediate fallback, running model discovery concurrently in the background.
- **🔄 Dynamic Multi-API Management**: Admins can add and remove custom OpenAI-compatible endpoints directly inside Telegram (`/addapi`, `/removeapi`, `/apis`).
- **📑 Paginated Model Selector (`/models`)**: Interactive 9-models-per-page inline grid to switch between OpenRouter free models and custom endpoints.
- **🛡️ Anti-Link Group Protection**: Automatically formats links (e.g. `instagram .com`) in non-admin group chats to prevent group link bans.
- **🤫 Silent Model Fallbacks**: Transparently switches to working fallback models with zero user disruption or exposed technical model names.
- **🎲 Spontaneous Group Chatter**: Randomly joins group conversations with playful responses and configurable cooldowns.
- **📊 Admin Dashboard (`/stats`)**: Displays real-time uptime, RAM usage, active models, and MongoDB user/message statistics.
- **💬 Rich Inline Mode**: Use the bot in any chat via `@bot_username <prompt>` with full sender profile context.

---

## 📋 Bot Commands

| Command | Scope | Description |
| :--- | :--- | :--- |
| `/start` | User | Welcome message and bot introduction |
| `/models` | User | Interactive 9-per-page AI model selector |
| `/model <name>` | User | Switch active model directly |
| `/search <query>` | User | Search the web for live information |
| `/reset` | User | Reset current conversation session |
| `/help` | User | Display help menu and commands |
| `/stats` | Admin | Live system, host, database & AI status dashboard |
| `/addapi` | Admin | Interactive wizard to add custom API endpoints |
| `/removeapi` | Admin | Remove custom API endpoints via interactive buttons |
| `/apis` | Admin | List all registered custom API endpoints |
| `/adduser <id>` | Admin | Grant access to a user (when public access is disabled) |
| `/removeuser <id>`| Admin | Revoke access from a user |
| `/users` | Admin | List all allowed users |
| `/cancel` | Admin | Cancel active setup sessions |

---

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.10+
- MongoDB instance (local or Atlas)
- Telegram API Credentials from [my.telegram.org](https://my.telegram.org)

### 2. Configuration
Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```ini
# Telegram API Credentials
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Admin User IDs (comma-separated)
ADMIN_USER_IDS=123456789

# MongoDB Connection
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=ggqtbot
```

### 3. Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Start the bot
python3 -m bot.main
```

### 4. Run with Docker / Podman

```bash
# Using Docker Compose
docker-compose up -d

# Or using Podman Compose
podman-compose up -d
```

---

## 📜 License
MIT License
