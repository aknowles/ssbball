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
from collections import defaultdict
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
        now = datetime.now()
        if now.month >= 8:
            season = str(now.year + 1)
        else:
            season = str(now.year)

    data = urllib.parse.urlencode({
        'clientid': client_id,
        'yrseason': season,
        'teamno': team_no
    }).encode('utf-8')

    # Set origin based on client_id
    if client_id == 'ssybl':
        origin = 'https://ssybl.org'
    else:
        origin = 'https://metrowestbball.com'

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Origin': origin,
        'Referer': f'{origin}/',
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

        hour, minute = 12, 0
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


def parse_schedule_response(data, team_config: dict) -> list[dict]:
    """Parse the API response into game objects."""
    games = []
    team_name = team_config.get('team_name', 'Team')
    short_name = team_config.get('short_name', team_name)
    league = team_config.get('league', 'Basketball')
    grade = team_config.get('grade', '')

    # Handle different response formats
    if isinstance(data, list):
        schedule_data = data
    elif isinstance(data, dict):
        schedule_data = data.get('schedule', data.get('games', data.get('data', [])))
        if isinstance(schedule_data, dict):
            schedule_data = schedule_data.get('games', [])
    else:
        schedule_data = []

    if not isinstance(schedule_data, list):
        logger.warning(f"Unexpected schedule format: {type(schedule_data)}")
        if isinstance(data, dict):
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

            if opponent:
                opponent = re.sub(r'^[@vs.\s]+', '', str(opponent), flags=re.I).strip()

            if not opponent:
                opponent = "TBD"

            game = {
                'datetime': game_dt,
                'opponent': opponent,
                'location': str(location) if location else '',
                'team_name': team_name,
                'short_name': short_name,
                'game_type': str(game_type) if game_type else '',
                'league': league,
                'grade': grade
            }
            games.append(game)
            logger.info(f"Found game: {game_dt.strftime('%b %d %I:%M%p')} vs {opponent}")

        except Exception as e:
            logger.debug(f"Error parsing game: {e}")
            continue

    return games


def fetch_team_games(config: dict) -> list[dict]:
    """Fetch games for a single team."""
    team_name = config.get('team_name', 'Basketball Team')
    client_id = config.get('client_id', 'metrowbb')
    team_no = config.get('team_no', '')
    season = config.get('season', None)

    if not team_no:
        logger.error(f"No team_no configured for {team_name}")
        return []

    data = fetch_schedule(client_id, team_no, season)

    if data:
        games = parse_schedule_response(data, config)
        logger.info(f"Found {len(games)} games for {team_name}")
        return games
    else:
        logger.warning(f"No data returned for {team_name}")
        return []


def dedupe_games(games: list[dict]) -> list[dict]:
    """Remove duplicate games."""
    seen = set()
    unique = []
    for game in games:
        key = (game['datetime'].isoformat(), game['opponent'].lower(), game.get('grade', ''))
        if key not in seen:
            seen.add(key)
            unique.append(game)
    return unique


def generate_ical(games: list[dict], calendar_name: str, calendar_id: str) -> bytes:
    """Generate iCalendar content."""
    cal = Calendar()
    cal.add('prodid', f'-//Basketball Schedule//{calendar_id}//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', calendar_name)
    cal.add('x-wr-timezone', 'America/New_York')

    for game in sorted(games, key=lambda g: g['datetime']):
        event = Event()

        uid = hashlib.md5(
            f"{game['datetime'].isoformat()}-{game['opponent']}-{game.get('grade', '')}-{game.get('league', '')}".encode()
        ).hexdigest()
        event.add('uid', f'{uid}@{calendar_id}')

        opponent = game.get('opponent', 'TBD')
        game_type = game.get('game_type', '').lower()
        short_name = game.get('short_name', '')

        # Build summary with team identifier if multiple teams
        if short_name:
            prefix = f"[{short_name}] "
        else:
            prefix = ""

        if 'away' in game_type or game_type == 'a':
            event.add('summary', f"{prefix}🏀 @ {opponent}")
        else:
            event.add('summary', f"{prefix}🏀 vs {opponent}")

        event.add('dtstart', game['datetime'])
        event.add('dtend', game['datetime'] + timedelta(hours=1, minutes=30))

        if game.get('location'):
            event.add('location', game['location'])

        desc = [
            f"Team: {game.get('team_name', 'Unknown')}",
            f"Opponent: {opponent}",
            f"League: {game.get('league', 'Basketball')}"
        ]
        if game.get('location'):
            desc.append(f"Location: {game['location']}")
        if game.get('game_type'):
            desc.append(f"Game: {game['game_type']}")
        event.add('description', '\n'.join(desc))
        event.add('dtstamp', datetime.now(EASTERN))

        alarm = Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('trigger', timedelta(hours=-1))
        alarm.add('description', f'Basketball game vs {opponent} in 1 hour')
        event.add_component(alarm)

        cal.add_component(event)

    return cal.to_ical()


