# Polymarket NegRisk Split Bot

A Python script to perform `splitPosition` on Polymarket **Negative Risk** markets (Polygon Mainnet) via `NegRiskAdapter` — bypassing the _"Condition Not Prepared"_ error that occurs when attempting to split directly through the main CTF contract.

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
| `PRIVATE_KEY` | Your wallet private key (64 hex chars, with or without `0x`) |
| `POLYGON_RPC_URL` | Polygon RPC URL — see options below |
| `SPLIT_AMOUNT_USDC` | USDC.e amount per condition (default: `5`) |
| `MAX_CONDITIONS` | Max conditions to process (default: `30`) |

**Polygon RPC options (choose one):**

```bash
# Free & stable (no registration needed):
POLYGON_RPC_URL=https://1rpc.io/matic

# Alchemy (more reliable, register at alchemy.com):
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_API_KEY
```

---

## Usage

> **Always activate venv first** before running the script:
> ```bash
> source venv/bin/activate
> ```

### Dry Run (simulation, no real transactions)

Run this **first** before live execution to verify your configuration:

```bash
python polymarket_split.py --dry-run
```

### Small Test (first 3 conditions only)

```bash
python polymarket_split.py --amount 1 --max 3
```

### Full Execution (all 30 conditions)

```bash
python polymarket_split.py
```

### All CLI Options

```
--dry-run          Simulate without sending transactions
--amount  FLOAT    USDC.e per condition (overrides .env)
--max     INT      Max number of conditions (overrides .env)
--market-id HEX   NegRisk Market ID (bytes32)
--slug    STRING   Market slug for Gamma API lookup
```

---

## How It Works

```
1. Fetch conditionIds  ←  Gamma API (events?slug=...)
        ↓
2. Diagnostics         ←  MATIC balance, USDC.e balance, allowance
        ↓
3. Balance check       ←  Ensure sufficient USDC.e
        ↓
4. Approve USDC.e      →  NegRiskAdapter (once, unlimited amount)
        ↓
5. Loop 30x:
   splitPosition(conditionId, amount)  →  NegRiskAdapter
        ↓
6. Summary + Polygonscan links
```

---

## NegRisk Architecture Explained

A NegRisk market is **not** a single CTF condition with 30 outcome slots. The structure is:

```
NegRisk Market (1 marketId)
├─ Question[0]  → conditionId[0]  → YES token + NO token
├─ Question[1]  → conditionId[1]  → YES token + NO token
│  ...
└─ Question[29] → conditionId[29] → YES token + NO token
```

- `getOutcomeSlotCount() = 0` on the main CTF for this marketId is **NORMAL** — not an error
- Each condition is split **individually** (30 separate `splitPosition` calls)
- The `partition` parameter on NegRiskAdapter is **ignored** — there is no single call for all 30 outcomes at once

### What you receive after a full split (`SPLIT_AMOUNT_USDC=5`)

| Tokens received | Count |
|---|---|
| YES tokens (one per condition) | 150 (5 × 30) |
| NO tokens (one per condition) | 150 (5 × 30) |
| **Total tokens** | **300** |
| **USDC.e spent** | **150 USDC** |

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

- `.env` should be in `.gitignore` — **never commit it**
- Private key is read from environment variables only, never hardcoded
- Always run `--dry-run` before live execution
- Ensure sufficient MATIC for gas (~0.1 MATIC for 30 transactions)

---

> Indonesian version: [README-INDO.md](README-INDO.md)
