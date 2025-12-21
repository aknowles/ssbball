# Basketball iCal Subscriptions

Automatically syncs basketball schedules from MetroWest Basketball and SSYBL to your calendar. Subscribe once and your calendar stays up to date — schedules are checked hourly during game hours (6 AM - 9 PM ET).

## Subscribe to Calendars

Visit the GitHub Pages site to get subscription URLs:

**https://aknowles.github.io/ssbball**

## How It Works

1. **GitHub Actions** runs hourly during game hours (6 AM - 9 PM ET)
2. **API calls** fetch schedules from metrowestbball.com and ssybl.org
3. **iCal files** are generated and deployed to **GitHub Pages**
4. Your calendar app automatically fetches updates

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   GitHub    │────▶│   GitHub    │────▶│    Your     │
│   Actions   │     │   Pages     │     │  Calendar   │
│  (scraper)  │     │  (.ics)     │     │    App      │
└─────────────┘     └─────────────┘     └─────────────┘
    Hourly            Static            Auto-sync
```

## Fork for Your Town

Want to set this up for your own town? It's easy — just change the config file!

### 1. Fork this repository

### 2. Edit `teams.json`

```json
{
  "town_name": "YourTown",
  "leagues": ["ssybl", "metrowbb"],
  "grades": [3, 4, 5, 6, 7, 8],
  "genders": ["M", "F"],
  "colors": [],
  "include_nl_games": true,
  "base_url": "https://YOUR-USERNAME.github.io/YOUR-REPO"
}
```

### 3. Enable GitHub Pages
- Go to Settings → Pages
- Source: "GitHub Actions"

### 4. Push changes
The Action will run automatically and discover all your town's teams!

### Configuration Options

| Field | Description | Example |
|-------|-------------|---------|
| `town_name` | Your town name (must match league website) | `"Needham"` |
| `leagues` | Which leagues to check | `["ssybl", "metrowbb"]` or just one |
| `grades` | Grade levels to include | `[5, 6, 7, 8]` |
| `genders` | `"M"` for boys, `"F"` for girls | `["M", "F"]` for both |
| `colors` | Filter to specific teams (empty = all) | `["White", "Red"]` or `[]` |
| `include_nl_games` | Include tournaments/playoffs (default: true) | `true` or `false` |
| `base_url` | Your GitHub Pages URL | `"https://user.github.io/repo"` |
| `coaches` | Coach names displayed on calendar page | See below |
| `team_aliases` | Map variant team names to canonical colors | See below |
| `season` | Season start/end dates for practices | See below |
| `practices` | Recurring practice schedules per team | See below |

### Coaches

Display coach names on the calendar page to help parents find their team:

```json
{
  "coaches": {
    "5-M-White": "Coach Smith",
    "5-M-Red": ["Coach Jones", "jones@email.com"],
    "8-M-Blue": [["Coach Davis", "davis@email.com"], ["Coach Miller"]]
  }
}
```

Key format: `"grade-gender-color"` (e.g., `"5-M-White"` for 5th grade boys white)

Value formats:
- `"Name"` — just a name
- `["Name", "email"]` — name with mailto link
- `[["Name1", "email1"], ["Name2"]]` — multiple coaches

### Team Aliases

If leagues use different team naming conventions, aliases help match them for combined calendars:

```json
{
  "team_aliases": {
    "White": ["White 1", "Squirt White", "Milton White"],
    "Red": ["Red Team", "Travel Red"]
  }
}
```

This ensures teams named "Milton White 1" in one league and "Milton (White)" in another are grouped together.

### Practice Schedules

Add recurring practice schedules that appear in team calendars alongside games:

```json
{
  "season": {
    "start": "2026-01-01",
    "end": "2026-03-31",
    "blackout_dates": [
      {"start": "2026-02-16", "end": "2026-02-20", "reason": "February Vacation"},
      {"start": "2026-04-20", "end": "2026-04-24", "reason": "April Vacation"}
    ]
  },
  "practices": {
    "5-M-White": {
      "recurring": [
        {
          "day": "Tuesday",
          "time": "18:15",
          "duration": 90,
          "location": "170 Centre St, Milton, MA 02186",
          "notes": "Milton Academy ACC Gym"
        }
      ],
      "adhoc": [],
      "modifications": []
    }
  }
}
```

**Fields:**
- `season.start` / `season.end` — Date range for generating practices (YYYY-MM-DD)
- `season.blackout_dates` — School vacation weeks when recurring practices are skipped (ad-hoc practices still allowed)
- `recurring` — Weekly practices with day, time (24h), duration (minutes), location, and optional notes
- `adhoc` — One-off practices: `{"date": "2026-02-15", "time": "10:00", "duration": 60, "location": "..."}`
- `modifications` — Cancel or change specific dates (see below)

**Automatic skipping:** Recurring practices are automatically skipped when they:
- Fall during a blackout period (school vacations)
- Conflict with a scheduled game (within 1 hour before/after)

**Modifying specific practices:**

```json
{
  "modifications": [
    {"date": "2026-01-14", "action": "cancel"},
    {"date": "2026-01-21", "action": "modify", "time": "19:00", "location": "New Gym"}
  ]
}
```

#### Crowdsourced Schedule Updates

Practice schedules are **community-maintained**. Unlike games (which are pulled automatically from league websites), practice times are manually configured and rely on coaches/parents to keep them up to date.

If a practice time changes, please submit a change request so everyone's calendars stay accurate!

#### Request Practice Changes via GitHub Issues

Coaches can request practice changes using issue templates:

- **[Cancel a Practice](../../issues/new?template=cancel-practice.yml)** — Remove a practice for a specific date
- **[Modify a Practice](../../issues/new?template=modify-practice.yml)** — Change time, location, or duration
- **[Add a Practice](../../issues/new?template=add-practice.yml)** — Schedule an extra one-time practice

**Approval required:** After submitting, a maintainer must add the `approved` label. Once approved, the workflow automatically updates `teams.json` and regenerates calendars.

### Built-in Leagues

| League | ID | Website |
|--------|-----|---------|
| SSYBL | `ssybl` | ssybl.org |
| MetroWest Basketball | `metrowbb` | metrowestbball.com |

### Adding Other Leagues

Other leagues using the sportsite2 platform can be added via `other_leagues`:

```json
{
  "town_name": "Gloucester",
  "leagues": ["capeann"],
  "other_leagues": {
    "capeann": {
      "name": "Cape Ann",
      "origin": "https://capeannybl.com"
    },
    "cmybl": {
      "name": "CMYBL",
      "origin": "https://cmybl.org"
    }
  },
  "grades": [5, 6],
  "genders": ["M", "F"],
  "base_url": "https://YOUR-USERNAME.github.io/YOUR-REPO"
}
```

Known compatible leagues:
- Cape Ann Youth Basketball League (capeannybl.com)
- CMYBL (cmybl.org)
- RI MetroWest Basketball (rimetrowestbball.com)

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

## Troubleshooting

**Calendar not updating?**
- Most calendar apps refresh subscriptions every few hours to 24 hours
- The workflow runs hourly, so fresh data is always available
- You can force refresh in your calendar app's settings

**No games showing?**
- The schedule may not be posted yet on the league website
- Check that your `town_name` matches the dropdown value exactly
- Look at the workflow run logs for errors

**Town not found?**
- The `town_name` must match exactly what appears in the league website dropdown
- Check spelling and capitalization

**Coach name not showing for my team?**
- Coach names are manually configured — we've only added the ones we know about
- [Submit a GitHub issue](https://github.com/aknowles/ssbball/issues) with your team (grade, gender, color) and coach name to request an addition

**Want notifications when the workflow fails?**
- The workflow automatically creates a GitHub issue with the `workflow-failure` label when it fails
- Close the issue after fixing the problem; a new one will be created on the next failure

**Practice not showing on calendar?**
- Practice schedules are crowdsourced — they may not be configured for your team yet
- Check if your team has an entry in the `practices` section of `teams.json`
- [Submit a GitHub issue](../../issues/new?template=add-practice.yml) to request your team's practice schedule be added

**Practice showing during school vacation?**
- Check that the vacation week is listed in `season.blackout_dates`
- Only recurring practices are skipped during blackouts — ad-hoc practices are not affected

**Why is a practice missing on game day?**
- Practices within 1 hour of a scheduled game are automatically skipped
- If the practice should still occur, [add it as an ad-hoc practice](../../issues/new?template=add-practice.yml) for that specific date

**How do I update practice times?**
- [Cancel a practice](../../issues/new?template=cancel-practice.yml), [modify a practice](../../issues/new?template=modify-practice.yml), or [add a practice](../../issues/new?template=add-practice.yml)
- A maintainer will review and approve changes before they take effect

## Season Rollover

At the start of each new season, update the configuration for the new year using the rollover helper script:

```bash
# Preview what would change
python rollover_season.py 2027

