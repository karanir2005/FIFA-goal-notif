"""FIFA World Cup Goal Notifier.

Polls ESPN's free unofficial soccer API every few seconds, detects when a goal
is scored (a team's score goes up), and fires an instant push notification to
your phone via ntfy.sh — so you can flip on the TV before the broadcast catches
up.

Run:
    python notifier.py            # normal: poll forever
    python notifier.py --test     # send one fake goal push and exit
    python notifier.py --once     # do a single poll cycle and exit (debugging)
"""

import sys
import time
import logging
from datetime import datetime, timezone

import requests

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("goal-notifier")

ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/"
    "{league}/scoreboard"
)

# Per-match last-seen scores: {event_id: (home_score, away_score)}.
# Lives in memory; primed on first poll so a restart never re-notifies for
# goals that were already on the board.
_last_scores: dict[str, tuple[int, int]] = {}

# Match ids we've already sent a "starting soon" reminder for, so it only
# fires once per match even though we poll every few seconds.
_kickoff_alerted: set[str] = set()

KICKOFF_WARNING_MINUTES = 5


def fetch_scoreboard() -> list[dict]:
    """Return the list of event dicts from ESPN, or [] on any failure."""
    url = ESPN_URL.format(league=config.ESPN_LEAGUE)
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json().get("events", [])


def parse_event(event: dict) -> dict | None:
    """Pull the bits we care about out of one ESPN event.

    Returns None if the event isn't a normal two-team match we can read.
    """
    try:
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        # ESPN lists home first, away second, but it's marked explicitly too.
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")
        return {
            "id": event["id"],
            "home_name": home["team"]["shortDisplayName"],
            "away_name": away["team"]["shortDisplayName"],
            "home_id": home["team"]["id"],
            "away_id": away["team"]["id"],
            "home_score": int(home["score"]),
            "away_score": int(away["score"]),
            "state": event["status"]["type"]["state"],  # pre / in / post
            "clock": event["status"].get("displayClock", ""),
            "details": comp.get("details", []),
            "start_date": comp.get("startDate", ""),
        }
    except (KeyError, StopIteration, ValueError, IndexError):
        return None


def describe_goal(match: dict, scoring_team_id: str) -> str:
    """Find the most recent goal in the details for the team that just scored,
    and return a human string like 'Messi 23' (Goal - Penalty)'. Falls back to
    '' if details aren't populated yet."""
    goals = [
        d
        for d in match["details"]
        if d.get("scoringPlay")
        and str(d.get("team", {}).get("id")) == str(scoring_team_id)
    ]
    if not goals:
        return ""
    g = goals[-1]  # latest goal for that team
    scorer = ""
    athletes = g.get("athletesInvolved") or []
    if athletes:
        scorer = athletes[0].get("displayName", "")
    minute = g.get("clock", {}).get("displayValue", "")
    goal_type = g.get("type", {}).get("text", "Goal")
    bits = [b for b in (scorer, minute) if b]
    head = " ".join(bits)
    if goal_type and goal_type != "Goal":
        return f"{head} ({goal_type})".strip()
    return head


def send_ntfy(title: str, message: str) -> None:
    """Send a high-priority push to the ntfy topic."""
    if not config.NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set — would have pushed: %s | %s", title, message)
        return
    url = f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}"
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                # HTTP headers are Latin-1 only, so the title must be plain
                # ASCII/Latin-1. The "soccer" tag below makes ntfy render a ⚽
                # emoji on the notification automatically — that's where the
                # emoji comes from, not the title string.
                "Title": title.encode("latin-1", "ignore").decode("latin-1"),
                "Priority": "high",
                "Tags": "soccer",
            },
            timeout=10,
        )
        log.info("NTFY SENT  %s | %s", title, message)
    except requests.RequestException as e:
        log.error("Failed to send ntfy push: %s", e)


def send_discord(title: str, message: str) -> None:
    """Post the alert to a Discord channel via webhook, for group sharing."""
    if not config.DISCORD_WEBHOOK:
        return
    try:
        requests.post(
            config.DISCORD_WEBHOOK,
            json={"content": f"**{title}**\n{message}"},
            timeout=10,
        )
        log.info("DISCORD SENT  %s | %s", title, message)
    except requests.RequestException as e:
        log.error("Failed to send Discord push: %s", e)


