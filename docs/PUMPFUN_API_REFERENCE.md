# Pump.fun Production API Reference (Verified Live)

This document contains the verified, reverse-engineered reference for Pump.fun's live production APIs. These endpoints are currently utilized by Pump.fun's web application and swap interface.

---

## ⚡ Key Architecture & Summary

* **No Authentication Required**: All market data, candlesticks, trade histories, token details, and dev profiles documented below are **100% public** and do not require API keys, JWT tokens, or signed wallet messages.
* **RPC Credit Savings**: Replaces thousands of heavy `getTransaction` / `getSignaturesForAddress` RPC calls with lightweight, sub-second HTTP GET requests.
* **Two Core Production Hosts**:
  1. `https://swap-api.pump.fun` — Real-time OHLC candlesticks (1s to 24h) and parsed trade streams.
  2. `https://frontend-api-v3.pump.fun` — Token metadata, pre-computed ATHs, creator history, and live SOL price.

---

## 1. Real-Time Candlesticks (OHLCV)

High-resolution candlestick data directly from Pump.fun's swap engine.

### `GET https://swap-api.pump.fun/v2/coins/{mint}/candles`

#### Query Parameters:
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `interval` | string | **Yes** | Timeframe. Must be one of: `1s`, `15s`, `30s`, `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `6h`, `12h`, `24h`. |
| `limit` | int | **Yes** | Max number of candles to return (e.g. `300`). |
| `createdTs` | int | **Yes** | Unix timestamp cutoff (pass `0` for full available history). |
| `currency` | string | No | Quote currency (defaults to SOL/token pricing). |

#### Request Example:
```bash
curl "https://swap-api.pump.fun/v2/coins/5Jr9hGmJgxBRjjF8XGcGgQzXUdsbpZNNMpigEv8Wpump/candles?createdTs=0&interval=1s&limit=300"
```

#### Response Example (`200 OK`):
```json
[
  {
    "timestamp": 1788434473000,
    "open": "0.0045280613300213189959696781",
    "high": "0.0045280613300213189959696781",
    "low": "0.0045280613300213189959696781",
    "close": "0.0045280613300213189959696781",
    "volume": "14.98663100550"
  },
  {
    "timestamp": 1788436153000,
    "open": "0.0045280613300213189959696781",
    "high": "0.0045324150473872562028704282",
    "low": "0.0045280613300213189959696781",
    "close": "0.0045324150473872562028704282",
    "volume": "2.237207278808313348"
  }
]
```
> **Note on Timestamp**: Timestamp is returned in **milliseconds** (divide by `1000` for standard Unix epoch seconds).

---

## 2. Parsed Trade History

Clean, structured trade records including buyer/seller wallet addresses, fill prices, and SOL/USD quantities.

### `GET https://swap-api.pump.fun/v2/coins/{mint}/trades`

#### Query Parameters:
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `limit` | int | No | Number of trades per page (default: `50`, max: `200`). |
| `cursor` | string | No | Pagination cursor returned by `nextCursor`. |

#### Request Example:
```bash
curl "https://swap-api.pump.fun/v2/coins/5Jr9hGmJgxBRjjF8XGcGgQzXUdsbpZNNMpigEv8Wpump/trades?limit=2"
```

#### Response Example (`200 OK`):
```json
{
  "trades": [
    {
      "slotIndexId": "00044397988000059600030104",
      "tx": "54mDx1ohusMyvp1HCxbBew1QDEaujDXZG3sZdPeMT2EHcxeJXaNVPnMmWaTTuyzdqsBFvSGMQz25bJAzoV6E4ibk",
      "timestamp": "2026-09-03T13:13:44.000Z",
      "userAddress": "3i51cKbLbaKAqvRJdCUaq9hsnvf9kqCfMujNgFj7nRKt",
      "type": "buy",
      "program": "raydium_v4_amm",
      "priceUsd": "0.004531401918289406",
      "priceSol": "0.000044622372410530",
      "amountUsd": "49.42790370750",
      "amountSol": "0.486734650",
      "baseAmount": "10884.4176",
      "quoteAmount": "0.48673465",
      "fillPriceUsd": "0.004541162010129049",
      "fillPriceSol": "0.000044718483605406"
    }
  ],
  "pagination": {
    "nextCursor": "00044397746100084100000401-1788440463000",
    "hasMore": true,
    "limit": 2
  }
}
```

---

## 3. Token Metadata & Pre-Computed ATH

Returns complete token state, bonding curve reserves, graduation progress, and pre-computed ATH market caps.

### `GET https://frontend-api-v3.pump.fun/coins/{mint}`

#### Request Example:
```bash
curl "https://frontend-api-v3.pump.fun/coins/5Jr9hGmJgxBRjjF8XGcGgQzXUdsbpZNNMpigEv8Wpump"
```

