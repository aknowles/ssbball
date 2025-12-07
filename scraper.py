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
TEAM_NL_SCHEDULE_URL = f"{API_BASE}/getTeamNLSchedule.php"
TEAM_DISCOVERY_URL = f"{API_BASE}/getTownGenderGradeTeams.php"
DIVISION_STANDINGS_URL = f"{API_BASE}/getDivisionStandings.php"

# Default league configurations (can be extended via custom_leagues in config)
DEFAULT_LEAGUES = {
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

# Global leagues dict (updated at runtime with other_leagues)
LEAGUES = DEFAULT_LEAGUES.copy()


def get_leagues(config: dict = None) -> dict:
    """Get leagues config, merging defaults with any other_leagues from config."""
    leagues = DEFAULT_LEAGUES.copy()
    if config:
        other = config.get('other_leagues', {})
        for league_id, league_config in other.items():
            # Build full league config from entry
            origin = league_config.get('origin', '')
            leagues[league_id] = {
                'name': league_config.get('name', league_id.upper()),
                'url': f"{origin}/launch.php" if origin else '',
                'origin': origin
            }
    return leagues


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


def parse_team_color(team_name: str, team_aliases: dict = None) -> str:
    """Extract color from team name.

    Tries parentheses format first like '(White) D2', then falls back to
    searching for known color words anywhere in the name. This ensures
    consistent color extraction across leagues with different team name formats.

    Args:
        team_name: The team name string to parse
        team_aliases: Optional dict mapping canonical colors to lists of aliases.
                      e.g. {"White": ["White 1", "Squirt White"], "Red": ["Red Team"]}
    """
    team_aliases = team_aliases or {}
    name_lower = team_name.lower()

    # First check team_aliases - these take priority for custom naming
    for canonical_color, aliases in team_aliases.items():
        if isinstance(aliases, list):
            for alias in aliases:
                if alias.lower() in name_lower:
                    return canonical_color.capitalize()
        elif isinstance(aliases, str) and aliases.lower() in name_lower:
            return canonical_color.capitalize()

    # Then try parentheses format (most specific for standard naming)
    match = re.search(r'\((\w+)\)', team_name)
    if match:
        candidate = match.group(1)
        # Verify it's actually a color word
        known_colors = ['white', 'red', 'blue', 'black', 'gold', 'green',
                        'orange', 'purple', 'silver', 'grey', 'gray']
        if candidate.lower() in known_colors:
            # Normalize grey/gray to Gray
            if candidate.lower() in ['grey', 'gray']:
                return 'Gray'
            return candidate.capitalize()

    # Fallback: search for known colors anywhere in name
    known_colors = ['white', 'red', 'blue', 'black', 'gold', 'green',
                    'orange', 'purple', 'silver', 'grey', 'gray']
    for color in known_colors:
        if color in name_lower:
            # Normalize grey/gray to Gray
            if color in ['grey', 'gray']:
                return 'Gray'
            return color.capitalize()

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


def fetch_nl_schedule(client_id: str, team_no: str, season: str = None) -> dict:
    """Fetch non-league schedule (tournaments, playoffs) from sportsite2.com API."""
    if not season:
        season = get_season()

    data = {
        'clientid': client_id,
        'yrseason': season,
        'teamno': team_no
    }

    logger.info(f"Fetching NL schedule: clientid={client_id}, teamno={team_no}, season={season}")
    return fetch_api(TEAM_NL_SCHEDULE_URL, data, client_id)


def fetch_division_standings(division_no: str, client_id: str) -> dict:
    """Fetch standings for a division. Returns dict mapping team_no to standings info."""
    if not division_no:
        return {}

    data = {'divisionno': division_no}
    logger.info(f"Fetching standings for division {division_no}")

    result = fetch_api(DIVISION_STANDINGS_URL, data, client_id)

    standings = {}
    if isinstance(result, list):
        for team in result:
            team_no = team.get('teamno', '')
            if team_no:
                wins = int(team.get('numwin', 0) or 0)
                losses = int(team.get('numloss', 0) or 0)
                ties = int(team.get('numties', 0) or 0)
                rank = team.get('rank', 0)

                standings[team_no] = {
                    'wins': wins,
                    'losses': losses,
                    'ties': ties,
                    'rank': rank
                }
        logger.info(f"Found standings for {len(standings)} teams in division {division_no}")

    return standings


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

            # Check if this is a tournament/non-league game
            week = item.get('week', '')
            is_tournament = week == 'NL' or game_type == 'Tourn'

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
                'color': color,
                'is_tournament': is_tournament,
                'jerseys': team_config.get('jerseys', {})
            }
            games.append(game)
            logger.info(f"Found game: {game_dt.strftime('%b %d %I:%M%p')} vs {opponent}")

        except Exception as e:
            logger.debug(f"Error parsing game: {e}")
            continue

    return games


