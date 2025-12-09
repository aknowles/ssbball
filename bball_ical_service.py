#!/usr/bin/env python3
"""
Basketball iCal Subscription Service

A self-contained service that:
1. Scrapes your team's schedule from metrowestbball.com or ssybl.org
2. Generates an iCal file
3. Serves it via HTTP for calendar subscription
4. Auto-refreshes on a schedule (default: every 6 hours)

Run this on:
- Your computer (while it's on)
- A Raspberry Pi (24/7, ~$35)
- Free cloud hosting (PythonAnywhere, Render, Railway)

Requirements:
    pip install -r requirements.txt

Usage:
    # First, run in interactive mode to find your team ID
    python bball_ical_service.py --setup

    # Then run the service
    python bball_ical_service.py --config config.json

    # Subscribe in your calendar app to:
    # http://YOUR_IP:5000/calendar.ics
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Check dependencies
def check_deps():
    missing = []
    try:
        from selenium import webdriver
    except ImportError:
        missing.append('selenium')
    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        missing.append('webdriver-manager')
    try:
        from icalendar import Calendar, Event
    except ImportError:
        missing.append('icalendar')
    try:
        from flask import Flask, Response
    except ImportError:
        missing.append('flask')
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        missing.append('apscheduler')

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)

check_deps()

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from icalendar import Calendar, Event
from flask import Flask, Response
from apscheduler.schedulers.background import BackgroundScheduler

# Global calendar storage
current_calendar = None
last_update = None
games_cache = []

# Eastern timezone for MA basketball leagues
EASTERN = ZoneInfo("America/New_York")


def create_driver(headless=True):
    """Create a Selenium Chrome driver."""
    options = Options()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def parse_datetime(date_str: str, time_str: str = "") -> Optional[datetime]:
    """Parse various date/time formats and return timezone-aware datetime."""
    # Try to find date in string
    date_match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', date_str)
    if date_match:
        month, day, year = date_match.groups()
        year = int(year)
        if year < 100:
            year += 2000
        try:
            parsed_date = datetime(year, int(month), int(day), tzinfo=EASTERN)
        except ValueError:
            return None
    else:
        return None

    # Parse time from time_str or date_str
    combined = f"{date_str} {time_str}"
    time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?', combined)
    if time_match:
        hour, minute, ampm = time_match.groups()
        hour = int(hour)
        minute = int(minute)
        if ampm and ampm.upper() == 'PM' and hour != 12:
            hour += 12
        elif ampm and ampm.upper() == 'AM' and hour == 12:
            hour = 0
        return parsed_date.replace(hour=hour, minute=minute)

    # Default to noon if no time found
    return parsed_date.replace(hour=12, minute=0)


def scrape_metrowest(config: dict) -> list[dict]:
    """Scrape schedule from metrowestbball.com."""
    games = []
    driver = None

    try:
        logger.info("Starting MetroWest scrape...")
        driver = create_driver(headless=True)
        driver.get("https://metrowestbball.com/launch.php")
        time.sleep(3)

        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Find and interact with dropdowns
        selects = driver.find_elements(By.TAG_NAME, 'select')
        logger.info(f"Found {len(selects)} dropdown menus")

        # Log available options for debugging
        for i, sel in enumerate(selects):
            try:
                select_obj = Select(sel)
                options = [opt.text for opt in select_obj.options]
                logger.info(f"Dropdown {i}: {options[:10]}...")  # First 10 options
            except Exception:
                pass

        # Try to find and select group/grade
        for sel in selects:
            try:
                select_obj = Select(sel)
                grade = config.get('grade', '').lower()
                gender = config.get('gender', '').lower()

                for option in select_obj.options:
                    opt_text = option.text.lower()
                    # Match on grade and gender
                    if grade in opt_text and gender in opt_text:
                        logger.info(f"Selecting group: {option.text}")
                        select_obj.select_by_visible_text(option.text)
                        time.sleep(2)
                        break
            except Exception as e:
                logger.debug(f"Dropdown error: {e}")
                continue

        # Refresh dropdowns after selection
        time.sleep(2)
        selects = driver.find_elements(By.TAG_NAME, 'select')

        # Try to find and select team/town
        for sel in selects:
            try:
                select_obj = Select(sel)
                for option in select_obj.options:
                    opt_text = option.text.lower()
                    town = config.get('town', '').lower()
                    team = config.get('team', '').lower()

                    if town in opt_text:
                        if not team or team in opt_text:
                            logger.info(f"Selecting team: {option.text}")
                            select_obj.select_by_visible_text(option.text)
                            time.sleep(2)
                            break
            except Exception as e:
                logger.debug(f"Team dropdown error: {e}")
                continue

        # Wait for schedule to load
        time.sleep(3)

        # Try clicking any "View Schedule" or similar buttons
        for btn_text in ['Schedule', 'View', 'Games', 'Show']:
            try:
                buttons = driver.find_elements(By.XPATH, f"//button[contains(text(), '{btn_text}')] | //a[contains(text(), '{btn_text}')] | //input[@value='{btn_text}']")
                for btn in buttons:
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(2)
                        break
            except Exception:
                pass

        # Scrape schedule from tables
        tables = driver.find_elements(By.TAG_NAME, 'table')
        logger.info(f"Found {len(tables)} tables")

        for table in tables:
            rows = table.find_elements(By.TAG_NAME, 'tr')
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) >= 3:
                    game = parse_table_row(cells, config.get('team_name', 'Team'))
                    if game:
                        game['league'] = 'MetroWest'
                        games.append(game)

        # Also try div-based layouts
        page_text = driver.page_source
        games.extend(parse_schedule_from_html(page_text, config.get('team_name', 'Team'), 'MetroWest'))

        # Deduplicate games
        games = dedupe_games(games)

        logger.info(f"Found {len(games)} games from MetroWest")

    except Exception as e:
        logger.error(f"Error scraping MetroWest: {e}")
    finally:
        if driver:
            driver.quit()

    return games


def scrape_ssybl(config: dict) -> list[dict]:
    """Scrape schedule from ssybl.org."""
    games = []
    driver = None

    try:
        logger.info("Starting SSYBL scrape...")
        driver = create_driver(headless=True)
        driver.get("https://ssybl.org/launch.php")
        time.sleep(3)

        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Find dropdowns
        selects = driver.find_elements(By.TAG_NAME, 'select')
        logger.info(f"Found {len(selects)} dropdown menus")

        # Log available options
        for i, sel in enumerate(selects):
            try:
                select_obj = Select(sel)
                options = [opt.text for opt in select_obj.options]
                logger.info(f"Dropdown {i}: {options[:10]}...")
            except Exception:
                pass

        # Select grade/gender
        for sel in selects:
            try:
                select_obj = Select(sel)
                grade = config.get('grade', '').lower()
                gender = config.get('gender', '').lower()

                for option in select_obj.options:
                    opt_text = option.text.lower()
                    if grade in opt_text and gender in opt_text:
                        logger.info(f"Selecting: {option.text}")
                        select_obj.select_by_visible_text(option.text)
                        time.sleep(2)
                        break
            except Exception:
                continue

        # Refresh and select town/team
        time.sleep(2)
        selects = driver.find_elements(By.TAG_NAME, 'select')

        for sel in selects:
            try:
                select_obj = Select(sel)
                for option in select_obj.options:
                    opt_text = option.text.lower()
                    town = config.get('town', '').lower()
                    team = config.get('team', '').lower()

                    if town in opt_text:
                        if not team or team in opt_text:
                            logger.info(f"Selecting: {option.text}")
                            select_obj.select_by_visible_text(option.text)
                            time.sleep(2)
                            break
            except Exception:
                continue

        time.sleep(3)

        # Scrape schedule from tables
        tables = driver.find_elements(By.TAG_NAME, 'table')
        logger.info(f"Found {len(tables)} tables")

        for table in tables:
            rows = table.find_elements(By.TAG_NAME, 'tr')
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) >= 3:
                    game = parse_table_row(cells, config.get('team_name', 'Team'))
                    if game:
                        game['league'] = 'SSYBL'
                        games.append(game)

        # Also try HTML parsing
        page_text = driver.page_source
        games.extend(parse_schedule_from_html(page_text, config.get('team_name', 'Team'), 'SSYBL'))

        # Deduplicate
        games = dedupe_games(games)

        logger.info(f"Found {len(games)} games from SSYBL")

    except Exception as e:
        logger.error(f"Error scraping SSYBL: {e}")
    finally:
        if driver:
            driver.quit()

    return games


def parse_table_row(cells, team_name: str) -> Optional[dict]:
    """Parse a table row into a game dict."""
    try:
        texts = [c.text.strip() for c in cells if c.text.strip()]

        if len(texts) < 2:
            return None

        # Find date, time, opponent, location
        date_str = ""
        time_str = ""
        opponent = ""
        location = ""

        for i, text in enumerate(texts):
            # Look for date pattern
            if re.search(r'\d{1,2}[/-]\d{1,2}', text) and not date_str:
                date_str = text
                # Time might be in same cell
                time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', text, re.I)
                if time_match:
                    time_str = time_match.group(1)
            # Standalone time
            elif re.search(r'^\d{1,2}:\d{2}', text) and not time_str:
                time_str = text
            # Opponent indicator
            elif re.search(r'\bvs\.?\b', text, re.I) or text.startswith('@'):
                opponent = re.sub(r'^(vs\.?|@)\s*', '', text, flags=re.I).strip()
            # Text that looks like a team name
            elif text and not re.search(r'[\d:/-]', text) and not opponent:
                if len(text) > 2 and text.lower() not in ['home', 'away', 'tbd']:
                    opponent = text

        # Location is often last non-date/time cell
        if len(texts) >= 4:
            for text in reversed(texts):
                if text and not re.search(r'^\d{1,2}[/-]\d{1,2}', text):
                    if not re.search(r'^\d{1,2}:\d{2}', text):
                        if text != opponent and len(text) > 3:
                            location = text
                            break

        game_dt = parse_datetime(date_str, time_str)

        if game_dt and opponent:
            return {
                'datetime': game_dt,
                'opponent': opponent,
                'location': location,
                'home_team': team_name,
            }
    except Exception as e:
        logger.debug(f"Row parse error: {e}")
    return None


def parse_schedule_from_html(html: str, team_name: str, league: str) -> list[dict]:
    """Extract games from HTML text patterns."""
    games = []

    # Pattern: date, time, opponent, location
    patterns = [
        # Pattern 1: Date - Time - vs Team - Location
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*[-\s]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*[-\s]*(?:vs\.?|@)?\s*([A-Za-z][A-Za-z\s\']+?)(?:\s+at\s+|\s*[-@]\s*)([A-Za-z][A-Za-z0-9\s\'\-]+?)(?=\n|\d{1,2}[/-]|$)',
        # Pattern 2: More flexible
        r'(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\D+(\d{1,2}:\d{2}\s*(?:AM|PM)?)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html, re.I | re.M)
        for match in matches:
            if len(match) >= 2:
                date_str = match[0]
                time_str = match[1] if len(match) > 1 else "12:00 PM"
                opponent = match[2].strip() if len(match) > 2 else "TBD"
                location = match[3].strip() if len(match) > 3 else ""

                game_dt = parse_datetime(date_str, time_str)

                if game_dt and (opponent or True):  # Include even if opponent unknown
                    games.append({
                        'datetime': game_dt,
                        'opponent': opponent or "TBD",
                        'location': location,
                        'home_team': team_name,
                        'league': league
                    })

    return games


def normalize_opponent(opponent: str) -> str:
    """Normalize opponent name for deduplication.

    Removes grade/gender indicators (5B, 6G, etc.) and division info (D1, D2, etc.)
    to match the same game across different leagues.
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

    Matches games by datetime and normalized opponent name.
    When duplicates are found, prefers the league game (is_tournament=False)
    over the non-league/tournament game.
    """
    # Sort so league games come first (is_tournament=False before True)
    sorted_games = sorted(games, key=lambda g: (g.get('is_tournament', False), g['datetime']))

    seen = {}  # key -> game (keep first = league game if available)
    for game in sorted_games:
        normalized_opp = normalize_opponent(game['opponent'])
        key = (game['datetime'].isoformat(), normalized_opp)
        if key not in seen:
            seen[key] = game

    # Return games in their original order (by datetime)
    return sorted(seen.values(), key=lambda g: g['datetime'])


def generate_ical(games: list[dict], team_name: str) -> bytes:
    """Generate iCalendar content."""
    cal = Calendar()
    cal.add('prodid', '-//Basketball Schedule Service//basketball-ical//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', f'{team_name} Basketball')
    cal.add('x-wr-timezone', 'America/New_York')

    for game in games:
        event = Event()

        # Create stable UID
        uid = hashlib.md5(
            f"{game['datetime'].isoformat()}-{game['opponent']}-{team_name}".encode()
        ).hexdigest()
        event.add('uid', f'{uid}@basketball-ical')

        opponent = game.get('opponent', 'TBD')
        event.add('summary', f"vs {opponent}")

        event.add('dtstart', game['datetime'])
        event.add('dtend', game['datetime'] + timedelta(hours=1, minutes=30))

        if game.get('location'):
            event.add('location', game['location'])

        desc_parts = [
            f"Team: {team_name}",
            f"Opponent: {opponent}",
            f"League: {game.get('league', 'Basketball')}"
        ]
        if game.get('location'):
            desc_parts.append(f"Location: {game['location']}")
        event.add('description', '\n'.join(desc_parts))

        event.add('dtstamp', datetime.now(EASTERN))

        # Add alarm 1 hour before
        from icalendar import Alarm
        alarm = Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('trigger', timedelta(hours=-1))
        alarm.add('description', f'Basketball game vs {opponent} in 1 hour')
        event.add_component(alarm)

        cal.add_component(event)

    return cal.to_ical()


def update_calendar(config: dict):
    """Scrape and update the calendar."""
    global current_calendar, last_update, games_cache

    logger.info("Updating calendar...")

    all_games = []

    # Check which sites to scrape
    sites = config.get('sites', [config.get('site', 'metrowest')])
    if isinstance(sites, str):
        sites = [sites]

    for site in sites:
        site_config = config.copy()
        site_config['site'] = site

        if site == 'metrowest':
            games = scrape_metrowest(site_config)
        elif site == 'ssybl':
            games = scrape_ssybl(site_config)
        else:
            logger.warning(f"Unknown site: {site}")
            continue

        all_games.extend(games)

    if all_games:
        all_games = dedupe_games(all_games)
        team_name = config.get('team_name', f"{config.get('town', '')} {config.get('grade', '')} {config.get('gender', '')}")
        current_calendar = generate_ical(all_games, team_name.strip())
        last_update = datetime.now(EASTERN)
        games_cache = all_games
        logger.info(f"Calendar updated with {len(all_games)} total games")
    else:
        logger.warning("No games found - keeping existing calendar")


def run_setup():
    """Interactive setup to find team configuration."""
    print("\n" + "="*60)
    print("Basketball iCal Service - Interactive Setup")
    print("="*60)

    print("\nThis will help you find the correct filter values for your team.")
    print("A browser window will open - watch it to see what's happening.\n")

    site = input("Which site? (metrowest/ssybl/both): ").strip().lower()
    if site == 'both':
        sites = ['metrowest', 'ssybl']
    elif site in ['metrowest', 'ssybl']:
        sites = [site]
    else:
        print("Invalid site. Use 'metrowest', 'ssybl', or 'both'")
        return

    # Open browser to explore
    for s in sites:
        url = "https://metrowestbball.com/launch.php" if s == 'metrowest' else "https://ssybl.org/launch.php"
        print(f"\nOpening {url}...")
        print("Look at the dropdowns and note the exact text for your team.\n")

        driver = create_driver(headless=False)
        driver.get(url)

        input(f"Press Enter when you've noted the dropdown values for {s}...")
        driver.quit()

    print("\nNow enter the values you saw:")
    town = input("Town (e.g., Milton): ").strip()
    grade = input("Grade (e.g., 5th Grade or 8th Grade): ").strip()
    gender = input("Gender (Boys/Girls): ").strip()
    team = input("Team designation if any (e.g., White, Red, A, or leave blank): ").strip()

    team_name = f"{town} {grade} {gender}"
    if team:
        team_name += f" {team}"

    config = {
        'sites': sites,
        'town': town,
        'grade': grade,
        'gender': gender,
        'team': team,
        'team_name': team_name,
        'refresh_hours': 6
    }

    print(f"\nTrying to scrape with these settings...")

    all_games = []
    for s in sites:
        site_config = config.copy()
        if s == 'metrowest':
            games = scrape_metrowest(site_config)
        else:
            games = scrape_ssybl(site_config)
        all_games.extend(games)

    if all_games:
        print(f"\nFound {len(all_games)} games:")
        for g in sorted(all_games, key=lambda x: x['datetime'])[:10]:
            print(f"   - {g['datetime'].strftime('%b %d %I:%M%p')} vs {g['opponent']} ({g.get('league', '')})")
        if len(all_games) > 10:
            print(f"   ... and {len(all_games)-10} more")
    else:
        print("\nNo games found. The schedule might not be posted yet,")
        print("or the filter values might need adjustment.")
        print("The service will keep checking and update when games appear.")

    # Save config
    config_file = "bball_config.json"
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

    print(f"\nConfiguration saved to: {config_file}")
    print(f"\nTo run the service:")
    print(f"   python bball_ical_service.py --config {config_file}")


def create_app(config: dict):
    """Create Flask app."""
    app = Flask(__name__)

    @app.route('/')
    def index():
        return f"""
        <html>
        <head><title>Basketball Calendar Service</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px;">
            <h1>Basketball Calendar</h1>
            <p><strong>Team:</strong> {config.get('team_name', 'Unknown')}</p>
            <p><strong>Last Updated:</strong> {last_update.strftime('%Y-%m-%d %H:%M') if last_update else 'Never'}</p>
            <p><strong>Games Found:</strong> {len(games_cache)}</p>

            <h2>Subscribe</h2>
            <p>Add this URL to your calendar app:</p>
            <code style="background: #f0f0f0; padding: 10px; display: block; word-break: break-all;">
                {os.environ.get('PUBLIC_URL', 'http://localhost:5000')}/calendar.ics
            </code>

            <h3>Instructions:</h3>
            <ul>
                <li><strong>Google Calendar:</strong> Other calendars (+) &rarr; From URL</li>
                <li><strong>Apple Calendar:</strong> File &rarr; New Calendar Subscription</li>
                <li><strong>Outlook:</strong> Add calendar &rarr; Subscribe from web</li>
            </ul>

            <h2>Upcoming Games</h2>
            <ul>
            {''.join(f"<li>{g['datetime'].strftime('%b %d %I:%M%p')} vs {g['opponent']}</li>" for g in sorted(games_cache, key=lambda x: x['datetime'])[:10])}
            </ul>
        </body>
        </html>
        """

    @app.route('/calendar.ics')
    @app.route('/basketball.ics')
    def serve_calendar():
        if current_calendar is None:
            return Response(
                "Calendar not yet available. Please wait for first update.",
                status=503,
                mimetype='text/plain'
            )

        return Response(
            current_calendar,
            mimetype='text/calendar',
            headers={
                'Content-Disposition': 'inline; filename="basketball.ics"',
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'X-Last-Update': last_update.isoformat() if last_update else 'never'
            }
        )

    @app.route('/status')
    def status():
        return {
            'status': 'running',
            'last_update': last_update.isoformat() if last_update else None,
            'has_calendar': current_calendar is not None,
            'team': config.get('team_name', 'Unknown'),
            'games_count': len(games_cache)
        }

    @app.route('/refresh', methods=['POST'])
    def refresh():
        update_calendar(config)
        return {'status': 'refreshed', 'games_count': len(games_cache)}

    return app


def main():
    parser = argparse.ArgumentParser(description='Basketball iCal Subscription Service')
    parser.add_argument('--setup', action='store_true', help='Run interactive setup')
    parser.add_argument('--config', '-c', help='Config file (JSON)')
    parser.add_argument('--port', '-p', type=int, default=5000, help='HTTP port (default: 5000)')
    parser.add_argument('--refresh', '-r', type=int, help='Refresh interval in hours (default: 6)')
    parser.add_argument('--once', action='store_true', help='Scrape once and output ICS to stdout')

    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    if not args.config:
        print("Basketball iCal Subscription Service")
        print("="*40)
        print("\nUsage:")
        print("  First run:  python bball_ical_service.py --setup")
        print("  Then:       python bball_ical_service.py --config bball_config.json")
        print("\nOptions:")
        print("  --port PORT     HTTP port (default: 5000)")
        print("  --refresh N     Refresh every N hours (default: 6)")
        print("  --once          Scrape once and print ICS to stdout")
        return

    # Load config
    with open(args.config) as f:
        config = json.load(f)

    if args.refresh:
        config['refresh_hours'] = args.refresh

    refresh_hours = config.get('refresh_hours', 6)

    # One-shot mode
    if args.once:
        update_calendar(config)
        if current_calendar:
            print(current_calendar.decode('utf-8'))
        return

    print(f"\nBasketball iCal Subscription Service")
    print(f"   Team: {config.get('team_name', 'Unknown')}")
    print(f"   Sites: {config.get('sites', [config.get('site', 'Unknown')])}")
    print(f"   Refresh: Every {refresh_hours} hours")

    # Initial update
    print(f"\nFetching initial schedule...")
    update_calendar(config)

    # Set up scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: update_calendar(config),
        'interval',
        hours=refresh_hours,
        id='update_calendar'
    )
    scheduler.start()

    # Get local IP
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "localhost"

    print(f"\nServer starting...")
    print(f"   Subscribe URL: http://{local_ip}:{args.port}/calendar.ics")
    print(f"\n   Add this URL to:")
    print(f"   - Google Calendar: Other calendars (+) -> From URL")
    print(f"   - Apple Calendar: File -> New Calendar Subscription")
    print(f"   - Outlook: Add calendar -> Subscribe from web")
    print(f"\n   Web interface: http://{local_ip}:{args.port}/")
    print(f"   Status JSON: http://{local_ip}:{args.port}/status")
    print(f"\n   Press Ctrl+C to stop\n")

    # Run Flask
    app = create_app(config)
    try:
        app.run(host='0.0.0.0', port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("\nService stopped.")


if __name__ == '__main__':
    main()
