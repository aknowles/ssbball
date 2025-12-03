# Basketball iCal Subscriptions

Automatically syncs basketball schedules from MetroWest Basketball and SSYBL to your calendar.

## Subscribe to Calendars

Visit the GitHub Pages site to get subscription URLs:

**https://aknowles.github.io/ssbball**

### Available Calendars

| Team | League | Subscribe URL |
|------|--------|---------------|
| Milton 5th Grade Boys White | MetroWest | `https://aknowles.github.io/ssbball/milton-5th-boys-white.ics` |
| Milton 8th Grade Boys White | SSYBL | `https://aknowles.github.io/ssbball/milton-8th-boys-white.ics` |

## How to Subscribe

### Google Calendar
1. Click the **+** next to "Other calendars"
2. Select "From URL"
3. Paste the subscription URL
4. Click "Add calendar"

### Apple Calendar (Mac)
1. File → New Calendar Subscription
2. Paste the subscription URL
3. Click Subscribe

### iPhone/iPad
1. Settings → Calendar → Accounts
2. Add Account → Other
3. Add Subscribed Calendar
4. Paste the URL

### Outlook
1. Add calendar → Subscribe from web
2. Paste the URL

## How It Works

1. **GitHub Actions** runs every 6 hours
2. **Selenium** scrapes schedules from metrowestbball.com and ssybl.org
3. **iCal files** are generated and deployed to **GitHub Pages**
4. Your calendar app automatically fetches updates

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   GitHub    │────▶│   GitHub    │────▶│    Your     │
│   Actions   │     │   Pages     │     │  Calendar   │
│  (scraper)  │     │  (.ics)     │     │    App      │
└─────────────┘     └─────────────┘     └─────────────┘
     Every 6h           Static            Auto-sync
```

## Adding More Teams

Edit `teams.json` to add teams:

```json
{
  "teams": [
    {
      "id": "your-team-id",
      "team_name": "Display Name",
      "sites": ["metrowest"],
      "town": "YourTown",
      "grade": "5th Grade",
      "gender": "Boys",
      "team": "White"
    }
  ]
}
```

### Configuration Fields

| Field | Description | Example |
|-------|-------------|---------|
| `id` | URL-safe identifier | `milton-5th-boys-white` |
| `team_name` | Display name | `Milton 5th Grade Boys White` |
| `sites` | Which sites to scrape | `["metrowest"]` or `["ssybl"]` or both |
| `town` | Town name (must match dropdown) | `Milton` |
| `grade` | Grade level | `5th Grade`, `8th Grade` |
| `gender` | `Boys` or `Girls` | `Boys` |
| `team` | Team designation (optional) | `White`, `Red`, `A` |

## Manual Trigger

To force an immediate update:

1. Go to Actions tab
2. Select "Update Basketball Calendars"
3. Click "Run workflow"

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run scraper locally
python scraper.py --config teams.json --output docs

# View generated files
open docs/index.html
```

## Setup for Your Own Teams

1. **Fork this repository**

2. **Enable GitHub Pages:**
   - Go to Settings → Pages
   - Source: "GitHub Actions"

3. **Edit `teams.json`** with your teams

4. **Update `base_url`** in `teams.json` to match your GitHub Pages URL:
   ```
   https://YOUR-USERNAME.github.io/YOUR-REPO-NAME
   ```

5. **Push changes** - the Action will run automatically

## Troubleshooting

**Calendar not updating?**
- Most calendar apps only refresh subscriptions every 24 hours
- The workflow runs every 6 hours, so fresh data is always available
- You can force refresh in your calendar app's settings

**No games showing?**
- The schedule may not be posted yet on the league website
- Check that your team config matches the dropdown values exactly
- Look at the workflow run logs for errors

**Wrong games showing?**
- The `town`, `grade`, `gender`, and `team` fields must match the dropdown text
- Try running `--setup` locally to see exact dropdown values

## Files

| File | Purpose |
|------|---------|
| `teams.json` | Team configurations |
| `scraper.py` | Main scraper script |
| `.github/workflows/update-calendars.yml` | GitHub Actions workflow |
| `docs/` | Generated output (GitHub Pages) |
| `bball_ical_service.py` | Standalone server version (optional) |