def fetch_team_games(config: dict, include_nl_games: bool = True) -> list[dict]:
    """Fetch games for a single team, optionally including non-league games."""
    team_name = config.get('team_name', 'Basketball Team')
    client_id = config.get('client_id', 'metrowbb')
    team_no = config.get('team_no', '')
    season = config.get('season', None)

    if not team_no:
        logger.error(f"No team_no configured for {team_name}")
        return []

    games = []

    # Fetch regular league schedule
    data = fetch_schedule(client_id, team_no, season)
    if data:
        league_games = parse_schedule_response(data, config)
        logger.info(f"Found {len(league_games)} league games for {team_name}")
        games.extend(league_games)
    else:
        logger.warning(f"No league data returned for {team_name}")

    # Fetch non-league schedule (tournaments, playoffs) if enabled
    if include_nl_games:
        nl_data = fetch_nl_schedule(client_id, team_no, season)
        if nl_data:
            nl_games = parse_schedule_response(nl_data, config)
            logger.info(f"Found {len(nl_games)} non-league games for {team_name}")
            games.extend(nl_games)

    return games


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
        is_tournament = game.get('is_tournament', False)

        # Build summary with team identifier if multiple teams
        if short_name:
            prefix = f"[{short_name}] "
        else:
            prefix = ""

        # Use trophy emoji for tournament/playoff games
        emoji = "🏆" if is_tournament else "🏀"

        if 'away' in game_type or game_type == 'a':
            event.add('summary', f"{prefix}{emoji} @ {opponent}")
        else:
            event.add('summary', f"{prefix}{emoji} vs {opponent}")

        event.add('dtstart', game['datetime'])
        event.add('dtend', game['datetime'] + timedelta(hours=1))

        if game.get('location'):
            event.add('location', game['location'])

        desc = [
            f"Team: {game.get('team_name', 'Unknown')}",
            f"Opponent: {opponent}",
            f"League: {game.get('league', 'Basketball')}"
        ]
        if is_tournament:
            desc.append("Type: Tournament/Playoff")
        if game.get('location'):
            desc.append(f"Location: {game['location']}")
        if game.get('game_type') and not is_tournament:
            desc.append(f"Game: {game['game_type']}")
        # Add jersey info based on home/away
        jerseys = game.get('jerseys', {})
        if jerseys:
            is_away = 'away' in game_type or game_type == 'a'
            jersey_color = jerseys.get('away' if is_away else 'home')
            if jersey_color:
                desc.append(f"Jersey: {jersey_color}")
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


