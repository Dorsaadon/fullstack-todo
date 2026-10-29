# tradebot

A multi-venue, multi-strategy trading bot for **US equities (Alpaca)**, **crypto
(Binance, Kraken and 100+ others via ccxt)** and **prediction markets
(Polymarket)**. It runs four strategies: cross-exchange arbitrage, triangular
arbitrage, prediction-market basket arbitrage and mean reversion.

It is built so that you can go **demo → paper → small live → scale** without
rewriting anything. The same strategy code runs at every stage, and every order
passes the risk manager.

```bash
cd trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"

python -m tradebot run -c config/demo.toml --ticks 40   # offline, no keys
pytest                                                  # 24 tests
```

## Architecture

```
            ┌──────────────── Engine (engine.py) ─────────────────┐
 venues ──► │ snapshot ──► strategies ──► risk.check ──► execute  │ ──► venues
 (books,    │ (all books,   (pure: return   (limits,     (legs in  │
 balances)  │  balances)    TradePlans)     kill switch)  parallel,│
            │                                             unwind)  │
            └──────────────────────┬──────────────────────────────┘
                                   ▼
                      journal (JSONL audit of every decision)
```

| Layer | Files | Job |
|---|---|---|
| Models | `models.py` | `OrderBook`, `OrderRequest`, `TradePlan`… All money is `Decimal`. |
| Venues | `exchanges/` | One `Exchange` interface. `PaperExchange` simulates fills against any data source. |
| Strategies | `strategies/` | Pure decision functions: snapshot in, `TradePlan`s out. They never place orders. |
| Risk | `risk/` | Per-order and per-position caps, daily loss halt, order-rate limit, stale-data guard, kill switch. |
| Engine | `engine.py` | Polling loop. Fires a plan's legs concurrently and **unwinds uneven fills** (legging risk). |
| Config | `config.py`, `config/*.toml` | Everything is declared in TOML. Secrets come from env vars. |

## Strategies

| Kind | Where | Idea | Main risk |
|---|---|---|---|
| `cross_exchange_arb` | crypto | Buy on venue A, sell on venue B at the same moment, from inventory pre-funded on both | One leg fills and the other doesn't. Inventory drifts to one side. |
| `triangular_arb` | one crypto venue | USDT→BTC→ETH→USDT where the cross rate is mispriced | Edges are tiny and contested. Fees decide everything. |
| `prediction_market_arb` | Polymarket | Outcomes are exhaustive and exactly one pays $1, so asks summing below $1 is locked-in profit | An outcome missing from the basket. Resolution or oracle disputes. Capital locked until resolution. |
| `mean_reversion` | stocks | Buy at z ≤ −entry, exit when price reverts | **Directional market risk.** It is not arbitrage. Backtest it first. |

Every arbitrage strategy sizes trades by walking order-book depth, after fees.
It picks the size with the largest absolute profit that still clears
`min_edge_bps`.

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
- **Record and replay data.** Log books to disk, then replay them through
  `PaperExchange.set_order_book` for repeatable backtests. Paper fills ignore
  queue position and latency, so treat paper P&L as an upper bound.
- **WebSockets instead of polling.** Polling every 2s is fine for prediction
  markets and mean reversion, but far too slow for crypto arbitrage. ccxt.pro
  (bundled with ccxt) has `watch_order_book`, Alpaca has `StockDataStream`, and
  Polymarket has a market WebSocket channel. Add a `watch` path to `Exchange`
  and make the engine event-driven.
- **Inventory rebalancing** for cross-exchange arbitrage, since it piles up
  coins on one venue and cash on the other.
- **Alerts** (Telegram, Slack or email) on halts, unwinds and daily P&L.

### Phase 4: Go live, small
1. Set `engine.mode = "live"` **and** pass `--live`. Both are required, by design.
2. Start with limits you'd be fine losing entirely: `max_order_notional = 50`,
   `max_daily_loss = 25`.
3. API keys: **trade-only, withdrawals disabled, IP-whitelisted** to your server.
4. Run it on a small cloud VM near the venue (Binance runs on AWS Tokyo,
   Coinbase on AWS us-east-1) under `systemd` or Docker with auto-restart.
5. `touch KILL` in the working directory halts everything on the next tick.

### Phase 5: Scale what actually works
Only grow the size of strategies with a live track record. Next strategies
worth adding: **cross-venue prediction-market arbitrage** (Polymarket vs
Kalshi, where the same event is priced differently), **market making** (earning
the spread, which needs WebSockets and inventory skew), and **funding-rate
arbitrage** (spot vs perpetual on crypto).

## Honest expectations

- **Crypto cross-exchange and triangular arbitrage on major pairs** is
  dominated by colocated HFT firms. From a home connection with polling you
  will mostly see "opportunities" that are gone by the time you trade. Edges
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
`config._build_live_adapter`. IOC and FOK orders must come back with their
final filled amount.

**New strategy:** subclass `Strategy`, implement `subscriptions()` and
`on_snapshot()` to return `TradePlan`s, and register it in `config.STRATEGIES`.
Set `atomic=True` on multi-leg plans so the engine unwinds uneven fills.
Constructor parameters become TOML keys automatically, and parameters typed
`Decimal` are converted for you.
