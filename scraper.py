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
import sys
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
    options.binary_location = os.environ.get('CHROME_BIN', '/usr/bin/google-chrome')

    service = Service()
    return webdriver.Chrome(service=service, options=options)


def parse_datetime(date_str: str, time_str: str = "") -> Optional[datetime]:
    """Parse various date/time formats and return timezone-aware datetime."""
    date_match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', date_str)
    if not date_match:
        return None

    month, day, year = date_match.groups()
    year = int(year)
    if year < 100:
        year += 2000

    try:
        parsed_date = datetime(year, int(month), int(day), tzinfo=EASTERN)
    except ValueError:
        return None

    # Parse time
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

    return parsed_date.replace(hour=12, minute=0)


def parse_table_row(cells, team_name: str) -> Optional[dict]:
    """Parse a table row into a game dict."""
    try:
        texts = [c.text.strip() for c in cells if c.text.strip()]
        if len(texts) < 2:
            return None

        date_str = ""
        time_str = ""
        opponent = ""
        location = ""

        for text in texts:
            if re.search(r'\d{1,2}[/-]\d{1,2}', text) and not date_str:
                date_str = text
                time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', text, re.I)
                if time_match:
                    time_str = time_match.group(1)
            elif re.search(r'^\d{1,2}:\d{2}', text) and not time_str:
                time_str = text
            elif re.search(r'\bvs\.?\b', text, re.I) or text.startswith('@'):
                opponent = re.sub(r'^(vs\.?|@)\s*', '', text, flags=re.I).strip()
            elif text and not re.search(r'[\d:/-]', text) and not opponent:
                if len(text) > 2 and text.lower() not in ['home', 'away', 'tbd']:
                    opponent = text

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


def scrape_site(driver, url: str, config: dict, league: str) -> list[dict]:
    """Generic scraper for basketball sites."""
    games = []

    try:
        logger.info(f"Scraping {league} from {url}...")
        driver.get(url)
        time.sleep(3)

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Find and interact with dropdowns
        selects = driver.find_elements(By.TAG_NAME, 'select')
        logger.info(f"Found {len(selects)} dropdowns")

        # Log options for debugging
        for i, sel in enumerate(selects):
            try:
                select_obj = Select(sel)
                options = [opt.text for opt in select_obj.options]
                logger.info(f"Dropdown {i}: {options[:5]}...")
            except Exception:
                pass

        # Select grade/gender
        grade = config.get('grade', '').lower()
        gender = config.get('gender', '').lower()

        for sel in selects:
            try:
                select_obj = Select(sel)
                for option in select_obj.options:
                    opt_text = option.text.lower()
                    if grade and gender and grade in opt_text and gender in opt_text:
                        logger.info(f"Selecting group: {option.text}")
                        select_obj.select_by_visible_text(option.text)
                        time.sleep(2)
                        break
            except Exception:
                continue

        time.sleep(2)
        selects = driver.find_elements(By.TAG_NAME, 'select')

        # Select town/team
        town = config.get('town', '').lower()
        team = config.get('team', '').lower()

        for sel in selects:
            try:
                select_obj = Select(sel)
                for option in select_obj.options:
                    opt_text = option.text.lower()
                    if town in opt_text:
                        if not team or team in opt_text:
                            logger.info(f"Selecting team: {option.text}")
                            select_obj.select_by_visible_text(option.text)
                            time.sleep(2)
                            break
            except Exception:
                continue

        time.sleep(3)

        # Click view/schedule buttons
        for btn_text in ['Schedule', 'View', 'Games', 'Show']:
            try:
                buttons = driver.find_elements(
                    By.XPATH,
                    f"//button[contains(text(), '{btn_text}')] | "
                    f"//a[contains(text(), '{btn_text}')] | "
                    f"//input[@value='{btn_text}']"
                )
                for btn in buttons:
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(2)
                        break
            except Exception:
                pass

        # Parse tables
        tables = driver.find_elements(By.TAG_NAME, 'table')
        logger.info(f"Found {len(tables)} tables")

        team_name = config.get('team_name', 'Team')
        for table in tables:
            rows = table.find_elements(By.TAG_NAME, 'tr')
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, 'td')
                if len(cells) >= 3:
                    game = parse_table_row(cells, team_name)
                    if game:
                        game['league'] = league
                        games.append(game)

        logger.info(f"Found {len(games)} games from {league}")

    except Exception as e:
        logger.error(f"Error scraping {league}: {e}")

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
                url = "https://metrowestbball.com/launch.php"
                games = scrape_site(driver, url, config, 'MetroWest')
            elif site == 'ssybl':
                url = "https://ssybl.org/launch.php"
                games = scrape_site(driver, url, config, 'SSYBL')
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


def generate_index_html(teams: list[dict], base_url: str) -> str:
    """Generate the landing page HTML."""
    now = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M %Z')

    team_cards = []
    for team in teams:
        team_id = team.get('id', 'team')
        team_name = team.get('team_name', 'Team')
        ics_url = f"{base_url}/{team_id}.ics"

        team_cards.append(f'''
        <div class="team-card">
            <h2>{team_name}</h2>
            <p class="league">{', '.join(team.get('sites', ['Unknown']))}</p>
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
            results.append({
                'team': team_name,
                'id': team_id,
                'games': 0,
                'error': str(e)
            })

    # Generate index.html
    index_html = generate_index_html(teams, base_url)
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