def generate_index_html(calendars: list[dict], base_url: str, town_name: str, include_nl_games: bool = True, coaches: dict = None) -> str:
    """Generate the landing page HTML with hierarchical sections: Grade -> Color -> Calendars.

    Args:
        calendars: List of calendar info dicts
        base_url: Base URL for calendar links
        town_name: Town name for display
        include_nl_games: Whether tournament games are included
        coaches: Optional dict mapping team keys (e.g. "5-M-White") to coach info
                 Values can be strings or lists: "Coach Name" or ["Coach Name", "coach@email.com"]
    """
    coaches = coaches or {}
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
        division_tier = cal.get('division_tier', '')
        wins = cal.get('wins', 0)
        losses = cal.get('losses', 0)
        ties = cal.get('ties', 0)
        rank = cal.get('rank', 0)
        ics_url = f"{base_url}/{cal_id}.ics"
        league = cal.get('league', '')

        # Shorter display name for league calendars
        if cal_type == 'combined':
            display_name = "⭐ Combined (All Leagues)"
            highlight_class = "highlight"
        else:
            # Extract just the league name
            display_name = f"{league}" if league else cal_name
            highlight_class = ""

        games_info = f"{games_count} games" if games_count else "No games"

        # Build division/standings badges (only shown when toggle is on)
        badges_html = ''

        # Division tier badge
        if division_tier:
            division_tooltip = f"{league} Division {division_tier}" if league else f"Division {division_tier}"
            badges_html += f'<span class="division-badge" title="{division_tooltip}">{division_tier}</span>'

        # Rank badge (only for non-combined with valid rank AND winning record)
        has_winning_record = wins > losses
        if rank and rank > 0 and cal_type != 'combined' and has_winning_record:
            badges_html += f'<span class="division-badge rank-badge" title="Current standing in division">#{rank}</span>'

        # W-L record badge
        if wins or losses or ties:
            if ties:
                record = f'{wins}-{losses}-{ties}'
                record_title = "Win-Loss-Tie record"
            else:
                record = f'{wins}-{losses}'
                record_title = "Win-Loss record"
            badges_html += f'<span class="division-badge record-badge" title="{record_title}">{record}</span>'

        if compact:
            return f'''
            <div class="calendar-card compact {highlight_class}">
                <div class="card-header">
                    <span class="card-title">{display_name}{badges_html}</span>
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
                <h3>{cal_name}{badges_html}</h3>
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

                # Get gender code for data attribute (M or F)
                gender_code = 'M' if gender == 'Boys' else 'F'

                # Look up coaches for this team (try multiple key formats)
                coach_key = f"{grade}-{gender_code}-{color}"
                coach_info = coaches.get(coach_key) or coaches.get(f"{grade}{gender_code}-{color}") or coaches.get(color)
                coach_html = ''
                if coach_info:
                    if isinstance(coach_info, list):
                        coach_name = coach_info[0]
                        coach_email = coach_info[1] if len(coach_info) > 1 else None
                    else:
                        coach_name = coach_info
                        coach_email = None
                    if coach_email:
                        coach_html = f'<span class="coach-info">Coach: <a href="mailto:{coach_email}">{coach_name}</a></span>'
                    else:
                        coach_html = f'<span class="coach-info">Coach: {coach_name}</span>'

                color_sections.append(f'''
                <div class="team-group" data-gender="{gender_code}" data-games="{team_games}">
                    <div class="team-header">{team_label}{coach_html}</div>
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

    # Note about what games are included
    if include_nl_games:
        games_included_note = 'These calendars include <strong>league games and tournaments/playoffs</strong> (🏆 indicates tournament games).'
    else:
        games_included_note = 'These calendars include <strong>league games only</strong> — tournaments and playoffs are not included.'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Subscribe to {town_name} basketball game schedules. Auto-syncing calendars for MetroWest and SSYBL leagues.">
    <meta name="theme-color" content="#1a1a2e" media="(prefers-color-scheme: light)">
    <meta name="theme-color" content="#0f0f1a" media="(prefers-color-scheme: dark)">
    <title>{town_name} Basketball Calendars</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        /* CSS Custom Properties */
        :root {{
            --font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            --max-width: 900px;
            --spacing-xs: 4px;
            --spacing-sm: 8px;
            --spacing-md: 16px;
            --spacing-lg: 24px;
            --spacing-xl: 32px;
            --radius-sm: 8px;
            --radius-md: 12px;
            --radius-lg: 16px;
            --transition-fast: 0.15s ease;
            --transition-normal: 0.25s ease;
            --transition-slow: 0.35s ease;

            /* Light mode colors */
            --color-bg: #f8f9fa;
            --color-bg-elevated: #ffffff;
            --color-bg-subtle: #f0f0f0;
            --color-bg-muted: #e8e8e8;
            --color-text: #1a1a2e;
            --color-text-secondary: #5a5a6e;
            --color-text-muted: #888;
            --color-primary: #e63946;
            --color-primary-hover: #d62839;
            --color-primary-gradient: linear-gradient(135deg, #e63946 0%, #f25c69 100%);
            --color-secondary: #1a1a2e;
            --color-secondary-hover: #2a2a4e;
            --color-accent: #4caf50;
            --color-accent-bg: #e8f5e9;
            --color-warning-bg: #fff8e6;
            --color-border: #e0e0e0;
            --color-border-light: #eee;
            --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
            --shadow-md: 0 4px 12px rgba(0,0,0,0.1);
            --shadow-lg: 0 8px 24px rgba(0,0,0,0.12);
            --shadow-glow: 0 0 20px rgba(230, 57, 70, 0.15);
        }}

        /* Dark mode */
        @media (prefers-color-scheme: dark) {{
            :root {{
                --color-bg: #0f0f1a;
                --color-bg-elevated: #1a1a2e;
                --color-bg-subtle: #252540;
                --color-bg-muted: #2a2a4e;
                --color-text: #f0f0f5;
                --color-text-secondary: #a0a0b0;
                --color-text-muted: #707080;
                --color-primary: #ff4d5a;
                --color-primary-hover: #ff6b76;
                --color-primary-gradient: linear-gradient(135deg, #e63946 0%, #ff6b76 100%);
                --color-secondary: #3a3a5e;
                --color-secondary-hover: #4a4a7e;
                --color-accent: #66bb6a;
                --color-accent-bg: #1a2e1a;
                --color-warning-bg: #2e2a1a;
                --color-border: #3a3a5e;
                --color-border-light: #2a2a4e;
                --shadow-sm: 0 1px 3px rgba(0,0,0,0.3);
                --shadow-md: 0 4px 12px rgba(0,0,0,0.4);
                --shadow-lg: 0 8px 24px rgba(0,0,0,0.5);
                --shadow-glow: 0 0 30px rgba(230, 57, 70, 0.2);
            }}
        }}

        /* Reduced motion */
        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
            }}
        }}

        /* Base styles */
        * {{ box-sizing: border-box; }}

        html {{
            scroll-behavior: smooth;
        }}

        body {{
            font-family: var(--font-family);
            font-size: 17px;
            line-height: 1.6;
            letter-spacing: -0.01em;
            max-width: var(--max-width);
            margin: 0 auto;
            padding: var(--spacing-lg);
            background: var(--color-bg);
            color: var(--color-text);
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }}

        /* Hero section */
        .hero {{
            text-align: center;
            padding: var(--spacing-xl) var(--spacing-md);
            margin: calc(-1 * var(--spacing-lg));
            margin-bottom: var(--spacing-xl);
            background: linear-gradient(135deg, var(--color-secondary) 0%, #2a2a4e 100%);
            border-radius: 0 0 var(--radius-lg) var(--radius-lg);
            position: relative;
            overflow: hidden;
        }}

        .hero::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.03'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
            opacity: 0.5;
        }}

        .hero-content {{
            position: relative;
            z-index: 1;
        }}

        .hero-icon {{
            font-size: 3.5rem;
            margin-bottom: var(--spacing-md);
            display: inline-block;
            animation: bounce 2s ease-in-out infinite;
        }}

        @keyframes bounce {{
            0%, 100% {{ transform: translateY(0); }}
            50% {{ transform: translateY(-8px); }}
        }}

        .hero h1 {{
            color: white;
            font-size: 2.25rem;
            font-weight: 700;
            margin: 0 0 var(--spacing-sm) 0;
            letter-spacing: -0.02em;
        }}

        .hero .subtitle {{
            color: rgba(255, 255, 255, 0.8);
            font-size: 1.1rem;
            margin: 0;
            font-weight: 400;
        }}

        /* Section headers */
        h2 {{
            color: var(--color-text);
            font-size: 1.35rem;
            font-weight: 700;
            margin: var(--spacing-xl) 0 var(--spacing-md) 0;
            padding-bottom: var(--spacing-sm);
            border-bottom: 3px solid var(--color-primary);
            display: inline-block;
        }}

        /* Auto-sync banner */
        .auto-sync-note {{
            background: var(--color-accent-bg);
            border-left: 4px solid var(--color-accent);
            padding: var(--spacing-md);
            border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
            margin-bottom: var(--spacing-lg);
            font-size: 0.95rem;
            display: flex;
            align-items: flex-start;
            gap: var(--spacing-sm);
        }}

        .auto-sync-note::before {{
            content: '✓';
            background: var(--color-accent);
            color: white;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 700;
            flex-shrink: 0;
        }}

        /* Calendar cards */
        .calendar-card {{
            background: var(--color-bg-elevated);
            border-radius: var(--radius-md);
            padding: var(--spacing-lg);
            margin-bottom: var(--spacing-md);
            box-shadow: var(--shadow-sm);
            transition: box-shadow var(--transition-normal), transform var(--transition-normal);
        }}

        .calendar-card:hover {{
            box-shadow: var(--shadow-md);
        }}

        .calendar-card.highlight {{
            border: 2px solid var(--color-primary);
            box-shadow: var(--shadow-glow);
        }}

        .calendar-card h3 {{
            margin: 0 0 var(--spacing-sm) 0;
            color: var(--color-text);
            font-weight: 600;
        }}

        .description {{
            color: var(--color-text-secondary);
            margin: 0 0 var(--spacing-md) 0;
            font-size: 0.9rem;
        }}

        .subscribe-url {{
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            background: var(--color-bg-subtle);
            padding: var(--spacing-sm) var(--spacing-md);
            border-radius: var(--radius-sm);
            margin-bottom: var(--spacing-md);
        }}

        .subscribe-url code {{
            flex: 1;
            font-size: 0.7rem;
            word-break: break-all;
            color: var(--color-text-secondary);
            font-family: 'SF Mono', Monaco, monospace;
        }}

        .subscribe-url button {{
            background: none;
            border: none;
            cursor: pointer;
            font-size: 1rem;
            padding: var(--spacing-xs);
            transition: transform var(--transition-fast);
        }}

        .subscribe-url button:hover {{
            transform: scale(1.1);
        }}

        .subscribe-url button:active {{
            transform: scale(0.95);
        }}

        /* Buttons */
        .buttons {{
            display: flex;
            gap: var(--spacing-sm);
            flex-wrap: wrap;
        }}

        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: var(--spacing-sm) var(--spacing-md);
            border-radius: var(--radius-sm);
            text-decoration: none;
            font-weight: 600;
            font-size: 0.85rem;
            border: none;
            cursor: pointer;
            transition: all var(--transition-fast);
            min-height: 44px;
        }}

        .btn:focus-visible {{
            outline: 3px solid var(--color-primary);
            outline-offset: 2px;
        }}

        .btn-primary {{
            background: var(--color-primary-gradient);
            color: white;
            box-shadow: 0 2px 8px rgba(230, 57, 70, 0.3);
        }}

        .btn-primary:hover {{
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(230, 57, 70, 0.4);
        }}

        .btn-primary:active {{
            transform: translateY(0);
        }}

        .btn-secondary {{
            background: var(--color-secondary);
            color: white;
        }}

        .btn-secondary:hover {{
            background: var(--color-secondary-hover);
        }}

        /* Collapsible sections */
        .grade-section {{
            margin-bottom: var(--spacing-md);
        }}

        .collapsible {{
            width: 100%;
            background: var(--color-secondary);
            color: white;
            padding: var(--spacing-md) var(--spacing-lg);
            border: none;
            border-radius: var(--radius-md);
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 1rem;
            font-family: var(--font-family);
            transition: all var(--transition-normal);
        }}

        .collapsible:hover {{
            background: var(--color-secondary-hover);
        }}

        .collapsible:focus-visible {{
            outline: 3px solid var(--color-primary);
            outline-offset: 2px;
        }}

        .collapsible.active {{
            border-radius: var(--radius-md) var(--radius-md) 0 0;
        }}

        .grade-title {{
            font-weight: 700;
        }}

        .grade-info {{
            font-size: 0.85rem;
            opacity: 0.8;
            margin-left: auto;
            margin-right: var(--spacing-md);
        }}

        .arrow {{
            transition: transform var(--transition-normal);
            font-size: 0.8rem;
        }}

        .collapsible.active .arrow {{
            transform: rotate(180deg);
        }}

        .collapsible-content {{
            max-height: 0;
            overflow: hidden;
            transition: max-height var(--transition-slow), padding var(--transition-normal);
            background: var(--color-bg-muted);
            border-radius: 0 0 var(--radius-md) var(--radius-md);
            padding: 0 var(--spacing-md);
        }}

        .collapsible-content.open {{
            max-height: 5000px;
            padding: var(--spacing-md);
        }}

        /* Team groups */
        .team-group {{
            margin-bottom: var(--spacing-lg);
        }}

        .team-group:last-child {{
            margin-bottom: 0;
        }}

        .team-header {{
            font-weight: 700;
            font-size: 0.95rem;
            color: var(--color-text);
            margin-bottom: var(--spacing-sm);
            padding-bottom: var(--spacing-xs);
            border-bottom: 1px solid var(--color-border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: var(--spacing-xs);
        }}

        .coach-info {{
            font-weight: 400;
            font-size: 0.8rem;
            color: var(--color-text-secondary);
        }}

        .coach-info a {{
            color: var(--color-primary);
            text-decoration: none;
        }}

        .coach-info a:hover {{
            text-decoration: underline;
        }}

        .team-calendars {{
            display: flex;
            flex-direction: column;
            gap: var(--spacing-sm);
        }}

        /* Compact calendar cards */
        .calendar-card.compact {{
            padding: var(--spacing-md);
            margin-bottom: 0;
            background: var(--color-bg-elevated);
        }}

        .calendar-card.compact .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: var(--spacing-sm);
            flex-wrap: wrap;
            gap: var(--spacing-xs);
        }}

        .calendar-card.compact .card-title {{
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--color-text);
        }}

        .calendar-card.compact .card-games {{
            font-size: 0.8rem;
            color: var(--color-text-muted);
            background: var(--color-bg-subtle);
            padding: 2px 8px;
            border-radius: 12px;
        }}

        .calendar-card.compact .card-actions {{
            display: flex;
            gap: var(--spacing-sm);
            align-items: center;
            flex-wrap: wrap;
        }}

        .btn-sm {{
            padding: var(--spacing-xs) var(--spacing-sm);
            font-size: 0.8rem;
            min-height: 36px;
        }}

        /* Filter controls */
        .filter-bar {{
            display: flex;
            align-items: center;
            gap: var(--spacing-md);
            margin-bottom: var(--spacing-lg);
            flex-wrap: wrap;
        }}

        .filter-label {{
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--color-text-secondary);
        }}

        .filter-buttons {{
            display: flex;
            gap: var(--spacing-xs);
        }}

        .filter-btn {{
            padding: var(--spacing-xs) var(--spacing-md);
            border: 2px solid var(--color-border);
            background: var(--color-bg-elevated);
            color: var(--color-text-secondary);
            border-radius: var(--radius-sm);
            font-size: 0.85rem;
            font-weight: 500;
            cursor: pointer;
            transition: all var(--transition-fast);
            font-family: var(--font-family);
        }}

        .filter-btn:hover {{
            border-color: var(--color-primary);
            color: var(--color-primary);
        }}

        .filter-btn.active {{
            background: var(--color-primary);
            border-color: var(--color-primary);
            color: white;
        }}

        .filter-btn:focus-visible {{
            outline: 3px solid var(--color-primary);
            outline-offset: 2px;
        }}

        /* Division/standings badges */
        .division-badge {{
            display: none;
            font-size: 0.7rem;
            font-weight: 600;
            background: var(--color-secondary);
            color: white;
            padding: 2px 6px;
            border-radius: 4px;
            margin-left: var(--spacing-xs);
        }}

        .division-badge.rank-badge {{
            background: #6366f1;
        }}

        .division-badge.record-badge {{
            background: #059669;
        }}

        .show-divisions .division-badge {{
            display: inline-block;
        }}

        /* Settings toggle */
        .settings-toggle {{
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            margin-top: var(--spacing-lg);
            padding: var(--spacing-md);
            background: var(--color-bg-elevated);
            border-radius: var(--radius-sm);
            font-size: 0.85rem;
        }}

        .toggle-switch {{
            position: relative;
            width: 44px;
            height: 24px;
            flex-shrink: 0;
        }}

        .toggle-switch input {{
            opacity: 0;
            width: 0;
            height: 0;
        }}

        .toggle-slider {{
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: var(--color-bg-muted);
            transition: var(--transition-fast);
            border-radius: 24px;
        }}

        .toggle-slider:before {{
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 3px;
            bottom: 3px;
            background: white;
            transition: var(--transition-fast);
            border-radius: 50%;
            box-shadow: var(--shadow-sm);
        }}

        .toggle-switch input:checked + .toggle-slider {{
            background: var(--color-primary);
        }}

        .toggle-switch input:checked + .toggle-slider:before {{
            transform: translateX(20px);
        }}

        .toggle-switch input:focus-visible + .toggle-slider {{
            outline: 3px solid var(--color-primary);
            outline-offset: 2px;
        }}

        /* Hidden team groups (for filtering) */
        .team-group.hidden {{
            display: none;
        }}

        /* Instructions card */
        .instructions {{
            background: var(--color-bg-elevated);
            border-radius: var(--radius-md);
            padding: var(--spacing-lg);
            margin-top: var(--spacing-xl);
            box-shadow: var(--shadow-sm);
        }}

        .instructions h2 {{
            margin-top: 0;
            border: none;
            display: block;
        }}

        .instructions ul {{
            padding-left: var(--spacing-lg);
            margin: var(--spacing-md) 0;
        }}

        .instructions li {{
            margin-bottom: var(--spacing-sm);
            color: var(--color-text-secondary);
        }}

        .instructions li strong {{
            color: var(--color-text);
        }}

        .tip {{
            background: var(--color-bg-subtle);
            padding: var(--spacing-sm) var(--spacing-md);
            border-radius: var(--radius-sm);
            font-size: 0.9rem;
            color: var(--color-text-secondary);
        }}

        /* Warning box */
        .warning-box {{
            background: var(--color-warning-bg);
            border-radius: var(--radius-md);
            padding: var(--spacing-lg);
            margin-top: var(--spacing-md);
            box-shadow: var(--shadow-sm);
        }}

        .warning-box h2 {{
            margin-top: 0;
            border: none;
            display: block;
        }}

        .warning-box ul {{
            padding-left: var(--spacing-lg);
            margin: var(--spacing-md) 0 0 0;
        }}

        .warning-box li {{
            margin-bottom: var(--spacing-sm);
            color: var(--color-text-secondary);
        }}

        .warning-box a {{
            color: var(--color-primary);
        }}

        /* FAQ section */
        .faq-section {{
            background: var(--color-bg-elevated);
            border-radius: var(--radius-md);
            padding: var(--spacing-lg);
            margin-top: var(--spacing-md);
            box-shadow: var(--shadow-sm);
        }}

        .faq-section h2 {{
            margin-top: 0;
            border: none;
            display: block;
        }}

        .faq-item {{
            border-bottom: 1px solid var(--color-border-light);
            padding: var(--spacing-md) 0;
        }}

        .faq-item:last-child {{
            border-bottom: none;
            padding-bottom: 0;
        }}

        .faq-item:first-of-type {{
            padding-top: 0;
        }}

        .faq-question {{
            font-weight: 600;
            color: var(--color-text);
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: var(--spacing-xs) 0;
            transition: color var(--transition-fast);
        }}

        .faq-question:hover {{
            color: var(--color-primary);
        }}

        .faq-question:focus-visible {{
            outline: 2px solid var(--color-primary);
            outline-offset: 4px;
            border-radius: 4px;
        }}

        .faq-answer {{
            max-height: 0;
            overflow: hidden;
            transition: max-height var(--transition-normal), padding var(--transition-normal);
            color: var(--color-text-secondary);
            font-size: 0.95rem;
        }}

        .faq-answer.open {{
            max-height: 500px;
            padding-top: var(--spacing-sm);
        }}

        .faq-answer ul {{
            margin: var(--spacing-sm) 0;
            padding-left: var(--spacing-lg);
        }}

        .faq-answer li {{
            margin-bottom: var(--spacing-xs);
        }}

        .faq-answer a {{
            color: var(--color-primary);
        }}

        /* Footer */
        .footer {{
            text-align: center;
            margin-top: var(--spacing-xl);
            padding-top: var(--spacing-lg);
            border-top: 1px solid var(--color-border);
            color: var(--color-text-muted);
            font-size: 0.85rem;
        }}

        .footer-links {{
            display: flex;
            justify-content: center;
            gap: var(--spacing-lg);
            margin-bottom: var(--spacing-md);
        }}

        .footer-links a {{
            color: var(--color-text-secondary);
            text-decoration: none;
            display: flex;
            align-items: center;
            gap: var(--spacing-xs);
            transition: color var(--transition-fast);
        }}

        .footer-links a:hover {{
            color: var(--color-primary);
        }}

        .footer-meta {{
            margin-bottom: var(--spacing-md);
        }}

        .footer-disclaimer {{
            font-size: 0.8rem;
            color: var(--color-text-muted);
            max-width: 500px;
            margin: 0 auto;
            line-height: 1.5;
        }}

        .footer-disclaimer a {{
            color: var(--color-text-secondary);
        }}

        /* Toast notification */
        .toast {{
            position: fixed;
            top: var(--spacing-lg);
            right: var(--spacing-lg);
            background: var(--color-accent);
            color: white;
            padding: var(--spacing-md) var(--spacing-lg);
            border-radius: var(--radius-sm);
            font-weight: 500;
            box-shadow: var(--shadow-lg);
            transform: translateX(calc(100% + var(--spacing-lg)));
            opacity: 0;
            transition: all var(--transition-normal);
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
        }}

        .toast.show {{
            transform: translateX(0);
            opacity: 1;
        }}

        .toast::before {{
            content: '✓';
        }}

        /* Mobile responsive */
        @media (max-width: 640px) {{
            body {{
                padding: var(--spacing-md);
                font-size: 16px;
            }}

            .hero {{
                margin: calc(-1 * var(--spacing-md));
                margin-bottom: var(--spacing-lg);
                padding: var(--spacing-lg) var(--spacing-md);
            }}

            .hero h1 {{
                font-size: 1.75rem;
            }}

            .hero .subtitle {{
                font-size: 1rem;
            }}

            .hero-icon {{
                font-size: 2.5rem;
            }}

            h2 {{
                font-size: 1.15rem;
            }}

            .collapsible {{
                padding: var(--spacing-md);
            }}

            .grade-info {{
                display: none;
            }}

            .calendar-card.compact .card-actions {{
                width: 100%;
                justify-content: flex-start;
            }}

            .btn {{
                flex: 1;
                min-width: 80px;
            }}

            .footer-links {{
                flex-direction: column;
                gap: var(--spacing-sm);
            }}

            .toast {{
                left: var(--spacing-md);
                right: var(--spacing-md);
                transform: translateY(-100%);
            }}

            .toast.show {{
                transform: translateY(0);
            }}
        }}
    </style>
