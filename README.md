# Basketball iCal Subscription Service

Automatically create iCal calendar subscriptions from MetroWest Basketball and SSYBL (South Shore Youth Basketball League) schedules.

## What This Does

1. Scrapes your team's schedule from metrowestbball.com and/or ssybl.org
2. Generates an iCal file with all games
3. Serves it via HTTP for calendar subscription
4. Auto-refreshes every 6 hours (configurable)

Once set up, your calendar will automatically update when new games are added or schedules change.

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Note: This also requires Chrome or Chromium browser to be installed (for Selenium).

### 2. Run Interactive Setup

```bash
python bball_ical_service.py --setup
```

This will:
- Open a browser window so you can see the dropdown options
- Guide you through selecting your team
- Create a config file automatically

### 3. Run the Service

```bash
python bball_ical_service.py --config bball_config.json
```

### 4. Subscribe in Your Calendar

Add this URL to your calendar app:
```
http://YOUR_IP:5000/calendar.ics
```

**Google Calendar:** Other calendars (+) → From URL

**Apple Calendar:** File → New Calendar Subscription

**Outlook:** Add calendar → Subscribe from web

## Pre-made Configs for Milton Teams

Example configs are included:

- `config_milton_5th_white.json` - Milton 5th Grade Boys White (MetroWest)
- `config_milton_8th_white.json` - Milton 8th Grade Boys White (SSYBL)
- `config_both_teams.json` - Both teams combined

Run with:
```bash
python bball_ical_service.py --config config_milton_5th_white.json
```

## Running Multiple Teams

To track multiple teams, you have two options:

### Option 1: Combined Config
Edit the config to list both sites and leave grade blank. Games from all matching teams will appear in one calendar.

### Option 2: Multiple Instances
Run multiple instances on different ports:

```bash
python bball_ical_service.py --config config_milton_5th_white.json --port 5001 &
python bball_ical_service.py --config config_milton_8th_white.json --port 5002 &
```

## Deployment Options

### Run on Your Computer
Just run the script while your computer is on. Calendar apps will sync when the service is available.

### Run on a Raspberry Pi (Recommended for 24/7)
1. Install Chrome/Chromium: `sudo apt install chromium-browser`
2. Copy files to Pi
3. Install dependencies: `pip install -r requirements.txt`
4. Run with systemd or screen for persistence

### Free Cloud Hosting

**PythonAnywhere (Free tier):**
- Upload files
- Set up a scheduled task to run the scraper
- Host the ICS file as a static file

**Render.com / Railway:**
- Deploy as a web service
- Set `PORT` environment variable
- Use `PUBLIC_URL` env var for correct subscription URLs

## Configuration Options

```json
{
  "sites": ["metrowest", "ssybl"],  // Which sites to scrape
  "town": "Milton",                  // Town name
  "grade": "5th Grade",              // Grade (e.g., "5th Grade", "8th Grade")
  "gender": "Boys",                  // "Boys" or "Girls"
  "team": "White",                   // Team designation (optional)
  "team_name": "Milton 5th Boys",    // Display name in calendar
  "refresh_hours": 6                 // How often to re-scrape
}
```

## Command Line Options

```
--setup          Interactive setup wizard
--config FILE    Load config from JSON file
--port PORT      HTTP port (default: 5000)
--refresh N      Refresh interval in hours
--once           Scrape once and output ICS to stdout
```

## Troubleshooting

**No games found:**
- The schedule may not be posted yet
- Double-check the filter values match exactly what you see on the website
- Run `--setup` again to verify dropdown options

**Browser errors:**
- Make sure Chrome or Chromium is installed
- On Linux: `sudo apt install chromium-browser`
- On Mac: Install Chrome from google.com/chrome

**Calendar not updating:**
- Most calendar apps only refresh subscribed calendars every 24 hours
- You can force refresh in your calendar app settings
- The service refreshes every 6 hours by default

## How It Works

1. Uses Selenium with headless Chrome to load the basketball websites
2. Interacts with dropdowns to filter to your specific team
3. Parses the schedule from HTML tables
4. Generates an iCal (.ics) file with proper timezone handling
5. Serves the file via Flask HTTP server
6. Uses APScheduler to refresh the data periodically

The scraping approach is necessary because these league websites don't offer an API or built-in calendar export feature.
