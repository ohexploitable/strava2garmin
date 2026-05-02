"""
Syncs activities from Strava to Garmin Connect.

Flow:
  1. Authorize with Strava (OAuth2, opens browser on first run)
  2. Fetch recent activities from Strava, skipping Garmin-originated and manual ones
  3. Connect to Garmin Connect and filter out activities already present (by start time)
  4. Fetch GPS/metrics streams + calorie data from Strava and build a TCX file
  5. Upload each TCX to Garmin Connect
"""

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")

STRAVA_TOKEN_FILE = Path(".strava_token.json")
GARMIN_TOKENSTORE = Path(".garmin_tokens")
TCX_DIR = Path("tcx_cache")

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
REDIRECT_URI = "http://localhost:8000/callback"

SPORT_MAP = {
    "Ride": "Biking", "VirtualRide": "Biking", "EBikeRide": "Biking",
    "MountainBikeRide": "Biking", "GravelRide": "Biking",
    "Run": "Running", "VirtualRun": "Running", "TrailRun": "Running",
}


# ── Strava OAuth ──────────────────────────────────────────────────────────────

def strava_authorize():
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all",
    }
    print("Opening Strava authorization in your browser...")
    webbrowser.open(f"{STRAVA_AUTH_URL}?{urlencode(params)}")

    code = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal code
            code = parse_qs(urlparse(self.path).query).get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization complete. You can close this tab.")

        def log_message(self, *_):
            pass

    print("Waiting for Strava authorization...")
    HTTPServer(("localhost", 8000), Handler).handle_request()

    if not code:
        print("Authorization failed — no code received.")
        sys.exit(1)

    return code


def get_strava_token():
    if STRAVA_TOKEN_FILE.exists():
        token_data = json.loads(STRAVA_TOKEN_FILE.read_text())
        if token_data["expires_at"] < time.time() + 60:
            print("Refreshing Strava token...")
            resp = requests.post(STRAVA_TOKEN_URL, data={
                "client_id": STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "refresh_token": token_data["refresh_token"],
                "grant_type": "refresh_token",
            })
            resp.raise_for_status()
            token_data = resp.json()
            STRAVA_TOKEN_FILE.write_text(json.dumps(token_data))
        return token_data["access_token"]

    code = strava_authorize()
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    token_data = resp.json()
    STRAVA_TOKEN_FILE.write_text(json.dumps(token_data))
    print("Strava authorization successful.\n")
    return token_data["access_token"]


# ── Strava data ───────────────────────────────────────────────────────────────

def strava_get(path, token, **params):
    resp = requests.get(
        f"{STRAVA_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_activities(token, after=None):
    activities = []
    page = 1
    params = {"per_page": 50}
    if after:
        params["after"] = int(after)
    while True:
        page_data = strava_get("/athlete/activities", token, page=page, **params)
        if not page_data:
            break
        activities.extend(page_data)
        page += 1
    return activities


def fetch_streams(token, activity_id):
    keys = "time,latlng,altitude,heartrate,cadence,watts,distance"
    try:
        return strava_get(f"/activities/{activity_id}/streams", token, keys=keys, key_by_type=True)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return {}
        raise


def should_sync(activity):
    ext_id = (activity.get("external_id") or "").lower()
    if ext_id.startswith("garmin_"):
        return False
    if activity.get("manual"):
        return False
    return True


# ── TCX builder ───────────────────────────────────────────────────────────────

def build_tcx(activity, detail, streams):
    sport      = SPORT_MAP.get(activity.get("type", ""), "Other")
    name       = "[s2g] " + activity.get("name", "Activity")
    start_time = activity.get("start_date", "")
    calories   = int(detail.get("calories") or 0)
    elapsed    = detail.get("elapsed_time", 0)
    distance   = detail.get("distance", 0)

    times       = streams.get("time",      {}).get("data", [])
    latlng      = streams.get("latlng",    {}).get("data", [])
    altitude    = streams.get("altitude",  {}).get("data", [])
    heartrate   = streams.get("heartrate", {}).get("data", [])
    cadence     = streams.get("cadence",   {}).get("data", [])
    power       = streams.get("watts",     {}).get("data", [])
    dist_stream = streams.get("distance",  {}).get("data", [])

    start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))

    def iso(offset):
        ts = start_dt.timestamp() + offset
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TrainingCenterDatabase',
        '  xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"',
        '  xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">',
        '  <Activities>',
        f'    <Activity Sport="{sport}">',
        f'      <Id>{start_time}</Id>',
        f'      <Lap StartTime="{start_time}">',
        f'        <TotalTimeSeconds>{elapsed}</TotalTimeSeconds>',
        f'        <DistanceMeters>{distance:.1f}</DistanceMeters>',
        f'        <Calories>{calories}</Calories>',
        '        <Intensity>Active</Intensity>',
        '        <TriggerMethod>Manual</TriggerMethod>',
        '        <Track>',
    ]

    for i, t in enumerate(times):
        if i >= len(latlng):
            break
        lat, lon = latlng[i]
        lines.append('          <Trackpoint>')
        lines.append(f'            <Time>{iso(t)}</Time>')
        lines.append('            <Position>')
        lines.append(f'              <LatitudeDegrees>{lat}</LatitudeDegrees>')
        lines.append(f'              <LongitudeDegrees>{lon}</LongitudeDegrees>')
        lines.append('            </Position>')
        if i < len(altitude):
            lines.append(f'            <AltitudeMeters>{altitude[i]:.1f}</AltitudeMeters>')
        if i < len(dist_stream):
            lines.append(f'            <DistanceMeters>{dist_stream[i]:.1f}</DistanceMeters>')
        if i < len(heartrate):
            lines.append(f'            <HeartRateBpm><Value>{heartrate[i]}</Value></HeartRateBpm>')
        if i < len(cadence):
            lines.append(f'            <Cadence>{cadence[i]}</Cadence>')
        if i < len(power):
            lines.append(f'            <Extensions><ns3:TPX><ns3:Watts>{power[i]}</ns3:Watts></ns3:TPX></Extensions>')
        lines.append('          </Trackpoint>')

    lines += [
        '        </Track>',
        '      </Lap>',
        f'      <Notes>{name}</Notes>',
        '    </Activity>',
        '  </Activities>',
        '</TrainingCenterDatabase>',
    ]
    return "\n".join(lines)


