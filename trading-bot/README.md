# tradebot

A multi-venue, multi-strategy trading bot for **US equities (Alpaca)**, **crypto
(Binance, Kraken and 100+ others via ccxt)** and **prediction markets
(Polymarket)**. It runs five strategies: cross-exchange arbitrage, triangular
arbitrage, prediction-market basket arbitrage, opening range breakout and
mean reversion.

It is built so that you can go **demo → paper → small live → scale** without
rewriting anything. The same strategy code runs at every stage, and every order
passes the risk manager.

```bash
cd trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"

python -m tradebot run -c config/demo.toml --ticks 150  # offline, no keys
pytest                                                  # 88 tests
```

## Architecture

```
 WebSockets   ┌ MarketDataHub ┐   ┌──────────────── Engine (engine.py) ────────────────┐
 (or REST  ──►│ latest books, │──►│ wake on update ──► strategies ──► risk ──► execute │──► venues
  polling)    │ reconnects    │   │ (changed books     (pure: return  (limits, (legs in│
              └───────────────┘   │  only)             TradePlans)   kill sw.) parallel,│
                                  │                                           unwind)  │
                                  └──────────────────────┬────────────────────────────┘
                                                         ▼
                                            journal (JSONL audit of every decision)
```

| Layer | Files | Job |
|---|---|---|
| Models | `models.py` | `OrderBook`, `OrderRequest`, `TradePlan`… All money is `Decimal`. |
| Venues | `exchanges/` | One `Exchange` interface. `PaperExchange` simulates fills against any data source. |
| Strategies | `strategies/` | Pure decision functions: snapshot in, `TradePlan`s out. They never place orders. |
| Risk | `risk/` | Per-order and per-position caps, daily loss halt, order-rate limit, stale-data guard, market-hours gate, kill switch. |
| Market hours | `market_hours.py` | NYSE sessions, holidays and early closes, computed from the exchange's rules. |
| Market data | `marketdata.py` | One stream per venue. Keeps the latest books, reconnects with backoff, tracks feed health. |
| Engine | `engine.py` | Event-driven loop. Fires a plan's legs concurrently and **unwinds uneven fills** (legging risk). |
| Recording | `recording.py` | Writes every book update to daily gzip files; reads them back. |
| Backtest | `backtest.py` | Replays recordings through the real engine on a simulated clock, with a latency model. |
| State | `state.py` | Saves halts, the daily-loss baseline, positions and paper balances; single-instance lock. |
| Alerts | `alerts.py` | Telegram / Slack alerts, daily summary, dead-man's-switch pings. |
| Config | `config.py`, `config/*.toml` | Everything is declared in TOML. Secrets come from env vars. |

## Streaming market data

With `engine.data_mode = "stream"` (the default), each venue pushes order-book
updates and the engine reacts within milliseconds instead of waiting for a
poll. Only strategies whose books changed are re-run.

| Venue | Transport | Notes |
|---|---|---|
| Binance, Kraken, Coinbase, OKX… | ccxt.pro WebSockets (bundled with ccxt) | Venues ccxt.pro doesn't cover fall back to REST polling |
| Alpaca | Market-data WebSocket, quotes channel | One connection per account on the free plan; all symbols share it |
| Polymarket | CLOB market channel | `book` snapshots plus `price_change` deltas, applied to a local copy |
| Synthetic | In-process | A new book every market step |

How it stays safe:
- **Reconnects** with exponential backoff (1s up to 30s). Any adapter error
  ends the stream and the hub reconnects that venue.
- **Staleness is feed health, not quote age.** A quiet Polymarket book can be
  minutes old but still current. Heartbeats (WebSocket pongs, idle-but-open
  sockets) count as "alive", so `max_book_age_s` rejects trades only when a
  feed has actually gone silent.
- **Balances** are cached and refreshed after every trade and every
  `balance_refresh_s`, so REST rate limits aren't spent on every update.
- Bursts of updates are coalesced (`min_tick_interval_s`), and repeated
  rejections are logged once, not on every update.