</head>
<body>
    <header class="hero">
        <div class="hero-content">
            <div class="hero-icon" role="img" aria-label="Basketball">🏀</div>
            <h1>{town_name} Basketball</h1>
            <p class="subtitle">Subscribe to automatically sync game schedules to your calendar</p>
        </div>
    </header>

    <div class="auto-sync-note" role="status">
        <span><strong>Automatically Updated:</strong> Schedules are checked hourly during game season. Changes typically appear within an hour of being posted to the league websites. Subscribe once — your calendar stays current automatically.</span>
    </div>

    <div id="toast" class="toast" role="alert" aria-live="polite">URL Copied!</div>

    <section aria-labelledby="calendars-heading">
        <h2 id="calendars-heading">Team Calendars</h2>
        <p style="color: var(--color-text-secondary); font-size: 0.9rem; margin-bottom: var(--spacing-md);">Click a grade to expand. ⭐ Combined calendars include all leagues.</p>

        <div class="filter-bar" role="group" aria-label="Filter teams">
            <span class="filter-label">Show:</span>
            <div class="filter-buttons">
                <button class="filter-btn active" data-filter="all" aria-pressed="true">Both</button>
                <button class="filter-btn" data-filter="M" aria-pressed="false">Boys</button>
                <button class="filter-btn" data-filter="F" aria-pressed="false">Girls</button>
            </div>
        </div>

        {grade_html}
    </section>

    <section class="instructions" aria-labelledby="subscribe-heading">
        <h2 id="subscribe-heading">How to Subscribe</h2>
        <ul>
            <li><strong>Google Calendar:</strong> Other calendars (+) → From URL → paste URL</li>
            <li><strong>Apple Calendar:</strong> File → New Calendar Subscription → paste URL</li>
            <li><strong>iPhone/iPad:</strong> Tap "Subscribe" button, or Settings → Calendar → Accounts → Add Subscribed Calendar</li>
            <li><strong>Outlook:</strong> Add calendar → Subscribe from web</li>
        </ul>
        <p class="tip"><strong>Tip:</strong> Subscribed calendars auto-update periodically (usually every few hours). Data is refreshed hourly during game hours.</p>
    </section>

    <section class="warning-box" aria-labelledby="notes-heading">
        <h2 id="notes-heading">⚠️ Important Notes</h2>
        <ul>
            <li>{games_included_note}</li>
            <li>Schedule data is sourced from league websites. Always verify with official league sources.</li>
            <li>Game times and locations may change — check for updates before traveling.</li>
        </ul>
    </section>

    <section class="faq-section" aria-labelledby="faq-heading">
        <h2 id="faq-heading">Frequently Asked Questions</h2>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                How do I unsubscribe or remove a calendar?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <ul>
                    <li><strong>iPhone/iPad:</strong> Settings → Calendar → Accounts → tap the subscribed calendar → Delete Account</li>
                    <li><strong>Google Calendar:</strong> Hover over the calendar in the left sidebar → click ⋮ → Settings → scroll down → Unsubscribe</li>
                    <li><strong>Apple Calendar (Mac):</strong> Right-click the calendar in the sidebar → Unsubscribe</li>
                    <li><strong>Outlook:</strong> Right-click the calendar → Remove</li>
                </ul>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                How often does the schedule data update?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Schedule data is refreshed <strong>hourly from 6 AM to 9 PM ET</strong> during game season, with one overnight update at 2 AM ET. Your calendar app will typically pull these updates every few hours automatically.</p>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                Why don't I see any games on my calendar?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <ul>
                    <li>The league schedule may not be posted yet — check the official league website</li>
                    <li>Your calendar app may take up to 24 hours to sync initially</li>
                    <li>Try refreshing the calendar manually in your app's settings</li>
                </ul>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                Can I add this calendar to multiple devices?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Yes! If you use a synced calendar service (Google, iCloud, Outlook), just add the subscription on one device and it will appear on all your synced devices automatically.</p>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                Why are some game locations missing or incorrect?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Location data comes directly from the league websites. If a location is missing or wrong, it needs to be corrected there first. Always verify game locations before traveling.</p>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                I found a bug or have a suggestion. How do I report it?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Please submit an issue on our <a href="https://github.com/aknowles/ssbball/issues">GitHub Issues page</a>. We appreciate your feedback!</p>
            </div>
        </div>
    </section>

    <div class="settings-toggle">
        <label class="toggle-switch">
            <input type="checkbox" id="division-toggle" aria-describedby="division-label">
            <span class="toggle-slider"></span>
        </label>
        <span id="division-label">Show division tiers</span>
    </div>

    <footer class="footer">
        <div class="footer-links">
            <a href="https://github.com/aknowles/ssbball" target="_blank" rel="noopener">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/></svg>
                View on GitHub
            </a>
            <a href="https://github.com/aknowles/ssbball/issues" target="_blank" rel="noopener">
                Report an Issue
            </a>
        </div>
        <div class="footer-meta">
            Last updated: {now}
        </div>
        <p class="footer-disclaimer">
            This is an unofficial community project. Not affiliated with, endorsed by, or connected to
            <a href="http://miltontravelbasketball.com">Milton Travel Basketball</a>,
            <a href="https://metrowestbball.com">MetroWest Basketball</a>, or
            <a href="https://ssybl.org">SSYBL</a>.
            For informational purposes only.
        </p>
    </footer>

    <script>
        // ===== Core Functions =====
        function copyUrl(url) {{
            navigator.clipboard.writeText(url).then(() => {{
                const toast = document.getElementById('toast');
                toast.classList.add('show');
                setTimeout(() => toast.classList.remove('show'), 2500);
            }});
        }}

        function toggleSection(btn) {{
            btn.classList.toggle('active');
            const content = btn.nextElementSibling;
            content.classList.toggle('open');
        }}

        function toggleFaq(el) {{
            const answer = el.nextElementSibling;
            const arrow = el.querySelector('.arrow');
            const isOpen = answer.classList.toggle('open');
            arrow.style.transform = isOpen ? 'rotate(180deg)' : 'rotate(0deg)';
            el.setAttribute('aria-expanded', isOpen);
        }}

        // ===== Gender Filter =====
        const filterBtns = document.querySelectorAll('.filter-btn');
        const teamGroups = document.querySelectorAll('.team-group');
        const gradeSections = document.querySelectorAll('.grade-section');

        function applyGenderFilter(filter) {{
            teamGroups.forEach(group => {{
                const gender = group.dataset.gender;
                if (filter === 'all' || gender === filter) {{
                    group.classList.remove('hidden');
                }} else {{
                    group.classList.add('hidden');
                }}
            }});

            // Update grade section counts based on visible teams
            gradeSections.forEach(section => {{
                const groups = section.querySelectorAll('.team-group');
                let visibleTeams = 0;
                let visibleGames = 0;

                groups.forEach(group => {{
                    if (!group.classList.contains('hidden')) {{
                        visibleTeams++;
                        visibleGames += parseInt(group.dataset.games || 0, 10);
                    }}
                }});

                const infoEl = section.querySelector('.grade-info');
                if (infoEl) {{
                    infoEl.textContent = `${{visibleTeams}} team${{visibleTeams !== 1 ? 's' : ''}} • ${{visibleGames}} games`;
                }}
            }});

            // Update button states
            filterBtns.forEach(btn => {{
                const isActive = btn.dataset.filter === filter;
                btn.classList.toggle('active', isActive);
                btn.setAttribute('aria-pressed', isActive);
            }});

            // Save preference
            localStorage.setItem('genderFilter', filter);
        }}

        filterBtns.forEach(btn => {{
            btn.addEventListener('click', () => {{
                applyGenderFilter(btn.dataset.filter);
            }});
        }});

        // ===== Division Toggle =====
        const divisionToggle = document.getElementById('division-toggle');

        function applyDivisionToggle(show) {{
            if (show) {{
                document.body.classList.add('show-divisions');
            }} else {{
                document.body.classList.remove('show-divisions');
            }}
            divisionToggle.checked = show;
            localStorage.setItem('showDivisions', show);
        }}

        divisionToggle.addEventListener('change', () => {{
            applyDivisionToggle(divisionToggle.checked);
        }});

        // ===== Initialize from localStorage =====
        document.addEventListener('DOMContentLoaded', () => {{
            // Restore gender filter
            const savedFilter = localStorage.getItem('genderFilter') || 'all';
            applyGenderFilter(savedFilter);

            // Restore division toggle
            const savedDivisions = localStorage.getItem('showDivisions') === 'true';
            applyDivisionToggle(savedDivisions);
        }});

        // ===== Keyboard Accessibility =====
        document.querySelectorAll('.faq-question').forEach(q => {{
            q.addEventListener('keydown', e => {{
                if (e.key === 'Enter' || e.key === ' ') {{
                    e.preventDefault();
                    toggleFaq(q);
                }}
            }});
        }});
    </script>