def generate_index_html(calendars: list[dict], base_url: str) -> str:
    """Generate the landing page HTML."""
    now = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')

    # Group calendars
    combined_cals = [c for c in calendars if c.get('type') == 'combined']
    team_cals = [c for c in calendars if c.get('type') == 'team']

    def make_card(cal, highlight=False):
        cal_id = cal.get('id', 'calendar')
        cal_name = cal.get('name', 'Calendar')
        description = cal.get('description', '')
        games_count = cal.get('games', 0)
        ics_url = f"{base_url}/{cal_id}.ics"

        highlight_class = "highlight" if highlight else ""
        games_info = f"{games_count} games" if games_count else "No games"

        return f'''
        <div class="calendar-card {highlight_class}">
            <h3>{cal_name}</h3>
            <p class="description">{description} &bull; {games_info}</p>
            <div class="subscribe-url">
                <code>{ics_url}</code>
                <button onclick="copyUrl('{ics_url}')" title="Copy URL">📋</button>
            </div>
            <div class="buttons">
                <a href="{cal_id}.ics" class="btn btn-primary" download>Download</a>
                <a href="webcal://{ics_url.replace('https://', '')}" class="btn btn-secondary">Subscribe</a>
            </div>
        </div>
        '''

    combined_html = ''.join(make_card(c, highlight=True) for c in combined_cals)
    team_html = ''.join(make_card(c) for c in team_cals)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Milton Basketball Calendars</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #333;
        }}
        h1 {{ text-align: center; color: #1a1a2e; }}
        h2 {{ color: #1a1a2e; margin-top: 30px; border-bottom: 2px solid #e63946; padding-bottom: 8px; }}
        .subtitle {{ text-align: center; color: #666; margin-bottom: 30px; }}
        .calendar-card {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .calendar-card.highlight {{
            border: 2px solid #e63946;
        }}
        .calendar-card h3 {{ margin: 0 0 8px 0; color: #1a1a2e; }}
        .description {{ color: #666; margin: 0 0 12px 0; font-size: 14px; }}
        .subscribe-url {{
            display: flex;
            align-items: center;
            gap: 8px;
            background: #f0f0f0;
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 12px;
        }}
        .subscribe-url code {{ flex: 1; font-size: 11px; word-break: break-all; }}
        .subscribe-url button {{
            background: none;
            border: none;
            cursor: pointer;
            font-size: 16px;
            padding: 4px;
        }}
        .buttons {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .btn {{
            display: inline-block;
            padding: 8px 16px;
            border-radius: 6px;
            text-decoration: none;
            font-weight: 500;
            font-size: 13px;
        }}
        .btn-primary {{ background: #e63946; color: white; }}
        .btn-secondary {{ background: #1a1a2e; color: white; }}
        .instructions {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-top: 30px;
        }}
        .instructions h2 {{ margin-top: 0; border: none; }}
        .instructions ul {{ padding-left: 20px; }}
        .instructions li {{ margin-bottom: 10px; }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            color: #666;
            font-size: 13px;
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
            z-index: 1000;
        }}
    </style>
</head>
<body>
    <h1>🏀 Milton Basketball</h1>
    <p class="subtitle">Subscribe to automatically sync game schedules to your calendar</p>

    <div id="copied" class="copied">URL Copied!</div>

    <h2>📅 Combined Calendars</h2>
    <p style="color: #666; font-size: 14px;">Best for seeing all games at once</p>
    {combined_html}

    <h2>🏀 Individual Team Calendars</h2>
    <p style="color: #666; font-size: 14px;">One calendar per team/league</p>
    {team_html}

    <div class="instructions">
        <h2>How to Subscribe</h2>
        <ul>
            <li><strong>Google Calendar:</strong> Other calendars (+) → From URL → paste URL</li>
            <li><strong>Apple Calendar:</strong> File → New Calendar Subscription → paste URL</li>
            <li><strong>iPhone/iPad:</strong> Tap "Subscribe" button, or Settings → Calendar → Accounts → Add Subscribed Calendar</li>
            <li><strong>Outlook:</strong> Add calendar → Subscribe from web</li>
        </ul>
        <p><strong>Tip:</strong> Calendars auto-update every 24 hours. Data refreshes every 6 hours.</p>
    </div>

    <p class="footer">
        Last updated: {now}<br>
        Data from MetroWest Basketball &amp; SSYBL
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

    with open(args.config) as f:
        config = json.load(f)

    teams = config.get('teams', [])
    combined_calendars = config.get('combined_calendars', [])
    base_url = args.base_url or config.get('base_url', 'https://example.github.io/ssbball')

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fetch all team schedules
    all_games = []
    team_games = {}  # team_id -> games
    calendar_info = []  # For index.html

    for team_config in teams:
        team_id = team_config.get('id', 'team')
        team_name = team_config.get('team_name', 'Team')

        logger.info(f"Fetching {team_name}...")
        games = fetch_team_games(team_config)

        team_games[team_id] = games
        all_games.extend(games)

        # Generate individual team calendar
        ical_data = generate_ical(games, team_name, team_id)
        ics_path = output_dir / f"{team_id}.ics"
        ics_path.write_bytes(ical_data)
        logger.info(f"Wrote {ics_path} with {len(games)} games")

        calendar_info.append({
            'type': 'team',
            'id': team_id,
            'name': team_config.get('short_name', team_name),
            'description': f"{team_config.get('league', '')}",
            'games': len(games)
        })

    # Generate combined calendars
    for combo in combined_calendars:
        combo_id = combo.get('id', 'combined')
        combo_name = combo.get('name', 'Combined')
        combo_filter = combo.get('filter', {})

        # Filter games
        if combo_filter:
            filtered_games = [
                g for g in all_games
                if all(g.get(k) == v for k, v in combo_filter.items())
            ]
        else:
            filtered_games = all_games

        filtered_games = dedupe_games(filtered_games)

        # Generate calendar
        ical_data = generate_ical(filtered_games, combo_name, combo_id)
        ics_path = output_dir / f"{combo_id}.ics"
        ics_path.write_bytes(ical_data)
        logger.info(f"Wrote {ics_path} with {len(filtered_games)} games")

        calendar_info.insert(0, {  # Add at beginning
            'type': 'combined',
            'id': combo_id,
            'name': combo_name,
            'description': combo.get('description', ''),
            'games': len(filtered_games)
        })

    # Generate index.html
    index_html = generate_index_html(calendar_info, base_url)
    index_path = output_dir / 'index.html'
    index_path.write_text(index_html)
    logger.info(f"Wrote {index_path}")

    # Write status
    summary = {
        'updated': datetime.now(EASTERN).isoformat(),
        'calendars': calendar_info
    }
    (output_dir / 'status.json').write_text(json.dumps(summary, indent=2))

    # Print summary
    print("\n" + "="*50)
    print("Scrape Complete")
    print("="*50)
    for cal in calendar_info:
        print(f"  {cal['name']}: {cal['games']} games")
    print(f"\nOutput: {output_dir}/")


if __name__ == '__main__':
    main()
