# XAU Stop-And-Reverse Martingale

Trading bot for XAUUSD over the **cTrader MCP server**, deployed on Railway and
controlled entirely from a phone. No cTrader Desktop, no cBot, no PC.

## ⚠️ This is a martingale

The loss is not realised — it accumulates. With a 2.0 multiplier from 0.01 lots,
the sixth rung is 0.32 lots and the whole ladder is 0.63 lots. At that size a
$1.00 move in gold is **$63**.

`MAX_STEPS`, `MAX_TOTAL_LOTS` and `DAILY_LOSS_LIMIT_USD` are not decoration —
they are the only thing between this and an empty account. Start-up prints the
full ladder in lots and dollars so the exposure is never abstract:

```
Martingale ladder: 0.01 + 0.02 + 0.04 + 0.08 + 0.16 + 0.32 = 0.63 lots total
                   if every rung fills. MAX_TOTAL_LOTS=0.64 stops it first.
At 0.64 lots a 1.00 move in gold is 64.00 USD.
```

`TRADING_MODE` is `paper` by default and the engine **never starts itself** —
something has to press Start on the panel.

---

## What it does

1. Market entry with the base lot.
2. An opposite pending **STOP** at a fixed distance from price.
3. The pending trails behind price, only in the favourable direction.
4. When it triggers, the new position opens and both stay open (hedged).
5. Next rung's lot: basket in **loss** → multiplied; basket in **profit** → base lot.
6. The whole basket closes when net profit reaches the target.

---

## Setup

Two variables. That is the whole credential story.

| Variable | Value |
|---|---|
| `CTRADER_MCP_TOKEN` | your cTrader MCP Bearer token |
| `PANEL_PASSWORD` | choose a strong one |

1. **GitHub** — upload this folder as a repo (the GitHub app works fine from a phone).
2. **Railway** — New Project → Deploy from GitHub repo. The `Dockerfile` is picked up automatically.
3. **Settings → Networking → Generate Domain.** That URL is the panel.
4. **Variables** — set the two above, plus anything from `.env.example` you want to change.
5. Open the URL, enter the password, press **Start**.

No OAuth application, no redirect URI, no refresh token, no mounted volume. The
token carries the account, the plant and the environment, and the bot decodes and
logs which one it is at start-up:

```
cTrader account environment=DEMO plant=icmarkets
```

---

## The panel

Live status — price, spread, basket P&L, ladder depth, next lot, day P&L — plus
three buttons: **Start**, **Stop** (leaves open positions alone) and **Close
everything now**.

The basket target is checked even while the engine is stopped, so an open cycle
can still close in profit instead of hanging there.

`/health` is unauthenticated for Railway's healthcheck and leaks nothing; every
other route requires the password.

---

## Parameters

| Variable | Default | Meaning |
|---|---|---|
| `START_LOTS` | 0.01 | first rung |
| `MULTIPLIER` | 2.0 | martingale multiplier |
| `REVERSE_PIPS` | 30 | pending distance (**$0.30**) |
| `TRAIL_STEP_PIPS` | 5 | how far price must move to trail the pending |
| `STOP_PIPS` | 60 | stop loss per position |
| `BASKET_TARGET_USD` | 5.0 | profit that closes the cycle |
| `MAX_STEPS` | 6 | deepest rung |
| `MAX_TOTAL_LOTS` | 0.64 | maximum exposure |
| `DAILY_LOSS_LIMIT_USD` | 100 | halts the bot for the day |
| `MAX_SPREAD_PIPS` | 40 | no entry on a wide spread |

### A pip here is $0.01

Not the $0.10 of FX convention. cTrader reports `pipPosition=2` for XAUUSD, and
the recorded session this bot reproduces describes 4341.87 → 4342.15 as "~28
pips". `PIP_SIZE=0.10` would place every pending ten times too far away and the
cycle would never work as designed.

MCP's `get_symbols` does not publish `pipPosition`, so it is configured rather
than read from the broker.

---

## How it talks to the broker

One Bearer token, HTTP, and tool schemas read live from the server at start-up.
The wire conventions below were each verified against the running server — every
one of them was a real bug first:

* `volume` is declared **integer** and carries cTrader centi-units
  (`lots × contract_size × 100`). Sent as lots it rounds to zero and every order
  is rejected.
* A **MARKET** order cannot carry an absolute `stopLoss`; it takes
  `relativeStopLoss`, a distance on the same 1e5 scale prices use. A pending
  **STOP** does have a known trigger price, so its stop is absolute.
* Prices arrive as integers scaled by 1e5, money in cents.
* Order and position ids are integers, not strings.

There is no streaming. State is polled every `POLL_INTERVAL_SECONDS`, which
costs nothing that matters here: the pending STOP that opens each reversal lives
at the broker and triggers server-side whether or not this loop is looking.

Polling also removed a whole class of bug — there is no incremental bookkeeping
left to drift out of sync with the account.

---

## Tests

```bash
pip install -r requirements.txt pytest
pytest
```

13 tests covering the parts that decide how much money is at stake: pip sizing,
centi-unit volume, relative vs absolute stops, the martingale ladder in both
profit and loss, hedge netting, and the spread gate.

---

## No backtester

The Open API offers historical bars, but a realistic backtest of a martingale
grid needs tick data and spread modelling. Real backtesting belongs in cTrader
Desktop with a cBot version — one session on a computer is enough for that.