`data_mode = "poll"` keeps the original REST loop. `tradebot check` always
uses REST.

## Running it for real

See **[DEPLOY.md](DEPLOY.md)**: server setup, Docker Compose or systemd,
alerts and the dead-man's switch, updates, backups and the rollout plan.

- **Restarts are safe.** State is saved after every trade and every 10s, and
  restored at startup. Open positions are checked against the broker before
  the first trade.
- **Halts survive restarts**, so auto-restart can't lift them. Clear one with
  `tradebot state -c <config> --clear-halt`.
- **One instance only:** a lock file stops a second copy trading the same account.
- **Alerts** go out on halts, unwinds, venue outages and position mismatches,
  plus a daily summary. A healthcheck ping, sent only while the trading loop
  is alive, catches the bot dying.

## Recording and backtesting

```bash
python -m tradebot record   -c config/local.toml                   # collect data, no trading
python -m tradebot backtest -c config/local.toml --latency-ms 100   # replay it through the strategies
python -m tradebot backtest -c config/local.toml --start 2026-10-01 --end 2026-10-28
```

**Recording** writes every order-book update the bot receives to
`data/recordings/YYYY-MM-DD.jsonl.gz`, plus once-a-second "feed alive"
heartbeats so quiet books replay correctly. Compression runs off the event
loop, so it never slows trading. A crash loses at most the last 5 seconds,
and earlier data stays readable. Set `[recording] enabled = true` to record
while the bot trades, or run `tradebot record` to only collect data.
Roughly: one liquid crypto pair at 10 levels is tens of MB per day
compressed. Use `min_interval_s` to downsample.

**Backtesting** replays the recording through the *same* engine, strategies
and risk manager that trade live. Only the clock is simulated and the venues
are paper. No network, API keys or venue SDKs are needed. Updates are grouped
into ticks the way the live loop groups them, market hours and daily loss
limits follow simulated time, and the warm-up rule applies.

**Latency** is the setting that matters most: `--latency-ms 100` (the
default) fills each order against the book as it was 100ms after the
decision. With `--latency-ms 0`, orders fill against the very book that
triggered them, which flatters everything. The test suite has a fleeting
arbitrage that makes +$2 with instant fills and loses $1 to an unwind at
100ms. Run your strategies at a realistic latency for your setup (50–300ms
from a home connection) before believing any number.

The report shows P&L, max drawdown, annualized Sharpe ratio (from daily P&L),
fees and borrow costs, plans and fills per strategy, and daily P&L. It's saved
with `trades.csv` and `equity.csv` to `data/backtests/<timestamp>/`.

**Still not modelled:** queue position and market impact. Your orders never
move recorded prices, so results for size that is large relative to the book
are optimistic. Recordings also only contain what the bot subscribed to, so
add symbols to the config *before* recording the data you want.

## Strategies

| Kind | Where | Idea | Main risk |
|---|---|---|---|
| `cross_exchange_arb` | crypto | Buy on venue A, sell on venue B at the same moment, from inventory pre-funded on both | One leg fills and the other doesn't. Inventory drifts to one side. |
| `triangular_arb` | one crypto venue | USDT→BTC→ETH→USDT where the cross rate is mispriced | Edges are tiny and contested. Fees decide everything. |
| `prediction_market_arb` | Polymarket | Outcomes are exhaustive and exactly one pays $1, so asks summing below $1 is locked-in profit | An outcome missing from the basket. Resolution or oracle disputes. Capital locked until resolution. |
| `opening_range_breakout` | stocks | Record the first N minutes' high and low, buy the break above (or short the break below), stop at the other side, flatten before the close | **Directional.** False breakouts are common; the stop and the daily flatten cap the damage. |
| `mean_reversion` | stocks | Buy at z ≤ −entry, exit when price reverts | **Directional market risk.** It is not arbitrage. Backtest it first. |

Every arbitrage strategy sizes trades by walking order-book depth, after fees.
It picks the size with the largest absolute profit that still clears
`min_edge_bps`.

### Opening range breakout

The classic first day-trading bot. Each trading day, for each symbol:

1. **Range:** track the high and low of the first `range_minutes` (5–30 is typical).
2. **Entry:** buy when the ask clears the range high plus `breakout_buffer_bps`.
   With `allow_short = true`, also sell short when the bid breaks below the
   range low. Entries only until `entry_cutoff_minutes` after the open. Days
   whose range is narrower than `min_range_bps` or wider than `max_range_bps`
   are skipped.
3. **Size by risk:** shares = `risk_per_trade` / |entry − stop|, so a
   stopped-out trade loses about `risk_per_trade`. Capped by
   `max_position_notional` and cash.
4. **Exit:** the stop is the other side of the range (the low for a long, the
   high for a short). The target is `take_profit_r` × risk. Positions always
   flatten `flatten_minutes_before_close` before the close (so 12:55 on a
   1:00 pm early close). Nothing is held overnight.

One round trip per symbol per day, on whichever side breaks first. If the bot
starts after the opening range has ended, it sits that day out rather than
guess the range.

## Short selling

Shorting is **off unless a venue opts in** with `allow_short = true`.

- **Risk guard (paper and live):** on any other venue, a sell that would take a
  position below zero is refused. This matters live: on an Alpaca margin
  account, a strategy bug that oversells would otherwise silently open a short.
- **Position limits are symmetric.** `max_position_notional` caps short
  exposure like long exposure. Trades that shrink a position (stops, covers)
  are never blocked by the cap.
- **Paper simulation** models a US margin account:
  - selling more than you hold opens a short, and the proceeds are credited to cash;
  - **Reg T initial margin**: cash must cover 150% of the value of all open
    shorts (`short_initial_margin = 0.5`). Orders that would breach it are cut
    down (IOC) or rejected (FOK). Long holdings don't count as collateral,
    which is stricter than a real broker;
  - a **borrow fee** accrues on short value at `borrow_rate_annual`. About 0.3%
    is typical for easy-to-borrow names; hard-to-borrow stocks can cost 10–100%+;
  - buying covers the short first;
  - only assets quoted in the venue's own currency can be shorted, so crypto
    cross pairs can't create accidental shorts.
- **Not modelled:** locate and borrow availability (every symbol is assumed
  shortable), forced buy-ins, and maintenance-margin liquidation. The risk
  manager's position and daily-loss limits are meant to stop you well before
  a margin call.
- **Live:** shorting on Alpaca needs a margin account, and the stock must be
  "easy to borrow". Real short losses are unbounded, so keep
  `max_position_notional` tight.

## Market hours

`market_hours.py` computes NYSE regular sessions, all exchange holidays and
the 1:00 pm early closes from the exchange's own rules. It needs no data files
or API calls, handles daylight saving, and is tested against published NYSE
calendars.

- Venues with `market_hours = "us_equities"` (the default for `kind = "alpaca"`)
  get a **hard gate in the risk manager**: any order outside the regular
  session is refused, whatever the strategy does.
- Closures nobody can schedule (national days of mourning, like 2025-01-09)
  go in `[market_calendar] extra_holidays`.
- `market_hours = "demo"` runs short back-to-back sessions so intraday
  strategies can be watched end to end at any hour. The demo config uses it.
- Only regular hours (9:30–16:00 ET) are modelled, not pre-market or after-hours.

Both stock strategies (ORB and mean reversion) only ever close positions they
opened themselves, so they never touch other holdings in the same account.

## The road from here: what to do, in order

### Phase 1: Learn the system (now)
1. Run the demo and read `data/demo-journal.jsonl`. Every plan, order and
   rejection is in there.
2. Read `strategies/cross_exchange_arb.py` end to end. Every strategy follows its pattern.
3. Run `pytest`. `tests/test_engine.py` shows how unwinding protects you.

### Phase 2: Paper trade on real market data (1–4 weeks)
1. `cp config/example.toml config/local.toml` and `cp .env.example .env`.
2. Get keys. Paper mode only needs market data, which ccxt and Polymarket
   serve without keys. Alpaca needs a free paper-account key even for data.