</body>
</html>
'''


def discover_and_fetch_teams(config: dict) -> tuple[list[dict], list[dict]]:
    """
    Discover teams dynamically and fetch their schedules.

    Returns: (team_configs, all_games)
    """
    # Update global LEAGUES with any custom leagues from config
    global LEAGUES
    LEAGUES = get_leagues(config)

    town_name = config.get('town_name', 'Milton')
    leagues = config.get('leagues', ['ssybl', 'metrowbb'])
    grades = config.get('grades', [5, 8])
    genders = config.get('genders', ['M'])
    colors = config.get('colors', ['White'])  # Filter to specific colors, or empty for all
    include_nl_games = config.get('include_nl_games', True)  # Include tournaments/playoffs by default
    jerseys = config.get('jerseys', {})  # Jersey colors for home/away games
    team_aliases = config.get('team_aliases', {})  # Map canonical colors to aliases
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
                    color = parse_team_color(team['team_name'], team_aliases)
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
                        'division_no': team.get('division_no', ''),
                        'division_tier': team.get('division_tier', '')
                    })

    logger.info(f"Discovered {len(discovered_teams)} teams")

    # Fetch standings for each unique division
    all_standings = {}  # Maps team_no -> standings info
    unique_divisions = set()
    for team in discovered_teams:
        div_no = team.get('division_no', '')
        if div_no:
            unique_divisions.add((div_no, team['league']))

    logger.info(f"Fetching standings for {len(unique_divisions)} divisions")
    for div_no, league in unique_divisions:
        standings = fetch_division_standings(div_no, league)
        all_standings.update(standings)

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

        # Get standings for this team
        team_standings = all_standings.get(team_no, {})

        team_config = {
            'id': team_id,
            'team_name': team_name,
            'short_name': short_name,
            'client_id': league,
            'team_no': team_no,
            'league': league_name,
            'grade': str(grade),
            'gender': gender,
            'color': color,
            'division_tier': team.get('division_tier', ''),
            'wins': team_standings.get('wins', 0),
            'losses': team_standings.get('losses', 0),
            'ties': team_standings.get('ties', 0),
            'rank': team_standings.get('rank', 0),
            'jerseys': jerseys
        }
        team_configs.append(team_config)

        # Fetch games
        games = fetch_team_games(team_config, include_nl_games=include_nl_games)
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

    # Initialize leagues (merge defaults with any custom_leagues)
    global LEAGUES
    LEAGUES = get_leagues(config)

    base_url = args.base_url or config.get('base_url', 'https://example.github.io/ssbball')
    town_name = config.get('town_name', 'Milton')

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if using new dynamic config or legacy static config
    include_nl_games = config.get('include_nl_games', True)

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
            games = fetch_team_games(team_config, include_nl_games=include_nl_games)
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
            'games': len(team_games),
            'gender': team_config.get('gender', ''),
            'division_tier': team_config.get('division_tier', ''),
            'wins': team_config.get('wins', 0),
            'losses': team_config.get('losses', 0),
            'ties': team_config.get('ties', 0),
            'rank': team_config.get('rank', 0)
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

        # Get gender from filter
        combo_gender = combo_filter.get('gender', '')

        # Check if all component teams have matching division tiers
        # Also aggregate W-L records across leagues
        combo_division = ''
        combo_wins = 0
        combo_losses = 0
        combo_ties = 0
        if combo_filter:
            matching_teams = [
                tc for tc in team_configs
                if all(tc.get(k) == v for k, v in combo_filter.items())
            ]
            division_tiers = set(tc.get('division_tier', '') for tc in matching_teams)
            # Only set division if all teams have the same non-empty tier
            if len(division_tiers) == 1:
                combo_division = division_tiers.pop()

            # Aggregate W-L across all matching teams
            for tc in matching_teams:
                combo_wins += tc.get('wins', 0)
                combo_losses += tc.get('losses', 0)
                combo_ties += tc.get('ties', 0)

        calendar_info.insert(0, {  # Add at beginning
            'type': 'combined',
            'id': combo_id,
            'name': combo_name,
            'description': combo.get('description', ''),
            'games': len(filtered_games),
            'gender': combo_gender,
            'division_tier': combo_division,
            'wins': combo_wins,
            'losses': combo_losses,
            'ties': combo_ties,
            'rank': 0  # No rank for combined calendars
        })

    # Generate index.html
    coaches = config.get('coaches', {})
    index_html = generate_index_html(calendar_info, base_url, town_name, include_nl_games, coaches=coaches)
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
