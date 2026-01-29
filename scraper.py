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


def ordinal(n) -> str:
    """Return ordinal string for a number (1st, 2nd, 3rd, 4th, etc.)."""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if 11 <= n % 100 <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


# =============================================================================
# Schedule Change Detection and Notifications
# =============================================================================

def game_to_key(game: dict) -> str:
    """Create a unique key for a game for comparison purposes.

    Uses date, opponent, and team info to create a stable identifier.
    """
    dt = game.get('datetime')
    if hasattr(dt, 'strftime'):
        date_str = dt.strftime('%Y-%m-%d')
    else:
        date_str = str(dt)[:10] if dt else 'unknown'

    opponent = game.get('opponent', 'TBD').lower().strip()
    grade = game.get('grade', '')
    gender = game.get('gender', '')
    color = game.get('color', '')

    return f"{grade}-{gender}-{color}|{date_str}|{opponent}"


def game_to_state(game: dict) -> dict:
    """Convert a game to a serializable state dict for storage."""
    dt = game.get('datetime')
    if hasattr(dt, 'isoformat'):
        datetime_str = dt.isoformat()
    else:
        datetime_str = str(dt) if dt else None

    return {
        'datetime': datetime_str,
        'opponent': game.get('opponent', ''),
        'location': game.get('location', ''),
        'team_name': game.get('team_name', ''),
        'short_name': game.get('short_name', ''),
        'grade': game.get('grade', ''),
        'gender': game.get('gender', ''),
        'color': game.get('color', ''),
        'game_type': game.get('game_type', ''),
        'is_practice': game.get('is_practice', False)
    }


def load_previous_state(state_path: Path) -> dict:
    """Load the previous schedule state from JSON file."""
    if not state_path.exists():
        logger.info(f"No previous state file found at {state_path}")
        return {}

    try:
        with open(state_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error loading previous state: {e}")
        return {}


def save_current_state(state_path: Path, games: list[dict], practices: list[dict]) -> None:
    """Save the current schedule state to JSON file for next comparison."""
    state = {
        'updated': datetime.now(EASTERN).isoformat(),
        'games': {},
        'practices': {}
    }

    for game in games:
        key = game_to_key(game)
        state['games'][key] = game_to_state(game)

    for practice in practices:
        key = game_to_key(practice)
        state['practices'][key] = game_to_state(practice)

    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2)

    logger.info(f"Saved schedule state to {state_path}")


def detect_changes(previous_state: dict, current_games: list[dict], current_practices: list[dict]) -> dict:
    """Compare previous and current schedules to detect changes.

    Returns:
        Dict with 'new', 'deleted', 'modified' lists of change descriptions
    """
    changes = {
        'new': [],
        'deleted': [],
        'modified': []
    }

    prev_games = previous_state.get('games', {})
    prev_practices = previous_state.get('practices', {})

    # Build current state dicts
    curr_games = {}
    for game in current_games:
        key = game_to_key(game)
        curr_games[key] = game

    curr_practices = {}
    for practice in current_practices:
        key = game_to_key(practice)
        curr_practices[key] = practice

    # Check for new and modified games
    for key, game in curr_games.items():
        team_key = f"{game.get('grade')}-{game.get('gender')}-{game.get('color')}"
        if key not in prev_games:
            changes['new'].append({
                'type': 'game',
                'team_key': team_key,
                'event': game
            })
        else:
            # Check for modifications (time or location change)
            prev = prev_games[key]
            curr_state = game_to_state(game)

            time_changed = prev.get('datetime') != curr_state.get('datetime')
            location_changed = prev.get('location') != curr_state.get('location')

            if time_changed or location_changed:
                changes['modified'].append({
                    'type': 'game',
                    'team_key': team_key,
                    'event': game,
                    'previous': prev,
                    'time_changed': time_changed,
                    'location_changed': location_changed
                })

    # Check for deleted games
    for key, prev in prev_games.items():
        if key not in curr_games:
            team_key = f"{prev.get('grade')}-{prev.get('gender')}-{prev.get('color')}"
            changes['deleted'].append({
                'type': 'game',
                'team_key': team_key,
                'previous': prev
            })

    # Same checks for practices
    for key, practice in curr_practices.items():
        team_key = f"{practice.get('grade')}-{practice.get('gender')}-{practice.get('color')}"
        if key not in prev_practices:
            changes['new'].append({
                'type': 'practice',
                'team_key': team_key,
                'event': practice
            })
        else:
            prev = prev_practices[key]
            curr_state = game_to_state(practice)

            time_changed = prev.get('datetime') != curr_state.get('datetime')
            location_changed = prev.get('location') != curr_state.get('location')

            if time_changed or location_changed:
                changes['modified'].append({
                    'type': 'practice',
                    'team_key': team_key,
                    'event': practice,
                    'previous': prev,
                    'time_changed': time_changed,
                    'location_changed': location_changed
                })

    for key, prev in prev_practices.items():
        if key not in curr_practices:
            team_key = f"{prev.get('grade')}-{prev.get('gender')}-{prev.get('color')}"
            changes['deleted'].append({
                'type': 'practice',
                'team_key': team_key,
                'previous': prev
            })

    return changes


def format_datetime_for_notification(dt_str: str) -> str:
    """Format a datetime string for human-readable notification."""
    if not dt_str:
        return "TBD"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime('%a %b %d @ %I:%M%p').replace(' 0', ' ').replace('AM', 'am').replace('PM', 'pm')
    except (ValueError, TypeError):
        return dt_str[:16] if len(dt_str) > 16 else dt_str


def send_ntfy_notification(topic: str, title: str, message: str, priority: str = 'default', tags: list = None, dry_run: bool = False) -> bool:
    """Send a notification via ntfy.sh.

    Args:
        topic: The ntfy topic to send to
        title: Notification title
        message: Notification body
        priority: 'min', 'low', 'default', 'high', 'urgent'
        tags: List of emoji tags (e.g., ['basketball', 'warning'])
        dry_run: If True, log what would be sent but don't actually send

    Returns:
        True if successful (or dry_run), False otherwise
    """
    url = f"https://ntfy.sh/{topic}"

    if dry_run:
        logger.info(f"[DRY-RUN] Would send notification to {topic}:")
        logger.info(f"  Title: {title}")
        logger.info(f"  Priority: {priority}")
        logger.info(f"  Tags: {tags}")
        for line in message.split('\n'):
            logger.info(f"  Message: {line}")
        return True

    headers = {
        'Title': title,
        'Priority': priority
    }

    if tags:
        headers['Tags'] = ','.join(tags)

    try:
        data = message.encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                logger.info(f"Sent notification to {topic}: {title}")
                return True
            else:
                logger.warning(f"Notification failed with status {response.status}")
                return False
    except Exception as e:
        logger.warning(f"Failed to send notification to {topic}: {e}")
        return False


