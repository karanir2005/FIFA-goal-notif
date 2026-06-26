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

# Per-match set of goal keys we've already notified about, e.g.
# {event_id: {(team_id, clock_seconds, athlete_id), ...}}. A goal under VAR
# review can make the score dip and then climb back to the same number a
# few minutes later — comparing raw scores alone would treat that as a new
# goal and notify twice. Keying on the goal's own identity instead means a
# reinstated goal is recognized as "already seen" and stays quiet.
_notified_goals: dict[str, set[tuple]] = {}

# Per-match score total (home + away) as of the last poll, used to confirm a
# goal disappearing from the details feed actually corresponds to the
# scoreboard number dropping — rather than the feed just being mid-update.
_last_totals: dict[str, int] = {}

# Match ids we've already sent a "starting soon" reminder for, so it only
# fires once per match even though we poll every few seconds.
_kickoff_alerted: set[str] = set()

KICKOFF_WARNING_MINUTES = 5

# Emoji per ESPN goal type.text, prefixed onto the message body so Discord
# (which has no tag/emoji concept) shows the right glyph.
GOAL_EMOJI = {
    "goal": "⚽",
    "penalty": "🥅",
    "own goal": "🥴",
    "header": "👑",
    "free-kick": "🎯",
    "free kick": "🎯",
}
DISALLOWED_EMOJI = "🚫"
KICKOFF_EMOJI = "⏰"

# ntfy renders its own emoji glyph in front of the title based on the "Tags"
# header (an ntfy emoji-shortcode, not a literal char) — so this maps the
# same goal type to ntfy's shortcode instead of duplicating GOAL_EMOJI's
# literal char, which would otherwise show twice (once from ntfy's tag emoji,
# once from the literal emoji we put in the body).
NTFY_TAG = {
    "goal": "soccer",
    "penalty": "goal_net",
    "own goal": "woozy_face",
    "header": "crown",
    "free-kick": "dart",
    "free kick": "dart",
}
DISALLOWED_NTFY_TAG = "no_entry_sign"
KICKOFF_NTFY_TAG = "alarm_clock"


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


def goals_for_team(match: dict, team_id: str) -> list[dict]:
    """All scoring-play detail entries for one team, in feed order."""
    return [
        d
        for d in match["details"]
        if d.get("scoringPlay")
        and str(d.get("team", {}).get("id")) == str(team_id)
    ]


def goal_key(goal: dict) -> tuple:
    """A stable identity for a goal that survives VAR back-and-forth: the
    score doesn't uniquely identify a goal, but (team, clock) does. Scorer
    name/id is deliberately excluded — ESPN sometimes posts a goal before
    athlete details are backfilled, and including it would make the key
    change between polls for the same goal (looking like a new goal, or
    worse, making the original key look "revoked")."""
    return (
        str(goal.get("team", {}).get("id")),
        goal.get("clock", {}).get("value"),
    )


def describe_goal(goal: dict) -> str:
    """Human string like 'Messi 23' (Penalty)' for one goal detail."""
    scorer = ""
    athletes = goal.get("athletesInvolved") or []
    if athletes:
        scorer = athletes[0].get("displayName", "")
    minute = goal.get("clock", {}).get("displayValue", "")
    goal_type = goal.get("type", {}).get("text", "Goal")
    # ESPN sometimes prefixes the type with "Goal - " (e.g. "Goal - Free-kick")
    # even though it's already obviously a goal — drop the redundant prefix.
    goal_type = goal_type.removeprefix("Goal - ")
    bits = [b for b in (scorer, minute) if b]
    head = " ".join(bits)
    if goal_type and goal_type != "Goal":
        return f"{head} ({goal_type})".strip()
    return head


def send_ntfy(title: str, message: str, tag: str = "soccer") -> None:
    """Send a high-priority push to the ntfy topic. `tag` is an ntfy
    emoji-shortcode (not a literal emoji char) — ntfy renders it once in
    front of the title itself, so the message body shouldn't also carry a
    literal emoji or it'll show twice."""
    if not config.NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set — would have pushed: %s | %s", title, message)
        return
    url = f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}"
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                # HTTP headers are Latin-1 only, so the title must be plain ASCII/Latin-1.
                "Title": title.encode("latin-1", "ignore").decode("latin-1"),
                "Priority": "high",
                "Tags": tag,
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


