"""
app.py
------
HTTP API + web UI for the YouTube Shorts downloader.

Endpoints
---------
GET  /                → web UI (templates/index.html)
GET  /api/health      → {"status":"ok", "browser_cache": "..."}
GET  /api/version     → {"name":"ytshortdown-scraper", "version":"1.0.0"}
POST /api/fetch       → main endpoint, JSON in / JSON out

POST /api/fetch
---------------
Request:
    Content-Type: application/json
    {
        "url": "https://www.youtube.com/shorts/XXXXXXXXXXX",
        "quality": "1080p"        # optional, default "1080p"
                                  # one of: 360p, 480p, 720p, 1080p
    }

Response 200 (success):
    {
        "success": true,
        "download_url": "https://cdn400.savetube.vip/media/.../...savetube.me.mp4",
        "quality": "1080p",
        "source": "json",
        "error": null
    }

Response 200 (scraper returned no URL):
    {
        "success": false,
        "download_url": null,
        "quality": "1080p",
        "source": null,
        "error": "No viable download URL found."
    }

Response 400 (bad input):
    {
        "success": false,
        "download_url": null,
        "quality": "1080p",
        "source": null,
        "error": "url is required"
    }
"""

import os

# ---------------------------------------------------------------------------
# Point Playwright at the browsers already installed on this machine.
# MUST be set before importing anything that touches playwright.
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    r"C:\Users\PC\AppData\Local\ms-playwright",
)

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

from scraper import get_download_url


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)   # allow browser clients from any origin to call /api/*

API_NAME    = "ytshortdown-scraper"
API_VERSION = "1.0.0"

# Optional API key. Leave empty to disable auth.
API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

ALLOWED_QUALITIES = {"360p", "480p", "720p", "1080p"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_error(message: str, *, quality: str = "1080p", status: int = 400):
    return jsonify({
        "success": False,
        "download_url": None,
        "quality": quality,
        "source": None,
        "error": message,
    }), status


def _check_api_key():
    """Return None if OK, otherwise an error response."""
    if not API_KEY:
        return None
    provided = (
        request.headers.get("X-API-Key")
        or (request.args.get("api_key") if request.method == "GET" else None)
        or ""
    ).strip()
    if provided != API_KEY:
        return _json_error("Invalid or missing API key.", status=401)
    return None


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "name": API_NAME,
        "version": API_VERSION,
        "browser_cache": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        "auth_required": bool(API_KEY),
    })


@app.route("/api/version", methods=["GET"])
def version():
    return jsonify({
        "name": API_NAME,
        "version": API_VERSION,
        "endpoints": {
            "fetch":  "POST /api/fetch",
            "health": "GET  /api/health",
            "version":"GET  /api/version",
        },
    })


@app.route("/api/fetch", methods=["POST"])
def fetch():
    # ---- auth (optional) ----
    auth_err = _check_api_key()
    if auth_err:
        return auth_err

    # ---- parse body ----
    data = request.get_json(silent=True)
    if data is None:
        # Also accept form-encoded bodies for convenience
        data = request.form.to_dict() if request.form else None
    if not isinstance(data, dict):
        return _json_error("JSON body is required.")

    short_url = (data.get("url") or "").strip()
    quality   = (data.get("quality") or "1080p").strip().lower()

    # ---- validate ----
    if not short_url:
        return _json_error("url is required", quality=quality)

    if not ("youtube.com" in short_url or "youtu.be" in short_url):
        return _json_error(
            "Only YouTube / YouTube Shorts URLs are supported.",
            quality=quality,
        )

    if quality not in ALLOWED_QUALITIES:
        return _json_error(
            f"quality must be one of: {', '.join(sorted(ALLOWED_QUALITIES))}",
            quality=quality,
        )

    # ---- run the scraper ----
    try:
        result = get_download_url(short_url, quality)
    except Exception as e:
        return _json_error(f"{type(e).__name__}: {e}", quality=quality)

    # ---- normalize the response shape ----
    response = {
        "success":      bool(result.get("success")),
        "download_url": result.get("download_url"),
        "quality":      result.get("quality") or quality,
        "source":       result.get("source"),
        "error":        result.get("error"),
    }
    return jsonify(response), 200


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # use_reloader=False prevents Flask's auto-reloader from killing the
    # Playwright Node driver mid-run (EPIPE crashes on Windows).
    app.run(
        debug=True,
        use_reloader=False,
        host="0.0.0.0",
        port=10000,
    )