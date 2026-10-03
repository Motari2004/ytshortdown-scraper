"""
app.py — Render-ready Flask API + web UI.

Reads PORT from environment (Render sets this automatically).
"""

import os

# Load .env for local dev
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

from scraper import get_download_url


app = Flask(__name__)
CORS(app)

API_NAME    = "ytshortdown-scraper"
API_VERSION = "1.0.0"

API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()

ALLOWED_QUALITIES = {"360p", "480p", "720p", "1080p"}


def _json_error(message: str, *, quality: str = "1080p", status: int = 400):
    return jsonify({
        "success": False,
        "download_url": None,
        "quality": quality,
        "source": None,
        "error": message,
    }), status


def _check_api_key():
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "name": API_NAME,
        "version": API_VERSION,
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
    auth_err = _check_api_key()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict() if request.form else None
    if not isinstance(data, dict):
        return _json_error("JSON body is required.")

    short_url = (data.get("url") or "").strip()
    quality   = (data.get("quality") or "1080p").strip().lower()

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

    try:
        result = get_download_url(short_url, quality)
    except Exception as e:
        return _json_error(f"{type(e).__name__}: {e}", quality=quality)

    response = {
        "success":      bool(result.get("success")),
        "download_url": result.get("download_url"),
        "quality":      result.get("quality") or quality,
        "source":       result.get("source"),
        "error":        result.get("error"),
    }
    return jsonify(response), 200


if __name__ == "__main__":
    # Local dev: 5000. On Render, PORT is provided automatically.
    port = int(os.environ.get("PORT", 10000))
    app.run(
        debug=False,
        use_reloader=False,
        host="0.0.0.0",
        port=port,
    )