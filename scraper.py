#!/usr/bin/env python3
"""
Basketball Schedule Scraper for GitHub Actions

Fetches schedules from sportsite2.com API (used by metrowestbball.com and ssybl.org),
generates iCal files, and outputs them for GitHub Pages hosting.

No Selenium required - uses direct API calls!

Usage:
    python scraper.py --config teams.json --output docs/
"""

import argparse
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
import urllib.request
import urllib.parse

from icalendar import Calendar, Event, Alarm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Eastern timezone for MA basketball leagues
EASTERN = ZoneInfo("America/New_York")

# API endpoint
API_URL = "https://sportsite2.com/getTeamSchedule.php"


def fetch_schedule(client_id: str, team_no: str, season: str = None) -> dict:
    """Fetch schedule from sportsite2.com API."""
    if not season:
        # Season is typically the ending year (2025-2026 season = 2026)
        now = datetime.now()
        if now.month >= 8:  # Aug onwards is next year's season
            season = str(now.year + 1)
        else:
            season = str(now.year)

    data = urllib.parse.urlencode({
        'clientid': client_id,
        'yrseason': season,
        'teamno': team_no
    }).encode('utf-8')

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Origin': f'https://{client_id.replace("wbb", "westbball")}.com',
        'Referer': f'https://{client_id.replace("wbb", "westbball")}.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    logger.info(f"Fetching schedule: clientid={client_id}, teamno={team_no}, season={season}")

    req = urllib.request.Request(API_URL, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read().decode('utf-8')
            logger.debug(f"Response: {content[:500]}")
            return json.loads(content)
    except Exception as e:
        logger.error(f"API request failed: {e}")
        return {}


def parse_api_date(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse date and time from API response."""
    try:
        # Handle various date formats
        # Format: "12/7/2025" or "2025-12-07"
        if '/' in date_str:
            parts = date_str.split('/')
            if len(parts) == 3:
                month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                return None
        elif '-' in date_str:
            parts = date_str.split('-')
            if len(parts) == 3:
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                return None
        else:
            # Try "Dec 7" format
            match = re.match(r'([A-Za-z]+)\s*(\d{1,2})', date_str)
            if match:
                months = {
                    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
                }
                month = months.get(match.group(1).lower()[:3], 1)
                day = int(match.group(2))
                now = datetime.now()
                year = now.year + 1 if month < 6 and now.month > 8 else now.year
            else:
                return None

        # Parse time
        hour, minute = 12, 0  # Default
        if time_str:
            time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?', time_str)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                ampm = time_match.group(3)
                if ampm:
                    if ampm.upper() == 'PM' and hour != 12:
                        hour += 12
                    elif ampm.upper() == 'AM' and hour == 12:
                        hour = 0

        return datetime(year, month, day, hour, minute, tzinfo=EASTERN)
    except Exception as e:
        logger.warning(f"Could not parse date/time: {date_str} {time_str} - {e}")
        return None


def parse_schedule_response(data: dict, team_name: str, league: str) -> list[dict]:
    """Parse the API response into game objects."""
    games = []

    # The API returns various formats - try to handle them
    schedule_data = data.get('schedule', data.get('games', data.get('data', [])))

    if isinstance(schedule_data, dict):
        schedule_data = schedule_data.get('games', [])

    if not isinstance(schedule_data, list):
        logger.warning(f"Unexpected schedule format: {type(schedule_data)}")
        # Try to find any list in the response
        for key, value in data.items():
            if isinstance(value, list) and len(value) > 0:
                schedule_data = value
                break

    logger.info(f"Found {len(schedule_data) if isinstance(schedule_data, list) else 0} items in schedule")

    if not isinstance(schedule_data, list):
        return games

    for item in schedule_data:
        try:
            if not isinstance(item, dict):
                continue

            # Extract fields (field names may vary)
            date_str = item.get('date', item.get('gamedate', item.get('gdate', '')))
            time_str = item.get('time', item.get('gametime', item.get('gtime', '')))
            opponent = item.get('opponent', item.get('opp', item.get('oppname', '')))
            location = item.get('location', item.get('loc', item.get('facility', '')))
            game_type = item.get('homeaway', item.get('ha', item.get('type', '')))

            if not date_str:
                continue

            game_dt = parse_api_date(date_str, time_str)
            if not game_dt:
                continue

            # Clean opponent name
            if opponent:
                opponent = re.sub(r'^[@vs.\s]+', '', str(opponent), flags=re.I).strip()

            if not opponent:
                opponent = "TBD"

            game = {
                'datetime': game_dt,
                'opponent': opponent,
                'location': str(location) if location else '',
                'home_team': team_name,
                'game_type': str(game_type) if game_type else '',
                'league': league
            }
            games.append(game)
            logger.info(f"Found game: {game_dt.strftime('%b %d %I:%M%p')} vs {opponent}")

        except Exception as e:
            logger.debug(f"Error parsing game: {e}")
            continue

    return games


def scrape_team(config: dict) -> tuple[list[dict], bytes]:
    """Fetch schedule for a single team."""
    all_games = []

    team_name = config.get('team_name', 'Basketball Team')

    # Get API parameters
    client_id = config.get('client_id', 'metrowbb')
    team_no = config.get('team_no', '')
    season = config.get('season', None)
    league = config.get('league', 'MetroWest')

    if not team_no:
        logger.error(f"No team_no configured for {team_name}")
        return [], generate_ical([], team_name, config.get('id', 'team'))

    # Fetch from API
    data = fetch_schedule(client_id, team_no, season)

    if data:
        games = parse_schedule_response(data, team_name, league)
        all_games.extend(games)
        logger.info(f"Found {len(games)} games for {team_name}")
    else:
        logger.warning(f"No data returned for {team_name}")

    # Dedupe
    all_games = dedupe_games(all_games)

    calendar_id = config.get('id', 'basketball')
    ical_data = generate_ical(all_games, team_name, calendar_id)

    return all_games, ical_data


def dedupe_games(games: list[dict]) -> list[dict]:
    """Remove duplicate games."""
    seen = set()
    unique = []
    for game in games:
        key = (game['datetime'].isoformat(), game['opponent'].lower())
        if key not in seen:
            seen.add(key)
            unique.append(game)
    return unique


def generate_ical(games: list[dict], team_name: str, calendar_id: str) -> bytes:
    """Generate iCalendar content."""
    cal = Calendar()
    cal.add('prodid', f'-//Basketball Schedule//{calendar_id}//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f'{team_name} Basketball')
    cal.add('x-wr-timezone', 'America/New_York')

    for game in sorted(games, key=lambda g: g['datetime']):
        event = Event()

        uid = hashlib.md5(
            f"{game['datetime'].isoformat()}-{game['opponent']}-{team_name}".encode()
        ).hexdigest()
        event.add('uid', f'{uid}@{calendar_id}')

        opponent = game.get('opponent', 'TBD')
        game_type = game.get('game_type', '').lower()

        # Include home/away in title
        if 'away' in game_type or game_type == 'a':
            event.add('summary', f"🏀 @ {opponent}")
        else:
            event.add('summary', f"🏀 vs {opponent}")

        event.add('dtstart', game['datetime'])
        event.add('dtend', game['datetime'] + timedelta(hours=1, minutes=30))

        if game.get('location'):
            event.add('location', game['location'])

        desc = [
            f"Team: {team_name}",
            f"Opponent: {opponent}",
            f"League: {game.get('league', 'Basketball')}"
        ]
        if game.get('location'):
            desc.append(f"Location: {game['location']}")
        if game.get('game_type'):
            desc.append(f"Game: {game['game_type']}")
        event.add('description', '\n'.join(desc))
        event.add('dtstamp', datetime.now(EASTERN))

        # 1-hour reminder
        alarm = Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('trigger', timedelta(hours=-1))
        alarm.add('description', f'Basketball game vs {opponent} in 1 hour')
        event.add_component(alarm)

        cal.add_component(event)

    return cal.to_ical()


def generate_index_html(teams: list[dict], base_url: str, results: list[dict]) -> str:
    """Generate the landing page HTML."""
    now = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')

    team_cards = []
    for team, result in zip(teams, results):
        team_id = team.get('id', 'team')
        team_name = team.get('team_name', 'Team')
        ics_url = f"{base_url}/{team_id}.ics"
        games_count = result.get('games', 0)

        games_info = f"{games_count} games" if games_count else "No games found"

        team_cards.append(f'''
        <div class="team-card">
            <h2>{team_name}</h2>
            <p class="league">{team.get('league', 'Basketball')} &bull; {games_info}</p>
            <div class="subscribe-url">
                <code>{ics_url}</code>
                <button onclick="copyUrl('{ics_url}')" title="Copy URL">📋</button>
            </div>
            <div class="buttons">
                <a href="{team_id}.ics" class="btn btn-primary" download>Download .ics</a>
                <a href="webcal://{ics_url.replace('https://', '')}" class="btn btn-secondary">Subscribe (iOS/macOS)</a>
            </div>
        </div>
        ''')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Basketball Calendar Subscriptions</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #333;
        }}
        h1 {{
            text-align: center;
            color: #1a1a2e;
        }}
        .subtitle {{
            text-align: center;
            color: #666;
            margin-bottom: 30px;
        }}
        .team-card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .team-card h2 {{
            margin: 0 0 8px 0;
            color: #1a1a2e;
        }}
        .league {{
            color: #666;
            margin: 0 0 16px 0;
            font-size: 14px;
        }}
        .subscribe-url {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: #f0f0f0;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 16px;
        }}
        .subscribe-url code {{
            flex: 1;
            font-size: 12px;
            word-break: break-all;
        }}
        .subscribe-url button {{
            background: none;
            border: none;
            cursor: pointer;
            font-size: 18px;
            padding: 4px;
        }}
        .buttons {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .btn {{
            display: inline-block;
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
            font-size: 14px;
            transition: transform 0.1s;
        }}
        .btn:hover {{ transform: translateY(-1px); }}
        .btn-primary {{
            background: #e63946;
            color: white;
        }}
        .btn-secondary {{
            background: #1a1a2e;
            color: white;
        }}
        .instructions {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-top: 30px;
        }}
        .instructions h2 {{
            margin-top: 0;
        }}
        .instructions ul {{
            padding-left: 20px;
        }}
        .instructions li {{
            margin-bottom: 12px;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            color: #666;
            font-size: 14px;
        }}
        .copied {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #4caf50;
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            display: none;
        }}
    </style>
</head>
<body>
    <h1>🏀 Basketball Calendars</h1>
    <p class="subtitle">Subscribe to automatically sync game schedules to your calendar</p>

    <div id="copied" class="copied">URL Copied!</div>

    {''.join(team_cards)}

    <div class="instructions">
        <h2>How to Subscribe</h2>
        <ul>
            <li><strong>Google Calendar:</strong> Click the + next to "Other calendars" → "From URL" → paste the URL</li>
            <li><strong>Apple Calendar (Mac):</strong> File → New Calendar Subscription → paste the URL</li>
            <li><strong>iPhone/iPad:</strong> Click the "Subscribe" button above, or go to Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar</li>
            <li><strong>Outlook:</strong> Add calendar → Subscribe from web → paste the URL</li>
        </ul>
        <p><strong>Note:</strong> Subscribed calendars auto-update! Most apps refresh every 24 hours, but schedules are updated every 6 hours.</p>
    </div>

    <p class="footer">
        Last updated: {now}<br>
        Schedules sourced from MetroWest Basketball and SSYBL
    </p>

    <script>
        function copyUrl(url) {{
            navigator.clipboard.writeText(url).then(() => {{
                const el = document.getElementById('copied');
                el.style.display = 'block';
                setTimeout(() => el.style.display = 'none', 2000);
            }});
        }}
    </script>
</body>
</html>
'''


def main():
    parser = argparse.ArgumentParser(description='Basketball Schedule Scraper')
    parser.add_argument('--config', '-c', required=True, help='Teams config file (JSON)')
    parser.add_argument('--output', '-o', default='docs', help='Output directory for ICS files')
    parser.add_argument('--base-url', '-u', default='', help='Base URL for calendar links')
    args = parser.parse_args()

    # Load config
    with open(args.config) as f:
        config = json.load(f)

    teams = config.get('teams', [config])
    base_url = args.base_url or config.get('base_url', 'https://example.github.io/ssbball')

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Process each team
    results = []
    for team_config in teams:
        team_id = team_config.get('id', 'team')
        team_name = team_config.get('team_name', 'Team')

        logger.info(f"Processing {team_name}...")

        try:
            games, ical_data = scrape_team(team_config)

            # Write ICS file
            ics_path = output_dir / f"{team_id}.ics"
            ics_path.write_bytes(ical_data)
            logger.info(f"Wrote {ics_path} with {len(games)} games")

            results.append({
                'team': team_name,
                'id': team_id,
                'games': len(games),
                'file': str(ics_path)
            })
        except Exception as e:
            logger.error(f"Failed to process {team_name}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            results.append({
                'team': team_name,
                'id': team_id,
                'games': 0,
                'error': str(e)
            })

    # Generate index.html
    index_html = generate_index_html(teams, base_url, results)
    index_path = output_dir / 'index.html'
    index_path.write_text(index_html)
    logger.info(f"Wrote {index_path}")

    # Write results summary
    summary = {
        'updated': datetime.now(EASTERN).isoformat(),
        'teams': results
    }
    summary_path = output_dir / 'status.json'
    summary_path.write_text(json.dumps(summary, indent=2))

    # Print summary
    print("\n" + "="*50)
    print("Scrape Complete")
    print("="*50)
    for r in results:
        status = f"{r['games']} games" if r['games'] else f"ERROR: {r.get('error', 'unknown')}"
        print(f"  {r['team']}: {status}")
    print(f"\nOutput: {output_dir}/")


if __name__ == '__main__':
    main()
