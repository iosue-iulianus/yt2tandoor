"""
pipeline.py — Shared pipeline logic for yt2tandoor.

Provides the full video-to-recipe pipeline: download audio, transcribe,
extract recipe via Claude, and publish to Tandoor. Used by both the CLI
script and the Telegram bot.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger("yt2tandoor")


# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "yt2tandoor"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    tandoor_url: str
    tandoor_api_key: str
    whisper_model: str = "medium"
    language: str = "en"


@dataclass
class PipelineCallbacks:
    on_downloading: Callable[[], None] | None = None
    on_transcribing: Callable[[], None] | None = None
    on_extracting: Callable[[], None] | None = None
    on_publishing: Callable[[], None] | None = None
    on_complete: Callable[["PipelineResult"], None] | None = None
    on_error: Callable[[str], None] | None = None
    on_progress: Callable[[str], None] | None = None  # General status text updates


@dataclass
class PipelineResult:
    success: bool
    recipe_name: str = ""
    recipe_url: str = ""
    thumbnail_path: str = ""
    error_message: str = ""
    recipe_data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PipelineError(Exception):
    """Raised when a pipeline step fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(config: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Config saved to {CONFIG_FILE}")


# ---------------------------------------------------------------------------
# Transcript Cache
# ---------------------------------------------------------------------------

def _video_id(url: str) -> str | None:
    """Extract video ID from YouTube, Instagram, or TikTok URL."""
    # YouTube
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
    if m:
        return m.group(1)
    # Instagram
    m = re.search(r"instagram\.com/(?:reel|p)/([\w-]+)", url)
    if m:
        return f"ig_{m.group(1)}"
    # TikTok
    m = re.search(r"tiktok\.com/.*/video/(\d+)", url)
    if m:
        return f"tt_{m.group(1)}"
    return None


def _cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.transcript"


def load_cached_transcript(url: str) -> str | None:
    vid = _video_id(url)
    if not vid:
        return None
    path = _cache_path(vid)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def save_transcript_cache(url: str, transcript: str):
    vid = _video_id(url)
    if not vid:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(vid).write_text(transcript, encoding="utf-8")


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a recipe extraction assistant. Given a transcript of a cooking video,
extract a structured recipe and return ONLY valid JSON (no markdown fences, no preamble).

The JSON must follow schema.org Recipe format exactly like this structure:

{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "<recipe name>",
  "description": "<brief description>",
  "author": {
    "@type": "Person",
    "name": "<creator name>"
  },
  "url": "<source_url>",
  "recipeCategory": "<meal type: Breakfast, Lunch, Dinner, Dessert, Snack, Appetizer, Side Dish, Beverage>",
  "recipeCuisine": "<country or region of origin>",
  "recipeYield": "<servings>",
  "prepTime": "<ISO 8601 duration>",
  "cookTime": "<ISO 8601 duration>",
  "totalTime": "<ISO 8601 duration>",
  "recipeIngredient": [
    "<amount> <unit> <ingredient>, <note>"
  ],
  "recipeInstructions": [
    {
      "@type": "HowToStep",
      "name": "Instructions",
      "text": "## Step Name\\nDetailed instruction for this step.\\n\\n## Next Step Name\\nDetailed instruction for the next step."
    }
  ],
  "keywords": ["<keyword1>", "<keyword2>"],
  "nutrition": {
    "@type": "NutritionInformation"
  }
}

Rules:
- Combine ALL steps into a SINGLE HowToStep object. Use markdown ## headers to separate logical sections
  within the "text" field. Each ## section will become a separate step in Tandoor.
  Example: "text": "## Brown the meat\\nBrown ground beef...\\n\\n## Make the sauce\\nAdd tomatoes..."
- Use METRIC units (grams, ml, etc.) for all ingredients. If the video gives imperial, convert them.
  Include the original imperial measurement in parentheses if helpful.
- If the video mentions gram weights, prefer those over volume measurements.
- Combine related steps into logical groups (don't make a step for every sentence).
- Estimate prep/cook/total times from context if not explicitly stated.
- For recipeCategory, pick the best fit from: Breakfast, Lunch, Dinner, Dessert, Snack, Appetizer, Side Dish, Beverage
- For recipeCuisine, identify the country or region (e.g., Mexican, Italian, Turkish, American, Japanese, etc.)
- Keywords should include the cuisine, meal type, key ingredients, and any notable attributes (no-bake, quick, vegetarian, etc.)
- Return ONLY the JSON object. No explanation, no markdown."""


# ---------------------------------------------------------------------------
# Pipeline Steps
# ---------------------------------------------------------------------------

def download_thumbnail(url: str, output_dir: str) -> str | None:
    """Download the best available thumbnail for a video."""
    import requests

    vid = _video_id(url)
    if not vid:
        return None

    # YouTube thumbnails
    if not vid.startswith(("ig_", "tt_")):
        thumb_urls = [
            f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
            f"https://img.youtube.com/vi/{vid}/sddefault.jpg",
            f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
        ]
        for thumb_url in thumb_urls:
            try:
                resp = requests.get(thumb_url, timeout=10)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    thumb_path = os.path.join(output_dir, f"{vid}.jpg")
                    with open(thumb_path, "wb") as f:
                        f.write(resp.content)
                    return thumb_path
            except Exception:
                continue

    # For non-YouTube or as fallback, try yt-dlp thumbnail extraction
    try:
        result = subprocess.run(
            ["yt-dlp", "--write-thumbnail", "--skip-download",
             "-o", os.path.join(output_dir, "thumb"), url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            for ext in ["jpg", "png", "webp"]:
                for f in Path(output_dir).glob(f"thumb*.{ext}"):
                    return str(f)
    except Exception:
        pass

    return None


def download_audio(url: str, output_dir: str) -> str:
    """Download audio from a video URL using yt-dlp."""
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3",
        "-o", output_template, "--no-playlist", url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(f"yt-dlp failed: {result.stderr.strip()}")

    for ext in ["mp3", "m4a", "webm", "opus", "wav"]:
        for f in Path(output_dir).glob(f"*.{ext}"):
            return str(f)

    raise PipelineError("No audio file found after download.")


def transcribe_audio(audio_path: str, model_name: str = "medium", language: str = "en",
                     progress_cb: Callable[[str], None] | None = None) -> str:
    """Transcribe audio using Whisper."""
    import whisper

    # Progress ticker — updates every 30s so the user knows it's alive
    stop_ticker = threading.Event()

    def _ticker():
        start = time.time()
        while not stop_ticker.wait(15):
            elapsed = int(time.time() - start)
            mins, secs = divmod(elapsed, 60)
            if progress_cb:
                progress_cb(f"Transcribing... ({mins}m {secs:02d}s elapsed)")
            logger.info("Transcription in progress (%dm %02ds)", mins, secs)

    ticker = threading.Thread(target=_ticker, daemon=True)
    ticker.start()

    try:
        model = whisper.load_model(model_name)
        result = model.transcribe(audio_path, language=language)
        transcript = result["text"].strip()
        return transcript
    finally:
        stop_ticker.set()
        ticker.join(timeout=2)


def extract_recipe(transcript: str, source_url: str) -> dict:
    """Extract structured recipe from transcript using Claude Code CLI."""
    user_prompt = f"""Here is a transcript of a cooking video. Extract the recipe.

Source URL: {source_url}

Transcript:
{transcript}"""

    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    result = subprocess.run(
        ["claude", "-p", full_prompt],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise PipelineError(f"Claude Code error: {result.stderr.strip()}")

    response_text = result.stdout.strip()

    # Strip markdown fences if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
    if response_text.endswith("```"):
        response_text = response_text.rsplit("```", 1)[0]
    response_text = response_text.strip()

    try:
        recipe = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise PipelineError(f"Invalid JSON from Claude: {e}\nRaw: {response_text[:500]}")

    return recipe


# ---------------------------------------------------------------------------
# Duplicate Detection
# ---------------------------------------------------------------------------

def check_duplicate(recipe_name: str, config: PipelineConfig) -> int | None:
    """Search Tandoor for existing recipe with same name. Returns recipe ID or None."""
    import requests

    headers = {"Authorization": f"Bearer {config.tandoor_api_key}"}

    try:
        resp = requests.get(
            f"{config.tandoor_url}/api/recipe/",
            headers=headers,
            params={"query": recipe_name, "page_size": 5},
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            for r in results:
                if r.get("name", "").strip().lower() == recipe_name.strip().lower():
                    return r.get("id")
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Format Conversion
# ---------------------------------------------------------------------------

def parse_iso_duration_minutes(iso: str) -> int:
    """Parse ISO 8601 duration (e.g. PT1H30M) to minutes."""
    if not iso:
        return 0
    hours = re.search(r"(\d+)H", iso)
    mins = re.search(r"(\d+)M", iso)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    return total


def parse_ingredient_string(s: str) -> dict:
    """Parse a JSON-LD ingredient string into Tandoor's API format."""
    s = s.strip()
    note = ""

    if ", " in s:
        parts = s.split(", ", 1)
        s = parts[0]
        note = parts[1]

    known_units = {
        "g", "kg", "mg", "ml", "l", "dl", "cl",
        "tsp", "tbsp", "cup", "cups", "oz", "lb", "lbs",
        "clove", "cloves", "piece", "pieces", "pinch",
        "bunch", "can", "cans", "slice", "slices",
        "sprig", "sprigs", "head", "heads", "stalk", "stalks",
        "handful", "dash", "drop", "drops", "packet", "packets",
        "sheet", "sheets", "stick", "sticks", "whole",
        "grams", "gram",
    }

    m = re.match(
        r"^([\d]+(?:[./][\d]+)?(?:\s*-\s*[\d]+(?:[./][\d]+)?)?)\s+"
        r"([a-zA-Z]+(?:\s[a-zA-Z]+)?)\s+"
        r"(.+)$",
        s,
    )

    if m:
        amount_str, unit, food = m.group(1), m.group(2), m.group(3)
        amount = _parse_amount(amount_str)

        if unit.lower() in known_units:
            return {
                "amount": amount,
                "unit": {"name": unit},
                "food": {"name": food.strip()},
                "note": note,
            }
        else:
            return {
                "amount": amount,
                "unit": None,
                "food": {"name": f"{unit} {food}".strip()},
                "note": note,
            }

    m2 = re.match(r"^([\d]+(?:[./][\d]+)?(?:\s*-\s*[\d]+(?:[./][\d]+)?)?)\s+(.+)$", s)
    if m2:
        amount = _parse_amount(m2.group(1))
        return {
            "amount": amount,
            "unit": None,
            "food": {"name": m2.group(2).strip()},
            "note": note,
        }

    return {
        "amount": 0,
        "unit": None,
        "food": {"name": s},
        "note": note,
        "no_amount": True,
    }


def _parse_amount(s: str) -> float:
    """Parse amount string handling fractions, decimals, and ranges."""
    s = s.strip()

    if "-" in s:
        parts = s.split("-")
        try:
            return max(_parse_amount(p) for p in parts)
        except (ValueError, RecursionError):
            return 0

    if "/" in s:
        try:
            num, den = s.split("/")
            return float(num.strip()) / float(den.strip())
        except (ValueError, ZeroDivisionError):
            return 0

    try:
        return float(s)
    except ValueError:
        return 0


def _split_instruction_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown instruction text on ## headers into (header, body) pairs."""
    sections = []
    if not text:
        return sections

    parts = text.split("## ")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.split("\n", 1)
        header = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        sections.append((header, body))

    if not sections and text.strip():
        sections.append(("Instructions", text.strip()))

    return sections


def jsonld_to_tandoor(recipe: dict, servings_override: int | None = None) -> dict:
    """Convert a schema.org JSON-LD recipe to Tandoor's native API format."""
    ingredients = []
    for ing_str in recipe.get("recipeIngredient", []):
        ingredients.append(parse_ingredient_string(ing_str))

    steps_ld = recipe.get("recipeInstructions", [])
    instruction_text = ""
    if steps_ld:
        instruction_text = steps_ld[0].get("text", "")

    prep = parse_iso_duration_minutes(recipe.get("prepTime", ""))
    cook = parse_iso_duration_minutes(recipe.get("cookTime", ""))

    servings_raw = recipe.get("recipeYield", "4")
    servings_match = re.search(r"(\d+)", str(servings_raw))
    original_servings = int(servings_match.group(1)) if servings_match else 4
    target_servings = servings_override or original_servings

    if servings_override and original_servings > 0 and servings_override != original_servings:
        scale = servings_override / original_servings
        for ing in ingredients:
            if ing.get("amount", 0) > 0:
                ing["amount"] = round(ing["amount"] * scale, 2)

    keywords = []
    for kw in recipe.get("keywords", []):
        if isinstance(kw, str) and kw.strip():
            keywords.append({"name": kw.strip()})
    for field_name in ("recipeCuisine", "recipeCategory"):
        val = recipe.get(field_name, "")
        if val and val.strip():
            keywords.append({"name": val.strip()})

    desc = recipe.get("description", "")
    author = recipe.get("author", {})
    if isinstance(author, dict):
        author_name = author.get("name", "")
    else:
        author_name = str(author)
    if author_name and author_name not in desc:
        desc = f"By {author_name}. {desc}" if desc else f"By {author_name}"

    return {
        "name": recipe.get("name", "Untitled Recipe"),
        "description": desc,
        "working_time": prep,
        "waiting_time": cook,
        "servings": target_servings,
        "servings_text": "servings",
        "source_url": recipe.get("url", ""),
        "internal": True,
        "keywords": keywords,
        "steps": [
            {
                "name": "",
                "instruction": "",
                "ingredients": ingredients,
                "show_ingredients_table": True,
            },
        ] + [
            {
                "name": header,
                "instruction": body,
                "ingredients": [],
                "show_ingredients_table": False,
            }
            for header, body in _split_instruction_sections(instruction_text)
        ],
    }


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def publish_to_tandoor(recipe: dict, config: PipelineConfig,
                       thumb_path: str | None = None,
                       servings_override: int | None = None) -> tuple[int | None, str | None]:
    """Publish recipe to Tandoor. Returns (recipe_id, recipe_url) or (None, None) on failure."""
    import requests

    headers = {"Authorization": f"Bearer {config.tandoor_api_key}"}
    tandoor_recipe = jsonld_to_tandoor(recipe, servings_override)

    resp = requests.post(
        f"{config.tandoor_url}/api/recipe/",
        headers={**headers, "Content-Type": "application/json"},
        json=tandoor_recipe,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        detail = ""
        try:
            detail = str(resp.json())
        except Exception:
            detail = resp.text[:300]
        _save_fallback(tandoor_recipe)
        raise PipelineError(f"Tandoor API error (HTTP {resp.status_code}): {detail}")

    data = resp.json()
    recipe_id = data.get("id")
    recipe_url = f"{config.tandoor_url}/view/recipe/{recipe_id}"

    # Upload thumbnail
    if thumb_path and recipe_id:
        try:
            with open(thumb_path, "rb") as img:
                requests.put(
                    f"{config.tandoor_url}/api/recipe/{recipe_id}/image/",
                    headers=headers,
                    files={"image": ("thumbnail.jpg", img, "image/jpeg")},
                    timeout=30,
                )
        except Exception:
            pass  # Non-fatal

    return recipe_id, recipe_url


def _save_fallback(tandoor_recipe: dict) -> str | None:
    """Save Tandoor-native JSON for manual retry."""
    try:
        safe_name = tandoor_recipe.get("name", "recipe").replace(" ", "_")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in "_-")
        path = f"{safe_name}_tandoor.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tandoor_recipe, f, indent=2, ensure_ascii=False)
        return path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# High-level Pipeline
# ---------------------------------------------------------------------------

def process_video(url: str, config: PipelineConfig,
                  callbacks: PipelineCallbacks | None = None,
                  no_cache: bool = False) -> PipelineResult:
    """Run the full pipeline: download -> transcribe -> extract -> publish.

    Returns a PipelineResult with recipe data and Tandoor URL on success.
    """
    cb = callbacks or PipelineCallbacks()

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Check cache
            cached = load_cached_transcript(url)
            if cached and not no_cache:
                logger.info("Using cached transcript for %s", url)
                transcript = cached
            else:
                # Download
                if cb.on_downloading:
                    cb.on_downloading()
                logger.info("Downloading audio: %s", url)
                audio_path = download_audio(url, tmp_dir)

                # Transcribe
                if cb.on_transcribing:
                    cb.on_transcribing()
                logger.info("Transcribing with whisper %s", config.whisper_model)
                transcript = transcribe_audio(
                    audio_path, config.whisper_model, config.language,
                    progress_cb=cb.on_progress,
                )
                save_transcript_cache(url, transcript)
                logger.info("Transcription done (%d chars)", len(transcript))

            # Extract
            if cb.on_extracting:
                cb.on_extracting()
            logger.info("Extracting recipe via Claude")
            recipe = extract_recipe(transcript, url)
            logger.info("Extracted: %s", recipe.get("name", "Unknown"))

            # Thumbnail — copy out of temp dir so it survives cleanup
            thumb_path = download_thumbnail(url, tmp_dir)
            persistent_thumb = None
            if thumb_path:
                persistent_thumb = os.path.join(
                    tempfile.gettempdir(),
                    f"yt2t_thumb_{os.path.basename(thumb_path)}",
                )
                shutil.copy2(thumb_path, persistent_thumb)

            # Publish
            if cb.on_publishing:
                cb.on_publishing()
            logger.info("Publishing to Tandoor")
            recipe_id, recipe_url = publish_to_tandoor(recipe, config, persistent_thumb)
            logger.info("Published: %s -> %s", recipe.get("name"), recipe_url)

            result = PipelineResult(
                success=True,
                recipe_name=recipe.get("name", "Unknown"),
                recipe_url=recipe_url or "",
                thumbnail_path=persistent_thumb or "",
                recipe_data=recipe,
            )

            if cb.on_complete:
                cb.on_complete(result)

            return result

    except PipelineError as e:
        error_msg = str(e)
        logger.error("Pipeline error: %s", error_msg)
        if cb.on_error:
            cb.on_error(error_msg)
        return PipelineResult(success=False, error_message=error_msg)
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.exception("Pipeline unexpected error")
        if cb.on_error:
            cb.on_error(error_msg)
        return PipelineResult(success=False, error_message=error_msg)
