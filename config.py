"""Configuration via environment variables (with sensible defaults).

Override these in your shell or in the cloud host's env settings:

    NTFY_TOPIC    -> your private ntfy topic name (REQUIRED for real pushes)
    NTFY_SERVER   -> ntfy server base url (default: https://ntfy.sh)
    ESPN_LEAGUE   -> ESPN soccer league slug (default: eng.1 for testing)
    POLL_SECONDS  -> how often to poll ESPN, in seconds (default: 3)
"""

import os

# ntfy.sh topic. Pick something long & unguessable so it's effectively private,
# e.g. "rushil-wc-goals-7f3a9". Anyone who knows the topic can read/post to it.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# ESPN soccer league slug.
#   eng.1   = Premier League (good for testing — frequently has live matches)
#   usa.1   = MLS
#   fifa.world = FIFA World Cup (set this once the tournament starts; the slug
#               only returns fixtures once they're loaded — confirm against the
#               live endpoint before relying on it).
ESPN_LEAGUE = os.environ.get("ESPN_LEAGUE", "eng.1")

# Poll interval. 3s beats TV comfortably while staying gentle on ESPN.
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))
