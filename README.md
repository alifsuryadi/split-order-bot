# Polymarket NegRisk Split Bot

Script Python untuk melakukan `splitPosition` pada pasar **Negative Risk** Polymarket (Polygon Mainnet) melalui `NegRiskAdapter` — menghindari masalah _"Condition Not Prepared"_ yang muncul saat split langsung di CTF utama.

---

## Instalasi

### 1. Buat & aktifkan virtual environment

```bash
# Buat venv (cukup sekali)
python3 -m venv venv

# Aktifkan venv (lakukan setiap buka terminal baru)
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# Prompt terminal akan berubah menjadi: (venv) $
```

### 2. Install dependensi

```bash
pip install -r requirements.txt
```

### 3. Buat file `.env`

```bash
cp .env.example .env
```

Buka `.env` dan isi nilai berikut:

| Variabel | Keterangan |
|---|---|
| `PRIVATE_KEY` | Private key wallet Anda (64 hex chars, dengan atau tanpa `0x`) |
| `POLYGON_RPC_URL` | URL RPC Polygon — lihat opsi di bawah |
| `SPLIT_AMOUNT_USDC` | Jumlah USDC.e per kondisi (default: `5`) |
| `MAX_CONDITIONS` | Batas kondisi yang diproses (default: `30`) |

**Opsi RPC Polygon (pilih salah satu):**

```bash
# Gratis & stabil (tidak perlu daftar):
POLYGON_RPC_URL=https://1rpc.io/matic

# Alchemy (lebih andal, perlu daftar di alchemy.com):
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/API_KEY_ANDA
```

---

## Cara Menjalankan

> **Selalu aktifkan venv dulu** sebelum menjalankan script:
> ```bash
> source venv/bin/activate
> ```

### Dry Run (simulasi, tidak ada transaksi nyata)

Jalankan ini **dulu** sebelum eksekusi nyata untuk memverifikasi konfigurasi:

```bash
python polymarket_split.py --dry-run
```

### Test Kecil (3 kondisi pertama saja)

```bash
python polymarket_split.py --amount 1 --max 3
```

### Eksekusi Penuh (semua 30 kondisi)

```bash
python polymarket_split.py
```

### Semua Opsi CLI

```
--dry-run          Simulasi tanpa kirim transaksi
--amount  FLOAT    USDC.e per kondisi (override .env)
--max     INT      Batas jumlah kondisi (override .env)
--market-id HEX   NegRisk Market ID (bytes32)
--slug    STRING   Slug pasar untuk Gamma API
```

---

## Alur Kerja Script

```
1. Fetch conditionId  ←  Gamma API (events?slug=...)
        ↓
2. Diagnostik         ←  Saldo MATIC, USDC.e, allowance
        ↓
3. Validasi saldo      ←  Pastikan USDC.e mencukupi
        ↓
4. Approve USDC.e     →  NegRiskAdapter (sekali, unlimited)
        ↓
5. Loop 30x:
   splitPosition(conditionId, amount)  →  NegRiskAdapter
        ↓
6. Ringkasan hasil + link Polygonscan
```

---

## Penjelasan Arsitektur NegRisk

Pasar NegRisk **bukan** satu kondisi CTF dengan 30 outcome slot. Strukturnya:

```
NegRisk Market (1 marketId)
├─ Question[0]  → conditionId[0]  → YES token + NO token
├─ Question[1]  → conditionId[1]  → YES token + NO token
│  ...
└─ Question[29] → conditionId[29] → YES token + NO token
```

- `getOutcomeSlotCount() = 0` di CTF utama untuk marketId ini adalah **NORMAL**
- Setiap kondisi di-split secara **terpisah** (30 pemanggilan `splitPosition`)
- Parameter `partition` pada NegRiskAdapter **diabaikan** — tidak ada single call untuk 30 outcomes sekaligus

---

## Kontrak yang Digunakan (Polygon Mainnet)

| Kontrak | Alamat |
|---|---|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| CTF (Conditional Tokens) | `0x4D97dcd97eC945f40cF65F87097ACe5EA0476045` |
| **NegRiskAdapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| NegRisk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

---

## Keamanan

- File `.env` sudah ada di `.gitignore` — **jangan commit**
- Private key hanya dibaca dari environment variable, tidak pernah hardcode
- Jalankan `--dry-run` sebelum eksekusi nyata
- Pastikan saldo MATIC cukup untuk biaya gas (~0.1 MATIC untuk 30 transaksi)
