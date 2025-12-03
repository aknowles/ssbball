#!/usr/bin/env python3
"""
Basketball Schedule Scraper for GitHub Actions

Scrapes schedules from metrowestbball.com and ssybl.org,
generates iCal files, and outputs them for GitHub Pages hosting.

Usage:
    python scraper.py --config teams.json --output docs/
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from icalendar import Calendar, Event, Alarm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Eastern timezone for MA basketball leagues
EASTERN = ZoneInfo("America/New_York")


def create_driver():
    """Create a headless Chrome driver for CI."""
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    # For GitHub Actions, Chrome is pre-installed
    chrome_bin = os.environ.get('CHROME_BIN')
    if chrome_bin:
        options.binary_location = chrome_bin

    service = Service()
    return webdriver.Chrome(service=service, options=options)


def parse_date_and_time(date_str: str, time_str: str, year: int = None) -> Optional[datetime]:
    """Parse date and time strings into a datetime object."""
    if not year:
        year = datetime.now().year

    # Parse date like "Dec 7" or "Jan 18"
    date_match = re.match(r'([A-Za-z]+)\s*(\d{1,2})', date_str.strip())
    if not date_match:
        # Try numeric format like "12/7" or "1/18"
        date_match = re.match(r'(\d{1,2})[/-](\d{1,2})', date_str.strip())
        if date_match:
            month = int(date_match.group(1))
            day = int(date_match.group(2))
        else:
            return None
    else:
        month_str = date_match.group(1).lower()
        day = int(date_match.group(2))
        months = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
        }
        month = months.get(month_str[:3], 1)

    # Handle year rollover (if month is Jan-Mar and current month is Oct-Dec, use next year)
    now = datetime.now()
    if month < 6 and now.month > 8:
        year = now.year + 1
    elif month > 8 and now.month < 6:
        year = now.year - 1
    else:
        year = now.year

    # Parse time like "2:00 PM" or "11:15 AM"
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
    else:
        hour, minute = 12, 0  # Default to noon

    try:
        return datetime(year, month, day, hour, minute, tzinfo=EASTERN)
    except ValueError as e:
        logger.warning(f"Invalid date: {year}-{month}-{day} {hour}:{minute} - {e}")
        return None


def scrape_metrowest(driver, config: dict) -> list[dict]:
    """Scrape schedule from metrowestbball.com."""
    games = []
    url = config.get('site_url', 'https://metrowestbball.com')

    try:
        logger.info(f"Scraping MetroWest from {url}...")
        driver.get(url)
        time.sleep(3)

        # Wait for page to load
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Take screenshot for debugging
        logger.info(f"Page title: {driver.title}")

        # Find all select elements
        selects = driver.find_elements(By.TAG_NAME, 'select')
        logger.info(f"Found {len(selects)} dropdowns")

        # Log all dropdown options for debugging
        dropdown_map = {}
        for sel in selects:
            try:
                # Try to identify dropdown by nearby label or name attribute
                sel_id = sel.get_attribute('id') or sel.get_attribute('name') or 'unknown'
                select_obj = Select(sel)
                options = [opt.text.strip() for opt in select_obj.options if opt.text.strip()]
                logger.info(f"Dropdown '{sel_id}': {options[:8]}")

                # Map dropdowns by content
                option_text = ' '.join(options).lower()
                if 'boys' in option_text or 'girls' in option_text:
                    dropdown_map['gender'] = sel
                elif any(g in option_text for g in ['5th', '6th', '7th', '8th', '4th']):
                    dropdown_map['grade'] = sel
                elif any(t in option_text for t in ['milton', 'newton', 'brookline', 'needham']):
                    if 'white' in option_text or 'red' in option_text or 'd1' in option_text.lower():
                        dropdown_map['team'] = sel
                    else:
                        dropdown_map['town'] = sel
            except Exception as e:
                logger.debug(f"Error reading dropdown: {e}")

        # Select Gender
        gender = config.get('gender', 'Boys')
        if 'gender' in dropdown_map:
            try:
                select_obj = Select(dropdown_map['gender'])
                for opt in select_obj.options:
                    if gender.lower() in opt.text.lower():
                        logger.info(f"Selecting gender: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
            except Exception as e:
                logger.warning(f"Could not select gender: {e}")

        # Re-find dropdowns after selection (page may have updated)
        time.sleep(1)
        selects = driver.find_elements(By.TAG_NAME, 'select')

        # Select Grade
        grade = config.get('grade', '5th')
        for sel in selects:
            try:
                select_obj = Select(sel)
                for opt in select_obj.options:
                    if grade.lower() in opt.text.lower():
                        logger.info(f"Selecting grade: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
            except Exception:
                continue

        # Re-find and select Town
        time.sleep(1)
        selects = driver.find_elements(By.TAG_NAME, 'select')
        town = config.get('town', 'Milton')
        for sel in selects:
            try:
                select_obj = Select(sel)
                for opt in select_obj.options:
                    if town.lower() in opt.text.lower() and 'white' not in opt.text.lower() and 'red' not in opt.text.lower():
                        logger.info(f"Selecting town: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
            except Exception:
                continue

        # Re-find and select Team
        time.sleep(1)
        selects = driver.find_elements(By.TAG_NAME, 'select')
        team = config.get('team', '(White)')
        for sel in selects:
            try:
                select_obj = Select(sel)
                options_text = [opt.text for opt in select_obj.options]
                logger.info(f"Team dropdown options: {options_text}")

                # Try exact match first
                for opt in select_obj.options:
                    if team.lower() == opt.text.lower().strip():
                        logger.info(f"Selecting team (exact): {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
                else:
                    # Try partial match
                    for opt in select_obj.options:
                        if team.lower() in opt.text.lower():
                            logger.info(f"Selecting team (partial): {opt.text}")
                            select_obj.select_by_visible_text(opt.text)
                            time.sleep(1)
                            break
            except Exception as e:
                logger.debug(f"Team selection error: {e}")
                continue

        # Click Search button
        time.sleep(1)
        try:
            search_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'SEARCH')] | //button[contains(text(), 'Search')] | //input[@value='Search'] | //input[@value='SEARCH'] | //a[contains(text(), 'SEARCH')]")
            logger.info("Clicking Search button")
            search_btn.click()
            time.sleep(3)
        except NoSuchElementException:
            logger.info("No Search button found, schedule may already be visible")

        # Wait for schedule table to load
        time.sleep(2)

        # Parse the schedule table
        # Looking for table with columns: DATE, DAY, GAME, OPPONENT, LOCATION, TIME
        tables = driver.find_elements(By.TAG_NAME, 'table')
        logger.info(f"Found {len(tables)} tables")

        team_name = config.get('team_name', 'Team')

        for table in tables:
            rows = table.find_elements(By.TAG_NAME, 'tr')
            logger.info(f"Table has {len(rows)} rows")

            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) >= 5:
                    try:
                        # Expected columns: DATE, DAY, GAME, OPPONENT, LOCATION, TIME, SCORE, OPP
                        cell_texts = [c.text.strip() for c in cells]
                        logger.debug(f"Row: {cell_texts}")

                        date_str = cell_texts[0] if len(cell_texts) > 0 else ""
                        day_str = cell_texts[1] if len(cell_texts) > 1 else ""
                        game_type = cell_texts[2] if len(cell_texts) > 2 else ""  # Home/Away
                        opponent = cell_texts[3] if len(cell_texts) > 3 else ""
                        location = cell_texts[4] if len(cell_texts) > 4 else ""
                        time_str = cell_texts[5] if len(cell_texts) > 5 else ""

                        # Skip header rows or empty rows
                        if not date_str or date_str.upper() == 'DATE' or not opponent:
                            continue

                        # Parse the datetime
                        game_dt = parse_date_and_time(date_str, time_str)
                        if not game_dt:
                            logger.warning(f"Could not parse date/time: {date_str} {time_str}")
                            continue

                        # Clean up opponent name (remove @ prefix for away games)
                        opponent = re.sub(r'^@\s*', '', opponent).strip()

                        game = {
                            'datetime': game_dt,
                            'opponent': opponent,
                            'location': location,
                            'home_team': team_name,
                            'game_type': game_type,
                            'league': 'MetroWest'
                        }
                        games.append(game)
                        logger.info(f"Found game: {game_dt.strftime('%b %d')} vs {opponent}")

                    except Exception as e:
                        logger.debug(f"Error parsing row: {e}")
                        continue

        logger.info(f"Found {len(games)} games from MetroWest")

    except Exception as e:
        logger.error(f"Error scraping MetroWest: {e}")
        import traceback
        logger.error(traceback.format_exc())

    return games


def scrape_ssybl(driver, config: dict) -> list[dict]:
    """Scrape schedule from ssybl.org."""
    games = []
    url = config.get('site_url', 'https://ssybl.org')

    try:
        logger.info(f"Scraping SSYBL from {url}...")
        driver.get(url)
        time.sleep(3)

        # Wait for page to load
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        logger.info(f"Page title: {driver.title}")

        # Similar approach to MetroWest - find and interact with dropdowns
        selects = driver.find_elements(By.TAG_NAME, 'select')
        logger.info(f"Found {len(selects)} dropdowns")

        # Log dropdown options
        for i, sel in enumerate(selects):
            try:
                select_obj = Select(sel)
                options = [opt.text.strip() for opt in select_obj.options if opt.text.strip()]
                logger.info(f"Dropdown {i}: {options[:8]}")
            except Exception:
                pass

        # Select dropdowns in order
        gender = config.get('gender', 'Boys')
        grade = config.get('grade', '8th')
        town = config.get('town', 'Milton')
        team = config.get('team', '(White)')

        # Try to select each filter
        for sel in selects:
            try:
                select_obj = Select(sel)
                for opt in select_obj.options:
                    opt_lower = opt.text.lower()
                    # Match gender
                    if gender.lower() in opt_lower and ('boy' in opt_lower or 'girl' in opt_lower):
                        logger.info(f"Selecting: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
                    # Match grade
                    elif grade.lower() in opt_lower and ('grade' in opt_lower or opt_lower.endswith('th')):
                        logger.info(f"Selecting: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
                    # Match town
                    elif town.lower() in opt_lower:
                        logger.info(f"Selecting: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
                    # Match team
                    elif team.lower() in opt_lower:
                        logger.info(f"Selecting: {opt.text}")
                        select_obj.select_by_visible_text(opt.text)
                        time.sleep(1)
                        break
            except Exception:
                continue

        # Click Search/View button
        time.sleep(1)
        try:
            for btn_text in ['SEARCH', 'Search', 'VIEW', 'View', 'GO', 'Go']:
                try:
                    btn = driver.find_element(By.XPATH, f"//button[contains(text(), '{btn_text}')] | //input[@value='{btn_text}'] | //a[contains(text(), '{btn_text}')]")
                    logger.info(f"Clicking {btn_text} button")
                    btn.click()
                    time.sleep(3)
                    break
                except NoSuchElementException:
                    continue
        except Exception:
            logger.info("No search button found")

        # Parse schedule table
        time.sleep(2)
        tables = driver.find_elements(By.TAG_NAME, 'table')
        logger.info(f"Found {len(tables)} tables")

        team_name = config.get('team_name', 'Team')

        for table in tables:
            rows = table.find_elements(By.TAG_NAME, 'tr')
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) >= 4:
                    try:
                        cell_texts = [c.text.strip() for c in cells]

                        # Try to find date, opponent, location, time in the cells
                        date_str = ""
                        time_str = ""
                        opponent = ""
                        location = ""

                        for text in cell_texts:
                            # Date pattern
                            if re.match(r'[A-Za-z]{3}\s*\d{1,2}', text) or re.match(r'\d{1,2}[/-]\d{1,2}', text):
                                date_str = text
                            # Time pattern
                            elif re.match(r'\d{1,2}:\d{2}', text):
                                time_str = text
                            # Opponent (usually has @ or vs or team-like name)
                            elif '@' in text or 'vs' in text.lower() or re.match(r'^[A-Z][a-z]+', text):
                                if not opponent:
                                    opponent = re.sub(r'^[@vs.\s]+', '', text, flags=re.I).strip()
                            # Location (usually longer text)
                            elif len(text) > 10 and not re.match(r'^\d', text):
                                location = text

                        if date_str and opponent:
                            game_dt = parse_date_and_time(date_str, time_str)
                            if game_dt:
                                game = {
                                    'datetime': game_dt,
                                    'opponent': opponent,
                                    'location': location,
                                    'home_team': team_name,
                                    'league': 'SSYBL'
                                }
                                games.append(game)
                                logger.info(f"Found game: {game_dt.strftime('%b %d')} vs {opponent}")
                    except Exception as e:
                        logger.debug(f"Error parsing row: {e}")

        logger.info(f"Found {len(games)} games from SSYBL")

    except Exception as e:
        logger.error(f"Error scraping SSYBL: {e}")
        import traceback
        logger.error(traceback.format_exc())

    return games


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
        game_type = game.get('game_type', '')

        # Include home/away in title if available
        if game_type.lower() == 'away':
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
        if game_type:
            desc.append(f"Game: {game_type}")
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


def scrape_team(config: dict) -> tuple[list[dict], bytes]:
    """Scrape a single team's schedule."""
    driver = None
    all_games = []

    try:
        driver = create_driver()

        sites = config.get('sites', [config.get('site', 'metrowest')])
        if isinstance(sites, str):
            sites = [sites]

        for site in sites:
            if site == 'metrowest':
                games = scrape_metrowest(driver, config)
            elif site == 'ssybl':
                games = scrape_ssybl(driver, config)
            else:
                logger.warning(f"Unknown site: {site}")
                continue

            all_games.extend(games)

    finally:
        if driver:
            driver.quit()

    all_games = dedupe_games(all_games)

    team_name = config.get('team_name', 'Basketball Team')
    calendar_id = config.get('id', 'basketball')
    ical_data = generate_ical(all_games, team_name, calendar_id)

    return all_games, ical_data


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
            <p class="league">{', '.join(team.get('sites', ['Unknown']))} &bull; {games_info}</p>
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

    teams = config.get('teams', [config])  # Support single team or list
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
