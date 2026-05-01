# strava-to-garmin

Syncs non-Garmin activities from Strava to Garmin Connect. Uploads as TCX files with calorie data, and skips activities that already exist on Garmin Connect.

## Setup

**1. Install dependencies**
```
pip install -r requirements.txt
```

**2. Create your `.env` file**
```
cp .env.example .env
```

Fill in your credentials:
- **Strava**: create an API application at [strava.com/settings/api](https://www.strava.com/settings/api) and copy the Client ID and Secret
- **Garmin**: your regular Garmin Connect login email and password

**3. Authorize Strava**

On the first run, a browser tab will open asking you to authorize the app. After you click "Authorize", the token is saved to `strava_token.json` and reused automatically from then on.

## Usage

```
python sync.py
```

**Options**

| Flag | Description |
|------|-------------|
| `--after YYYY-MM-DD` | Only sync activities after this date |
| `--dry-run` | Show what would be synced without uploading |

**Examples**
```
# Sync everything from 1 Jan 2025 onwards
python sync.py --after 2025-01-01

# Preview what would be synced
python sync.py --after 2025-01-01 --dry-run
```

## What gets synced

Activities are skipped if they:
- originated from a Garmin device (already on Garmin Connect)
- were manually entered on Strava (no GPS or sensor data)
- already exist on Garmin Connect (matched by start time)