# ── Garmin connection ─────────────────────────────────────────────────────────

def connect_garmin():
    garmin = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    try:
        garmin.login(str(GARMIN_TOKENSTORE))
        print("Garmin: loaded saved session.")
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        print("Garmin: no saved session — logging in fresh...")
        garmin.login()
        garmin.garth.dump(str(GARMIN_TOKENSTORE))
        print("Garmin: session saved.")
    except GarminConnectTooManyRequestsError:
        print("Garmin: rate limited — wait a few minutes and retry.")
        sys.exit(1)
    return garmin





# ── Credential bootstrap (Cloud Run) ─────────────────────────────────────────

def bootstrap_credentials():
    # Restore Strava token file from env var if missing (e.g. cold start on Cloud Run)
    refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")
    if refresh_token and not STRAVA_TOKEN_FILE.exists():
        STRAVA_TOKEN_FILE.write_text(json.dumps({
            "refresh_token": refresh_token,
            "access_token": "",
            "expires_at": 0,
        }))
        print("Restored Strava token from environment.")

    # Restore Garmin token directory from base64-encoded tar.gz env var if missing
    garmin_tokens_b64 = os.getenv("GARMIN_TOKENS")
    if garmin_tokens_b64 and not GARMIN_TOKENSTORE.exists():
        data = base64.b64decode(garmin_tokens_b64)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(".")
        print("Restored Garmin tokens from environment.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run=False, after=None):
    bootstrap_credentials()

    for var, name in [
        (STRAVA_CLIENT_ID, "STRAVA_CLIENT_ID"),
        (STRAVA_CLIENT_SECRET, "STRAVA_CLIENT_SECRET"),
        (GARMIN_EMAIL, "GARMIN_EMAIL"),
        (GARMIN_PASSWORD, "GARMIN_PASSWORD"),
    ]:
        if not var or var.startswith("your_"):
            print(f"Missing credential: {name} — please fill in .env")
            sys.exit(1)

    TCX_DIR.mkdir(exist_ok=True)

    garmin = connect_garmin()
    garmin_starts = {a["startTimeGMT"][:16] for a in garmin.get_activities(0, 200)}
    print(f"Garmin: {len(garmin_starts)} existing activities loaded for dedup.\n")

    print("Fetching Strava token...")
    token = get_strava_token()

    print("Fetching Strava activities...")
    all_activities = fetch_activities(token, after=after)
    activities = [
        a for a in all_activities
        if should_sync(a) and a["start_date"].replace("T", " ")[:16] not in garmin_starts
    ]
    print(f"Found {len(activities)} activities to sync "
          f"({len(all_activities) - len(activities)} skipped).\n")

    new_count = 0
    for activity in activities:
        activity_id = activity["id"]
        name = activity.get("name", "Activity")
        date = activity.get("start_date_local", "")[:10]
        sport = activity.get("type", "")
        print(f"Syncing: [{date}] {name} ({sport})")

        streams = fetch_streams(token, activity_id)
        if not streams.get("latlng"):
            print("  No GPS data — skipping.")
            continue

        detail = strava_get(f"/activities/{activity_id}", token)
        tcx_content = build_tcx(activity, detail, streams)
        tcx_path = TCX_DIR / f"{activity_id}.tcx"
        tcx_path.write_text(tcx_content)

        if dry_run:
            print(f"  [dry run] Would upload ({detail.get('calories', '?')} kcal).")
            new_count += 1
            continue

        try:
            garmin.upload_activity(str(tcx_path))
            tcx_path.unlink()
            print(f"  Uploaded ({detail.get('calories', '?')} kcal).")
            new_count += 1
        except Exception as e:
            print(f"  Upload failed: {e}")
            time.sleep(1)
            continue

        key = activity["start_date"].replace("T", " ")[:16]
        utc_dt = datetime.strptime(activity["start_date"][:10], "%Y-%m-%d")
        match = None
        for _ in range(10):
            time.sleep(2)
            fresh = garmin.get_activities_by_date(
                (utc_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
                (utc_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
            )
            match = next((a for a in fresh if a["startTimeGMT"][:16] == key), None)
            if match:
                break
        if match:
            try:
                garmin.garth.connectapi(
                    f"/activity-service/activity/{match['activityId']}",
                    method="PUT",
                    json={"activityName": "[s2g] " + name},
                )
                print(f"  Renamed to: [s2g] {name}")
            except Exception as e:
                print(f"  Rename failed: {e}")
        else:
            print("  Rename skipped: activity not yet indexed.")

    if new_count == 0:
        print("Nothing new to sync.")
    else:
        print(f"\nDone. Synced {new_count} new activities to Garmin Connect.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Strava activities to Garmin Connect.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without uploading.")
    parser.add_argument("--after", metavar="YYYY-MM-DD", help="Only sync activities after this date.")
    args = parser.parse_args()

    if args.after:
        after_ts = datetime.strptime(args.after, "%Y-%m-%d").timestamp()
    else:
        after_ts = (datetime.now() - timedelta(days=7)).timestamp()

    main(dry_run=args.dry_run, after=after_ts)
