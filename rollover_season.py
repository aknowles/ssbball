#!/usr/bin/env python3
"""
Season Rollover Helper Script

Generates new season dates and blackout dates for a given year,
and clears old modifications/adhoc entries from teams.json.

Usage:
    python rollover_season.py 2027              # Preview changes
    python rollover_season.py 2027 --apply      # Apply changes to teams.json

The script calculates:
- Season dates: January 1 to March 31 of the target year
- Blackout dates:
  - New Year's Day (January 1)
  - Martin Luther King Jr. Day (3rd Monday in January)
  - February Vacation (Presidents Day week - week containing 3rd Monday in February)
  - April Vacation (Patriots Day week - week containing 3rd Monday in April)
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Find the nth occurrence of a weekday in a given month.

    Args:
        year: Target year
        month: Target month (1-12)
        weekday: Day of week (0=Monday, 6=Sunday)
        n: Which occurrence (1=first, 2=second, etc.)

    Returns:
        The date of the nth occurrence
    """
    first_day = date(year, month, 1)
    # Days until first occurrence of target weekday
    days_until = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=days_until)
    # Add weeks to get nth occurrence
    return first_occurrence + timedelta(weeks=n - 1)


def get_vacation_week(holiday_date: date) -> tuple[date, date]:
    """Get the Monday-Friday week containing a holiday.

    Typically MA school vacations run the full week of the holiday.
    """
    # Find Monday of the week
    monday = holiday_date - timedelta(days=holiday_date.weekday())
    # Friday of the same week
    friday = monday + timedelta(days=4)
    return monday, friday


def generate_season_dates(year: int) -> dict:
    """Generate season start and end dates.

    Basketball season typically runs January through March.
    """
    return {
        "start": f"{year}-01-01",
        "end": f"{year}-03-31"
    }


def generate_blackout_dates(year: int) -> list[dict]:
    """Generate blackout dates for Massachusetts school calendar.

    Includes:
    - New Year's Day (January 1)
    - Martin Luther King Jr. Day (3rd Monday in January)
    - February Vacation (Presidents Day week)
    - April Vacation (Patriots Day week)
    """
    blackouts = []

    # New Year's Day
    new_years = date(year, 1, 1)
    blackouts.append({
        "start": new_years.isoformat(),
        "end": new_years.isoformat(),
        "reason": "New Year's Day"
    })

    # Martin Luther King Jr. Day (3rd Monday in January)
    mlk_day = nth_weekday_of_month(year, 1, 0, 3)  # 0 = Monday
    blackouts.append({
        "start": mlk_day.isoformat(),
        "end": mlk_day.isoformat(),
        "reason": "Martin Luther King Jr. Day"
    })

    # February Vacation (Presidents Day week - 3rd Monday in February)
    presidents_day = nth_weekday_of_month(year, 2, 0, 3)
    feb_vac_start, feb_vac_end = get_vacation_week(presidents_day)
    blackouts.append({
        "start": feb_vac_start.isoformat(),
        "end": feb_vac_end.isoformat(),
        "reason": "February Vacation (Presidents Day Week)"
    })

    # April Vacation (Patriots Day week - 3rd Monday in April)
    patriots_day = nth_weekday_of_month(year, 4, 0, 3)
    apr_vac_start, apr_vac_end = get_vacation_week(patriots_day)
    blackouts.append({
        "start": apr_vac_start.isoformat(),
        "end": apr_vac_end.isoformat(),
        "reason": "April Vacation (Patriots Day Week)"
    })

    return blackouts


def clear_old_entries(practices: dict) -> dict:
    """Clear old modifications and adhoc entries from all teams.

    These are date-specific and should be cleared between seasons.
    Recurring schedules are preserved since they usually don't change.
    """
    cleaned = {}
    for team, schedule in practices.items():
        cleaned[team] = {
            "recurring": schedule.get("recurring", []),
            "adhoc": [],  # Clear adhoc
            "modifications": []  # Clear modifications
        }
    return cleaned


def main():
    parser = argparse.ArgumentParser(
        description="Generate season dates and blackout dates for a new year"
    )
    parser.add_argument(
        "year",
        type=int,
        help="Target year for the new season (e.g., 2027)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to teams.json (without this flag, just preview)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("teams.json"),
        help="Path to teams.json (default: teams.json)"
    )
    parser.add_argument(
        "--keep-adhoc",
        action="store_true",
        help="Keep existing adhoc practices instead of clearing them"
    )
    parser.add_argument(
        "--keep-modifications",
        action="store_true",
        help="Keep existing modifications instead of clearing them"
    )

    args = parser.parse_args()

    # Generate new dates
    season_dates = generate_season_dates(args.year)
    blackout_dates = generate_blackout_dates(args.year)

    print(f"Season Rollover for {args.year}")
    print("=" * 40)
    print()
    print("Season Dates:")
    print(f"  Start: {season_dates['start']}")
    print(f"  End:   {season_dates['end']}")
    print()
    print("Blackout Dates:")
    for blackout in blackout_dates:
        if blackout['start'] == blackout['end']:
            print(f"  {blackout['start']}: {blackout['reason']}")
        else:
            print(f"  {blackout['start']} to {blackout['end']}: {blackout['reason']}")
    print()

    if not args.config.exists():
        print(f"Error: {args.config} not found")
        sys.exit(1)

    # Load current config
    with open(args.config) as f:
        config = json.load(f)

    # Preview or apply changes
    if args.apply:
        # Update season dates
        if "season" not in config:
            config["season"] = {}
        config["season"]["start"] = season_dates["start"]
        config["season"]["end"] = season_dates["end"]
        config["season"]["blackout_dates"] = blackout_dates

        # Clear old entries from practices
        if "practices" in config:
            for team, schedule in config["practices"].items():
                if not args.keep_adhoc:
                    schedule["adhoc"] = []
                if not args.keep_modifications:
                    schedule["modifications"] = []

        # Write updated config
        with open(args.config, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

        print(f"Updated {args.config}")
        if not args.keep_adhoc:
            print("  - Cleared adhoc practices")
        if not args.keep_modifications:
            print("  - Cleared modifications")
    else:
        print("Preview mode - no changes made.")
        print(f"Run with --apply to update {args.config}")
        print()

        # Show what would be cleared
        if "practices" in config:
            teams_with_adhoc = []
            teams_with_mods = []
            for team, schedule in config["practices"].items():
                if schedule.get("adhoc"):
                    teams_with_adhoc.append(f"{team} ({len(schedule['adhoc'])} entries)")
                if schedule.get("modifications"):
                    teams_with_mods.append(f"{team} ({len(schedule['modifications'])} entries)")

            if teams_with_adhoc or teams_with_mods:
                print("Entries that would be cleared:")
                if teams_with_adhoc:
                    print(f"  Adhoc practices: {', '.join(teams_with_adhoc)}")
                if teams_with_mods:
                    print(f"  Modifications: {', '.join(teams_with_mods)}")
                print()
                print("Use --keep-adhoc or --keep-modifications to preserve them.")


if __name__ == "__main__":
    main()