def send_push(title: str, message: str) -> None:
    """Send the alert to every configured channel (ntfy + Discord)."""
    send_ntfy(title, message)
    send_discord(title, message)


def handle_match(match: dict, prime_only: bool) -> None:
    """Compare this match's score to last-seen and notify on an increase."""
    eid = match["id"]
    cur = (match["home_score"], match["away_score"])
    prev = _last_scores.get(eid)
    _last_scores[eid] = cur

    if prime_only or prev is None:
        # First time we see this match (or boot priming): record, don't notify.
        return

    home_up = cur[0] > prev[0]
    away_up = cur[1] > prev[1]
    if not (home_up or away_up):
        return  # no increase (score same, or went DOWN due to VAR — stay quiet)

    scoring_team_id = match["home_id"] if home_up else match["away_id"]
    goal_str = describe_goal(match, scoring_team_id)

    score_line = f"{match['home_name']} {cur[0]}-{cur[1]} {match['away_name']}"
    title = "GOAL!"  # ntfy adds the ⚽ via the "soccer" tag (see send_push)
    message = score_line if not goal_str else f"{score_line}  ({goal_str})"
    send_push(title, message)


def handle_kickoff_reminder(match: dict, prime_only: bool) -> None:
    """Send a one-time push when a 'pre' match is within KICKOFF_WARNING_MINUTES
    of starting."""
    eid = match["id"]
    if match["state"] != "pre" or eid in _kickoff_alerted or not match["start_date"]:
        return

    try:
        start = datetime.fromisoformat(match["start_date"].replace("Z", "+00:00"))
    except ValueError:
        return

    minutes_until = (start - datetime.now(timezone.utc)).total_seconds() / 60

    if prime_only:
        # Booting up close to kickoff shouldn't trigger a reminder for a
        # match that's already past the warning window (e.g. 2 min out) —
        # only arm matches we're seeing comfortably ahead of the window.
        if minutes_until <= KICKOFF_WARNING_MINUTES:
            _kickoff_alerted.add(eid)
        return

    if 0 <= minutes_until <= KICKOFF_WARNING_MINUTES:
        _kickoff_alerted.add(eid)
        send_push(
            "Kickoff soon",
            f"{match['home_name']} vs {match['away_name']} starts in "
            f"{round(minutes_until)} min",
        )


def poll_once(prime_only: bool = False) -> None:
    """One full poll cycle. Never raises — logs and returns on error."""
    try:
        events = fetch_scoreboard()
    except requests.RequestException as e:
        log.warning("ESPN fetch failed (will retry): %s", e)
        return

    live = 0
    for event in events:
        match = parse_event(event)
        if match is None:
            continue
        # Track in-progress matches; also prime finished/upcoming so we never
        # fire for a goal that happened while we were off.
        if match["state"] == "in":
            live += 1
        handle_match(match, prime_only=prime_only)
        handle_kickoff_reminder(match, prime_only=prime_only)

    if prime_only:
        log.info("Primed %d matches (%d live). Watching for goals…",
                 len(_last_scores), live)


def run_forever() -> None:
    log.info(
        "Goal notifier starting | league=%s | poll=%ss | ntfy_topic=%s | discord=%s",
        config.ESPN_LEAGUE,
        config.POLL_SECONDS,
        config.NTFY_TOPIC or "(unset!)",
        "on" if config.DISCORD_WEBHOOK else "(unset)",
    )
    # Prime current scores silently so a restart mid-match doesn't spam.
    poll_once(prime_only=True)
    while True:
        time.sleep(config.POLL_SECONDS)
        poll_once()


def main() -> None:
    if "--test" in sys.argv:
        send_push("GOAL!", "TEST — Argentina 1-0 France  (Messi 23')")
        return
    if "--once" in sys.argv:
        poll_once(prime_only=True)
        poll_once()
        return
    run_forever()


if __name__ == "__main__":
    main()