# Apply changes to teams.json
python rollover_season.py 2027 --apply
```

The script automatically:
- Updates season dates (January 1 - March 31)
- Generates blackout dates for MA school calendar:
  - New Year's Day
  - Martin Luther King Jr. Day (3rd Monday in January)
  - February Vacation (Presidents Day week)
  - April Vacation (Patriots Day week)
- Clears old modifications and ad-hoc entries (use `--keep-adhoc` or `--keep-modifications` to preserve)

### What Stays the Same

- **Recurring practice schedules** — These typically don't change between seasons
- **Coach names** — Updated separately as needed
- **Team aliases** — League naming conventions stay consistent

### Manual Review

After running the rollover script, review:
1. **Practice locations** — Gyms may change between seasons
2. **Practice times** — Coaches may adjust schedules
3. **Blackout dates** — Add any town-specific dates not covered by the script

## Files

| File | Purpose |
|------|---------|
| `teams.json` | Town configuration (edit this!) |
| `scraper.py` | Main scraper script |
| `rollover_season.py` | Season rollover helper script |
| `.github/workflows/update-calendars.yml` | Hourly calendar update workflow |
| `.github/workflows/process-practice-changes.yml` | Processes practice change issues |
| `.github/ISSUE_TEMPLATE/` | Issue templates for practice changes |
| `docs/` | Generated output (GitHub Pages) |

## Issues & Feedback

Found a bug or have a suggestion? Please [open an issue](https://github.com/aknowles/ssbball/issues).

## Disclaimer

This is an unofficial community project. It is **not affiliated with, endorsed by, or connected to**:
- [Milton Travel Basketball](http://miltontravelbasketball.com)
- [MetroWest Basketball](https://metrowestbball.com)
- [SSYBL](https://ssybl.org)

Schedule data is provided for informational purposes only. Always verify game times and locations with official league sources before traveling.

## License

MIT
