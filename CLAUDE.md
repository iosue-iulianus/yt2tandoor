# yt2tandoor

Telegram bot + CLI that converts cooking videos (YouTube, Instagram, TikTok) into structured recipes and publishes them to a self-hosted Tandoor instance.

## What this project does

User sends a video URL to a Telegram bot. The bot downloads the audio, transcribes it with Whisper, extracts a structured recipe via Claude Code CLI, and publishes it to Tandoor with ingredients, steps, and thumbnail. Bot replies with a link to the finished recipe.

## Architecture

```
Telegram chat
    │
    ▼
yt2tandoor bot (Docker container on NAS)
    ├── yt-dlp        → download audio
    ├── whisper (CPU)  → transcribe
    ├── claude -p      → extract recipe JSON-LD
    └── requests       → POST to Tandoor API
                              │
                              ▼
                     Tandoor (Docker on same NAS)
                     http://recipes.lan
```

### Components
- `recipe_from_video.py` - CLI pipeline script (existing, keep working)
- `pipeline.py` - Refactored pipeline logic as importable module
- `bot.py` - Telegram bot (python-telegram-bot)
- `Dockerfile` + `docker-compose.yml` - Container deployment

## Tech Stack

- **Python 3.10+**
- **python-telegram-bot** - Telegram Bot API (async)
- **yt-dlp** - Video/audio download (YouTube, Instagram Reels, TikTok)
- **openai-whisper** - Local speech-to-text (CPU-only, no GPU on NAS)
- **Claude Code CLI** (`claude -p`) - Recipe extraction from transcript
- **requests** - Tandoor API communication
- **Tandoor 2.5.0** - Recipe manager (Docker, same NAS)

## Tandoor API Details

- Base URL: `http://recipes.lan`
- Auth: `Authorization: Bearer <api_key>` (NOT Token)
- Create recipe: `POST /api/recipe/` with native JSON format
- Upload image: `PUT /api/recipe/{id}/image/` with multipart file
- Search: `GET /api/recipe/?query=<name>&page_size=5`
- API key scope: read+write (Settings > API > Generate)

### Tandoor Recipe Format (native API)

Step 1 is always ingredients-only (no instructions). Each subsequent step is a separate instruction section with a bold header name. This is how Tandoor renders recipes properly.

```json
{
  "name": "Recipe Name",
  "description": "By Author. Description text.",
  "working_time": 10,
  "waiting_time": 30,
  "servings": 4,
  "servings_text": "servings",
  "source_url": "https://youtube.com/watch?v=...",
  "internal": true,
  "keywords": [{"name": "italian"}, {"name": "pasta"}],
  "steps": [
    {
      "name": "",
      "instruction": "",
      "ingredients": [
        {"amount": 500, "unit": {"name": "g"}, "food": {"name": "pasta"}, "note": "any shape"},
        {"amount": 2, "unit": {"name": "cloves"}, "food": {"name": "garlic"}, "note": "minced"}
      ],
      "show_ingredients_table": true
    },
    {
      "name": "Brown the beef",
      "instruction": "Heat olive oil in a large pot...",
      "ingredients": [],
      "show_ingredients_table": false
    }
  ]
}
```

### Intermediate Format (JSON-LD)

Claude extracts recipes into schema.org JSON-LD. The `jsonld_to_tandoor()` function converts to native format. Instructions use `## Headers` in a single HowToStep text block, split on `##` at publish time to create individual Tandoor steps.

## Telegram Bot Behavior

### Commands
- `/start` - Welcome message with usage instructions
- `/recipe <url>` - Process a video URL
- `/status` - Show if a job is currently processing
- `/help` - Usage info

### Flow
1. User sends URL (or `/recipe <url>`)
2. Bot replies: "⏳ Processing... this takes 2-5 minutes"
3. Bot edits message with progress updates:
   - "📥 Downloading audio..."
   - "🎙️ Transcribing..."
   - "🧠 Extracting recipe..."
   - "📤 Publishing to Tandoor..."
4. On success: Bot sends recipe thumbnail + name + Tandoor link
5. On error: Bot sends error message with what went wrong

### URL Detection
Bot should auto-detect video URLs in plain messages (no command needed). Support:
- YouTube: `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`
- Instagram: `instagram.com/reel/`, `instagram.com/p/`
- TikTok: `tiktok.com/@user/video/`, `vm.tiktok.com/`

### Concurrency
- Process one video at a time (Whisper is CPU-heavy)
- Queue additional requests, notify user of position
- Reject if queue is full (>3 pending)

## Infrastructure

### Deployment
- **Docker container on UGREEN NAS** (Linux/amd64, no GPU)
- Same NAS runs Tandoor, *arr stack, qBittorrent, Jellyseerr
- Container gets IP via macvlan (same as other services)
- Whisper runs CPU-only (slower but fine for async bot responses)

### Claude Code CLI Auth
- Deploy container once, then `docker exec -it yt2tandoor bash` and run `claude login`
- Auth token persists in mounted volume at `/home/appuser/.claude/`
- Never needs re-auth unless token expires (rare)
- This is the same deploy-once pattern used for other services on the NAS

### Docker Setup
```yaml
volumes:
  - ./config:/home/appuser/.config/yt2tandoor    # Tandoor config + transcript cache
  - ./claude-config:/home/appuser/.claude          # Claude Code CLI auth token
  - ./data:/app/data                               # Generated JSON files
```

### Environment Variables
```
TELEGRAM_BOT_TOKEN=<from @BotFather>
TANDOOR_URL=http://recipes.lan
TANDOOR_API_KEY=<from Tandoor Settings > API>
```

## Key Design Decisions

- **Whisper model**: Default `medium` for accuracy over speed
- **Metric units**: All ingredients in grams/ml with imperial in parentheses
- **Single HowToStep with ## headers**: Claude outputs one text block; split on `##` at publish
- **Transcript caching**: Keyed by video ID, avoids re-transcribing same video
- **Claude Code CLI over API**: Uses subscription, not per-token billing
- **Bearer auth**: Tandoor 2.5 uses Bearer not Token
- **Sequential processing**: One job at a time due to CPU Whisper constraints

## File Structure

```
yt2tandoor/
├── CLAUDE.md               # This file
├── recipe_from_video.py    # CLI script (standalone, keep working)
├── pipeline.py             # Shared pipeline logic (imported by CLI + bot)
├── bot.py                  # Telegram bot
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── data/                   # Generated recipe JSONs
```

## Style Preferences

- Python 3.10+ (use `str | None` not `Optional[str]`)
- Type hints on function signatures
- Concise docstrings
- No unnecessary abstractions
- Error messages should be actionable
- Async where needed (bot), sync is fine for pipeline internals
