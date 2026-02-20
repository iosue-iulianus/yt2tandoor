#!/usr/bin/env python3
"""
yt2tandoor CLI

Pipeline:
  1. Download audio from a video using yt-dlp
  2. Transcribe with local Whisper (cached)
  3. Use Claude Code CLI to extract a structured recipe (JSON-LD)
  4. Preview the recipe (dry run)
  5. Publish to Tandoor via API

Requirements:
  pip install openai-whisper yt-dlp requests
  Claude Code CLI authenticated via subscription

Configuration is saved to ~/.config/yt2tandoor/config.json

Usage:
  python recipe_from_video.py <video_url> [options]
  python recipe_from_video.py --batch urls.txt
  python recipe_from_video.py --setup
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pipeline import (
    PipelineConfig,
    PipelineError,
    load_config,
    save_config,
    load_cached_transcript,
    save_transcript_cache,
    download_audio,
    download_thumbnail,
    transcribe_audio,
    extract_recipe,
    check_duplicate,
    jsonld_to_tandoor,
    publish_to_tandoor,
    _save_fallback,
)

try:
    import requests as req_lib
except ImportError:
    req_lib = None


# ---------------------------------------------------------------------------
# CLI-only: Setup & Dependency Checks
# ---------------------------------------------------------------------------

def run_setup(force: bool = False) -> dict:
    """Interactive first-run setup for Tandoor connection."""
    config = load_config()

    if config.get("tandoor_url") and config.get("tandoor_api_key") and not force:
        return config

    print("\n╔══════════════════════════════════╗")
    print("║     yt2tandoor  -  Setup         ║")
    print("╚══════════════════════════════════╝\n")

    default_url = config.get("tandoor_url", "")
    if default_url:
        url_input = input(f"  Tandoor URL [{default_url}]: ").strip()
    else:
        url_input = input("  Tandoor URL (e.g. http://recipes.lan): ").strip()
    tandoor_url = (url_input or default_url).rstrip("/")

    if not tandoor_url:
        print("  Error: Tandoor URL is required.")
        sys.exit(1)

    default_key = config.get("tandoor_api_key", "")
    if default_key:
        masked = f"...{default_key[-8:]}"
        key_input = input(f"  API key [{masked}]: ").strip()
    else:
        key_input = input("  API key (Settings > API > Generate, scope: read+write): ").strip()
    tandoor_api_key = key_input or default_key

    if not tandoor_api_key:
        print("  Error: API key is required.")
        sys.exit(1)

    config["tandoor_url"] = tandoor_url
    config["tandoor_api_key"] = tandoor_api_key

    print(f"\n  Testing connection to {tandoor_url}...")
    if req_lib is None:
        print("  WARNING: 'requests' not installed, skipping connection test.")
    else:
        try:
            resp = req_lib.get(
                f"{tandoor_url}/api/recipe/",
                headers={"Authorization": f"Bearer {tandoor_api_key}"},
                params={"page_size": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                count = resp.json().get("count", "?")
                print(f"  Connected! {count} recipes found in Tandoor.")
            elif resp.status_code in (401, 403):
                print("  ERROR: Authentication failed. Check your API key and scope.")
                sys.exit(1)
            else:
                print(f"  WARNING: Got status {resp.status_code}. Saving config anyway.")
        except req_lib.ConnectionError:
            print(f"  ERROR: Could not reach {tandoor_url}")
            sys.exit(1)

    save_config(config)
    print()
    return config


def check_dependencies(skip_claude: bool = False):
    """Verify all required dependencies are installed."""
    errors = []

    result = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
    if result.returncode != 0:
        errors.append(
            "yt-dlp not found.\n"
            "  Install: pip install yt-dlp\n"
            "  Ensure ~/.local/bin is in PATH: export PATH=\"$HOME/.local/bin:$PATH\""
        )
    elif result.stdout.strip() == "/usr/bin/yt-dlp":
        print(
            "WARNING: Using system yt-dlp (likely outdated).\n"
            "  Recommended: pip install --upgrade yt-dlp\n"
            "  Then: export PATH=\"$HOME/.local/bin:$PATH\" in ~/.bashrc\n"
        )

    try:
        import whisper  # noqa: F401
    except ImportError:
        errors.append("openai-whisper not found.\n  Install: pip install openai-whisper")

    if req_lib is None:
        errors.append("requests not found.\n  Install: pip install requests")

    if not skip_claude:
        result = subprocess.run(["which", "claude"], capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(
                "Claude Code CLI not found.\n"
                "  Install: npm install -g @anthropic-ai/claude-code\n"
                "  Then: claude login"
            )

    if errors:
        print("Missing dependencies:\n")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI-only: Preview & Edit
# ---------------------------------------------------------------------------

def preview_recipe(recipe: dict):
    """Display a formatted dry-run preview of the recipe."""
    print("\n" + "=" * 60)
    print(f"  RECIPE PREVIEW (Dry Run)")
    print("=" * 60)

    print(f"\n  Name:     {recipe.get('name', 'N/A')}")
    print(f"  Cuisine:  {recipe.get('recipeCuisine', 'N/A')}")
    print(f"  Category: {recipe.get('recipeCategory', 'N/A')}")
    print(f"  Yield:    {recipe.get('recipeYield', 'N/A')}")
    print(f"  Prep:     {recipe.get('prepTime', 'N/A')}")
    print(f"  Cook:     {recipe.get('cookTime', 'N/A')}")
    print(f"  Total:    {recipe.get('totalTime', 'N/A')}")
    print(f"  Source:   {recipe.get('url', 'N/A')}")
    print(f"  Author:   {recipe.get('author', {}).get('name', 'N/A')}")

    keywords = recipe.get("keywords", [])
    if keywords:
        print(f"  Keywords: {', '.join(keywords)}")

    ingredients = recipe.get("recipeIngredient", [])
    if ingredients:
        print(f"\n  Ingredients ({len(ingredients)}):")
        for ing in ingredients:
            print(f"    - {ing}")

    steps = recipe.get("recipeInstructions", [])
    if steps:
        text = steps[0].get("text", "") if steps else ""
        sections = text.split("## ")
        sections = [s.strip() for s in sections if s.strip()]
        print(f"\n  Steps ({len(sections)}):")
        for i, section in enumerate(sections, 1):
            lines = section.split("\n", 1)
            header = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            preview_text = body[:120] + "..." if len(body) > 120 else body
            print(f"    {i}. {header}")
            if preview_text:
                print(f"       {preview_text}")

    print("\n" + "=" * 60)


def edit_recipe_json(recipe: dict) -> dict:
    """Open recipe JSON in $EDITOR for manual tweaks before publishing."""
    editor = os.environ.get("EDITOR", "nano")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="yt2tandoor_", delete=False
    ) as tmp:
        json.dump(recipe, tmp, indent=2, ensure_ascii=False)
        tmp_path = tmp.name

    print(f"\n  Opening recipe in {editor}...")
    print(f"  Save and close to continue. Delete all content to abort.")

    try:
        subprocess.run([editor, tmp_path], check=True)

        with open(tmp_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            print("  Empty file, using original recipe.")
            return recipe

        edited = json.loads(content)
        print("  Recipe updated from editor.")
        return edited

    except subprocess.CalledProcessError:
        print(f"  Editor exited with error, using original recipe.")
        return recipe
    except json.JSONDecodeError as e:
        print(f"  Invalid JSON after edit: {e}")
        print("  Using original recipe.")
        return recipe
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Process Single URL (CLI flow)
# ---------------------------------------------------------------------------

def process_single_url(url: str, args, config: dict) -> bool:
    """Process a single video URL through the full CLI pipeline. Returns success."""
    print(f"\n{'─' * 60}")
    print(f"  Processing: {url}")
    print(f"{'─' * 60}")

    pipeline_config = PipelineConfig(
        tandoor_url=config.get("tandoor_url", ""),
        tandoor_api_key=config.get("tandoor_api_key", ""),
        whisper_model=args.whisper_model,
        language=args.language,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        work_dir = tmp_dir if not args.keep_audio else "."

        # Check transcript cache
        cached = load_cached_transcript(url)
        if cached and not args.no_cache:
            print(f"\n[1/3] Audio download skipped (cached transcript)")
            print(f"\n[2/3] Transcription skipped (cached, {len(cached)} chars)")
            transcript = cached
        else:
            # Step 1: Download
            print(f"\n[1/3] Downloading audio...")
            try:
                audio_path = download_audio(url, work_dir)
                print(f"  Downloaded: {Path(audio_path).name}")
            except PipelineError as e:
                print(f"  {e}")
                return False

            # Step 2: Transcribe
            print(f"\n[2/3] Transcribing (whisper {args.whisper_model})...")
            transcript = transcribe_audio(audio_path, args.whisper_model, args.language)
            print(f"  Done ({len(transcript)} chars)")
            save_transcript_cache(url, transcript)

        if args.transcript_only:
            print("\n--- Transcript ---")
            print(transcript)
            return True

        # Step 3: Extract
        print("\n[3/3] Extracting recipe with Claude Code...")
        try:
            recipe = extract_recipe(transcript, url)
            print(f"  Extracted: {recipe.get('name', 'Unknown')}")
        except PipelineError as e:
            print(f"  {e}")
            return False

        # Editor
        if args.edit:
            recipe = edit_recipe_json(recipe)

        # Thumbnail
        thumb_path = None
        if req_lib:
            print("\n  Grabbing thumbnail...")
            thumb_path = download_thumbnail(url, work_dir)
            if thumb_path:
                thumb_size = os.path.getsize(thumb_path) // 1024
                print(f"  Thumbnail saved ({thumb_size}KB)")
            else:
                print("  Could not download thumbnail.")

        # Save JSON locally
        if args.output:
            output_path = args.output
        else:
            safe_name = recipe.get("name", "recipe").replace(" ", "_").replace("/", "-")
            safe_name = "".join(c for c in safe_name if c.isalnum() or c in "_-")
            output_path = f"{safe_name}.json"

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(recipe, f, indent=2, ensure_ascii=False)
        print(f"\n  JSON saved to: {output_path}")

        # Preview
        preview_recipe(recipe)
        if thumb_path:
            print(f"  Thumbnail: Ready to upload")

        # Publish
        no_publish = args.no_publish or args.dry_run
        if no_publish:
            print("\n  Skipping publish (dry run).")
            print(f"  Import manually: Tandoor > Import > paste/upload {output_path}")
            return True

        if not config.get("tandoor_url") or not config.get("tandoor_api_key"):
            print("\n  Tandoor not configured. Run with --setup to configure, or import manually.")
            return True

        # Duplicate detection
        recipe_name = recipe.get("name", "")
        existing_id = check_duplicate(recipe_name, pipeline_config)
        if existing_id:
            existing_url = f"{config['tandoor_url']}/view/recipe/{existing_id}"
            print(f"\n  DUPLICATE FOUND: '{recipe_name}' already exists (ID: {existing_id})")
            print(f"  View: {existing_url}")
            choice = input("  Publish anyway? [y/N]: ").strip().lower()
            if choice not in ("y", "yes"):
                print(f"  Skipped. Recipe saved at: {output_path}")
                return True
        else:
            if not args.batch:
                print()
                choice = input("  Publish to Tandoor? [Y/n]: ").strip().lower()
                if choice not in ("", "y", "yes"):
                    print(f"  Skipped. Recipe saved at: {output_path}")
                    return True

        servings_override = args.servings if args.servings else None
        print(f"\n  Publishing to {config['tandoor_url']}...")
        try:
            recipe_id, recipe_url = publish_to_tandoor(
                recipe, pipeline_config, thumb_path, servings_override
            )
            print(f"  Published! Recipe ID: {recipe_id}")
            print(f"  View at: {recipe_url}")
            return True
        except PipelineError as e:
            print(f"  {e}")
            print(f"\n  Recipe still saved locally at: {output_path}")
            print(f"  You can import manually via Tandoor UI.")
            return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="yt2tandoor: Video recipe -> Tandoor recipe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s "https://youtube.com/watch?v=..."
  %(prog)s --batch urls.txt
  %(prog)s --dry-run "https://youtube.com/watch?v=..."
  %(prog)s --edit "https://youtube.com/watch?v=..."
  %(prog)s --servings 2 "https://youtube.com/watch?v=..."
  %(prog)s --setup""",
    )
    parser.add_argument("url", nargs="?", help="Video URL")
    parser.add_argument("-o", "--output", default=None, help="Save JSON to file path")
    parser.add_argument(
        "--whisper-model", default="medium",
        choices=["tiny", "base", "small", "medium", "large", "large-v3"],
        help="Whisper model size (default: medium)",
    )
    parser.add_argument("--language", default="en", help="Whisper language code (default: en)")
    parser.add_argument("--keep-audio", action="store_true", help="Keep downloaded audio file")
    parser.add_argument("--transcript-only", action="store_true", help="Only transcribe, skip extraction")
    parser.add_argument("--no-publish", action="store_true", help="Skip the publish step")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't publish")
    parser.add_argument("--setup", action="store_true", help="Re-run Tandoor setup")
    parser.add_argument("--edit", action="store_true", help="Open JSON in $EDITOR before publishing")
    parser.add_argument("--servings", type=int, default=None,
                        help="Override servings count (scales ingredients)")
    parser.add_argument("--batch", type=str, default=None, metavar="FILE",
                        help="Process multiple URLs from a text file (one per line)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached transcripts, re-download and re-transcribe")

    args = parser.parse_args()

    # Handle --setup with no URL
    if args.setup:
        run_setup(force=True)
        if not args.url and not args.batch:
            print("Setup complete. Run again with a video URL to process a recipe.")
            return

    # Batch mode
    if args.batch:
        batch_file = Path(args.batch)
        if not batch_file.exists():
            print(f"Error: Batch file not found: {args.batch}")
            sys.exit(1)

        urls = [
            line.strip() for line in batch_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if not urls:
            print("Error: No URLs found in batch file.")
            sys.exit(1)

        print(f"\n  Batch mode: {len(urls)} URLs to process")

        check_dependencies(skip_claude=args.transcript_only)

        if not args.transcript_only and not (args.no_publish or args.dry_run):
            config = run_setup()
        else:
            config = load_config()

        results = {"success": 0, "failed": 0}
        for i, url in enumerate(urls, 1):
            print(f"\n{'═' * 60}")
            print(f"  [{i}/{len(urls)}]")
            print(f"{'═' * 60}")
            try:
                ok = process_single_url(url, args, config)
                if ok:
                    results["success"] += 1
                else:
                    results["failed"] += 1
            except SystemExit:
                results["failed"] += 1
                print(f"  Error processing {url}, continuing...")
            except Exception as e:
                results["failed"] += 1
                print(f"  Unexpected error: {e}")

        print(f"\n{'═' * 60}")
        print(f"  BATCH COMPLETE: {results['success']} ok, {results['failed']} failed")
        print(f"{'═' * 60}")
        return

    # Single URL mode
    if not args.url:
        parser.print_help()
        sys.exit(1)

    check_dependencies(skip_claude=args.transcript_only)

    if not args.transcript_only and not (args.no_publish or args.dry_run):
        config = run_setup()
    else:
        config = load_config()

    process_single_url(args.url, args, config)


if __name__ == "__main__":
    main()
