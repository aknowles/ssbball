#!/usr/bin/env python3
"""
Basketball Schedule Scraper for GitHub Actions

Fetches schedules from sportsite2.com API (used by metrowestbball.com and ssybl.org),
generates iCal files, and outputs them for GitHub Pages hosting.

Features:
- Dynamic team discovery by town name
- Parses town IDs from league websites
- Creates individual and combined calendars
- No Selenium required - uses direct API calls!

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

# API endpoints
API_BASE = "https://sportsite2.com"
TEAM_SCHEDULE_URL = f"{API_BASE}/getTeamSchedule.php"
TEAM_DISCOVERY_URL = f"{API_BASE}/getTownGenderGradeTeams.php"

# League configurations
LEAGUES = {
    'ssybl': {
        'name': 'SSYBL',
        'url': 'https://ssybl.org/launch.php',
        'origin': 'https://ssybl.org'
    },
    'metrowbb': {
        'name': 'MetroWest',
        'url': 'https://metrowestbball.com/launch.php',
        'origin': 'https://metrowestbball.com'
    }
}


def get_season() -> str:
    """Calculate the current season (year)."""
    now = datetime.now()
    # Season runs Aug-Mar, so Aug+ is next year's season
    if now.month >= 8:
        return str(now.year + 1)
    return str(now.year)


def fetch_url(url: str, headers: dict = None) -> str:
    """Fetch a URL and return content."""
    default_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    if headers:
        default_headers.update(headers)

    req = urllib.request.Request(url, headers=default_headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return ""


def fetch_api(url: str, data: dict, client_id: str) -> dict:
    """Make a POST request to the API."""
    league = LEAGUES.get(client_id, LEAGUES['metrowbb'])

    encoded_data = urllib.parse.urlencode(data).encode('utf-8')

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Origin': league['origin'],
        'Referer': f"{league['origin']}/",
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    req = urllib.request.Request(url, data=encoded_data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read().decode('utf-8')
            return json.loads(content)
    except Exception as e:
        logger.error(f"API request failed: {e}")
        return {}


def parse_towns_from_html(html: str) -> dict:
    """Parse town options from the HTML page."""
    towns = {}

    # Multiple patterns to handle different HTML formats
    # Pattern 1: <option value='3553'>Milton</option>
    # Pattern 2: <option value="3553">Milton</option>
    patterns = [
        r"<option\s+value=['\"]?(\d+)['\"]?>([^<]+)</option>",
        r"value=['\"](\d+)['\"]>([A-Za-z][^<]*)</option>",
    ]

    # First try to find the town select section specifically
    town_section_patterns = [
        r'id=["\']inputTown["\'][^>]*>(.*?)</select>',
        r'id=["\']popupTown["\'][^>]*>(.*?)</select>',
        r'for=["\']inputTown["\'].*?<select[^>]*>(.*?)</select>',
    ]

    section_html = html
    for sp in town_section_patterns:
        match = re.search(sp, html, re.DOTALL | re.IGNORECASE)
        if match:
            section_html = match.group(1)
            logger.debug(f"Found town section with pattern: {sp[:30]}...")
            break

    # Try each pattern
    for pattern in patterns:
        matches = re.findall(pattern, section_html, re.IGNORECASE)
        for town_id, town_name in matches:
            name = town_name.strip()
            # Filter out non-town options
            if name and not name.lower().startswith('choose') and len(name) > 1:
                # Avoid duplicates, prefer 4-digit IDs (likely town IDs)
                if name not in towns or len(town_id) == 4:
                    towns[name] = town_id

    # If still nothing, search whole page
    if not towns:
        logger.debug("Searching entire page for town options...")
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for town_id, town_name in matches:
                name = town_name.strip()
                # Filter: must look like a town name (starts with capital, reasonable length)
                if (name and
                    not name.lower().startswith('choose') and
                    len(name) > 2 and
                    len(town_id) >= 4 and
                    name[0].isupper()):
                    if name not in towns:
                        towns[name] = town_id

    logger.debug(f"Parsed towns: {list(towns.keys())[:10]}...")
    return towns


def get_town_id(client_id: str, town_name: str) -> Optional[str]:
    """Look up town ID by fetching and parsing the league page."""
    league = LEAGUES.get(client_id)
    if not league:
        logger.error(f"Unknown league: {client_id}")
        return None

    # Try to fetch and parse the page first (dynamic, always up-to-date)
    logger.info(f"Fetching {league['name']} page to find town ID for {town_name}...")
    html = fetch_url(league['url'])

    if html:
        towns = parse_towns_from_html(html)
        logger.info(f"Found {len(towns)} towns in {league['name']}")

        # Case-insensitive lookup
        for name, tid in towns.items():
            if name.lower() == town_name.lower():
                logger.info(f"Found {town_name} = {tid}")
                return tid

        # Partial match
        for name, tid in towns.items():
            if town_name.lower() in name.lower():
                logger.info(f"Partial match: {town_name} -> {name} = {tid}")
                return tid

    # Fallback to hardcoded values only if fetch/parse failed
    logger.warning(f"Could not find {town_name} dynamically, trying hardcoded fallback...")
    KNOWN_TOWNS = {
        'ssybl': {
            'Milton': '3553',
        },
        'metrowbb': {
            'Milton': '3488',
        }
    }

    if client_id in KNOWN_TOWNS:
        for name, tid in KNOWN_TOWNS[client_id].items():
            if name.lower() == town_name.lower():
                logger.info(f"Using fallback town ID: {town_name} = {tid}")
                return tid

    logger.error(f"Town '{town_name}' not found in {league['name']}")
    return None


def discover_teams(client_id: str, town_no: str, grade: int, gender: str, season: str = None) -> list[dict]:
    """Discover teams for a town/grade/gender combination."""
    if not season:
        season = get_season()

    data = {
        'clientid': client_id,
        'yrseason': season,
        'townno': town_no,
        'grade': str(grade),
        'gender': gender
    }

    logger.info(f"Discovering teams: {client_id} grade={grade} gender={gender} town={town_no}")

    result = fetch_api(TEAM_DISCOVERY_URL, data, client_id)

    if isinstance(result, list):
        teams = []
        for team in result:
            if team.get('teamno'):
                teams.append({
                    'team_no': team['teamno'],
                    'team_name': team.get('teamname', '').strip(),
                    'division_no': team.get('divisionno', ''),
                    'division_tier': team.get('divisiontier', '')
                })
        logger.info(f"Found {len(teams)} teams")
        return teams

    return []


def parse_team_color(team_name: str) -> str:
    """Extract color from team name like '(White) D2'."""
    match = re.search(r'\((\w+)\)', team_name)
    if match:
        return match.group(1)
    return ""


def fetch_schedule(client_id: str, team_no: str, season: str = None) -> dict:
    """Fetch schedule from sportsite2.com API."""
    if not season:
        season = get_season()

    data = {
        'clientid': client_id,
        'yrseason': season,
        'teamno': team_no
    }

    logger.info(f"Fetching schedule: clientid={client_id}, teamno={team_no}, season={season}")
    return fetch_api(TEAM_SCHEDULE_URL, data, client_id)


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
    color = team_config.get('color', '')

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

            # Use gamedate (YYYY-MM-DD format) if available, otherwise fallback
            date_str = item.get('gamedate', item.get('date', item.get('gdate', '')))
            time_str = item.get('starttime', item.get('time', item.get('gametime', '')))
            opponent = item.get('opponent', item.get('opp', item.get('oppname', '')))
            game_type = item.get('homeaway', item.get('ha', item.get('type', '')))

            # Build full location with address
            venue = item.get('location', item.get('loc', item.get('facility', ''))) or ''
            venue = str(venue).strip()
            street = str(item.get('street', '') or '').strip()
            citystzip = str(item.get('citystzip', '') or '').strip()
            directions = str(item.get('directions', '') or '').strip()

            # Extract court/gym info from venue name for better iOS geocoding
            # e.g., "Milton High School - Court 2" -> venue="Milton High School", court_info="Court 2"
            court_info = ''
            if ' - ' in venue:
                venue_parts = venue.split(' - ', 1)
                # Check if second part looks like court/gym info (not an address)
                if venue_parts[1] and not any(c.isdigit() and len(venue_parts[1]) > 20 for c in venue_parts[1]):
                    second = venue_parts[1].lower()
                    if any(word in second for word in ['court', 'gym', 'field', 'rink', 'front', 'back', 'main']):
                        venue = venue_parts[0].strip()
                        court_info = venue_parts[1].strip()

            # Combine venue and address in iOS-friendly format
            location_parts = []
            if venue:
                location_parts.append(venue)
            if street and citystzip:
                location_parts.append(f"{street}, {citystzip}")
            elif street:
                location_parts.append(street)
            elif citystzip:
                location_parts.append(citystzip)

            # Use comma separator for better geocoding, add court info at end
            location = ', '.join(location_parts)
            if court_info:
                location = f"{location} ({court_info})"

            if not date_str:
                continue

            game_dt = parse_api_date(date_str, time_str)
            if not game_dt:
                continue

            # Clean opponent name - remove @ prefix for away games
            if opponent:
                opponent = str(opponent).strip()
                if opponent.startswith('@'):
                    opponent = opponent[1:].strip()

            if not opponent:
                opponent = "TBD"

            game = {
                'datetime': game_dt,
                'opponent': opponent,
                'location': location,
                'directions': directions,
                'team_name': team_name,
                'short_name': short_name,
                'game_type': str(game_type) if game_type else '',
                'league': league,
                'grade': str(grade),
                'gender': team_config.get('gender', ''),
                'color': color
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
        event.add('dtend', game['datetime'] + timedelta(hours=1))

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
        if game.get('directions'):
            desc.append(f"\nDirections: {game['directions']}")
        event.add('description', '\n'.join(desc))
        event.add('dtstamp', datetime.now(EASTERN))

        # 1 hour reminder
        alarm1 = Alarm()
        alarm1.add('action', 'DISPLAY')
        alarm1.add('trigger', timedelta(hours=-1))
        alarm1.add('description', f'Basketball game vs {opponent} in 1 hour')
        event.add_component(alarm1)

        # 30 minute reminder
        alarm2 = Alarm()
        alarm2.add('action', 'DISPLAY')
        alarm2.add('trigger', timedelta(minutes=-30))
        alarm2.add('description', f'Basketball game vs {opponent} in 30 minutes')
        event.add_component(alarm2)

        cal.add_component(event)

    return cal.to_ical()


def generate_index_html(calendars: list[dict], base_url: str, town_name: str) -> str:
    """Generate the landing page HTML with hierarchical sections: Grade -> Color -> Calendars."""
    now = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')

    def extract_grade(cal):
        """Extract grade number from calendar."""
        cal_id = cal.get('id', '')
        cal_name = cal.get('name', '')
        for g in ['3rd', '4th', '5th', '6th', '7th', '8th']:
            if g in cal_id or g in cal_name:
                return g.replace('th', '').replace('rd', '')
        for g in ['3', '4', '5', '6', '7', '8']:
            if f'-{g}th-' in cal_id or f' {g}th ' in cal_name:
                return g
        return 'Other'

    def extract_color(cal):
        """Extract team color from calendar."""
        cal_id = cal.get('id', '').lower()
        cal_name = cal.get('name', '').lower()
        for color in ['white', 'red', 'blue', 'black', 'gold', 'green', 'orange', 'purple', 'silver', 'grey', 'gray']:
            if color in cal_id or color in cal_name:
                # Normalize grey/gray to Gray
                if color in ['grey', 'gray']:
                    return 'Gray'
                return color.capitalize()
        return 'Team'

    def extract_gender(cal):
        """Extract gender from calendar."""
        cal_id = cal.get('id', '').lower()
        cal_name = cal.get('name', '').lower()
        if 'girls' in cal_id or 'girls' in cal_name:
            return 'Girls'
        return 'Boys'

    # Group all calendars by grade -> gender -> color
    grade_gender_color_groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for cal in calendars:
        grade = extract_grade(cal)
        gender = extract_gender(cal)
        color = extract_color(cal)
        grade_gender_color_groups[grade][gender][color].append(cal)

    def make_card(cal, compact=False):
        cal_id = cal.get('id', 'calendar')
        cal_name = cal.get('name', 'Calendar')
        cal_type = cal.get('type', 'team')
        description = cal.get('description', '')
        games_count = cal.get('games', 0)
        ics_url = f"{base_url}/{cal_id}.ics"

        # Shorter display name for league calendars
        if cal_type == 'combined':
            display_name = "⭐ Combined (All Leagues)"
            highlight_class = "highlight"
        else:
            # Extract just the league name
            league = cal.get('league', '')
            display_name = f"{league}" if league else cal_name
            highlight_class = ""

        games_info = f"{games_count} games" if games_count else "No games"

        if compact:
            return f'''
            <div class="calendar-card compact {highlight_class}">
                <div class="card-header">
                    <span class="card-title">{display_name}</span>
                    <span class="card-games">{games_info}</span>
                </div>
                <div class="card-actions">
                    <a href="{cal_id}.ics" class="btn btn-sm btn-primary" download>Download</a>
                    <a href="webcal://{ics_url.replace('https://', '')}" class="btn btn-sm btn-secondary">Subscribe</a>
                    <button class="btn btn-sm" onclick="copyUrl('{ics_url}')" title="Copy URL">📋</button>
                </div>
            </div>
            '''
        else:
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

    # Build grade sections
    grade_sections = []
    grade_order = ['3', '4', '5', '6', '7', '8', 'Other']
    grade_labels = {'3': '3rd Grade', '4': '4th Grade', '5': '5th Grade',
                    '6': '6th Grade', '7': '7th Grade', '8': '8th Grade', 'Other': 'Other'}

    for grade in grade_order:
        if grade not in grade_gender_color_groups:
            continue

        gender_groups = grade_gender_color_groups[grade]
        grade_label = grade_labels.get(grade, grade)

        # Build color groups within this grade
        color_sections = []
        total_teams = 0
        total_games = 0

        for gender in ['Boys', 'Girls']:
            if gender not in gender_groups:
                continue
            color_groups = gender_groups[gender]

            for color in sorted(color_groups.keys()):
                cals = color_groups[color]
                if not cals:
                    continue

                # Sort: combined first, then by league name
                cals_sorted = sorted(cals, key=lambda c: (0 if c.get('type') == 'combined' else 1, c.get('league', '')))

                team_label = f"{gender} {color}"
                team_games = sum(c.get('games', 0) for c in cals)
                total_teams += 1
                total_games += team_games

                cards_html = ''.join(make_card(c, compact=True) for c in cals_sorted)

                color_sections.append(f'''
                <div class="team-group">
                    <div class="team-header">{team_label}</div>
                    <div class="team-calendars">
                        {cards_html}
                    </div>
                </div>
                ''')

        if color_sections:
            grade_sections.append(f'''
            <div class="grade-section">
                <button class="collapsible" onclick="toggleSection(this)">
                    <span class="grade-title">🏀 {grade_label}</span>
                    <span class="grade-info">{total_teams} teams &bull; {total_games} games</span>
                    <span class="arrow">▼</span>
                </button>
                <div class="collapsible-content">
                    {''.join(color_sections)}
                </div>
            </div>
            ''')

    grade_html = '\n'.join(grade_sections)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{town_name} Basketball Calendars</title>
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

        /* Collapsible sections */
        .grade-section {{
            margin-bottom: 12px;
        }}
        .collapsible {{
            width: 100%;
            background: #1a1a2e;
            color: white;
            padding: 16px 20px;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 16px;
            transition: background 0.2s;
        }}
        .collapsible:hover {{
            background: #2a2a4e;
        }}
        .collapsible.active {{
            border-radius: 10px 10px 0 0;
        }}
        .grade-title {{
            font-weight: 700;
        }}
        .grade-info {{
            font-size: 13px;
            opacity: 0.8;
        }}
        .arrow {{
            transition: transform 0.3s;
        }}
        .collapsible.active .arrow {{
            transform: rotate(180deg);
        }}
        .collapsible-content {{
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.3s ease-out;
            background: #e8e8e8;
            border-radius: 0 0 10px 10px;
            padding: 0 16px;
        }}
        .collapsible-content.open {{
            max-height: 5000px;
            padding: 16px;
        }}

        /* Team groups within grades */
        .team-group {{
            margin-bottom: 20px;
        }}
        .team-header {{
            font-weight: 700;
            font-size: 15px;
            color: #1a1a2e;
            margin-bottom: 10px;
            padding-bottom: 6px;
            border-bottom: 1px solid #ddd;
        }}
        .team-calendars {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        /* Compact calendar cards */
        .calendar-card.compact {{
            padding: 12px 16px;
            margin-bottom: 0;
        }}
        .calendar-card.compact .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }}
        .calendar-card.compact .card-title {{
            font-weight: 600;
            font-size: 14px;
            color: #1a1a2e;
        }}
        .calendar-card.compact .card-games {{
            font-size: 12px;
            color: #666;
        }}
        .calendar-card.compact .card-actions {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}
        .btn-sm {{
            padding: 6px 12px;
            font-size: 12px;
            border: none;
            cursor: pointer;
        }}

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
    <h1>🏀 {town_name} Basketball</h1>
    <p class="subtitle">Subscribe to automatically sync game schedules to your calendar</p>

    <div id="copied" class="copied">URL Copied!</div>

    <h2>🏀 Team Calendars</h2>
    <p style="color: #666; font-size: 14px;">Click a grade to expand. ⭐ Combined calendars include all leagues.</p>
    {grade_html}

    <div class="instructions">
        <h2>How to Subscribe</h2>
        <ul>
            <li><strong>Google Calendar:</strong> Other calendars (+) → From URL → paste URL</li>
            <li><strong>Apple Calendar:</strong> File → New Calendar Subscription → paste URL</li>
            <li><strong>iPhone/iPad:</strong> Tap "Subscribe" button, or Settings → Calendar → Accounts → Add Subscribed Calendar</li>
            <li><strong>Outlook:</strong> Add calendar → Subscribe from web</li>
        </ul>
        <p><strong>Tip:</strong> Calendars auto-update every 24 hours. Data refreshes every 3 hours.</p>
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

        function toggleSection(btn) {{
            btn.classList.toggle('active');
            const content = btn.nextElementSibling;
            content.classList.toggle('open');
        }}
    </script>
</body>
</html>
'''


def discover_and_fetch_teams(config: dict) -> tuple[list[dict], list[dict]]:
    """
    Discover teams dynamically and fetch their schedules.

    Returns: (team_configs, all_games)
    """
    town_name = config.get('town_name', 'Milton')
    leagues = config.get('leagues', ['ssybl', 'metrowbb'])
    grades = config.get('grades', [5, 8])
    genders = config.get('genders', ['M'])
    colors = config.get('colors', ['White'])  # Filter to specific colors, or empty for all
    season = get_season()

    # Cache town IDs per league
    town_ids = {}
    for league in leagues:
        town_id = get_town_id(league, town_name)
        if town_id:
            town_ids[league] = town_id
        else:
            logger.warning(f"Could not find {town_name} in {league}")

    if not town_ids:
        logger.error(f"Could not find {town_name} in any league!")
        return [], []

    # Discover all teams
    discovered_teams = []  # List of (league, grade, gender, team_info)

    for league, town_id in town_ids.items():
        for grade in grades:
            for gender in genders:
                teams = discover_teams(league, town_id, grade, gender, season)
                for team in teams:
                    color = parse_team_color(team['team_name'])
                    # Filter by color if specified
                    if colors and color and color not in colors:
                        logger.info(f"Skipping {team['team_name']} (color {color} not in {colors})")
                        continue
                    discovered_teams.append({
                        'league': league,
                        'grade': grade,
                        'gender': gender,
                        'color': color,
                        'team_no': team['team_no'],
                        'team_name_raw': team['team_name'],
                        'division_tier': team.get('division_tier', '')
                    })

    logger.info(f"Discovered {len(discovered_teams)} teams")

    # Build team configs and fetch schedules
    team_configs = []
    all_games = []

    gender_names = {'M': 'Boys', 'F': 'Girls'}
    league_names = {k: v['name'] for k, v in LEAGUES.items()}

    for team in discovered_teams:
        league = team['league']
        grade = team['grade']
        gender = team['gender']
        color = team['color']
        team_no = team['team_no']

        # Build identifiers
        gender_name = gender_names.get(gender, gender)
        league_name = league_names.get(league, league)

        team_id = f"{town_name.lower()}-{grade}th-{gender_name.lower()}-{color.lower()}-{league}".replace(' ', '-')
        team_name = f"{town_name} {grade}th {gender_name} {color} ({league_name})"
        short_name = f"{grade}{gender[0]}-{color}"

        team_config = {
            'id': team_id,
            'team_name': team_name,
            'short_name': short_name,
            'client_id': league,
            'team_no': team_no,
            'league': league_name,
            'grade': str(grade),
            'gender': gender,
            'color': color
        }
        team_configs.append(team_config)

        # Fetch games
        games = fetch_team_games(team_config)
        all_games.extend(games)

    return team_configs, all_games


def main():
    parser = argparse.ArgumentParser(description='Basketball Schedule Scraper')
    parser.add_argument('--config', '-c', required=True, help='Config file (JSON)')
    parser.add_argument('--output', '-o', default='docs', help='Output directory for ICS files')
    parser.add_argument('--base-url', '-u', default='', help='Base URL for calendar links')
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    base_url = args.base_url or config.get('base_url', 'https://example.github.io/ssbball')
    town_name = config.get('town_name', 'Milton')

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if using new dynamic config or legacy static config
    if 'teams' in config:
        # Legacy mode: static team definitions
        logger.info("Using legacy static team configuration")
        teams = config.get('teams', [])
        combined_calendars = config.get('combined_calendars', [])

        all_games = []
        team_configs = []

        for team_config in teams:
            team_id = team_config.get('id', 'team')
            team_name = team_config.get('team_name', 'Team')

            logger.info(f"Fetching {team_name}...")
            games = fetch_team_games(team_config)
            all_games.extend(games)
            team_configs.append(team_config)
    else:
        # Dynamic mode: discover teams
        logger.info("Using dynamic team discovery")
        team_configs, all_games = discover_and_fetch_teams(config)

        # Build combined calendars based on discovered teams
        combined_calendars = []

        # Group by grade+gender+color (across leagues)
        team_groups = defaultdict(list)
        for tc in team_configs:
            key = (tc['grade'], tc['gender'], tc['color'])
            team_groups[key].append(tc)

        # Create combined calendar for each group with multiple leagues
        for (grade, gender, color), group_teams in team_groups.items():
            if len(group_teams) > 1:
                gender_name = 'Boys' if gender == 'M' else 'Girls'
                combined_calendars.append({
                    'id': f"{town_name.lower()}-{grade}th-{gender_name.lower()}-{color.lower()}",
                    'name': f"{town_name} {grade}th {gender_name} {color}",
                    'description': 'All leagues combined',
                    'filter': {'grade': str(grade), 'gender': gender, 'color': color}
                })

    calendar_info = []  # For index.html

    # Generate individual team calendars
    for team_config in team_configs:
        team_id = team_config.get('id', 'team')
        team_name = team_config.get('team_name', 'Team')

        # Filter games for this team
        team_games = [g for g in all_games
                     if g.get('team_name') == team_name or
                        (g.get('grade') == team_config.get('grade') and
                         g.get('league') == team_config.get('league') and
                         g.get('color') == team_config.get('color'))]

        ical_data = generate_ical(team_games, team_name, team_id)
        ics_path = output_dir / f"{team_id}.ics"
        ics_path.write_bytes(ical_data)
        logger.info(f"Wrote {ics_path} with {len(team_games)} games")

        calendar_info.append({
            'type': 'team',
            'id': team_id,
            'name': team_config.get('short_name', team_name),
            'league': team_config.get('league', ''),
            'description': team_config.get('league', ''),
            'games': len(team_games)
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
    index_html = generate_index_html(calendar_info, base_url, town_name)
    index_path = output_dir / 'index.html'
    index_path.write_text(index_html)
    logger.info(f"Wrote {index_path}")

    # Write status
    summary = {
        'updated': datetime.now(EASTERN).isoformat(),
        'town': town_name,
        'teams_discovered': len(team_configs),
        'calendars': calendar_info
    }
    (output_dir / 'status.json').write_text(json.dumps(summary, indent=2))

    # Print summary
    print("\n" + "="*50)
    print("Scrape Complete")
    print("="*50)
    print(f"Town: {town_name}")
    print(f"Teams discovered: {len(team_configs)}")
    for cal in calendar_info:
        print(f"  {cal['name']}: {cal['games']} games")
    print(f"\nOutput: {output_dir}/")


if __name__ == '__main__':
    main()
