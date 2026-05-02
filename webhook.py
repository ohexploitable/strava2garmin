"""
Webhook listener for Strava activity events.

Strava calls this server when a new activity is created.
We return a quick "OK" to Strava, then trigger sync.py in the background.

Two endpoints:
  GET  /webhook  — one-time verification handshake when you register with Strava
  POST /webhook  — receives activity events going forward
"""

import os
import sys
import subprocess
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

STRAVA_VERIFY_TOKEN = os.getenv("STRAVA_VERIFY_TOKEN")

if not STRAVA_VERIFY_TOKEN or STRAVA_VERIFY_TOKEN.startswith("your_"):
    print("Missing credential: STRAVA_VERIFY_TOKEN — please fill in .env")
    sys.exit(1)

app = Flask(__name__)


@app.route("/webhook", methods=["GET"])
def verify():
    """
    Strava sends a GET request once when you first register the webhook.
    It includes a challenge token — we echo it back to prove we control this URL.
    """
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == STRAVA_VERIFY_TOKEN:
        print("Webhook verified by Strava.")
        return jsonify({"hub.challenge": challenge}), 200

    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive():
    """
    Strava POSTs here every time an activity event occurs.
    We must respond within a few seconds or Strava considers it failed,
    so we kick off sync.py as a background process and immediately return OK.
    """
    data = request.json
    print(f"Received event: {data}")

    if data.get("object_type") == "activity" and data.get("aspect_type") == "create":
        activity_id = data.get("object_id")
        print(f"New activity detected (id={activity_id}) — triggering sync...")
        # Popen is non-blocking: sync runs in the background while we return OK
        subprocess.Popen([sys.executable, "sync.py"], cwd=os.path.dirname(__file__))

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
