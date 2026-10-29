# Deploying tradebot

One small cloud server, the bot in Docker (or systemd), paper first, then
small and live. Read the whole page once before you start.

## 1. The server

- **Size:** 2 vCPU, 4 GB RAM, 50–100 GB disk (recordings grow by tens of MB
  per day per pair). AWS Lightsail, DigitalOcean or Hetzner, about $10–40/month.
- **Region:** US East (Virginia / New York) for US stocks, Polymarket and the
  US crypto venues. Latency barely matters for ORB and prediction markets.
  Binance arbitrage would need a separate server in AWS Tokyo; only buy that
  once a backtest at realistic latency says it survives.
- **A static IP**, so you can lock every API key to it.
- **Don't use the server's location to get around geo-blocks.** Venue
  eligibility follows where *you* live, not the server.

## 2. Server setup (Ubuntu 24.04)

```bash
# as root, once
adduser --disabled-password tradebot
apt update && apt -y upgrade
apt -y install docker.io docker-compose-v2 git chrony unattended-upgrades ufw
usermod -aG docker tradebot

# Accurate time: exchanges reject signed requests with a skewed clock, and the
# market calendar needs it. chrony keeps it synced.
systemctl enable --now chrony && chronyc tracking

# No inbound ports except SSH: the bot only makes outbound connections.
ufw default deny incoming && ufw allow OpenSSH && ufw enable

# SSH keys only: in /etc/ssh/sshd_config set PasswordAuthentication no, then
systemctl restart ssh
dpkg-reconfigure -plow unattended-upgrades   # automatic security updates
```

## 3. Install the bot

```bash
# as tradebot
git clone <your repo> ~/tradebot && cd ~/tradebot/trading-bot
cp config/example.toml config/local.toml   # edit: venues, strategies, limits
cp .env.example .env && chmod 600 .env      # edit: API keys and alert settings
mkdir -p data && sudo chown 1000:1000 data  # the container runs as uid 1000

docker compose run --rm tradebot python -m tradebot check -c config/local.toml   # dry run, no orders
docker compose up -d --build
docker compose logs -f tradebot
```

**API keys:** trading permission only, **withdrawals disabled**, restricted to
the server's IP. Then a compromised server can't move money out.

## 4. Alerts and the dead-man's switch

- **Telegram:** message @BotFather, `/newbot`, copy the token. Send your new bot
  any message, then open `https://api.telegram.org/bot<token>/getUpdates`. The
  `chat.id` there is `TELEGRAM_CHAT_ID`.
- **Slack:** create an incoming webhook, then set `SLACK_WEBHOOK_URL`.
- **healthchecks.io** (free): create a check with period 1 minute, grace 3
  minutes, and connect it to your email or phone. Set `HEALTHCHECK_URL` to its
  ping URL. The bot pings only while its trading loop is actually ticking, so a
  crash, a hang or a dead server all page you. Halts ping `/fail`.

You'll get alerts for: halts, uneven fills being unwound, a venue's market
data down (and back), position mismatches found at startup, starting while
halted, and a daily summary at `daily_summary_time`.

## 5. Restarts, crashes and halts

Docker (`restart: unless-stopped`) and systemd (`Restart=on-failure`) restart
a crashed bot. Saved state (`data/state.json`) makes that safe:

- The **daily loss limit** keeps its start-of-day baseline across restarts.
- **Open positions** are restored. At startup they're checked against the
  broker: where the broker holds less (closed by hand, filled while down),
  the bot shrinks its tracked position and alerts you. Extra shares at the
  broker are left alone, since they may be yours.
- A position that missed its end-of-day close (the bot was down at 15:55) is
  closed at the next open.
- **A halt survives restarts.** Auto-restart can't lift it; only you can:

```bash
docker compose exec tradebot python -m tradebot state -c config/local.toml               # what happened?
docker compose exec tradebot python -m tradebot state -c config/local.toml --clear-halt  # after you've checked
docker compose restart tradebot
```

`--reset --yes` deletes all saved state (paper balances go back to the config's).
A lock file stops a second copy of the bot from ever running against the
same data directory.

**Emergency stop:** `touch data/KILL` halts trading on the next tick (the
example config's `kill_switch_file`; `data/` is the mounted volume, so it works
from the host). Like any halt it persists: delete the file *and* run
`--clear-halt` to resume. If you can't reach the server, revoke the API keys
at the exchange.

## 6. Updating

US stocks: only outside 9:30–16:00 ET. Crypto: pick a quiet hour.

```bash
cd ~/tradebot && git pull
cd trading-bot && docker compose run --rm tradebot python -m tradebot check -c config/local.toml
docker compose up -d --build && docker compose logs -f tradebot   # watch for "restored state"
```

Dependencies are pinned in `requirements.lock`, so an upstream release can't
change behavior without you noticing. To upgrade, regenerate it, run the
tests, and paper trade the result first.

## 7. Backups

Recordings can't be collected again, and the state file is the bot's memory.

```bash
# crontab -e (as tradebot): nightly at 03:17 UTC, with rclone configured for S3/B2
17 3 * * * rclone copy ~/tradebot/trading-bot/data remote:tradebot-backup --exclude tradebot.lock
```

## 8. Without Docker (systemd)

```bash
sudo mkdir -p /opt/tradebot && sudo chown tradebot: /opt/tradebot
git clone <your repo> /opt/tradebot/src && ln -s /opt/tradebot/src/trading-bot /opt/tradebot/trading-bot
python3.13 -m venv /opt/tradebot/.venv
/opt/tradebot/.venv/bin/pip install -r /opt/tradebot/trading-bot/requirements.lock
/opt/tradebot/.venv/bin/pip install --no-deps /opt/tradebot/trading-bot
sudo cp /opt/tradebot/trading-bot/deploy/tradebot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tradebot
journalctl -u tradebot -f
```

The unit restarts on crashes, gives up after 5 crashes in 10 minutes rather
than crash-loop, and runs with a read-only filesystem except `data/`.

## 9. Rollout

1. **Paper on the server, recording on**, for 2–4 weeks. Watch it survive
   reconnects, restarts, a holiday and a half-day. Restart it deliberately
   mid-trade at least once.
2. **Backtest** what it recorded, at realistic latency.
3. **Live, tiny:** `engine.mode = "live"` plus the `--live` command in
   `docker-compose.yml`, with `max_order_notional = 50` and `max_daily_loss = 25`,
   on the one strategy with the best record.
4. Raise limits slowly, on live results only.