def send_change_notifications(changes: dict, ntfy_prefix: str, town_name: str, dry_run: bool = False) -> int:
    """Send notifications for all detected changes.

    Args:
        changes: Dict from detect_changes()
        ntfy_prefix: Prefix for ntfy topics (e.g., 'ssbball')
        town_name: Town name for notifications
        dry_run: If True, log what would be sent but don't actually send

    Returns:
        Number of notifications sent (or would be sent in dry_run mode)
    """
    sent = 0

    # Group changes by team
    teams_with_changes = set()
    for change in changes['new'] + changes['deleted'] + changes['modified']:
        teams_with_changes.add(change.get('team_key', 'unknown'))

    for team_key in teams_with_changes:
        team_changes = {
            'new': [c for c in changes['new'] if c.get('team_key') == team_key],
            'deleted': [c for c in changes['deleted'] if c.get('team_key') == team_key],
            'modified': [c for c in changes['modified'] if c.get('team_key') == team_key]
        }

        # Build notification message
        messages = []

        for change in team_changes['new']:
            event = change['event']
            event_type = 'Practice' if change['type'] == 'practice' else 'Game'
            dt = event.get('datetime')
            if hasattr(dt, 'strftime'):
                dt_str = dt.strftime('%a %b %d @ %I:%M%p').replace(' 0', ' ')
            else:
                dt_str = format_datetime_for_notification(str(dt) if dt else '')

            opponent = event.get('opponent', '')
            if change['type'] == 'practice':
                messages.append(f"NEW {event_type}: {dt_str}")
            else:
                messages.append(f"NEW {event_type}: {dt_str} vs {opponent}")

        for change in team_changes['deleted']:
            prev = change['previous']
            event_type = 'Practice' if change['type'] == 'practice' else 'Game'
            dt_str = format_datetime_for_notification(prev.get('datetime', ''))
            opponent = prev.get('opponent', '')

            if change['type'] == 'practice':
                messages.append(f"CANCELLED {event_type}: {dt_str}")
            else:
                messages.append(f"CANCELLED {event_type}: {dt_str} vs {opponent}")

        for change in team_changes['modified']:
            event = change['event']
            prev = change['previous']
            event_type = 'Practice' if change['type'] == 'practice' else 'Game'

            dt = event.get('datetime')
            if hasattr(dt, 'strftime'):
                new_dt_str = dt.strftime('%a %b %d @ %I:%M%p').replace(' 0', ' ')
            else:
                new_dt_str = format_datetime_for_notification(str(dt) if dt else '')

            opponent = event.get('opponent', '')

            change_desc = []
            if change.get('time_changed'):
                old_dt_str = format_datetime_for_notification(prev.get('datetime', ''))
                change_desc.append(f"time: {old_dt_str} → {new_dt_str}")
            if change.get('location_changed'):
                change_desc.append(f"location changed")

            change_info = ', '.join(change_desc)
            # Include date in message when time didn't change (so user knows which game)
            if change.get('time_changed'):
                # Time change already includes the date
                if change['type'] == 'practice':
                    messages.append(f"CHANGED {event_type}: {change_info}")
                else:
                    messages.append(f"CHANGED {event_type} vs {opponent}: {change_info}")
            else:
                # No time change, so include the date for context
                if change['type'] == 'practice':
                    messages.append(f"CHANGED {event_type} on {new_dt_str}: {change_info}")
                else:
                    messages.append(f"CHANGED {event_type} vs {opponent} on {new_dt_str}: {change_info}")

        if messages:
            # Create topic name: prefix-grade-gender-color (e.g., ssbball-5-m-red)
            topic = f"{ntfy_prefix}-{team_key}".lower().replace(' ', '-')

            # Determine priority based on urgency
            has_cancellation = len(team_changes['deleted']) > 0
            priority = 'high' if has_cancellation else 'default'

            # Determine tags
            tags = ['basketball']
            if has_cancellation:
                tags.append('warning')
            if team_changes['new']:
                tags.append('calendar')

            title = f"{town_name} {team_key} Schedule Update"
            message = '\n'.join(messages)

            if send_ntfy_notification(topic, title, message, priority=priority, tags=tags, dry_run=dry_run):
                sent += 1

    return sent


def send_test_notification(ntfy_prefix: str, team_key: str, town_name: str, custom_message: str = None) -> bool:
    """Send a test notification or custom ad hoc message.

    Args:
        ntfy_prefix: Prefix for ntfy topics (e.g., 'milton-basketball')
        team_key: Team identifier (e.g., '5-m-red')
        town_name: Town name for notifications
        custom_message: Optional custom message (for ad hoc announcements)

    Returns:
        True if successful, False otherwise
    """
    topic = f"{ntfy_prefix}-{team_key}".lower().replace(' ', '-')

    if custom_message:
        # Ad hoc announcement mode
        title = f"{town_name} {team_key.upper()} - Announcement"
        message = custom_message
        tags = ['basketball', 'loudspeaker']
        priority = 'high'
    else:
        # Test notification mode
        title = f"{town_name} {team_key.upper()} - Test Notification"
        message = (
            "This is a test notification from the schedule system.\n"
            "If you see this, notifications are working correctly!\n"
            "\n"
            "You will receive alerts when:\n"
            "- Games are added or cancelled\n"
            "- Game times or locations change\n"
            "- Practices are added, cancelled, or modified"
        )
        tags = ['basketball', 'white_check_mark']
        priority = 'default'

    logger.info(f"Sending notification to {topic}")
    return send_ntfy_notification(topic, title, message, priority=priority, tags=tags)


def get_season() -> str:
    """Calculate the current season (year)."""
    now = datetime.now()
    # Season runs Aug-Mar, so Aug+ is next year's season
    if now.month >= 8:
        return str(now.year + 1)
    return str(now.year)


# Day name to weekday number mapping
DAY_TO_WEEKDAY = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6
}


def parse_season_dates(config: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """Parse season start and end dates from config.

    Returns:
        Tuple of (start_date, end_date) as timezone-aware datetimes, or (None, None) if not configured.
    """
    season = config.get('season', {})
    if not season:
        return None, None

    start_str = season.get('start')
    end_str = season.get('end')

    start_date = None
    end_date = None

    if start_str:
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=EASTERN)
        except ValueError:
            logger.warning(f"Invalid season start date format: {start_str}")

    if end_str:
        try:
            # End date should include the entire day
            end_date = datetime.strptime(end_str, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, tzinfo=EASTERN
            )
        except ValueError:
            logger.warning(f"Invalid season end date format: {end_str}")

    return start_date, end_date


def parse_blackout_dates(config: dict) -> list[tuple[datetime, datetime, str]]:
    """Parse blackout date ranges from config (e.g., school vacations).

    Returns:
        List of (start_date, end_date, reason) tuples for blackout periods.
    """
    season = config.get('season', {})
    blackouts = season.get('blackout_dates', [])
    parsed = []

    for blackout in blackouts:
        start_str = blackout.get('start')
        end_str = blackout.get('end')
        reason = blackout.get('reason', 'Blackout')

        if not start_str or not end_str:
            continue

        try:
            start = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=EASTERN)
            end = datetime.strptime(end_str, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, tzinfo=EASTERN
            )
            parsed.append((start, end, reason))
        except ValueError:
            logger.warning(f"Invalid blackout date format: {start_str} - {end_str}")

    return parsed


def is_blackout_date(dt: datetime, blackouts: list[tuple[datetime, datetime, str]]) -> Optional[str]:
    """Check if a date falls within a blackout period.

    Returns:
        The reason string if date is blacked out, None otherwise.
    """
    for start, end, reason in blackouts:
        if start <= dt <= end:
            return reason
    return None


