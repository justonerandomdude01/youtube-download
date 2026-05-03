#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
MAX_PART_BYTES = 1_900 * 1024 * 1024  # 1.9 GB


def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    return "playlist?list=" in url or "/playlist/" in url


def yt_dlp_cmd(url, output_template, playlist):
    """Generate yt-dlp command with proper cookie handling"""

    # Use Deno with remote EJS components
    js_flags = "--js-runtimes deno --remote-components ejs:npm"

    # Format: prioritize 720p
    fmt = "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best"

    no_playlist = "" if playlist else "--no-playlist"

    # Cookies - ensure they're properly formatted
    cookies = f'--cookies "{COOKIES_FILE}"' if COOKIES_FILE and Path(COOKIES_FILE).exists() else ""

    # Critical: Use web client with cookies
    extractor_args = '--extractor-args "youtube:player_client=web"'

    # Mimic a real browser
    user_agent = '--user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'

    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--retries 10 --fragment-retries 10 --sleep-requests 5 '
        f'--sleep-interval 5 --max-sleep-interval 15 '
        f'--no-check-certificates --geo-bypass '
        f'{no_playlist} {cookies} {js_flags} {extractor_args} {user_agent} '
        f'-o "{output_template}" "{url}"'
    )


def read_info_json(tmpdir):
    info_jsons = sorted(Path(tmpdir).rglob("*.info.json"))
    if not info_jsons:
        return {}
    try:
        return json.loads(info_jsons[0].read_text())
    except Exception:
        return {}


def zip_files(files, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return zip_path


def split_and_zip(mp4, tmpdir):
    prefix = str(mp4) + ".part"
    run(f'split -b {MAX_PART_BYTES} "{mp4}" "{prefix}"')
    parts = sorted(Path(tmpdir).glob(mp4.name + ".part*"))
    zips = []
    for i, part in enumerate(parts, 1):
        zip_path = Path(tmpdir) / f"{mp4.stem}.part{i}.zip"
        zip_files([part], zip_path)
        zips.append(zip_path)
    return zips


def release_exists(tag):
    return run(f'gh release view "{tag}" --repo "{REPO}"').returncode == 0


def create_or_upload_release(tag, title, notes, files):
    files_str = " ".join(f'"{f}"' for f in files)
    notes_escaped = notes.replace('"', '\\"').replace('\n', '\\n')

    if release_exists(tag):
        print(f"  Uploading to existing release: {tag}")
        return run(f'gh release upload "{tag}" {files_str} --repo "{REPO}" --clobber')

    print(f"  Creating release: {tag}")
    return run(
        f'gh release create "{tag}" {files_str} '
        f'--repo "{REPO}" --title "{title}" --notes "{notes_escaped}"'
    )


def get_release_url(tag):
    result = run(f'gh release view "{tag}" --repo "{REPO}" --json url -q .url')
    return result.stdout.strip() if result.returncode == 0 else ""


def process_entry(entry, tmpdir):
    url = entry["url"]
    playlist = is_playlist(url)

    # Use title in filename for better readability
    output_template = (
        "%(playlist_title)s/%(playlist_index)03d-%(title)s.%(ext)s"
        if playlist
        else "%(title)s.%(ext)s"
    )

    print(f"\n📥 Downloading: {url}")
    result = run(yt_dlp_cmd(url, str(Path(tmpdir) / output_template), playlist))

    if result.returncode != 0:
        error_msg = result.stderr[-1000:].strip()
        print(f"  ❌ ERROR: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Find downloaded MP4 files
    mp4_files = sorted(Path(tmpdir).rglob("*.mp4"))
    if not mp4_files:
        # Check for other formats
        other_formats = list(Path(tmpdir).rglob("*.mkv")) + list(Path(tmpdir).rglob("*.webm"))
        if other_formats:
            print(f"  Converting {len(other_formats)} files to MP4...")
            for vf in other_formats:
                new_name = vf.with_suffix('.mp4')
                vf.rename(new_name)
                mp4_files.append(new_name)
        else:
            print("  ❌ ERROR: No video files produced")
            entry["status"] = "failed"
            entry["error"] = "No video files produced"
            return entry

    # Read metadata
    info = read_info_json(tmpdir)
    title = info.get("title") or info.get("playlist_title") or Path(mp4_files[0]).stem
    # Clean title for filesystem
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_', '.')).strip()[:100]
    print(f"  📝 Title: {safe_title}")
    print(f"  📁 Files: {len(mp4_files)} video file(s)")

    if playlist:
        pl_id = info.get("playlist_id") or info.get("playlist") or Path(tmpdir).name
        tag = f"yt-playlist-{pl_id}"[:100]

        upload_files = []
        for mp4 in mp4_files:
            size_mb = mp4.stat().st_size / (1024 * 1024)
            print(f"  📦 Size: {size_mb:.2f} MB")

            if mp4.stat().st_size > MAX_PART_BYTES:
                print(f"  ✂ Splitting large file...")
                upload_files.extend(split_and_zip(mp4, tmpdir))
            else:
                zip_path = Path(tmpdir) / f"{mp4.stem}.zip"
                upload_files.append(zip_files([mp4], zip_path))

        notes = f"Source: {url}\nVideos: {len(mp4_files)}"
    else:
        video_id = info.get("id") or mp4_files[0].stem
        tag = f"yt-{safe_title}"[:100]
        mp4 = mp4_files[0]
        size_mb = mp4.stat().st_size / (1024 * 1024)
        print(f"  📦 Size: {size_mb:.2f} MB")

        if mp4.stat().st_size > MAX_PART_BYTES:
            upload_files = split_and_zip(mp4, tmpdir)
            notes = f"Source: {url}\nSplit into {len(upload_files)} parts"
        else:
            zip_path = Path(tmpdir) / f"{safe_title}.zip"
            upload_files = [zip_files([mp4], zip_path)]
            notes = f"Source: {url}"

    result = create_or_upload_release(tag, safe_title, notes, upload_files)
    if result.returncode != 0:
        entry["status"] = "failed"
        entry["error"] = result.stderr[-500:].strip()
        return entry

    entry["status"] = "done"
    entry["title"] = safe_title
    entry["release_tag"] = tag
    entry["release_url"] = get_release_url(tag)
    entry["downloaded_at"] = date.today().isoformat()
    print(f"  ✅ Done: {entry['release_url']}")
    return entry


def main():
    print("=" * 60)
    print("🎬 YouTube Download Script")
    print("=" * 60)

    if not VIDEOS_JSON.exists():
        print(f"❌ Error: {VIDEOS_JSON} not found")
        exit(1)

    videos = json.loads(VIDEOS_JSON.read_text())
    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("✅ No pending videos.")
        return

    print(f"\n📋 Processing {len(pending)} video(s)...")

    for i, entry in enumerate(pending, 1):
        print(f"\n{'─' * 60}")
        print(f"📌 [{i}/{len(pending)}]")
        print(f"{'─' * 60}")

        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)

        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")

    # Summary
    successful = [v for v in videos if v.get("status") == "done"]
    failed = [v for v in videos if v.get("status") == "failed"]

    print("\n" + "=" * 60)
    print(f"✅ Successful: {len(successful)}")
    print(f"❌ Failed: {len(failed)}")

    for f in failed:
        print(f"  - {f.get('url')}: {f.get('error', 'Unknown')[:100]}")

    if failed:
        exit(1)


if __name__ == "__main__":
    main()