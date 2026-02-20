# yt2tandoor

A Telegram bot that converts cooking videos into structured recipes and publishes them to a self-hosted [Tandoor](https://github.com/TandoorRecipes/recipes) instance. Send a link, get a recipe.

Built for the countless cooking videos on YouTube, Instagram, and TikTok that talk through a recipe but never actually write it down anywhere. No more rewinding and pausing to catch ingredient amounts — this transcribes the audio and extracts a proper recipe with ingredients, steps, and timings.

## Features

- Paste a YouTube, Instagram Reel, or TikTok link and get a full recipe in Tandoor
- Auto-detects video URLs in chat messages (no command needed)
- Transcribes audio locally with Whisper (no cloud transcription APIs)
- Extracts structured recipes via Claude Code CLI
- Uploads thumbnail, ingredients, and step-by-step instructions to Tandoor
- Live progress updates in Telegram while processing
- Chat ID allowlist for access control
- Also works as a standalone CLI tool

## How It Works

```
[Telegram] ──URL──> [yt2tandoor container]
                        ├── yt-dlp         → download audio
                        ├── Whisper (CPU)  → transcribe
                        ├── Claude CLI     → extract recipe
                        └── Tandoor API    → publish recipe
                                                  │
                                                  ▼
                                          [Tandoor instance]
```

The bot polls Telegram outbound. Tandoor stays on your local network with no exposure.

## Quick Start

### 1. Clone the Repository

```bash
cd /opt/docker  # or wherever you run your containers
git clone https://github.com/iosue-iulianus/yt2tandoor.git
cd yt2tandoor
```

### 2. Create a Telegram Bot

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` and follow prompts
3. Copy the API token

### 3. Get Your Chat ID

For a group chat: add [@userinfobot](https://t.me/userinfobot) to the group and it will report the chat ID.

For a DM: message @userinfobot directly.

### 4. Get a Tandoor API Key

In Tandoor: Settings > API > Generate a new token with read+write scope.

### 5. Configure

Copy the example and fill in your values:

```bash
cp .env.example .env
```

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TANDOOR_URL=http://recipes.lan          # or IP address of your Tandoor instance
TANDOOR_API_KEY=tda_your_api_key_here
ALLOWED_TELEGRAM_CHATS=-5247779494      # comma-separated chat IDs
WHISPER_MODEL=small                     # tiny, base, small, medium, large
```

### 6. Build and Run

```bash
docker compose up -d --build
```

### 7. Authenticate Claude Code CLI (One-Time)

```bash
docker exec -it yt2tandoor bash
claude login
```

The auth token persists in the `./claude-config` volume mount.

### 8. Register Commands (Optional)

Message @BotFather:
```
/setcommands
```
Then send:
```
start - Welcome message
recipe - Process a video URL
status - Check if a job is running
help - Usage info
```

## Usage

**In Telegram:** Just paste a video URL into the chat. The bot will auto-detect it and start processing. You can also use `/recipe <url>`.

**CLI mode:** The standalone script still works for local use:
```bash
python recipe_from_video.py "https://youtube.com/watch?v=..."
python recipe_from_video.py --dry-run "https://youtube.com/watch?v=..."
python recipe_from_video.py --batch urls.txt
```

## Supported Platforms

- YouTube (`youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`)
- Instagram Reels (`instagram.com/reel/`, `instagram.com/p/`)
- TikTok (`tiktok.com/@user/video/`, `vm.tiktok.com/`)

## Whisper Model Selection

| Model | Size | Speed (CPU) | Accuracy |
|-------|------|-------------|----------|
| `tiny` | 75MB | Fast | Low |
| `base` | 140MB | Fast | Fair |
| `small` | 460MB | ~2 min | Good |
| `medium` | 1.5GB | ~5 min | Better |
| `large` | 3GB | ~15 min | Best |

`small` is recommended for NAS/CPU-only deployments. Set via `WHISPER_MODEL` in `.env`.

## Network Setup

The container needs to reach both Telegram (internet) and Tandoor (LAN). The default `docker-compose.yml` uses macvlan — adjust to match your network:

### Macvlan (Default)

If Tandoor is on a macvlan network, the bot container should be too:

```yaml
networks:
  macvlan:
    external: true
```

### Host Networking

If Tandoor is reachable from the host:

```yaml
network_mode: host
```

## Group Chat Setup

If using in a Telegram group:

1. Add the bot to the group
2. Message @BotFather: `/setprivacy` > Select bot > Disable
3. Remove and re-add the bot to the group
4. Add the group's chat ID to `ALLOWED_TELEGRAM_CHATS`

## Troubleshooting

**Bot not responding:**
- Check logs: `docker compose logs -f`
- Verify `ALLOWED_TELEGRAM_CHATS` matches your chat ID (logs show the incoming chat ID)

**Bot not responding in group:**
- Disable privacy mode via @BotFather
- Remove and re-add bot to group

**Can't publish to Tandoor ("No route to host"):**
- The container can't reach Tandoor's IP. Check your network mode.
- If Tandoor uses macvlan, the bot container should too (host can't reach macvlan IPs directly).
- Try using the IP address instead of hostname in `TANDOOR_URL`.

**"Unauthorized" in logs:**
- Your chat ID isn't in `ALLOWED_TELEGRAM_CHATS`
- The log shows the actual chat ID — add it to your `.env`

**Transcription is slow:**
- Expected on CPU. Use `WHISPER_MODEL=small` or `base` for faster results.
- The first run downloads the model weights (~460MB for `small`), subsequent runs are cached.

## License

MIT