#### Key Fields in Response:
```json
{
  "mint": "5Jr9hGmJgxBRjjF8XGcGgQzXUdsbpZNNMpigEv8Wpump",
  "name": "Mushu",
  "symbol": "MUSHU",
  "creator": "8RskwAy1HjSqPMufCuZu2BMs6FEvLsrC5J9CrTvXf5nA",
  "created_timestamp": 1719965724731,
  "bonding_curve": "FzdReSwGAPkiPtzmbhNCwsEk2G7rFLyiSVcVN9QSFBRX",
  "associated_bonding_curve": "8ghKoheLzsWS6XUR15XhEKamUmvDSAyf9TKNrm4Be22B",
  "complete": true,
  "raydium_pool": "HXH2Wp1NQ2iCo2RV5hQzmcirh7tvjsGWYKchY8ZjbbR7",
  "market_cap": 7605.77,
  "usd_market_cap": 771599.72,
  "ath_market_cap": 36389664,
  "ath_market_cap_timestamp": 1771258779918,
  "virtual_sol_reserves": 115005359443,
  "virtual_token_reserves": 279900000000000,
  "total_supply": 170113450469417,
  "twitter": "https://x.com/mushucoinsol",
  "telegram": "https://t.me/mushucoinsolana",
  "website": "https://mushu.lol/"
}
```
> **Game-Changer**: `ath_market_cap` and `ath_market_cap_timestamp` eliminate the need to backtrace all transactions on-chain to determine whether a token reached an ATH or rugged.

---

## 4. Operator / Dev Launch History (Type 1 Archetype Tracking)

Fetches all historical tokens created by an operator wallet address.

### `GET https://frontend-api-v3.pump.fun/coins-v2/user-created-coins/{creator_wallet}`

#### Query Parameters:
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `limit` | int | No | Tokens per page (default: `50`). |
| `offset` | int | No | Offset for pagination. |

#### Request Example:
```bash
curl "https://frontend-api-v3.pump.fun/coins-v2/user-created-coins/9vjSgmhB5Fc95KENA48UJmhagU6DSsWP1vXYnXwe8tVF"
```

#### Response Example (`200 OK`):
```json
{
  "limit": 50,
  "offset": 0,
  "count": 896,
  "coins": [
    {
      "mint": "6tVZVjcpXhVqS5gJ6P6d2qYkLqP4E3wD2zX1cMiharu",
      "name": "miharu",
      "symbol": "miharu",
      "usd_market_cap": 75924.12,
      "complete": true,
      "created_timestamp": 1788440000000
    },
    {
      "mint": "FzPdjvZrX7hWtQyyKukZ1KUGnSfrjd6YVwGUHOSU111",
      "name": "UHOSU",
      "symbol": "UHOSU",
      "usd_market_cap": 3460.50,
      "complete": false,
      "created_timestamp": 1788435000000
    }
  ]
}
```
> **Operator Profiling**: Instant calculation of operator sample size $N$ (`count`), graduation winrate (`complete: true` ratio), launch cadence, and historical peak market caps.

---

## 5. Token Feed & Discovery

Filter and sort tokens across the entire Pump.fun platform.

### `GET https://frontend-api-v3.pump.fun/coins`

#### Query Parameters:
| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `sort` | string | No | Sort field: `created_timestamp`, `last_trade_timestamp`, `market_cap`. |
| `order` | string | No | `ASC` or `DESC` (default: `DESC`). |
| `limit` | int | No | Items per page (default: `50`). |
| `offset` | int | No | Offset for pagination. |
| `includeNsfw` | bool | No | Include NSFW tokens (`true` or `false`). |
| `searchTerm` | string | No | Search query for token name, ticker, or mint. |

#### Request Example (Latest Launches):
```bash
curl "https://frontend-api-v3.pump.fun/coins?sort=created_timestamp&order=DESC&limit=10&includeNsfw=false"
```

---

## 6. Real-Time SOL Price

The canonical SOL/USD exchange rate used by Pump.fun's bonding curve and USD valuations.

### `GET https://frontend-api-v3.pump.fun/sol-price`

#### Request Example:
```bash
curl "https://frontend-api-v3.pump.fun/sol-price"
```

#### Response Example (`200 OK`):
```json
{
  "solPrice": 101.71804379496508,
  "asOfTimestamp": 1788440270801,
  "stale": false
}
```

---

## 7. What Still Requires Helius / Solana RPC?

While Pump.fun's REST API replaces **90%+ of read operations**, a low-latency Solana RPC (such as Helius) and Jito are still required for the following operations:

| Workflow | Pump.fun REST API | Helius / Solana RPC / Jito |
| :--- | :---: | :---: |
| **Candlestick Plotting & Visualizer** | ✅ (~400ms) | ❌ (Too slow / expensive) |
| **ATH & Multiplier Calculations** | ✅ (`ath_market_cap`) | ❌ (Unnecessary math) |
| **Dev History & Winrate Profiling** | ✅ (`coins-v2`) | ❌ (Slow signature crawl) |
| **Token Market Cap & Reserves** | ✅ | ❌ |
| **Sub-50ms Block-0 Sniper Trigger** | ❌ (REST polling is ~200-500ms) | ✅ (PumpPortal / Geyser WebSocket) |
| **Signing & Executing Buy/Sell Txns** | ❌ (Read-only) | ✅ (SendTransaction RPC) |
| **Jito MEV Bundles & Tip Submissions** | ❌ | ✅ (Jito Block Engine) |
| **On-Chain Balance Checks (`getBalance`)** | ❌ | ✅ (Local RPC) |

---

## 8. Python Client Usage (`src/rugbot/integrations/pumpfun_api.py`)

```python
from rugbot.integrations.pumpfun_api import get_client

client = get_client()

# Fetch 300 1-second candles
candles = client.fetch_candlesticks(
    "5Jr9hGmJgxBRjjF8XGcGgQzXUdsbpZNNMpigEv8Wpump",
    interval="1s",
    limit=300
)

# Output: [{'timestamp': 1788..., 'open': '...', 'high': '...', 'low': '...', 'close': '...', 'volume': '...'}]
```