def generate_practice_events(config: dict, team_key: str, team_name: str, short_name: str, team_games: list = None) -> list[dict]:
    """Generate practice events for a team based on recurring schedules and modifications.

    Args:
        config: Full config dict containing season and practices
        team_key: Team key like "5-M-Red"
        team_name: Full team name for display
        short_name: Short name like "5B-Red"
        team_games: Optional list of game events for this team (used to skip conflicting practices)

    Returns:
        List of practice event dicts compatible with generate_ical
    """
    practices_config = config.get('practices', {})
    team_practices = practices_config.get(team_key, {})
    team_games = team_games or []

    # Build a set of game datetimes for conflict checking
    # A practice conflicts if it's within 1 hour of a game
    def conflicts_with_game(practice_dt: datetime, duration: int) -> bool:
        """Check if a practice conflicts with any game (overlaps or within 1 hour)."""
        practice_end = practice_dt + timedelta(minutes=duration)
        buffer = timedelta(hours=1)

        for game in team_games:
            game_dt = game.get('datetime')
            if not game_dt:
                continue
            game_end = game_dt + timedelta(hours=1)  # Assume 1 hour game duration

            # Check if practice is within 1 hour before or after game
            # Practice conflicts if:
            # - practice starts within 1 hour before game ends, OR
            # - practice ends within 1 hour after game starts
            if (practice_dt < game_end + buffer) and (practice_end > game_dt - buffer):
                return True
        return False

    if not team_practices:
        return []

    season_start, season_end = parse_season_dates(config)
    if not season_start or not season_end:
        logger.warning(f"No season dates configured, skipping practices for {team_key}")
        return []

    # Parse blackout dates (school vacations, etc.)
    blackouts = parse_blackout_dates(config)

    events = []
    recurring = team_practices.get('recurring', [])
    adhoc = team_practices.get('adhoc', [])
    modifications = team_practices.get('modifications', [])

    # Build a lookup for modifications by date
    mod_by_date = {}
    for mod in modifications:
        mod_date = mod.get('date')
        if mod_date:
            mod_by_date[mod_date] = mod

    # Generate recurring practice events
    for schedule in recurring:
        day_name = schedule.get('day', '').lower()
        time_str = schedule.get('time', '18:00')
        duration = schedule.get('duration', 60)  # minutes
        location = schedule.get('location', '')
        notes = schedule.get('notes', '')

        if day_name not in DAY_TO_WEEKDAY:
            logger.warning(f"Invalid day name: {schedule.get('day')}")
            continue

        target_weekday = DAY_TO_WEEKDAY[day_name]

        # Parse time
        try:
            hour, minute = map(int, time_str.split(':'))
        except ValueError:
            logger.warning(f"Invalid time format: {time_str}")
            continue

        # Find first occurrence of this weekday on or after season start
        current = season_start
        days_until_target = (target_weekday - current.weekday()) % 7
        current = current + timedelta(days=days_until_target)
        current = current.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Generate events for each week
        while current <= season_end:
            date_str = current.strftime('%Y-%m-%d')

            # Check for modifications on this date
            if date_str in mod_by_date:
                mod = mod_by_date[date_str]
                if mod.get('action') == 'cancel':
                    # Skip this practice
                    current += timedelta(weeks=1)
                    continue
                elif mod.get('action') == 'modify':
                    # Apply modifications
                    if mod.get('time'):
                        try:
                            mod_hour, mod_minute = map(int, mod['time'].split(':'))
                            current = current.replace(hour=mod_hour, minute=mod_minute)
                        except ValueError:
                            pass
                    if mod.get('duration'):
                        duration = mod['duration']
                    if mod.get('location'):
                        location = mod['location']
                    if mod.get('notes'):
                        notes = mod['notes']

            # Check for blackout dates (school vacations)
            blackout_reason = is_blackout_date(current, blackouts)
            if blackout_reason:
                logger.info(f"Skipping practice on {current.date()} for {team_key} - {blackout_reason}")
                current += timedelta(weeks=1)
                continue

            # Check for game conflicts before creating practice
            if conflicts_with_game(current, duration):
                logger.info(f"Skipping practice on {current.date()} for {team_key} - conflicts with game")
                current += timedelta(weeks=1)
                continue

            # Create practice event
            event = {
                'datetime': current,
                'opponent': '',  # No opponent for practice
                'location': location,
                'team_name': team_name,
                'short_name': short_name,
                'game_type': 'practice',
                'league': 'Practice',
                'is_practice': True,
                'duration': duration,
                'notes': notes,
                'grade': team_key.split('-')[0] if '-' in team_key else '',
                'gender': team_key.split('-')[1] if '-' in team_key and len(team_key.split('-')) > 1 else '',
                'color': team_key.split('-')[2] if '-' in team_key and len(team_key.split('-')) > 2 else ''
            }
            events.append(event)

            current += timedelta(weeks=1)

    # Add ad-hoc practices
    for adhoc_practice in adhoc:
        date_str = adhoc_practice.get('date')
        time_str = adhoc_practice.get('time', '18:00')
        duration = adhoc_practice.get('duration', 60)
        location = adhoc_practice.get('location', '')
        notes = adhoc_practice.get('notes', '')

        if not date_str:
            continue

        try:
            hour, minute = map(int, time_str.split(':'))
            practice_dt = datetime.strptime(date_str, '%Y-%m-%d').replace(
                hour=hour, minute=minute, tzinfo=EASTERN
            )
        except ValueError:
            logger.warning(f"Invalid adhoc practice date/time: {date_str} {time_str}")
            continue

        # Check if within season and no game conflict (ad-hoc practices ignore blackouts)
        if season_start <= practice_dt <= season_end:
            if conflicts_with_game(practice_dt, duration):
                logger.info(f"Skipping adhoc practice on {practice_dt.date()} for {team_key} - conflicts with game")
                continue

            event = {
                'datetime': practice_dt,
                'opponent': '',
                'location': location,
                'team_name': team_name,
                'short_name': short_name,
                'game_type': 'practice',
                'league': 'Practice',
                'is_practice': True,
                'duration': duration,
                'notes': notes,
                'grade': team_key.split('-')[0] if '-' in team_key else '',
                'gender': team_key.split('-')[1] if '-' in team_key and len(team_key.split('-')) > 1 else '',
                'color': team_key.split('-')[2] if '-' in team_key and len(team_key.split('-')) > 2 else ''
            }
            events.append(event)

    logger.info(f"Generated {len(events)} practice events for {team_key}")
    return events


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

            # Extract score info for completed games
            team_score = item.get('teamscore', '')
            opponent_score = item.get('opponentscore', '')
            won_lost = item.get('wonlost', '')
            # Clean up scores - API returns "--" for unplayed games
            if team_score == '--':
                team_score = ''
            if opponent_score == '--':
                opponent_score = ''

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
                'jerseys': team_config.get('jerseys', {}),
                'team_score': team_score,
                'opponent_score': opponent_score,
                'won_lost': won_lost
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


def normalize_opponent(opponent: str) -> str:
    """Normalize opponent name for deduplication.

    Removes grade/gender indicators (5B, 6G, etc.) and division info (D1, D2, etc.)
    to match the same game across different leagues.

    Examples:
        "Stoughton 5B D1" -> "stoughton"
        "Stoughton D1" -> "stoughton"
        "Pembroke (Blue) 5B D1" -> "pembroke (blue)"
    """
    name = opponent.lower().strip()
    # Remove grade+gender indicators like "5B", "6G", "5b", "6g" (grade + Boys/Girls)
    name = re.sub(r'\s+\d+[bgBG]\b', '', name)
    # Remove division indicators like "D1", "D2", "d1", "d2"
    name = re.sub(r'\s+d\d+\b', '', name, flags=re.IGNORECASE)
    # Clean up any double spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def dedupe_games(games: list[dict]) -> list[dict]:
    """Remove duplicate games, preferring league games over non-league/tournament games.

    Matches games by datetime, normalized opponent name, and grade.
    When duplicates are found, prefers the league game (is_tournament=False)
    over the non-league/tournament game.
    """
    # Sort so league games come first (is_tournament=False before True)
    # This ensures league games are kept when duplicates are found
    sorted_games = sorted(games, key=lambda g: (g.get('is_tournament', False), g['datetime']))

    seen = {}  # key -> game (keep first = league game if available)
    for game in sorted_games:
        normalized_opp = normalize_opponent(game['opponent'])
        key = (game['datetime'].isoformat(), normalized_opp, game.get('grade', ''))
        if key not in seen:
            seen[key] = game

    # Return games in their original order (by datetime)
    return sorted(seen.values(), key=lambda g: g['datetime'])


def generate_ical(games: list[dict], calendar_name: str, calendar_id: str) -> bytes:
    """Generate iCalendar content for games and practices."""
    cal = Calendar()
    cal.add('prodid', f'-//Basketball Schedule//{calendar_id}//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', calendar_name)
    cal.add('x-wr-timezone', 'America/New_York')

    for game in sorted(games, key=lambda g: g['datetime']):
        event = Event()

        is_practice = game.get('is_practice', False)

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

        if is_practice:
            # Practice event formatting
            event.add('summary', f"{prefix}🏋️ Practice")
        else:
            # Game event formatting
            # Use trophy emoji for tournament/playoff games
            emoji = "🏆" if is_tournament else "🏀"

            # Build result prefix and score suffix for completed games
            won_lost = game.get('won_lost', '')
            team_score = game.get('team_score', '')
            opponent_score = game.get('opponent_score', '')
            result_prefix = ''
            score_suffix = ''
            if won_lost and team_score and opponent_score:
                # W/L emoji at the front, score at the end
                if won_lost == 'W':
                    result_prefix = '✅ '
                elif won_lost == 'L':
                    result_prefix = '❌ '
                score_suffix = f" [{team_score}-{opponent_score}]"

            if 'away' in game_type or game_type == 'a':
                event.add('summary', f"{prefix}{result_prefix}{emoji} @ {opponent}{score_suffix}")
            else:
                event.add('summary', f"{prefix}{result_prefix}{emoji} vs {opponent}{score_suffix}")

        event.add('dtstart', game['datetime'])
        # Use duration from event if available (for practices), else default to 1 hour
        duration_minutes = game.get('duration', 60)
        event.add('dtend', game['datetime'] + timedelta(minutes=duration_minutes))

        if game.get('location'):
            event.add('location', game['location'])

        if is_practice:
            # Practice description
            desc = [
                f"Team: {game.get('team_name', 'Unknown')}",
                f"Type: Practice",
                f"Duration: {duration_minutes} minutes"
            ]
            if game.get('location'):
                desc.append(f"Location: {game['location']}")
            if game.get('notes'):
                desc.append(f"\nNote: {game['notes']}")
        else:
            # Game description
            desc = [
                f"Team: {game.get('team_name', 'Unknown')}",
                f"Opponent: {opponent}",
                f"League: {game.get('league', 'Basketball')}"
            ]
            # Add score for completed games
            won_lost = game.get('won_lost', '')
            team_score = game.get('team_score', '')
            opponent_score = game.get('opponent_score', '')
            if won_lost and team_score and opponent_score:
                result_text = "Win" if won_lost == 'W' else "Loss" if won_lost == 'L' else won_lost
                desc.append(f"Result: {result_text} {team_score}-{opponent_score}")
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

        # Reminders
        alarm1 = Alarm()
        alarm1.add('action', 'DISPLAY')
        alarm1.add('trigger', timedelta(hours=-1))
        if is_practice:
            alarm1.add('description', f'Basketball practice in 1 hour')
        else:
            alarm1.add('description', f'Basketball game vs {opponent} in 1 hour')
        event.add_component(alarm1)

        alarm2 = Alarm()
        alarm2.add('action', 'DISPLAY')
        alarm2.add('trigger', timedelta(minutes=-30))
        if is_practice:
            alarm2.add('description', f'Basketball practice in 30 minutes')
        else:
            alarm2.add('description', f'Basketball game vs {opponent} in 30 minutes')
        event.add_component(alarm2)

        cal.add_component(event)

    return cal.to_ical()


