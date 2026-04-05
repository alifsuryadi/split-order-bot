# Polymarket NegRisk Split Bot

> English version: [README.md](README.md)

Script Python untuk mint token YES pada pasar **Negative Risk** Polymarket (Polygon Mainnet) melalui `NegRiskAdapter`, menggunakan strategi **Split**, **Convert**, **Transfer**, atau **Balance**.

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
| `PRIVATE_KEY` | Private key wallet EOA Anda (64 hex chars, dengan atau tanpa `0x`) |
| `POLYGON_RPC_URL` | URL RPC Polygon — lihat opsi di bawah |
| `NEG_RISK_MARKET_ID` | NegRisk market ID (bytes32) |
| `MARKET_SLUG` | Slug pasar untuk lookup Gamma API |
| `SPLIT_AMOUNT_USDC` | Jumlah USDC.e (default: `5`) |
| `MAX_CONDITIONS` | Batas kondisi yang diproses (default: `30`) |
| `TRANSFER_TO` | Alamat proxy wallet Polymarket (untuk `--strategy transfer`) |

**Opsi RPC Polygon (pilih salah satu):**

```bash
# Gratis & stabil (tidak perlu daftar):
POLYGON_RPC_URL=https://1rpc.io/matic

# Alchemy (lebih andal, perlu daftar di alchemy.com):
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/API_KEY_ANDA
```

> **Catatan:** RPC publik gratis bisa rate-limit. Jika ada error, gunakan Alchemy atau QuickNode.

---

## Cara Menjalankan

> **Selalu aktifkan venv dulu** sebelum menjalankan script:
> ```bash
> source venv/bin/activate
> ```

### Strategi: CONVERT (Direkomendasikan) — 2 transaksi, hanya token YES

Keluarkan `--amount` USDC → terima token YES untuk **semua 30 kondisi** dengan rata-rata ~3.3¢ per token.

```bash
# Dry run dulu (simulasi, tidak ada transaksi nyata)
python polymarket_split.py --strategy convert --amount 1 --dry-run

# Eksekusi nyata
python polymarket_split.py --strategy convert --amount 1
```

### Strategi: BALANCE — Lihat saldo token YES/NO per kondisi

Cek berapa banyak token YES dan NO yang Anda miliki per kondisi, lengkap dengan label range tweet.

```bash
python polymarket_split.py --strategy balance
```

### Strategi: TRANSFER — Pindahkan token YES dari EOA ke proxy wallet Polymarket

Setelah convert, transfer token ke akun Polymarket agar muncul di portofolio.

```bash
# Dry run dulu
python polymarket_split.py --strategy transfer --dry-run

# Eksekusi nyata (menggunakan TRANSFER_TO dari .env)
python polymarket_split.py --strategy transfer

# Transfer token YES dan NO sekaligus
python polymarket_split.py --strategy transfer --include-no

# Atau tentukan alamat proxy langsung
python polymarket_split.py --strategy transfer --transfer-to 0xAlamatProxyAnda
```

> **Cara cari alamat proxy:** Buka polymarket.com → klik profil → "Copy address"

### Strategi: SPLIT (Legacy) — N transaksi, token YES + NO per kondisi

```bash
# Dry run
python polymarket_split.py --strategy split --amount 5 --dry-run

# Eksekusi nyata
python polymarket_split.py --strategy split --amount 5
```

---

## Alur Kerja yang Direkomendasikan

```
1. Edit .env          →  Set PRIVATE_KEY, TRANSFER_TO, MARKET_SLUG
        ↓
2. Dry run convert    →  python polymarket_split.py --strategy convert --amount 1 --dry-run
        ↓
3. Run convert        →  python polymarket_split.py --strategy convert --amount 1
        ↓
4. Cek saldo          →  python polymarket_split.py --strategy balance
        ↓
5. Dry run transfer   →  python polymarket_split.py --strategy transfer --dry-run
        ↓
6. Run transfer       →  python polymarket_split.py --strategy transfer
        ↓
7. Cek portofolio     →  polymarket.com/portfolio
```

---

## Semua Opsi CLI