def send_push(title: str, message: str, emoji: str = "", tag: str = "soccer") -> None:
    """Send the alert to every configured channel (ntfy + Discord).

    `message` should be emoji-free; `emoji` (a literal char) is prefixed for
    Discord, while ntfy gets `tag` (its emoji-shortcode) so each channel
    shows the glyph exactly once instead of doubling up."""
    send_ntfy(title, message, tag=tag)
    send_discord(title, f"{emoji} {message}".strip())


def handle_match(match: dict, prime_only: bool) -> None:
    """Diff this match's goal list against what we've already notified for,
    so VAR reviews (which can make ESPN's score dip and climb back to the
    same number) don't cause the same goal to fire twice, and so a goal that
    gets disallowed after the fact gets its own "revoked" push."""
    eid = match["id"]
    seen = _notified_goals.setdefault(eid, set())
    cur = (match["home_score"], match["away_score"])
    was_primed = eid in _last_scores
    _last_scores[eid] = cur

    if prime_only or not was_primed:
        # First time we see this match (or boot priming): record every goal
        # already on the board as "seen" so we don't fire for old goals, but
        # don't notify.
        for team_id in (match["home_id"], match["away_id"]):
            for g in goals_for_team(match, team_id):
                seen.add(goal_key(g))
        _last_totals[eid] = cur[0] + cur[1]
        return

    # Walk every goal in chronological order so that if two goals land in the
    # same poll cycle, each push shows the score as it stood right after that
    # goal — not the match's final current score reused for both.
    all_goals = [
        (g, match["home_id"])
        for g in goals_for_team(match, match["home_id"])
    ] + [
        (g, match["away_id"])
        for g in goals_for_team(match, match["away_id"])
    ]
    all_goals.sort(key=lambda pair: pair[0].get("clock", {}).get("value") or 0)

    home_running, away_running = 0, 0
    for g, team_id in all_goals:
        if team_id == match["home_id"]:
            home_running += 1
        else:
            away_running += 1
        key = goal_key(g)
        if key in seen:
            continue
        seen.add(key)
        goal_str = describe_goal(g)
        score_line = f"{match['home_name']} {home_running}-{away_running} {match['away_name']}"
        goal_type = g.get("type", {}).get("text", "Goal").removeprefix("Goal - ").lower()
        title = "GOAL!"  # kept ASCII — ntfy's Title header can't carry emoji (Latin-1 only)
        body = score_line if not goal_str else f"{score_line}  ({goal_str})"
        send_push(
            title,
            body,
            emoji=GOAL_EMOJI.get(goal_type, GOAL_EMOJI["goal"]),
            tag=NTFY_TAG.get(goal_type, NTFY_TAG["goal"]),
        )

    # A goal we'd already notified for can later disappear from the details
    # list while ESPN's feed is still settling mid-review — that alone isn't
    # reliable signal. Only treat it as an actual disallowed goal once the
    # scoreboard number itself has dropped from what we last saw.
    still_present = set()
    for team_id in (match["home_id"], match["away_id"]):
        still_present.update(goal_key(g) for g in goals_for_team(match, team_id))
    revoked = seen - still_present
    prev_total = _last_totals.get(eid, cur[0] + cur[1])
    cur_total = cur[0] + cur[1]
    if revoked and cur_total < prev_total:
        seen -= revoked
        score_line = f"{match['home_name']} {cur[0]}-{cur[1]} {match['away_name']}"
        send_push(
            "Goal disallowed",
            f"VAR review — {score_line}",
            emoji=DISALLOWED_EMOJI,
            tag=DISALLOWED_NTFY_TAG,
        )
    _last_totals[eid] = cur_total


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
            emoji=KICKOFF_EMOJI,
            tag=KICKOFF_NTFY_TAG,
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
        send_push("GOAL!", "TEST — Argentina 1-0 France  (Messi 23')", emoji=GOAL_EMOJI["goal"])
        return
    if "--once" in sys.argv:
        poll_once(prime_only=True)
        poll_once()
        return
    run_forever()


if __name__ == "__main__":
    main()