def generate_index_html(calendars: list[dict], base_url: str, town_name: str, include_nl_games: bool = True, coaches: dict = None, all_games: list = None, ntfy_topic: str = None) -> str:
    """Generate the landing page HTML with hierarchical sections: Grade -> Color -> Calendars.

    Args:
        calendars: List of calendar info dicts
        base_url: Base URL for calendar links
        town_name: Town name for display
        include_nl_games: Whether tournament games are included
        coaches: Optional dict mapping team keys (e.g. "5-M-White") to coach info.
                 Single coach: "Name" or ["Name", "email@example.com"]
                 Multiple coaches: [["Name1", "email1"], ["Name2"], ["Name3", "email3"]]
        all_games: Optional list of all game dicts for schedule display
        ntfy_topic: Optional ntfy.sh topic prefix for push notifications
    """
    coaches = coaches or {}
    all_games = all_games or []
    now = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')

    def extract_grade(cal):
        """Extract grade number from calendar."""
        cal_id = cal.get('id', '')
        cal_name = cal.get('name', '')
        # Check for proper ordinals (1st, 2nd, 3rd, 4th, etc.)
        for g in ['1st', '2nd', '3rd', '4th', '5th', '6th', '7th', '8th']:
            if g in cal_id or g in cal_name:
                return g.replace('st', '').replace('nd', '').replace('rd', '').replace('th', '')
        # Fallback for legacy data with incorrect ordinals (e.g., "3th")
        for g in ['1', '2', '3', '4', '5', '6', '7', '8']:
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

    def get_team_games(grade: str, gender_code: str, color: str) -> list:
        """Get games for a specific team, deduplicated and sorted by date."""
        team_games = []
        for game in all_games:
            g_grade = str(game.get('grade', ''))
            g_gender = game.get('gender', '')
            g_color = game.get('color', '').lower()
            if g_grade == grade and g_gender == gender_code and g_color == color.lower():
                team_games.append(game)
        # Deduplicate games (prefers league over non-league for same game)
        return dedupe_games(team_games)

    def make_games_section_html() -> str:
        """Generate the Games section showing today's games and recent results (last 3 days)."""
        now_dt = datetime.now(EASTERN)
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        three_days_ago = today_start - timedelta(days=3)

        # Dedupe all games first
        deduped_games = dedupe_games(all_games)

        # Today's scheduled games (not yet played)
        todays_games = [g for g in deduped_games
                        if g['datetime'] >= now_dt and g['datetime'] < today_end
                        and not g.get('is_practice', False)]
        todays_games.sort(key=lambda g: g['datetime'])

        # Recent results (last 3 days, completed games with scores)
        recent_results = [g for g in deduped_games
                         if g['datetime'] >= three_days_ago and g['datetime'] < now_dt
                         and g.get('won_lost')
                         and not g.get('is_practice', False)]
        recent_results.sort(key=lambda g: g['datetime'], reverse=True)

        if not todays_games and not recent_results:
            return ''

        sections = []

        # Today's games section
        if todays_games:
            game_items = []
            for g in todays_games:
                dt = g['datetime']
                time_str = dt.strftime('%I:%M %p').lstrip('0').lower()
                opponent = g.get('opponent', 'TBD')
                game_type = g.get('game_type', '').lower()
                is_tournament = g.get('is_tournament', False)
                emoji = '🏆' if is_tournament else '🏀'
                short_name = g.get('short_name', '')
                location = g.get('location', '')
                venue = location.split(',')[0] if location else ''
                gender = g.get('gender', '')

                if 'away' in game_type or game_type == 'a':
                    matchup = f'@ {opponent}'
                else:
                    matchup = f'vs {opponent}'

                game_items.append(f'''
                    <div class="games-row" data-gender="{gender}">
                        <span class="games-time">{time_str}</span>
                        <span class="games-team">{short_name}</span>
                        <span class="games-matchup">{emoji} {matchup}</span>
                        <span class="games-venue">{venue}</span>
                    </div>
                ''')

            sections.append(f'''
                <div class="games-subsection">
                    <button class="collapsible active" onclick="toggleSection(this)">
                        <span class="subsection-title">Today's Games</span>
                        <span class="arrow">▼</span>
                    </button>
                    <div class="collapsible-content open">
                        <div class="games-list">
                            {''.join(game_items)}
                        </div>
                    </div>
                </div>
            ''')

        # Recent results section
        if recent_results:
            result_items = []
            for g in recent_results:
                dt = g['datetime']
                date_str = dt.strftime('%a %b %d').replace(' 0', ' ')
                opponent = g.get('opponent', 'TBD')
                game_type = g.get('game_type', '').lower()
                won_lost = g.get('won_lost', '')
                team_score = g.get('team_score', '')
                opp_score = g.get('opponent_score', '')
                is_tournament = g.get('is_tournament', False)
                emoji = '🏆' if is_tournament else '🏀'
                short_name = g.get('short_name', '')
                gender = g.get('gender', '')

                result_emoji = '✅' if won_lost == 'W' else '❌' if won_lost == 'L' else '➖'
                score = f'{team_score}-{opp_score}' if team_score and opp_score else ''

                if 'away' in game_type or game_type == 'a':
                    matchup = f'@ {opponent}'
                else:
                    matchup = f'vs {opponent}'

                result_items.append(f'''
                    <div class="games-row result" data-gender="{gender}">
                        <span class="games-result">{result_emoji}</span>
                        <span class="games-date">{date_str}</span>
                        <span class="games-team">{short_name}</span>
                        <span class="games-matchup">{emoji} {matchup}</span>
                        <span class="games-score">{score}</span>
                    </div>
                ''')

            sections.append(f'''
                <div class="games-subsection">
                    <button class="collapsible active" onclick="toggleSection(this)">
                        <span class="subsection-title">Recent Results</span>
                        <span class="arrow">▼</span>
                    </button>
                    <div class="collapsible-content open">
                        <div class="games-list">
                            {''.join(result_items)}
                        </div>
                    </div>
                </div>
            ''')

        return f'''
            <section class="games-section" aria-labelledby="games-heading">
                <h2 id="games-heading">Games</h2>
                {''.join(sections)}
            </section>
        '''

    def make_schedule_html(grade: str, gender_code: str, color: str) -> str:
        """Generate schedule HTML with upcoming games and recent results."""
        now_dt = datetime.now(EASTERN)
        games = get_team_games(grade, gender_code, color)
        if not games:
            return ''

        # Show whichever is larger: games in next 2 weeks, or next 4 games
        two_weeks = now_dt + timedelta(days=14)
        all_upcoming = [g for g in games if g['datetime'] > now_dt]
        in_two_weeks = [g for g in all_upcoming if g['datetime'] <= two_weeks]
        upcoming = in_two_weeks if len(in_two_weeks) > 4 else all_upcoming[:4]
        completed = [g for g in games if g['datetime'] <= now_dt and g.get('won_lost')]
        recent = completed[-5:] if completed else []  # Last 5 completed games
        recent.reverse()  # Most recent first

        sections = []

        if upcoming:
            upcoming_items = []
            for g in upcoming:
                dt = g['datetime']
                date_str = dt.strftime('%a %b %d').replace(' 0', ' ')
                time_str = dt.strftime('%I:%M %p').lstrip('0').lower()
                opponent = g.get('opponent', 'TBD')
                game_type = g.get('game_type', '').lower()
                is_tournament = g.get('is_tournament', False)
                emoji = '🏆' if is_tournament else ''

                # Location - extract just venue name (before address)
                location = g.get('location', '')
                venue = location.split(',')[0] if location else ''

                if 'away' in game_type or game_type == 'a':
                    matchup = f'@ {opponent}'
                else:
                    matchup = f'vs {opponent}'

                upcoming_items.append(f'''
                    <div class="schedule-game">
                        <span class="game-date">{date_str}</span>
                        <span class="game-time">{time_str}</span>
                        <span class="game-matchup">{emoji} {matchup}</span>
                        <span class="game-venue">{venue}</span>
                    </div>
                ''')
            sections.append(f'''
                <div class="schedule-section">
                    <div class="schedule-title">Upcoming</div>
                    {''.join(upcoming_items)}
                </div>
            ''')

        if recent:
            recent_items = []
            for g in recent:
                dt = g['datetime']
                date_str = dt.strftime('%b %d').replace(' 0', ' ')
                opponent = g.get('opponent', 'TBD')
                game_type = g.get('game_type', '').lower()
                won_lost = g.get('won_lost', '')
                team_score = g.get('team_score', '')
                opp_score = g.get('opponent_score', '')
                is_tournament = g.get('is_tournament', False)
                emoji = '🏆' if is_tournament else ''

                result_emoji = '✅' if won_lost == 'W' else '❌' if won_lost == 'L' else '➖'
                score = f'{team_score}-{opp_score}' if team_score and opp_score else ''

                if 'away' in game_type or game_type == 'a':
                    matchup = f'@ {opponent}'
                else:
                    matchup = f'vs {opponent}'

                recent_items.append(f'''
                    <div class="schedule-game result">
                        <span class="game-result">{result_emoji}</span>
                        <span class="game-date">{date_str}</span>
                        <span class="game-matchup">{emoji} {matchup}</span>
                        <span class="game-score">{score}</span>
                    </div>
                ''')
            sections.append(f'''
                <div class="schedule-section">
                    <div class="schedule-title">Recent</div>
                    {''.join(recent_items)}
                </div>
            ''')

        if not sections:
            return ''

        return f'''
            <div class="team-schedule">
                {''.join(sections)}
            </div>
        '''

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
        practices_count = cal.get('practices', 0)
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

        # Build games/practices info string
        info_parts = []
        if games_count:
            info_parts.append(f"{games_count} games")
        if practices_count:
            info_parts.append(f"{practices_count} practices")
        games_info = ", ".join(info_parts) if info_parts else "No events"

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
        total_wins = 0
        total_losses = 0
        total_ties = 0

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

                cards_html = ''.join(make_card(c, compact=True) for c in cals_sorted)

                # Get gender code for data attribute (M or F)
                gender_code = 'M' if gender == 'Boys' else 'F'

                # Generate schedule HTML for this team
                schedule_html = make_schedule_html(grade, gender_code, color)

                # Check for combined calendar first
                combined_cal = next((c for c in cals_sorted if c.get('type') == 'combined'), None)

                # Get game count - use combined if available, else sum individuals
                if combined_cal:
                    team_games = combined_cal.get('games', 0)
                else:
                    team_games = sum(c.get('games', 0) for c in cals_sorted)

                total_teams += 1
                total_games += team_games

                # Get W-L record - use combined if available, else sum individuals
                team_wins = 0
                team_losses = 0
                team_ties = 0
                team_division = ''

                if combined_cal:
                    team_wins = combined_cal.get('wins', 0)
                    team_losses = combined_cal.get('losses', 0)
                    team_ties = combined_cal.get('ties', 0)
                else:
                    # No combined - sum individual league records
                    for cal in cals_sorted:
                        team_wins += cal.get('wins', 0)
                        team_losses += cal.get('losses', 0)
                        team_ties += cal.get('ties', 0)

                # Add to grade-level totals
                total_wins += team_wins
                total_losses += team_losses
                total_ties += team_ties

                # Get division - use combined's division if combined exists, else use single calendar's
                if combined_cal:
                    # Combined exists - use its division (empty if teams in different divisions)
                    team_division = combined_cal.get('division_tier', '')
                elif len(cals_sorted) == 1:
                    # Single calendar - use its division
                    team_division = cals_sorted[0].get('division_tier', '')

                # Build left side info (division, coach)
                left_info = ''
                if team_division:
                    left_info += f'<span class="team-division">Div {team_division}</span>'

                # Look up coaches for this team (try multiple key formats)
                coach_key = f"{grade}-{gender_code}-{color}"
                coach_info = coaches.get(coach_key) or coaches.get(f"{grade}{gender_code}-{color}") or coaches.get(color)
                if coach_info:
                    def format_coach(c):
                        """Format a single coach entry."""
                        if isinstance(c, list):
                            name = c[0]
                            email = c[1] if len(c) > 1 else None
                        else:
                            name = c
                            email = None
                        if email:
                            return f'<a href="mailto:{email}">{name}</a>'
                        return name

                    # Check if it's multiple coaches (list of lists) or single coach
                    if isinstance(coach_info, list) and len(coach_info) > 0 and isinstance(coach_info[0], list):
                        # Multiple coaches: [["Name1", "email1"], ["Name2", "email2"]]
                        coach_names = ', '.join(format_coach(c) for c in coach_info)
                        left_info += f'<span class="coach-info">Coaches: {coach_names}</span>'
                    else:
                        # Single coach: "Name" or ["Name", "email"]
                        left_info += f'<span class="coach-info">Coach: {format_coach(coach_info)}</span>'

                # Build right side info (record, games)
                right_info = ''
                if team_wins or team_losses or team_ties:
                    if team_ties:
                        record = f'{team_wins}-{team_losses}-{team_ties}'
                    else:
                        record = f'{team_wins}-{team_losses}'
                    right_info += f'<span class="team-record">{record}</span>'
                right_info += f'<span class="team-games">{team_games} games</span>'

                color_sections.append(f'''
                <div class="team-group" data-gender="{gender_code}" data-games="{team_games}" data-wins="{team_wins}" data-losses="{team_losses}" data-ties="{team_ties}" onclick="toggleTeam(this)">
                    <div class="team-header">
                        <div class="team-info-left">
                            <span class="team-arrow">▶</span>
                            <span class="team-name">{team_label}</span>
                            {left_info}
                        </div>
                        <div class="team-info-right">
                            {right_info}
                        </div>
                    </div>
                    <div class="team-content">
                        <div class="team-calendars">
                            {cards_html}
                        </div>
                        {schedule_html}
                    </div>
                </div>
                ''')

        if color_sections:
            # Format aggregate W-L for grade header
            if total_wins or total_losses or total_ties:
                if total_ties:
                    grade_record = f'{total_wins}-{total_losses}-{total_ties}'
                else:
                    grade_record = f'{total_wins}-{total_losses}'
                record_html = f' &bull; <span class="grade-record">{grade_record}</span>'
            else:
                record_html = ''

            grade_sections.append(f'''
            <div class="grade-section" data-total-wins="{total_wins}" data-total-losses="{total_losses}" data-total-ties="{total_ties}">
                <button class="collapsible" onclick="toggleSection(this)">
                    <span class="grade-title">🏀 {grade_label}</span>
                    <span class="grade-info">{total_teams} teams{record_html} &bull; {total_games} games</span>
                    <span class="arrow">▼</span>
                </button>
                <div class="collapsible-content">
                    {''.join(color_sections)}
                </div>
            </div>
            ''')

    grade_html = '\n'.join(grade_sections)

    # Generate games section (today's games + recent results)
    games_section_html = make_games_section_html()

    # Note about what games are included
    if include_nl_games:
        games_included_note = 'These calendars include <strong>league games and tournaments/playoffs</strong> (🏆 indicates tournament games).'
    else:
        games_included_note = 'These calendars include <strong>league games only</strong> — tournaments and playoffs are not included.'

    # Generate notifications section if ntfy_topic is configured
    if ntfy_topic:
        # Build list of unique team topics from all calendars, deduped by grade-gender-color
        # Use same extraction logic as the working team groups section
        team_topics = []
        seen_keys = set()
        for cal in calendars:
            # Use extract functions - same as the working team schedules code
            grade = extract_grade(cal)
            gender_label = extract_gender(cal)  # Returns 'Boys' or 'Girls'
            color = extract_color(cal)

            # Skip if we can't determine the team identity
            if not grade or grade == 'Other' or not color or color == 'Team':
                continue

            # Convert gender label to code for data-gender attribute
            gender = 'M' if gender_label == 'Boys' else 'F'

            team_key = f"{grade}-{gender}-{color}".lower()
            if team_key not in seen_keys:
                seen_keys.add(team_key)
                topic = f"{ntfy_topic}-{team_key}".lower().replace(' ', '-')
                label = f"{ordinal(grade)} {gender_label} {color}"
                team_topics.append((topic, label, gender))

        # Sort by grade then gender then color
        team_topics.sort(key=lambda x: (x[1][0], x[2], x[1]))

        topics_html = '\n                '.join([
            f'<div class="topic-item" data-gender="{gender}"><code>{topic}</code> <span class="topic-label">{label}</span></div>'
            for topic, label, gender in team_topics
        ]) if team_topics else '<p>No team topics available yet.</p>'

        notifications_section = f'''
    <section class="notifications-section" aria-labelledby="notifications-heading">
        <h2 id="notifications-heading">Get Schedule Change Alerts</h2>
        <p>Want to be notified when games are added, cancelled, or rescheduled? Get push notifications on your phone!</p>

        <div class="notification-steps">
            <div class="step">
                <span class="step-number">1</span>
                <div class="step-content">
                    <strong>Install the ntfy app</strong>
                    <p>Free app for <a href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noopener">iPhone/iPad</a> or <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noopener">Android</a></p>
                </div>
            </div>
            <div class="step">
                <span class="step-number">2</span>
                <div class="step-content">
                    <strong>Subscribe to your team's topic</strong>
                    <p>In the app, tap + and enter your team's topic (see below)</p>
                </div>
            </div>
            <div class="step">
                <span class="step-number">3</span>
                <div class="step-content">
                    <strong>Get notified!</strong>
                    <p>You'll receive alerts when games are added, cancelled, or times/locations change</p>
                </div>
            </div>
        </div>

        <div class="notification-topics">
            <h3>Team Topics</h3>
            <p class="topic-instructions">Copy your team's topic and paste it in the ntfy app:</p>
            <div class="filter-bar topics-filter" role="group" aria-label="Filter topics">
                <span class="filter-label">Show:</span>
                <div class="filter-buttons">
                    <button class="filter-btn active" data-filter="all" data-target="topics" aria-pressed="true">Both</button>
                    <button class="filter-btn" data-filter="M" data-target="topics" aria-pressed="false">Boys</button>
                    <button class="filter-btn" data-filter="F" data-target="topics" aria-pressed="false">Girls</button>
                </div>
            </div>
            <div class="topics-grid" id="topics-grid">
                {topics_html}
            </div>
            <p class="topic-format"><strong>Topic format:</strong> <code>{ntfy_topic}-[grade]-[m/f]-[color]</code><br>
            <span class="topic-example">Example: {ntfy_topic}-5-m-red for 5th grade Boys Red</span></p>
        </div>
    </section>'''
    else:
        notifications_section = ''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Subscribe to {town_name} basketball game schedules. Auto-syncing calendars for MetroWest and SSYBL leagues.">
    <meta name="theme-color" content="#1a1a2e" media="(prefers-color-scheme: light)">
    <meta name="theme-color" content="#0f0f1a" media="(prefers-color-scheme: dark)">
    <meta http-equiv="refresh" content="300">
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

        /* Team groups - collapsible */
        .team-group {{
            margin-bottom: var(--spacing-sm);
            background: var(--color-bg-elevated);
            border-radius: var(--radius-sm);
            overflow: hidden;
        }}

        .team-group:last-child {{
            margin-bottom: 0;
        }}

        .team-header {{
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--color-text);
            padding: var(--spacing-md);
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: var(--spacing-sm);
            cursor: pointer;
            transition: background var(--transition-fast);
            user-select: none;
        }}

        .team-header:hover {{
            background: var(--color-bg-subtle);
        }}

        .team-info-left {{
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            min-width: 0;
        }}

        .team-info-right {{
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            flex-shrink: 0;
        }}

        .team-header .team-arrow {{
            font-size: 0.7rem;
            color: var(--color-text-muted);
            transition: transform var(--transition-normal);
            flex-shrink: 0;
        }}

        .team-group.open .team-header .team-arrow {{
            transform: rotate(90deg);
        }}

        .team-header .team-games {{
            font-size: 0.8rem;
            font-weight: 400;
            color: var(--color-text-muted);
            background: var(--color-bg-subtle);
            padding: 2px 8px;
            border-radius: 12px;
        }}

        .team-header .team-record {{
            font-size: 0.8rem;
            font-weight: 600;
            color: white;
            background: #059669;
            padding: 2px 8px;
            border-radius: 12px;
            white-space: nowrap;
        }}

        .team-header .team-division {{
            font-size: 0.75rem;
            font-weight: 500;
            color: var(--color-text-secondary);
            background: var(--color-bg-subtle);
            padding: 2px 6px;
            border-radius: 10px;
            white-space: nowrap;
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

        .team-content {{
            max-height: 0;
            overflow: hidden;
            transition: max-height var(--transition-slow);
        }}

        .team-group.open .team-content {{
            max-height: 2000px;
        }}

        .team-calendars {{
            display: flex;
            flex-direction: column;
            gap: var(--spacing-sm);
            padding: 0 var(--spacing-md) var(--spacing-md);
        }}

        /* Team schedule display */
        .team-schedule {{
            padding: 0 var(--spacing-md) var(--spacing-md);
            display: flex;
            gap: var(--spacing-lg);
            flex-wrap: wrap;
        }}

        .schedule-section {{
            flex: 1;
            min-width: 200px;
        }}

        .schedule-title {{
            font-weight: 600;
            font-size: 0.8rem;
            color: var(--color-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: var(--spacing-sm);
            padding-bottom: var(--spacing-xs);
            border-bottom: 1px solid var(--color-border-light);
        }}

        .schedule-game {{
            display: grid;
            grid-template-columns: auto auto 1fr auto;
            gap: var(--spacing-sm);
            align-items: center;
            padding: var(--spacing-xs) 0;
            font-size: 0.85rem;
            color: var(--color-text-secondary);
        }}

        .schedule-game.result {{
            grid-template-columns: auto auto 1fr auto;
        }}

        .schedule-game .game-date {{
            font-weight: 500;
            color: var(--color-text);
            min-width: 60px;
        }}

        .schedule-game .game-time {{
            color: var(--color-text-muted);
            min-width: 65px;
        }}

        .schedule-game .game-result {{
            font-size: 1rem;
        }}

        .schedule-game .game-matchup {{
            color: var(--color-text);
        }}

        .schedule-game .game-venue {{
            font-size: 0.8rem;
            color: var(--color-text-muted);
            text-align: right;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            max-width: 150px;
        }}

        .schedule-game .game-score {{
            font-weight: 600;
            color: var(--color-text);
            min-width: 45px;
            text-align: right;
        }}

        /* Games section (all teams summary) */
        .games-section {{
            background: var(--color-bg-elevated);
            border-radius: var(--radius-lg);
            padding: var(--spacing-lg);
            margin-bottom: var(--spacing-xl);
            box-shadow: var(--shadow-sm);
        }}

        .games-section h2 {{
            margin: 0 0 var(--spacing-md) 0;
            font-size: 1.25rem;
        }}

        .games-subsection {{
            margin-bottom: var(--spacing-lg);
        }}

        .games-subsection:last-child {{
            margin-bottom: 0;
        }}

        .games-subsection h3 {{
            font-weight: 600;
            font-size: 0.85rem;
            color: var(--color-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 0 0 var(--spacing-sm) 0;
            padding-bottom: var(--spacing-xs);
            border-bottom: 1px solid var(--color-border-light);
        }}

        .games-subsection .collapsible {{
            padding: var(--spacing-sm) var(--spacing-md);
            background: var(--color-bg-subtle);
        }}

        .games-subsection .collapsible.active {{
            border-radius: var(--radius-sm) var(--radius-sm) 0 0;
        }}

        .games-subsection .collapsible:not(.active) {{
            border-radius: var(--radius-sm);
        }}

        .games-subsection .collapsible-content {{
            background: var(--color-bg-subtle);
            border-radius: 0 0 var(--radius-sm) var(--radius-sm);
            padding: 0 var(--spacing-sm);
        }}

        .games-subsection .collapsible-content.open {{
            padding: 0 var(--spacing-sm) var(--spacing-sm) var(--spacing-sm);
        }}

        .subsection-title {{
            font-weight: 600;
            font-size: 0.85rem;
            color: var(--color-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .games-list {{
            display: flex;
            flex-direction: column;
            gap: var(--spacing-xs);
        }}

        .games-row {{
            display: grid;
            grid-template-columns: 70px 70px 1fr auto;
            gap: var(--spacing-sm);
            align-items: center;
            padding: var(--spacing-sm) var(--spacing-sm);
            font-size: 0.9rem;
            background: var(--color-bg-subtle);
            border-radius: var(--radius-sm);
        }}

        .games-row.result {{
            grid-template-columns: 28px 85px 70px 1fr auto;
        }}

        .games-time {{
            font-weight: 500;
            color: var(--color-text);
        }}

        .games-date {{
            font-weight: 500;
            color: var(--color-text);
        }}

        .games-team {{
            font-weight: 600;
            color: var(--color-primary);
        }}

        .games-matchup {{
            color: var(--color-text);
        }}

        .games-venue {{
            font-size: 0.8rem;
            color: var(--color-text-muted);
            text-align: right;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            max-width: 180px;
        }}

        .games-result {{
            font-size: 1rem;
        }}

        .games-score {{
            font-weight: 600;
            color: var(--color-text);
            min-width: 50px;
            text-align: right;
        }}

        @media (max-width: 640px) {{
            .games-row {{
                grid-template-columns: 60px 55px 1fr;
            }}

            .games-row.result {{
                grid-template-columns: 24px 70px 55px 1fr;
            }}

            .games-venue {{
                display: none;
            }}

            .games-score {{
                display: none;
            }}
        }}

        @media (max-width: 640px) {{
            .team-schedule {{
                flex-direction: column;
                gap: var(--spacing-md);
            }}

            .schedule-game {{
                grid-template-columns: auto 1fr auto;
            }}

            .schedule-game .game-time {{
                display: none;
            }}

            .schedule-game .game-venue {{
                max-width: 100px;
            }}
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

        /* Hidden elements (for filtering) */
        .team-group.hidden,
        .games-row.hidden,
        .topic-item.hidden {{
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

        /* Notifications section */
        .notifications-section {{
            background: linear-gradient(135deg, var(--color-bg-elevated) 0%, rgba(99, 102, 241, 0.1) 100%);
            border-radius: var(--radius-md);
            padding: var(--spacing-lg);
            margin-top: var(--spacing-md);
            box-shadow: var(--shadow-sm);
            border: 1px solid rgba(99, 102, 241, 0.2);
        }}

        .notifications-section h2 {{
            margin-top: 0;
            border: none;
            display: block;
        }}

        .notifications-section p {{
            color: var(--color-text-secondary);
            margin-bottom: var(--spacing-md);
        }}

        .notification-steps {{
            display: flex;
            flex-direction: column;
            gap: var(--spacing-md);
            margin: var(--spacing-lg) 0;
        }}

        .step {{
            display: flex;
            align-items: flex-start;
            gap: var(--spacing-md);
        }}

        .step-number {{
            background: var(--color-primary);
            color: white;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.9rem;
            flex-shrink: 0;
        }}

        .step-content strong {{
            display: block;
            color: var(--color-text);
            margin-bottom: 4px;
        }}

        .step-content p {{
            margin: 0;
            font-size: 0.9rem;
        }}

        .step-content a {{
            color: var(--color-primary);
        }}

        .notification-topics {{
            background: var(--color-bg-subtle);
            border-radius: var(--radius-sm);
            padding: var(--spacing-md);
            margin-top: var(--spacing-md);
        }}

        .notification-topics h3 {{
            margin: 0 0 var(--spacing-sm) 0;
            font-size: 1rem;
            color: var(--color-text);
        }}

        .topic-instructions {{
            margin-bottom: var(--spacing-md) !important;
            font-size: 0.9rem;
        }}

        .topics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: var(--spacing-sm);
            margin-bottom: var(--spacing-md);
        }}

        .topic-item {{
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            padding: var(--spacing-sm);
            background: var(--color-bg-elevated);
            border-radius: var(--radius-sm);
        }}

        .topic-item code {{
            background: var(--color-bg-subtle);
            padding: 4px 8px;
            border-radius: var(--radius-xs);
            font-size: 0.85rem;
            color: var(--color-primary);
            font-family: monospace;
        }}

        .topic-label {{
            color: var(--color-text-secondary);
            font-size: 0.85rem;
        }}

        .topic-format {{
            margin: 0 !important;
            font-size: 0.85rem;
            color: var(--color-text-secondary);
        }}

        .topic-format code {{
            background: var(--color-bg-elevated);
            padding: 2px 6px;
            border-radius: var(--radius-xs);
            font-size: 0.85rem;
        }}

        .topic-example {{
            font-style: italic;
            opacity: 0.8;
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
        <p style="color: var(--color-text-secondary); font-size: 0.9rem; margin-bottom: var(--spacing-md);">Click a grade, then a team to see schedule and calendar links. ⭐ Combined calendars include all leagues.</p>

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

    {notifications_section}

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
                Why doesn't my team show a coach name?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Coach names are manually configured and we've only added the ones we know about. If you'd like your coach added, please <a href="https://github.com/aknowles/ssbball/issues">submit a GitHub issue</a> with your team (grade, gender, color) and coach name.</p>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                How do I update practice times?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <p>Practice schedules are crowdsourced and we rely on coaches/parents to keep them up to date. To request a change:</p>
                <ul>
                    <li><a href="https://github.com/aknowles/ssbball/issues/new?template=cancel-practice.yml">Cancel a practice</a></li>
                    <li><a href="https://github.com/aknowles/ssbball/issues/new?template=modify-practice.yml">Modify a practice</a> (change time, location, or duration)</li>
                    <li><a href="https://github.com/aknowles/ssbball/issues/new?template=add-practice.yml">Add a practice</a> (schedule an extra one-time practice)</li>
                </ul>
                <p>A maintainer will review and approve changes before they take effect.</p>
            </div>
        </div>

        <div class="faq-item">
            <div class="faq-question" onclick="toggleFaq(this)" tabindex="0" role="button" aria-expanded="false">
                Why is a practice missing from my calendar?
                <span class="arrow" aria-hidden="true">▼</span>
            </div>
            <div class="faq-answer">
                <ul>
                    <li>Practices within 1 hour of a scheduled game are automatically skipped</li>
                    <li>Practices during school vacation weeks are skipped</li>
                    <li>Your team's practice schedule may not be configured yet — <a href="https://github.com/aknowles/ssbball/issues/new?template=add-practice.yml">submit a request</a> to add it</li>
                </ul>
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

    {games_section_html}

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
            <a href="https://github.com/aknowles/ssbball/issues/new/choose" target="_blank" rel="noopener">
                Request Practice Change
            </a>
        </div>
        <div class="footer-meta">
            Last updated: {now}
        </div>
        <p class="footer-disclaimer">
            <strong>Practice schedules are community-maintained.</strong> Games update automatically from league websites.
            Practices rely on crowdsourcing — <a href="https://github.com/aknowles/ssbball/issues/new/choose">submit a change request</a> if times change.
        </p>
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

        function toggleTeam(el) {{
            // Prevent toggle when clicking on links or buttons inside
            if (event.target.closest('a, button')) return;
            el.classList.toggle('open');
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

            // Filter game rows in Games section
            const gameRows = document.querySelectorAll('.games-row');
            gameRows.forEach(row => {{
                const gender = row.dataset.gender;
                if (filter === 'all' || gender === filter) {{
                    row.classList.remove('hidden');
                }} else {{
                    row.classList.add('hidden');
                }}
            }});

            // Filter notification topics
            const topicItems = document.querySelectorAll('.topic-item');
            topicItems.forEach(item => {{
                const gender = item.dataset.gender;
                if (filter === 'all' || gender === filter) {{
                    item.classList.remove('hidden');
                }} else {{
                    item.classList.add('hidden');
                }}
            }});

            // Update grade section counts based on visible teams
            gradeSections.forEach(section => {{
                const groups = section.querySelectorAll('.team-group');
                let visibleTeams = 0;
                let visibleGames = 0;
                let visibleWins = 0;
                let visibleLosses = 0;
                let visibleTies = 0;

                groups.forEach(group => {{
                    if (!group.classList.contains('hidden')) {{
                        visibleTeams++;
                        visibleGames += parseInt(group.dataset.games || 0, 10);
                        visibleWins += parseInt(group.dataset.wins || 0, 10);
                        visibleLosses += parseInt(group.dataset.losses || 0, 10);
                        visibleTies += parseInt(group.dataset.ties || 0, 10);
                    }}
                }});

                // Format W-L record
                let recordHtml = '';
                if (visibleWins || visibleLosses || visibleTies) {{
                    const record = visibleTies ? `${{visibleWins}}-${{visibleLosses}}-${{visibleTies}}` : `${{visibleWins}}-${{visibleLosses}}`;
                    recordHtml = ` • <span class="grade-record">${{record}}</span>`;
                }}

                const infoEl = section.querySelector('.grade-info');
                if (infoEl) {{
                    infoEl.innerHTML = `${{visibleTeams}} team${{visibleTeams !== 1 ? 's' : ''}}${{recordHtml}} • ${{visibleGames}} games`;
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

        team_id = f"{town_name.lower()}-{ordinal(grade)}-{gender_name.lower()}-{color.lower()}-{league}".replace(' ', '-')
        team_name = f"{town_name} {ordinal(grade)} {gender_name} {color} ({league_name})"
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
    parser.add_argument('--ntfy-topic', '-n', default='', help='ntfy.sh topic prefix for notifications (e.g., "ssbball")')
    parser.add_argument('--dry-run', action='store_true', help='Detect changes and log notifications without sending them')
    parser.add_argument('--test-notification', metavar='TEAM', help='Send a test notification to a specific team (e.g., "5-m-red")')
    parser.add_argument('--notification-message', metavar='MSG', help='Custom message for test notification (use with --test-notification)')
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    # Initialize leagues (merge defaults with any custom_leagues)
    global LEAGUES
    LEAGUES = get_leagues(config)

    base_url = args.base_url or config.get('base_url', 'https://example.github.io/ssbball')
    town_name = config.get('town_name', 'Milton')

    # Handle test notification / ad hoc message mode early (no scraping needed)
    ntfy_topic = args.ntfy_topic or config.get('ntfy_topic', '')
    test_team = args.test_notification
    if test_team:
        if not ntfy_topic:
            logger.error("Cannot send notification: no --ntfy-topic specified")
            return
        custom_message = args.notification_message
        if custom_message:
            logger.info(f"Sending ad hoc message to team: {test_team}")
        else:
            logger.info(f"Sending test notification to team: {test_team}")
        if send_test_notification(ntfy_topic, test_team, town_name, custom_message=custom_message):
            logger.info("Notification sent successfully!")
        else:
            logger.error("Failed to send notification")
        return

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
                    'id': f"{town_name.lower()}-{ordinal(grade)}-{gender_name.lower()}-{color.lower()}",
                    'name': f"{town_name} {ordinal(grade)} {gender_name} {color}",
                    'description': 'All leagues combined',
                    'filter': {'grade': str(grade), 'gender': gender, 'color': color}
                })

    # Generate practice events for teams with configured practices
    all_practices = []
    practices_config = config.get('practices', {})
    if practices_config:
        season_start, season_end = parse_season_dates(config)
        if season_start and season_end:
            logger.info(f"Season: {season_start.date()} to {season_end.date()}")

            # Build a lookup of team configs by key (grade-gender-color)
            team_lookup = {}
            for tc in team_configs:
                key = f"{tc.get('grade')}-{tc.get('gender')}-{tc.get('color')}"
                if key not in team_lookup:
                    team_lookup[key] = tc

            for team_key in practices_config:
                if team_key in team_lookup:
                    tc = team_lookup[team_key]
                    team_name = tc.get('team_name', team_key)
                    short_name = tc.get('short_name', team_key)
                else:
                    # Team not discovered but has practices configured
                    team_name = f"{town_name} {team_key}"
                    short_name = team_key

                # Get games for this team to check for conflicts
                parts = team_key.split('-')
                if len(parts) >= 3:
                    grade, gender, color = parts[0], parts[1], parts[2]
                    team_games_for_practice = [
                        g for g in all_games
                        if str(g.get('grade')) == grade and
                           g.get('gender') == gender and
                           g.get('color', '').lower() == color.lower()
                    ]
                else:
                    team_games_for_practice = []

                practices = generate_practice_events(config, team_key, team_name, short_name, team_games_for_practice)
                all_practices.extend(practices)

            logger.info(f"Generated {len(all_practices)} total practice events")

    # ==========================================================================
    # Schedule Change Detection and Notifications
    # ==========================================================================
    # Detects and notifies about the following changes:
    #   - New games/practices added
    #   - Games/practices cancelled (deleted)
    #   - Time/date changes to existing games/practices
    #   - Location changes to existing games/practices
    #
    # Does NOT notify about:
    #   - Score updates
    #   - Win/loss record changes
    #   - Home/away designation changes
    #   - Any other metadata changes
    # ==========================================================================

    state_path = output_dir / 'schedule_state.json'
    dry_run = args.dry_run

    if dry_run:
        logger.info("DRY-RUN MODE: Will detect changes but not send actual notifications")

    if ntfy_topic:
        logger.info(f"Notifications enabled with topic prefix: {ntfy_topic}")

        # Load previous state
        previous_state = load_previous_state(state_path)

        if previous_state:
            # Detect changes
            changes = detect_changes(previous_state, all_games, all_practices)

            total_changes = len(changes['new']) + len(changes['deleted']) + len(changes['modified'])
            if total_changes > 0:
                logger.info(f"Detected {total_changes} schedule changes:")
                logger.info(f"  - {len(changes['new'])} new events")
                logger.info(f"  - {len(changes['deleted'])} cancelled events")
                logger.info(f"  - {len(changes['modified'])} modified events")

                # Send notifications (or log them in dry-run mode)
                sent = send_change_notifications(changes, ntfy_topic, town_name, dry_run=dry_run)
                if dry_run:
                    logger.info(f"[DRY-RUN] Would have sent {sent} notifications")
                else:
                    logger.info(f"Sent {sent} notifications")
            else:
                logger.info("No schedule changes detected")
        else:
            logger.info("First run - no previous state to compare against")

        # Save current state for next run (skip in dry-run to allow re-testing)
        if not dry_run:
            save_current_state(state_path, all_games, all_practices)
        else:
            logger.info("[DRY-RUN] Skipping state save to allow re-testing")
    else:
        logger.info("Notifications disabled (no --ntfy-topic specified)")

    calendar_info = []  # For index.html

    # Generate individual team calendars
    for team_config in team_configs:
        team_id = team_config.get('id', 'team')
        team_name = team_config.get('team_name', 'Team')

        # Filter games for this team (must match grade, league, gender, AND color)
        team_games = [g for g in all_games
                     if g.get('team_name') == team_name or
                        (g.get('grade') == team_config.get('grade') and
                         g.get('league') == team_config.get('league') and
                         g.get('gender') == team_config.get('gender') and
                         g.get('color') == team_config.get('color'))]

        # Add practices for this team
        team_key = f"{team_config.get('grade')}-{team_config.get('gender')}-{team_config.get('color')}"
        team_practices = [p for p in all_practices
                         if f"{p.get('grade')}-{p.get('gender')}-{p.get('color')}" == team_key]
        all_events = team_games + team_practices

        ical_data = generate_ical(all_events, team_name, team_id)
        ics_path = output_dir / f"{team_id}.ics"
        ics_path.write_bytes(ical_data)
        logger.info(f"Wrote {ics_path} with {len(team_games)} games and {len(team_practices)} practices")

        calendar_info.append({
            'type': 'team',
            'id': team_id,
            'name': team_config.get('short_name', team_name),
            'league': team_config.get('league', ''),
            'description': team_config.get('league', ''),
            'games': len(team_games),
            'practices': len(team_practices),
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

        # Filter practices for this combined calendar
        if combo_filter:
            filtered_practices = [
                p for p in all_practices
                if all(p.get(k) == v for k, v in combo_filter.items())
            ]
        else:
            filtered_practices = all_practices

        # Combine games and practices for the calendar
        all_events = filtered_games + filtered_practices

        # Generate calendar
        ical_data = generate_ical(all_events, combo_name, combo_id)
        ics_path = output_dir / f"{combo_id}.ics"
        ics_path.write_bytes(ical_data)
        logger.info(f"Wrote {ics_path} with {len(filtered_games)} games and {len(filtered_practices)} practices")

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
            'practices': len(filtered_practices),
            'gender': combo_gender,
            'division_tier': combo_division,
            'wins': combo_wins,
            'losses': combo_losses,
            'ties': combo_ties,
            'rank': 0  # No rank for combined calendars
        })

    # Generate index.html
    coaches = config.get('coaches', {})
    index_html = generate_index_html(calendar_info, base_url, town_name, include_nl_games, coaches=coaches, all_games=all_games, ntfy_topic=ntfy_topic)
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
