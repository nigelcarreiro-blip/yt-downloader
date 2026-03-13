#!/usr/bin/env python3
import subprocess
import json
import os
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import anthropic
from config import ANTHROPIC_API_KEY

DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
YT_DLP        = "/opt/homebrew/bin/yt-dlp"
FFMPEG_DIR    = "/opt/homebrew/bin"

job_status = {}  # job_id -> {status, message, results:[{title,url}], downloaded:int, total:int}
status_lock = threading.Lock()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── LLM SEARCH QUERY ─────────────────────────────────────────────────────────

def refine_query(user_input: str) -> str:
    """Use Claude to turn natural language into an optimal YouTube search query."""
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": (
                f"Convert this into the best YouTube search query to find relevant videos. "
                f"Return ONLY the search query, nothing else, no quotes.\n\n"
                f"Request: {user_input}"
            ),
        }],
    )
    return msg.content[0].text.strip()


# ── SEARCH ────────────────────────────────────────────────────────────────────

MAX_DURATION = 600  # 10 minutes in seconds


def search_youtube(query: str, count: int) -> list:
    """Use yt-dlp to search YouTube, filter to <=10 min, return [{title, url, duration}]."""
    # Fetch 3x more than needed so we have enough after filtering long videos
    fetch = count * 3
    cmd = [
        YT_DLP,
        f"ytsearch{fetch}:{query}",
        "--print", "%(title)s|||%(id)s|||%(duration)s",
        "--no-playlist",
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]

    videos = []
    for line in lines:
        parts = line.split("|||")
        if len(parts) != 3:
            continue
        title, vid_id, duration_str = parts
        try:
            duration = int(float(duration_str))
        except (ValueError, TypeError):
            continue
        if duration > MAX_DURATION:
            continue  # skip videos over 10 minutes
        videos.append({
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "duration": duration,
        })
        if len(videos) >= count:
            break

    return videos


# ── DOWNLOAD ONE VIDEO ────────────────────────────────────────────────────────

def download_video(url: str, quality: str) -> str:
    """Download a single video, return filename."""
    duration_filter = ["--match-filter", f"duration <= {MAX_DURATION}"]

    if quality == "audio":
        cmd = [
            YT_DLP, "--ffmpeg-location", FFMPEG_DIR,
            "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "-o", os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s"),
            "--no-playlist", "--print", "after_move:filepath",
            *duration_filter,
            url,
        ]
    else:
        # Prefer h264+aac for maximum compatibility (QuickTime, Windows, etc.)
        h = {"1080p": 1080, "720p": 720, "480p": 480}.get(quality, None)
        if h:
            fmt = (
                f"bestvideo[vcodec^=avc1][height<={h}]+bestaudio[acodec^=mp4a]/"
                f"bestvideo[vcodec^=avc][height<={h}]+bestaudio[acodec^=mp4a]/"
                f"bestvideo[height<={h}]+bestaudio/best"
            )
        else:
            fmt = (
                "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/"
                "bestvideo+bestaudio/best"
            )

        cmd = [
            YT_DLP, "--ffmpeg-location", FFMPEG_DIR,
            "-f", fmt, "--merge-output-format", "mp4",
            "--postprocessor-args", "ffmpeg:-c:v copy -c:a aac",
            "-o", os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s"),
            "--no-playlist", "--print", "after_move:filepath",
            *duration_filter,
            url,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        filepath = result.stdout.strip().splitlines()[-1]
        return os.path.basename(filepath)
    else:
        err = result.stderr.strip().splitlines()[-1] if result.stderr else "Download failed"
        raise RuntimeError(err)


# ── MAIN JOB RUNNER ───────────────────────────────────────────────────────────

def run_job(job_id: str, query: str, count: int, quality: str):
    try:
        # Step 1: refine query
        with status_lock:
            job_status[job_id]["message"] = "Thinking about your search..."

        refined = refine_query(query)

        with status_lock:
            job_status[job_id]["message"] = f'Searching YouTube for "{refined}"...'
            job_status[job_id]["refined_query"] = refined

        # Step 2: search
        videos = search_youtube(refined, count)
        if not videos:
            with status_lock:
                job_status[job_id] = {"status": "error", "message": "No videos found. Try a different search."}
            return

        with status_lock:
            job_status[job_id]["results"] = videos
            job_status[job_id]["total"] = len(videos)
            job_status[job_id]["message"] = f"Found {len(videos)} videos. Downloading..."

        # Step 3: download each
        for i, video in enumerate(videos):
            with status_lock:
                job_status[job_id]["message"] = f"Downloading {i + 1} of {len(videos)}: {video['title'][:50]}..."

            try:
                filename = download_video(video["url"], quality)
                with status_lock:
                    job_status[job_id]["downloaded"] = i + 1
                    job_status[job_id]["last_saved"] = filename
            except Exception as e:
                with status_lock:
                    job_status[job_id]["errors"] = job_status[job_id].get("errors", [])
                    job_status[job_id]["errors"].append(f"{video['title'][:40]}: {str(e)}")

        downloaded = job_status[job_id].get("downloaded", 0)
        with status_lock:
            job_status[job_id]["status"] = "done"
            job_status[job_id]["message"] = f"Done! {downloaded} of {len(videos)} videos saved to Downloads."

    except Exception as e:
        with status_lock:
            job_status[job_id] = {"status": "error", "message": str(e)}


# ── HTTP SERVER ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_html(self):
        path = os.path.join(os.path.dirname(__file__), "index.html")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_html()
        elif parsed.path == "/status":
            qs = parse_qs(parsed.query)
            job_id = qs.get("id", [None])[0]
            with status_lock:
                info = dict(job_status.get(job_id, {"status": "unknown"}))
            self.send_json(info)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/search":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            query   = body.get("query", "").strip()
            count   = min(int(body.get("count", 5)), 10)
            quality = body.get("quality", "best")

            if not query:
                self.send_json({"error": "No search query provided"}, 400)
                return

            job_id = str(uuid.uuid4())[:8]
            with status_lock:
                job_status[job_id] = {
                    "status": "running",
                    "message": "Starting...",
                    "downloaded": 0,
                    "total": count,
                    "results": [],
                }

            t = threading.Thread(target=run_job, args=(job_id, query, count, quality), daemon=True)
            t.start()
            self.send_json({"job_id": job_id})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    if ANTHROPIC_API_KEY == "your-api-key-here":
        print("⚠️  Add your Anthropic API key to config.py first!")
    port = 8765
    print(f"YouTube AI Downloader running at http://localhost:{port}")
    print("Press Ctrl+C to stop.\n")
    HTTPServer(("localhost", port), Handler).serve_forever()
