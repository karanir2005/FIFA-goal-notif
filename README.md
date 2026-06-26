# FIFA World Cup Goal Notifier ⚽📲

Get an instant phone push **the moment a goal is scored** — before your TV catches up — so you
can look up in time to watch it live.

It polls [ESPN's free soccer API](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard)
every ~3 seconds, detects when a score goes up, and fires a push via [ntfy.sh](https://ntfy.sh)
and/or Discord.

## Quick start (local test)

1. **Install the ntfy app** on your phone (iOS App Store / Google Play).
2. In the app, **subscribe to a topic** — pick a long, unguessable name, e.g.
   `rushil-wc-goals-7f3a9`. (Anyone who knows the topic can read it, so make it random.)
3. On your computer:
   ```bash
   pip install -r requirements.txt
   # Windows PowerShell:
   $env:NTFY_TOPIC = "rushil-wc-goals-7f3a9"
   python notifier.py --test      # should buzz your phone immediately
   ```
4. Watch real goals (pre-WC, against the Premier League):
   ```bash
   python notifier.py             # runs forever, polling eng.1
   ```

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `NTFY_TOPIC` | _(none)_ | Your ntfy topic. **Required** for real pushes. |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server base URL. |
| `DISCORD_WEBHOOK` | _(none)_ | Discord channel webhook for group alerts. Optional. |
| `ESPN_LEAGUE` | `eng.1` | ESPN league slug. Use `fifa.world` for the World Cup. |
| `POLL_SECONDS` | `3` | How often to poll ESPN. |

## Run it 24/7 in the cloud (laptop can be off)

This is an **outbound-only worker** — no web server, no open ports. Easiest free host is Fly.io:

```bash
fly launch --copy-config --now
fly secrets set NTFY_TOPIC=rushil-wc-goals-7f3a9
fly secrets set ESPN_LEAGUE=eng.1     # switch to fifa.world for the WC
```

(Render "Background Worker" or Railway work too — point them at the `Dockerfile`.)

## During the World Cup

Just change one env var to the World Cup slug and redeploy — no code change:

```bash
fly secrets set ESPN_LEAGUE=fifa.world
```

> The exact WC slug only resolves once fixtures are loaded. Confirm by opening
> `https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard` in a browser and
> checking the `events` array is populated.

## How it avoids false / duplicate alerts

- **Each match is tracked independently**, keyed by ESPN's own event id, so two matches kicking
  off at the same time never share or collide on state.
- **Goals are tracked by per-team count, not by a goal's own fields** — a goal's clock/scorer
  details can get backfilled by ESPN a poll or two after it first appears (more likely when
  several matches are live at once), and a naive identity match would treat that as a second,
  new goal. Counting only fires when a team's goal list actually grows.
- **VAR-disallowed goals get their own alert** ("Goal disallowed") instead of staying silent.
- **Primes current scores silently on startup**, so a restart mid-match never re-fires for goals
  already on the board.
- The poll loop **never crashes** on a bad response or network blip — it logs and retries.

## Commands

```bash
python notifier.py          # poll forever (normal use)
python notifier.py --test   # send one fake goal push and exit
python notifier.py --once   # single poll cycle, for debugging
```