3. `python -m tradebot check -c config/local.toml` takes one dry snapshot and
   shows what each strategy *would* do.
4. `python -m tradebot run -c config/local.toml` paper trades on live books.
5. Watch how often each strategy finds an edge, and how big it is. **Most
   naive arbs disappear once you account for real fees, latency and depth.**
   That finding is useful, not a failure.

### Phase 3: Build the things paper trading can't tell you
- **Record weeks of data and backtest** (`tradebot record`, `tradebot backtest`).
  Tune parameters on one period and confirm on a different one; parameters
  tuned and judged on the same data look better than they are.
- **Stream your own fills and orders** (user-data WebSockets) instead of
  checking order state over REST after each submit.
- **Inventory rebalancing** for cross-exchange arbitrage, since it piles up
  coins on one venue and cash on the other.
- **Alerts** (Telegram, Slack or email) on halts, unwinds and daily P&L.

### Phase 4: Go live, small
1. Set `engine.mode = "live"` **and** pass `--live`. Both are required, by design.
2. Start with limits you'd be fine losing entirely: `max_order_notional = 50`,
   `max_daily_loss = 25`.
3. API keys: **trade-only, withdrawals disabled, IP-whitelisted** to your server.
4. Deploy per [DEPLOY.md](DEPLOY.md): Docker or systemd with auto-restart,
   alerts on, and the dead-man's switch configured.
5. `touch data/KILL` halts everything on the next tick.

### Phase 5: Scale what actually works
Only grow the size of strategies with a live track record. Next strategies
worth adding: **cross-venue prediction-market arbitrage** (Polymarket vs
Kalshi, where the same event is priced differently), **market making** (earning
the spread, which needs WebSockets and inventory skew), and **funding-rate
arbitrage** (spot vs perpetual on crypto).

## Honest expectations

- **Crypto cross-exchange and triangular arbitrage on major pairs** is
  dominated by colocated HFT firms. Even with WebSockets, from a home
  connection you will mostly see "opportunities" that are gone by the time you trade. Edges
  persist in smaller venues, newer listings and less liquid pairs.
- **Prediction markets** are where retail-scale arbitrage is most realistic
  today. Mispricings are larger and slower, especially in multi-outcome and
  low-volume markets.
- **Stocks via Alpaca** give you top-of-book only. Statistical strategies fit
  better than pure arbitrage here. The $25k pattern-day-trader minimum was
  replaced by intraday margin rules (FINRA Rule 4210, effective June 2026), but
  brokers phase this in and can set their own minimums, so check yours.
- **Arbitrage profit is not the same as account P&L.** Pre-funded inventory
  (BTC sitting on both venues) moves with the market. In the demo the arbs earn
  about +$75, yet the account can still finish down because the coins it holds
  fell. Hedge that inventory (for example, short a perpetual) or keep it small.
- Nothing here is financial advice. Every strategy can lose money.

## Jurisdiction and compliance (check before going live)

- **Binance.com is not available to US residents.** Use `exchange_id = "binanceus"`.
- **Polymarket's international exchange blocks US persons.** US users have a
  separate, CFTC-regulated Polymarket US offering and Kalshi. Use whichever is
  legal where you live, and read the venue's API terms.
- Taxes: every arbitrage leg is a taxable event in most jurisdictions. The
  journal gives you a full trade record.

## Extending

**New venue:** subclass `Exchange` (four methods: `fetch_order_book`,
`place_order`, `cancel_order`, `fetch_balances`), then add a branch in
`config._build_live_adapter`. Override `watch_order_books` to stream (yield
books, or `None` as a heartbeat; raise on disconnect); otherwise it polls.
IOC and FOK orders must come back with their
final filled amount.

**New strategy:** subclass `Strategy`, implement `subscriptions()` and
`on_snapshot()` to return `TradePlan`s, and register it in `config.STRATEGIES`.
Set `atomic=True` on multi-leg plans so the engine unwinds uneven fills.
Constructor parameters become TOML keys automatically, and parameters typed
`Decimal` are converted for you.
