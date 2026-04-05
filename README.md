# Polymarket NegRisk Split Bot

A Python script to mint YES tokens on Polymarket **Negative Risk** markets (Polygon Mainnet) via `NegRiskAdapter`, using the **Split**, **Convert**, **Transfer**, or **Balance** strategy.

---

## Installation

### 1. Create & activate virtual environment

```bash
# Create venv (once only)
python3 -m venv venv

# Activate venv (every time you open a new terminal)
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# Your terminal prompt will change to: (venv) $
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in the following values:

| Variable | Description |
|---|---|
| `PRIVATE_KEY` | Your EOA wallet private key (64 hex chars, with or without `0x`) |
| `POLYGON_RPC_URL` | Polygon RPC URL — see options below |
| `NEG_RISK_MARKET_ID` | NegRisk market ID (bytes32) |
| `MARKET_SLUG` | Market slug for Gamma API lookup |
| `SPLIT_AMOUNT_USDC` | USDC.e amount (default: `5`) |
| `MAX_CONDITIONS` | Max conditions to process (default: `30`) |
| `TRANSFER_TO` | Polymarket proxy wallet address (for `--strategy transfer`) |

**Polygon RPC options (choose one):**

```bash
# Free & stable (no registration needed):
POLYGON_RPC_URL=https://1rpc.io/matic

# Alchemy (more reliable, register at alchemy.com):
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

> **Note:** Free public RPCs may rate-limit. If you experience errors, switch to Alchemy or QuickNode.

---

## Usage

> **Always activate venv first** before running the script:
> ```bash
> source venv/bin/activate
> ```

### Strategy: CONVERT (Recommended) — 2 transactions, YES tokens only

Spend `--amount` USDC → receive YES tokens for **all 30 conditions** at avg ~3.3¢ each.

```bash
# Dry run first (simulation, no real transactions)
python polymarket_split.py --strategy convert --amount 1 --dry-run

# Live execution
python polymarket_split.py --strategy convert --amount 1
```

### Strategy: BALANCE — View YES/NO token balances per condition

Check how many YES and NO tokens you hold per condition, with tweet-range labels.

```bash
python polymarket_split.py --strategy balance
```

### Strategy: TRANSFER — Move YES tokens from EOA to Polymarket proxy wallet

After running Convert, transfer tokens to your Polymarket account so they appear in your portfolio.

```bash
# Dry run first
python polymarket_split.py --strategy transfer --dry-run

# Live execution (uses TRANSFER_TO from .env)
python polymarket_split.py --strategy transfer

# Transfer both YES and NO tokens
python polymarket_split.py --strategy transfer --include-no

# Or specify proxy address directly
python polymarket_split.py --strategy transfer --transfer-to 0xYourProxyAddress
```

> **How to find your proxy address:** Go to polymarket.com → click your profile → "Copy address"

### Strategy: SPLIT (Legacy) — N transactions, YES + NO tokens per condition

```bash
# Dry run
python polymarket_split.py --strategy split --amount 5 --dry-run

# Live execution
python polymarket_split.py --strategy split --amount 5
```

---

## Recommended Workflow

```
1. Edit .env          →  Set PRIVATE_KEY, TRANSFER_TO, MARKET_SLUG
        ↓
2. Dry run convert    →  python polymarket_split.py --strategy convert --amount 1 --dry-run
        ↓
3. Run convert        →  python polymarket_split.py --strategy convert --amount 1
        ↓
4. Check balances     →  python polymarket_split.py --strategy balance
        ↓
5. Dry run transfer   →  python polymarket_split.py --strategy transfer --dry-run
        ↓
6. Run transfer       →  python polymarket_split.py --strategy transfer
        ↓
7. Check portfolio    →  polymarket.com/portfolio
```

---

## All CLI Options

```
--dry-run               Simulate without sending transactions
--strategy  STRING      split | convert | transfer | balance  (default: split)
--amount    FLOAT       USDC.e to use (overrides .env SPLIT_AMOUNT_USDC)
--max       INT         Max conditions to process (overrides .env MAX_CONDITIONS)
--market-id HEX         NegRisk Market ID (bytes32)
--slug      STRING      Market slug for Gamma API lookup
--transfer-to ADDRESS   Proxy wallet address for transfer strategy (overrides .env TRANSFER_TO)
--include-no            Also transfer NO tokens (transfer strategy only)
```

---

## How It Works

### Convert Strategy (2 TX total)

```
Step 1: splitPosition(condition_0, amount)  →  YES_0 + NO_0
        ↓
Step 2: convertPositions(marketId, indexSet=1, amount)
        provide NO_0  →  receive YES_1 … YES_29
        ↓
Result: YES tokens for ALL 30 conditions, cost = 1× amount USDC
        Avg cost per YES ≈ amount / 30 ≈ 3.33¢  (for amount=1 USDC)
```

### Transfer Strategy (1 TX)

```
safeBatchTransferFrom(EOA → proxy, [YES_0..YES_29], [amounts])
        ↓
Tokens appear in Polymarket portfolio
```

> Use `--include-no` to also transfer NO tokens in the same transaction.

### Balance Strategy (read-only)

```
balanceOfBatch(EOA, [YES_0..YES_29, NO_0..NO_29])
        ↓
Displays per-condition balance with tweet-range label
```

---

## NegRisk Architecture

A NegRisk market is **not** a single CTF condition with 30 outcome slots. The structure is:

```
NegRisk Market (1 marketId)
├─ Question[0]  → conditionId[0]  → YES token + NO token
├─ Question[1]  → conditionId[1]  → YES token + NO token
│  ...
└─ Question[29] → conditionId[29] → YES token + NO token
```

- `getOutcomeSlotCount() = 2` on CTF per condition is **NORMAL** for NegRisk binary questions
- `getOutcomeSlotCount() = 0` on CTF for the marketId itself is also **NORMAL**

### What you receive after Convert strategy (`--amount 1`)

| | Split strategy | Convert strategy |
|---|---|---|
| YES tokens | 30 (1 per condition) | 30 (1 per condition) |
| NO tokens | 30 (1 per condition) | 0 |
| USDC spent | 30 USDC | **1 USDC** |
| Avg cost/YES | ~3.33¢ | **~3.33¢** |

---

## Contract Addresses (Polygon Mainnet)

| Contract | Address |
|---|---|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| CTF (Conditional Tokens) | `0x4D97dcd97eC945f40cF65F87097ACe5EA0476045` |
| **NegRiskAdapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| NegRisk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

---

## Security

- `.env` is in `.gitignore` — **never commit it**
- Private key is read from environment variables only, never hardcoded
- Always run `--dry-run` before live execution
- Ensure sufficient MATIC for gas (~0.5 MATIC recommended for all strategies)
- `convertPositions` uses a fixed **6M gas limit** (cold storage for 30 CTF positions requires ~4.5M minimum)
- Minimum gas price enforced at **200 gwei** to avoid stuck transactions on Polygon
- If `convertPositions` fails mid-way (split already done), re-run safely — the script detects existing NO_0 balance and skips the split step automatically
- Convert strategy aborts if NegRisk Q0 condition is already resolved — use `--strategy split` for partially resolved markets instead

---

> Indonesian version: [README-INDO.md](README-INDO.md)