```
--dry-run               Simulasi tanpa kirim transaksi
--strategy  STRING      split | convert | transfer | balance  (default: split)
--amount    FLOAT       USDC.e yang digunakan (override .env SPLIT_AMOUNT_USDC)
--max       INT         Batas kondisi yang diproses (override .env MAX_CONDITIONS)
--market-id HEX         NegRisk Market ID (bytes32)
--slug      STRING      Slug pasar untuk Gamma API
--transfer-to ADDRESS   Alamat proxy wallet untuk strategi transfer (override .env TRANSFER_TO)
--include-no            Ikutkan token NO saat transfer (hanya untuk strategi transfer)
```

---

## Cara Kerja

### Strategi Convert (total 2 TX)

```
Step 1: splitPosition(condition_0, amount)  →  YES_0 + NO_0
        ↓
Step 2: convertPositions(marketId, indexSet=1, amount)
        serahkan NO_0  →  terima YES_1 … YES_29
        ↓
Hasil: Token YES untuk SEMUA 30 kondisi, biaya = 1× amount USDC
       Rata-rata biaya per YES ≈ amount / 30 ≈ 3.33¢  (untuk amount=1 USDC)
```

### Strategi Transfer (1 TX)

```
safeBatchTransferFrom(EOA → proxy, [YES_0..YES_29], [amounts])
        ↓
Token muncul di portofolio Polymarket
```

> Gunakan `--include-no` untuk ikutkan token NO dalam transaksi yang sama.

### Strategi Balance (read-only)

```
balanceOfBatch(EOA, [YES_0..YES_29, NO_0..NO_29])
        ↓
Tampilkan saldo per kondisi dengan label range tweet
```

---

## Perbandingan Strategi Split vs Convert

| | Strategi Split | Strategi Convert |
|---|---|---|
| Token YES | 30 (1 per kondisi) | 30 (1 per kondisi) |
| Token NO | 30 (1 per kondisi) | 0 |
| USDC yang dikeluarkan | 30 USDC | **1 USDC** |
| Rata-rata biaya/YES | ~3.33¢ | **~3.33¢** |

---

## Arsitektur NegRisk

Pasar NegRisk **bukan** satu kondisi CTF dengan 30 outcome slot. Strukturnya:

```
NegRisk Market (1 marketId)
├─ Question[0]  → conditionId[0]  → YES token + NO token
├─ Question[1]  → conditionId[1]  → YES token + NO token
│  ...
└─ Question[29] → conditionId[29] → YES token + NO token
```

- `getOutcomeSlotCount() = 2` di CTF per kondisi adalah **NORMAL** untuk binary question NegRisk
- `getOutcomeSlotCount() = 0` di CTF untuk marketId itu sendiri juga **NORMAL**
- Urutan question index NegRisk (Q0, Q1, ...) **berbeda** dari urutan yang dikembalikan Gamma API. Script sudah menangani ini secara otomatis

---

## Kontrak yang Digunakan (Polygon Mainnet)

| Kontrak | Alamat |
|---|---|
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| CTF (Conditional Tokens) | `0x4D97dcd97eC945f40cF65F87097ACe5EA0476045` |
| **NegRiskAdapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| NegRisk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

---

## Keamanan & Catatan Penting

- File `.env` sudah ada di `.gitignore` — **jangan pernah commit**
- Private key hanya dibaca dari environment variable, tidak pernah hardcode
- Selalu jalankan `--dry-run` sebelum eksekusi nyata
- Pastikan saldo MATIC cukup untuk gas (~0.5 MATIC direkomendasikan untuk semua strategi)
- `convertPositions` menggunakan gas limit tetap **6 juta** (cold storage untuk 30 posisi CTF butuh ~4.5M minimum)
- Minimum gas price dibatasi **200 gwei** agar transaksi tidak nyangkut di Polygon
- Jika `convertPositions` gagal di tengah jalan (split sudah berhasil), jalankan ulang dengan aman — script otomatis mendeteksi saldo NO_0 yang sudah ada dan melewati langkah split
- Strategi convert akan batal otomatis jika NegRisk Q0 sudah resolved — gunakan `--strategy split` untuk pasar yang sudah sebagian resolved
