#!/usr/bin/env python3
"""
=============================================================================
  Polymarket Negative Risk Split Position Bot
  ============================================================================
  Melakukan splitPosition pada pasar Negative Risk Polymarket (Polygon Mainnet)
  melalui NegRiskAdapter — menghindari masalah "Condition Not Prepared" yang
  muncul ketika mencoba split langsung di CTF utama.

  ─── PENJELASAN ARSITEKTUR PENTING ────────────────────────────────────────
  Pasar NegRisk (seperti "Elon Musk Tweets") BUKAN satu kondisi tunggal dengan
  30 outcome slot di CTF utama. Strukturnya adalah:

      NegRisk Market (1 marketId)
      └─ Question 0  (questionId[0]) → conditionId[0]  → YES/NO tokens
      └─ Question 1  (questionId[1]) → conditionId[1]  → YES/NO tokens
      └─ ...
      └─ Question 29 (questionId[29]) → conditionId[29] → YES/NO tokens

  Itulah mengapa getOutcomeSlotCount() = 0 di CTF utama untuk marketId ini
  adalah NORMAL — kondisi dikelola secara internal oleh NegRiskAdapter.

  Untuk "minting" semua posisi, script ini memanggil:
      NegRiskAdapter.splitPosition(conditionId, amount)
  sebanyak 30 kali (satu per pertanyaan biner).

  Catatan parameter "partition": NegRiskAdapter MENGABAIKAN parameter partition
  dalam versi CTF-compatible. Tidak ada satu panggilan split untuk 30 outcomes
  sekaligus — arsitektur NegRisk bekerja per-pertanyaan biner (YES/NO).
  ──────────────────────────────────────────────────────────────────────────

  Referensi:
    - NegRiskAdapter: https://github.com/Polymarket/neg-risk-ctf-adapter
    - Alamat kontrak: https://docs.polymarket.com/resources/contract-addresses
=============================================================================
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from typing import List, Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# SETUP LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# LOAD .env
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()


# =============================================================================
# KONFIGURASI — Semua nilai sensitif berasal dari .env
# =============================================================================

# ─── Koneksi Blockchain ───────────────────────────────────────────────────────
# Daftarkan endpoint premium di Alchemy (https://alchemy.com) atau QuickNode
# untuk stabilitas lebih baik. Endpoint publik bisa rate-limited.
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")

# ─── Private Key Wallet ───────────────────────────────────────────────────────
# JANGAN hardcode private key di sini!
# Simpan di file .env sebagai:  PRIVATE_KEY=0x<64_karakter_hex>
# Pastikan .env ada di .gitignore agar tidak ter-commit ke git.
_RAW_PRIVATE_KEY_ENV: Optional[str] = os.getenv("PRIVATE_KEY")
# Normalisasi: strip 0x jika ada, zero-pad ke 64 chars, tambahkan 0x kembali
if _RAW_PRIVATE_KEY_ENV:
    _hex_part = _RAW_PRIVATE_KEY_ENV.lstrip("0x") if _RAW_PRIVATE_KEY_ENV.startswith("0x") else _RAW_PRIVATE_KEY_ENV
    _RAW_PRIVATE_KEY: Optional[str] = "0x" + _hex_part.zfill(64)
else:
    _RAW_PRIVATE_KEY = None

# ─── Konfigurasi Split ────────────────────────────────────────────────────────
# Jumlah USDC.e yang di-split PER KONDISI (per pertanyaan biner).
# Contoh: 5 USDC × 30 kondisi = 150 USDC total.
SPLIT_AMOUNT_USDC: float = float(os.getenv("SPLIT_AMOUNT_USDC", "5"))

# Batas maksimum kondisi yang diproses (default 30 = semua).
# Kurangi angka ini untuk testing dengan modal lebih kecil.
MAX_CONDITIONS: int = int(os.getenv("MAX_CONDITIONS", "30"))

# ─── Data Pasar Target ────────────────────────────────────────────────────────
# NegRisk Market ID (bytes32) — ini adalah ID induk pasar, BUKAN conditionId CTF.
NEG_RISK_MARKET_ID: str = os.getenv(
    "NEG_RISK_MARKET_ID",
    "0x11ab331c7409a321ec3cd9df51b40488b498bf01c8bd71e9ac64e0786be38700",
)
# Slug pasar untuk pencarian via Gamma API.
MARKET_SLUG: str = os.getenv(
    "MARKET_SLUG",
    "elon-musk-of-tweets-april-7-april-14",
)
# Alamat proxy wallet Polymarket (tujuan transfer YES token).
TRANSFER_TO: Optional[str] = os.getenv("TRANSFER_TO") or None


# =============================================================================
# ALAMAT KONTRAK — Polygon Mainnet (Jangan diubah kecuali ada pembaruan resmi)
# =============================================================================
ADDR: dict = {
    # Collateral token (USDC bridged)
    "USDC_E":            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    # Conditional Tokens Framework (CTF) utama
    "CTF":               "0x4D97dcd97eC945f40cF65F87097ACe5EA0476045",
    # NegRiskAdapter — target utama untuk splitPosition NegRisk
    "NEG_RISK_ADAPTER":  "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    # NegRisk CTF Exchange (untuk order book, bukan split)
    "NEG_RISK_EXCHANGE": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    # NegRiskOperator (mengelola siklus hidup pasar, bukan untuk user langsung)
    "NEG_RISK_OPERATOR": "0x71523d0f655B41E805Cec45b17163f528B59B820",
}


# =============================================================================
# ABI KONTRAK (Minimal — hanya fungsi yang dibutuhkan)
# =============================================================================

ERC20_ABI: list = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
        "stateMutability": "view",
    },
]

# NegRiskAdapter mendukung dua overload splitPosition:
#
#   [1] Simplified (DIREKOMENDASIKAN untuk NegRisk):
#       splitPosition(bytes32 conditionId, uint256 amount)
#
#   [2] CTF-compatible (parameter partition & parentCollectionId DIABAIKAN):
#       splitPosition(address, bytes32, bytes32 conditionId, uint256[], uint256 amount)
#
# Script ini menggunakan versi [1] karena lebih bersih dan eksplisit.
# Versi [2] disertakan sebagai fallback jika kontrak tidak mendukung [1].
NEG_RISK_ADAPTER_ABI: list = [
    # ── splitPosition versi singkat [1] ──────────────────────────────────────
    {
        "name": "splitPosition",
        "type": "function",
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    # ── splitPosition versi CTF-compatible [2] — partition DIABAIKAN ─────────
    {
        "name": "splitPosition",
        "type": "function",
        "inputs": [
            {"name": "_collateralToken", "type": "address"},
            {"name": "_parentCollectionId", "type": "bytes32"},  # selalu bytes32(0)
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_partition", "type": "uint256[]"},         # DIABAIKAN oleh adapter
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    # ── mergePositions — menggabungkan posisi kembali ke USDC.e ──────────────
    {
        "name": "mergePositions",
        "type": "function",
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    # ── getConditionId — konversi questionId → conditionId ───────────────────
    {
        "name": "getConditionId",
        "type": "function",
        "inputs": [{"name": "_questionId", "type": "bytes32"}],
        "outputs": [{"type": "bytes32"}],
        "stateMutability": "view",
    },
    # ── convertPositions — KUNCI STRATEGI CONVERT ─────────────────────────────
    # Fungsi ini memungkinkan pertukaran NO tokens → YES tokens lintas kondisi.
    #
    # Parameter _indexSet (bitmask):
    #   Bit SET   = kondisi yang NO token-nya DIBERIKAN oleh user (user memberi NO)
    #   Bit UNSET = kondisi yang YES token-nya DITERIMA oleh user (user dapat YES)
    #
    # Strategi "1 USDC → semua YES":
    #   Step 1: splitPosition(condition_0) → YES_0 + NO_0   (bayar 1 USDC)
    #   Step 2: convertPositions(marketId, _indexSet=0b1, amount)
    #           → user berikan NO_0, terima YES_1..YES_(N-1) (tanpa bayar USDC lagi!)
    #   Hasil : YES untuk semua N kondisi, avg cost = 1/N USDC per YES ≈ 3.4¢
    #
    # PENTING: Sebelum memanggil ini, user harus setApprovalForAll pada CTF
    # agar NegRiskAdapter boleh menarik NO token dari wallet user.
    {
        "name": "convertPositions",
        "type": "function",
        "inputs": [
            {"name": "_marketId",  "type": "bytes32"},
            {"name": "_indexSet",  "type": "uint256"},  # bitmask NO positions yang disediakan user
            {"name": "_amount",    "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    # ── getQuestionCount — jumlah kondisi dalam market ───────────────────────
    {
        "name": "getQuestionCount",
        "type": "function",
        "inputs": [{"name": "_marketId", "type": "bytes32"}],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
]

CTF_ABI: list = [
    # Diagnostik — cek status kondisi di CTF utama
    {
        "name": "getOutcomeSlotCount",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
    # Diperlukan untuk convertPositions — izinkan NegRiskAdapter tarik NO token
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    # Cek apakah NegRiskAdapter sudah diizinkan
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
    },
    # Cek saldo ERC-1155 batch
    {
        "name": "balanceOfBatch",
        "type": "function",
        "inputs": [
            {"name": "accounts", "type": "address[]"},
            {"name": "ids",      "type": "uint256[]"},
        ],
        "outputs": [{"type": "uint256[]"}],
        "stateMutability": "view",
    },
    # Transfer batch ERC-1155 ke alamat lain
    {
        "name": "safeBatchTransferFrom",
        "type": "function",
        "inputs": [
            {"name": "from",   "type": "address"},
            {"name": "to",     "type": "address"},
            {"name": "ids",    "type": "uint256[]"},
            {"name": "amounts","type": "uint256[]"},
            {"name": "data",   "type": "bytes"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


# =============================================================================
# GAMMA API CLIENT
# =============================================================================

class GammaClient:
    """Klien ringan untuk Polymarket Gamma Markets API."""

    BASE_URL: str = "https://gamma-api.polymarket.com"

    def __init__(self, timeout: int = 20) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def _get(self, endpoint: str, params: Optional[dict] = None) -> any:
        url = f"{self.BASE_URL}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            raise TimeoutError(f"Gamma API timeout: GET {url}")
        except requests.HTTPError as exc:
            raise ConnectionError(
                f"Gamma API HTTP {exc.response.status_code}: GET {url}"
            )
        except requests.RequestException as exc:
            raise ConnectionError(f"Gamma API request error: {exc}")

    def get_event_by_slug(self, slug: str) -> Optional[dict]:
        """
        Ambil event (induk pasar NegRisk) berdasarkan slug.
        Event berisi daftar sub-market beserta conditionId masing-masing.
        """
        data = self._get("/events", params={"slug": slug})
        if not data:
            return None
        return data[0] if isinstance(data, list) else data

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Ambil single market berdasarkan slug."""
        data = self._get("/markets", params={"slug": slug})
        if not data:
            return None
        return data[0] if isinstance(data, list) else data

    def get_markets_by_condition_ids(self, condition_ids: List[str]) -> List[dict]:
        """Ambil detail market berdasarkan daftar conditionId."""
        data = self._get(
            "/markets",
            params={"condition_ids": ",".join(condition_ids), "limit": 100},
        )
        return data if isinstance(data, list) else []

    def extract_condition_ids_from_event(self, event_data: dict) -> List[str]:
        """
        Ekstrak semua conditionId dari data event.
        Gamma API menyimpan sub-market di field 'markets' atau 'children'.
        """
        sub_markets: list = event_data.get("markets") or event_data.get("children") or []
        ids = [m["conditionId"] for m in sub_markets if m.get("conditionId")]
        return ids

    def extract_yes_token_ids_from_event(self, event_data: dict) -> List[int]:
        """
        Ekstrak YES token ID (clobTokenIds[0]) untuk setiap sub-market.
        Returns list of token IDs as integers (ERC-1155 token IDs on CTF).
        """
        import json as _json
        sub_markets: list = event_data.get("markets") or event_data.get("children") or []
        token_ids = []
        for m in sub_markets:
            raw = m.get("clobTokenIds")
            if raw:
                ids = _json.loads(raw) if isinstance(raw, str) else raw
                if ids:
                    token_ids.append(int(ids[0]))  # ids[0] = YES token
        return token_ids

    def extract_all_token_ids_from_event(self, event_data: dict) -> List[int]:
        """
        Ekstrak SEMUA token ID (YES dan NO) untuk setiap sub-market.
        Returns list: [YES_0, NO_0, YES_1, NO_1, ...]
        """
        import json as _json
        sub_markets: list = event_data.get("markets") or event_data.get("children") or []
        token_ids = []
        for m in sub_markets:
            raw = m.get("clobTokenIds")
            if raw:
                ids = _json.loads(raw) if isinstance(raw, str) else raw
                for tid in ids:
                    token_ids.append(int(tid))
        return token_ids


# =============================================================================
# SPLIT BOT UTAMA
# =============================================================================

class NegRiskSplitBot:
    """
    Bot untuk melakukan splitPosition pada semua kondisi dalam NegRisk market.

    Alur kerja:
      1. Validasi konfigurasi & koneksi ke Polygon
      2. Ambil daftar conditionId dari Gamma API (atau turunkan dari marketId)
      3. Verifikasi saldo USDC.e mencukupi
      4. Approve USDC.e ke NegRiskAdapter (sekali saja, max allowance)
      5. Loop: panggil splitPosition(conditionId, amount) untuk setiap kondisi
      6. Tampilkan ringkasan hasil
    """

    CHAIN_ID: int = 137        # Polygon Mainnet
    USDC_DECIMALS: int = 6
    NULL_BYTES32: bytes = b"\x00" * 32

    def __init__(self, dry_run: bool = False) -> None:
        """
        Args:
            dry_run: Jika True, cetak semua langkah tanpa mengirim transaksi.
                     Berguna untuk verifikasi sebelum eksekusi nyata.
        """
        self.dry_run = dry_run
        self.gamma = GammaClient()
        self._validate_config()

        # ── Inisialisasi Web3 ────────────────────────────────────────────────
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
        # Middleware wajib untuk Polygon (PoA chain)
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not self.w3.is_connected():
            log.error(f"Gagal terhubung ke RPC: {POLYGON_RPC_URL}")
            log.error("Coba RPC lain, misalnya Alchemy atau QuickNode.")
            sys.exit(1)

        # ── Load account dari private key ────────────────────────────────────
        self.account = Account.from_key(_RAW_PRIVATE_KEY)

        # ── Inisialisasi kontrak ─────────────────────────────────────────────
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(ADDR["USDC_E"]),
            abi=ERC20_ABI,
        )
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(ADDR["CTF"]),
            abi=CTF_ABI,
        )
        self.adapter = self.w3.eth.contract(
            address=Web3.to_checksum_address(ADDR["NEG_RISK_ADAPTER"]),
            abi=NEG_RISK_ADAPTER_ABI,
        )

        log.info("=" * 65)
        log.info("  POLYMARKET NEGATIVE RISK SPLIT BOT")
        log.info("=" * 65)
        log.info(f"  Wallet      : {self.account.address}")
        log.info(f"  RPC         : {POLYGON_RPC_URL}")
        log.info(f"  Block       : {self.w3.eth.block_number:,}")
        log.info(f"  Market ID   : {NEG_RISK_MARKET_ID}")
        log.info(f"  Slug        : {MARKET_SLUG}")
        log.info(f"  Amount/cond : {SPLIT_AMOUNT_USDC} USDC.e")
        log.info(f"  Max kondisi : {MAX_CONDITIONS}")
        log.info(f"  Dry Run     : {dry_run}")
        log.info("=" * 65)

    # ─────────────────────────────────────────────────────────────────────────
    # VALIDASI
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_config() -> None:
        """Hentikan eksekusi lebih awal jika konfigurasi kritis hilang."""
        if not _RAW_PRIVATE_KEY:
            log.error("PRIVATE_KEY tidak ditemukan di .env!")
            log.error("Salin .env.example ke .env dan isi private key Anda.")
            sys.exit(1)
        if len(_RAW_PRIVATE_KEY) != 66:  # "0x" + 64 hex chars
            log.error(
                f"PRIVATE_KEY tidak valid (panjang={len(_RAW_PRIVATE_KEY)}, harus 66).\n"
                "  Pastikan private key Anda terdiri dari 64 karakter hex."
            )
            sys.exit(1)

    # ─────────────────────────────────────────────────────────────────────────
    # RPC HELPER (retry untuk menghindari rate-limit)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _rpc_call(fn, *args, retries: int = 3, delay: float = 1.0):
        """Panggil fungsi contract dengan retry jika RPC rate-limit / empty response."""
        import time as _time
        last_exc = None
        for attempt in range(retries):
            try:
                return fn(*args).call()
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    _time.sleep(delay * (attempt + 1))
        raise last_exc

    def _wait_receipt(self, tx_hash, timeout: int = 600):
        """
        Tunggu receipt TX dengan retry otomatis jika RPC putus koneksi.
        Jika koneksi terputus (ConnectionResetError / ChunkedEncodingError),
        coba ulang polling hingga 5 kali dengan jeda 5 detik.
        TX yang sudah terkirim ke blockchain tetap aman — hanya menunggu konfirmasi.
        """
        import time as _t
        last_exc = None
        for attempt in range(5):
            try:
                return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
            except Exception as exc:
                err = str(exc)
                if "Connection" in err or "ConnectionReset" in err or "ChunkedEncoding" in err or "reset by peer" in err:
                    last_exc = exc
                    log.warning(
                        f"[RPC] Koneksi terputus saat menunggu receipt "
                        f"(attempt {attempt+1}/5). Coba ulang dalam 5 detik..."
                    )
                    _t.sleep(5)
                else:
                    raise
        log.error("[RPC] Gagal mendapatkan receipt setelah 5 percobaan.")
        raise last_exc

    def _get_nonce(self) -> int:
        """Ambil nonce wallet — coba pending lalu latest, ambil nilai tertinggi."""
        best = 0
        for blk in ("pending", "latest"):
            try:
                n = self.w3.eth.get_transaction_count(self.account.address, blk)
                if n > best:
                    best = n
            except Exception:
                pass
        return best

    def _gas_price(self) -> int:
        """
        Ambil gas price dari RPC, pastikan minimal 100 gwei agar tidak stuck di Polygon.
        Minimum 100 gwei = ~$0.003 per TX, cukup untuk konfirmasi cepat.
        """
        MIN_GWEI = 200
        try:
            rpc_wei = self.w3.eth.gas_price
            return max(rpc_wei, Web3.to_wei(MIN_GWEI, "gwei"))
        except Exception:
            return Web3.to_wei(MIN_GWEI, "gwei")

    def _signed_send(self, tx: dict) -> bytes:
        """
        Sign dan kirim TX. Jika RPC menolak karena 'nonce too low',
        baca nonce yang benar dari pesan error dan retry sekali.
        """
        import re
        signed = self.account.sign_transaction(tx)
        try:
            return self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            m = re.search(r"next nonce (\d+)", str(exc))
            if m:
                correct = int(m.group(1))
                log.warning(f"  Nonce koreksi: {tx['nonce']} → {correct}")
                tx["nonce"] = correct
                signed2 = self.account.sign_transaction(tx)
                return self.w3.eth.send_raw_transaction(signed2.raw_transaction)
            raise

    # ─────────────────────────────────────────────────────────────────────────
    # DIAGNOSTIK
    # ─────────────────────────────────────────────────────────────────────────

    def print_diagnostics(self, condition_ids: List[str]) -> None:
        """Tampilkan saldo, allowance, dan status kondisi untuk debugging."""
        log.info("[DIAGNOSTIK] ─────────────────────────────────────────────")

        # Saldo MATIC (untuk biaya gas)
        matic_wei = self.w3.eth.get_balance(self.account.address)
        log.info(f"  MATIC (gas) : {Web3.from_wei(matic_wei, 'ether'):.6f} MATIC")

        # Saldo USDC.e
        usdc_raw = self._rpc_call(self.usdc.functions.balanceOf, self.account.address)
        usdc_bal = usdc_raw / 10 ** self.USDC_DECIMALS
        log.info(f"  USDC.e      : {usdc_bal:.6f} USDC")

        # Allowance USDC.e → NegRiskAdapter
        allowance_raw = self._rpc_call(
            self.usdc.functions.allowance,
            self.account.address,
            Web3.to_checksum_address(ADDR["NEG_RISK_ADAPTER"]),
        )
        log.info(
            f"  Allowance   : {allowance_raw / 10**self.USDC_DECIMALS:.6f} USDC"
            f" → NegRiskAdapter"
        )

        # Cek beberapa kondisi pertama di CTF utama (harusnya = 0, ini normal)
        log.info("  Status kondisi di CTF utama (slotCount=0 = NORMAL untuk NegRisk):")
        for cid in condition_ids[:3]:
            cid_bytes = _hex_to_bytes32(cid)
            try:
                slot_count = self._rpc_call(self.ctf.functions.getOutcomeSlotCount, cid_bytes)
            except Exception:
                slot_count = "error"
            marker = "✓ NegRisk" if slot_count == 0 else f"!! slotCount={slot_count}"
            log.info(f"    {cid[:18]}…  slotCount={slot_count}  {marker}")

        if len(condition_ids) > 3:
            log.info(f"    … dan {len(condition_ids) - 3} kondisi lainnya")
        log.info("─────────────────────────────────────────────────────────────")

    # ─────────────────────────────────────────────────────────────────────────
    # FETCH CONDITION IDs
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_condition_ids(self) -> List[str]:
        """
        Ambil daftar conditionId untuk setiap pertanyaan biner dalam NegRisk market.

        Strategi (berurutan, berhenti di yang pertama berhasil):
          1. Gamma API — events endpoint (paling andal untuk NegRisk)
          2. Gamma API — markets endpoint
          3. Fallback: turunkan questionId dari marketId, lalu tanya NegRiskAdapter

        Returns:
            List conditionId hex string yang siap digunakan untuk splitPosition.
        """
        log.info(f"[API] Mengambil conditionId untuk: {MARKET_SLUG}")

        # ── Strategi 1: Event slug ───────────────────────────────────────────
        try:
            event = self.gamma.get_event_by_slug(MARKET_SLUG)
            if event:
                ids = self.gamma.extract_condition_ids_from_event(event)
                if ids:
                    log.info(f"[API] Ditemukan {len(ids)} conditionId dari /events")
                    return ids
        except Exception as exc:
            log.warning(f"[API] Event lookup gagal: {exc}")

        # ── Strategi 2: Market slug ──────────────────────────────────────────
        try:
            market = self.gamma.get_market_by_slug(MARKET_SLUG)
            if market:
                log.info(f"[API] Market: {market.get('title', MARKET_SLUG)}")
                # Cek apakah ada field conditionId langsung
                cid = market.get("conditionId")
                if cid:
                    # Mungkin pasar tunggal, cek apakah ada sub-market
                    sub = market.get("markets") or market.get("children") or []
                    sub_ids = [m["conditionId"] for m in sub if m.get("conditionId")]
                    if sub_ids:
                        log.info(f"[API] Ditemukan {len(sub_ids)} conditionId dari sub-market")
                        return sub_ids
                    log.info("[API] Ditemukan 1 conditionId dari market langsung")
                    return [cid]
        except Exception as exc:
            log.warning(f"[API] Market lookup gagal: {exc}")

        # ── Strategi 3: Fallback — turunkan dari marketId ────────────────────
        log.warning(
            "[FALLBACK] Gamma API tidak mengembalikan conditionId. "
            "Menurunkan questionIds dari marketId..."
        )
        return self._derive_condition_ids_from_market_id(NEG_RISK_MARKET_ID, n=MAX_CONDITIONS)

    def _derive_condition_ids_from_market_id(
        self, market_id_hex: str, n: int
    ) -> List[str]:
        """
        Turunkan conditionId dari marketId menggunakan pola NegRisk.

        Dalam arsitektur NegRisk Polymarket:
          - marketId   : bytes32 (byte terakhir biasanya 0x00)
          - questionId : bytes32 identik dengan marketId kecuali byte terakhir
                         yang berisi indeks pertanyaan (0x00 s/d 0x1d untuk 30 Q)
          - conditionId: NegRiskAdapter.getConditionId(questionId) → bytes32

        Args:
            market_id_hex: Market ID dalam format hex "0x..."
            n            : Jumlah pertanyaan dalam market

        Returns:
            List conditionId hex string
        """
        market_bytes = bytearray(bytes.fromhex(market_id_hex.lstrip("0x").zfill(64)))
        condition_ids: List[str] = []

        log.info(f"[FALLBACK] Menurunkan {n} conditionId dari marketId...")
        for i in range(n):
            q_bytes = bytearray(market_bytes)
            q_bytes[-1] = i  # Set byte terakhir ke indeks pertanyaan
            question_id = bytes(q_bytes)

            try:
                cond_bytes = self.adapter.functions.getConditionId(
                    question_id
                ).call()
                cond_hex = "0x" + cond_bytes.hex()
                condition_ids.append(cond_hex)
                log.debug(
                    f"  Q[{i:02d}]: questionId[-2:]={question_id.hex()[-4:]} "
                    f"→ conditionId={cond_hex[:18]}…"
                )
            except Exception as exc:
                log.error(f"  Q[{i:02d}]: Gagal dapat conditionId: {exc}")

        if not condition_ids:
            log.error(
                "Tidak ada conditionId yang berhasil diturunkan!\n"
                "  Kemungkinan:\n"
                "    • Market ID salah\n"
                "    • NegRiskAdapter tidak mengenal market ini\n"
                "    • Market belum terdaftar di NegRiskOperator"
            )
            sys.exit(1)

        log.info(f"[FALLBACK] Berhasil mendapatkan {len(condition_ids)} conditionId")
        return condition_ids

    # ─────────────────────────────────────────────────────────────────────────
    # APPROVE USDC.e
    # ─────────────────────────────────────────────────────────────────────────

    def ensure_usdc_approved(self, total_usdc_needed: float) -> Optional[str]:
        """
        Pastikan NegRiskAdapter memiliki allowance USDC.e yang cukup.
        Jika belum, kirim transaksi approve dengan jumlah maksimum (unlimited).

        Args:
            total_usdc_needed: Jumlah minimum USDC.e yang diperlukan

        Returns:
            TX hash approval (str) jika transaksi dikirim, None jika sudah cukup.

        Raises:
            RuntimeError: Jika transaksi approve gagal (status != 1)
        """
        spender = Web3.to_checksum_address(ADDR["NEG_RISK_ADAPTER"])
        required_raw = int(total_usdc_needed * 10 ** self.USDC_DECIMALS)

        # Cek allowance dengan retry — RPC bisa rate-limit setelah banyak panggilan
        current_raw = 0
        for _attempt in range(3):
            try:
                time.sleep(0.5 * (_attempt + 1))  # backoff: 0.5s, 1s, 1.5s
                current_raw = self.usdc.functions.allowance(
                    self.account.address, spender
                ).call()
                break
            except Exception as exc:
                if _attempt == 2:
                    log.warning(f"[APPROVE] Gagal baca allowance setelah 3 percobaan: {exc}")
                    log.warning("[APPROVE] Asumsikan allowance = 0, lanjut approve...")
        current_usdc = current_raw / 10 ** self.USDC_DECIMALS

        log.info(
            f"[APPROVE] Allowance saat ini : {current_usdc:.6f} USDC  |  "
            f"Dibutuhkan: {total_usdc_needed:.6f} USDC"
        )

        if current_raw >= required_raw:
            log.info("[APPROVE] Allowance sudah mencukupi, skip approval.")
            return None

        if self.dry_run:
            log.info("[DRY-RUN] Simulasi: approve USDC.e ke NegRiskAdapter (tidak dikirim)")
            return "dry-run-approve"

        MAX_UINT256 = 2**256 - 1  # Approve "unlimited" agar tidak perlu approve berulang
        gas_price = self._gas_price()
        nonce = self._get_nonce()

        tx = self.usdc.functions.approve(spender, MAX_UINT256).build_transaction(
            {
                "from": self.account.address,
                "nonce": nonce,
                "gasPrice": gas_price,  # +20% buffer agar tidak stuck
                "gas": 80_000,
                "chainId": self.CHAIN_ID,
            }
        )

        tx_hash = self._signed_send(tx)

        log.info(f"[APPROVE] TX terkirim  : {tx_hash.hex()}")
        log.info(f"[APPROVE] Polygonscan  : https://polygonscan.com/tx/{tx_hash.hex()}")
        log.info("[APPROVE] Menunggu konfirmasi...")

        receipt = self._wait_receipt(tx_hash, timeout=600)
        if receipt.status != 1:
            raise RuntimeError(
                f"Transaksi Approve GAGAL (status=0)!\n"
                f"  TX: https://polygonscan.com/tx/{tx_hash.hex()}"
            )

        log.info(f"[APPROVE] Berhasil! Gas used: {receipt.gasUsed:,}")
        return tx_hash.hex()

    # ─────────────────────────────────────────────────────────────────────────
    # SPLIT SINGLE CONDITION
    # ─────────────────────────────────────────────────────────────────────────

    def split_single_condition(
        self,
        condition_id_hex: str,
        amount_usdc: float,
        max_retries: int = 3,
    ) -> Optional[str]:
        """
        Panggil NegRiskAdapter.splitPosition(conditionId, amount).

        Satu panggilan ini mengubah `amount` USDC.e menjadi:
          - `amount` token YES untuk kondisi ini
          - `amount` token NO  untuk kondisi ini

        NegRiskAdapter menangani semua logika internal (condition preparation,
        token minting) secara transparan — tidak perlu parentCollectionId
        maupun partition.

        Args:
            condition_id_hex: conditionId dalam format "0x..." (bytes32)
            amount_usdc     : Jumlah USDC.e yang akan di-split
            max_retries     : Jumlah percobaan ulang jika terjadi error sementara

        Returns:
            TX hash (hex string) jika berhasil, None jika dry_run.

        Raises:
            RuntimeError: Jika semua percobaan gagal.
        """
        cond_bytes = _hex_to_bytes32(condition_id_hex)
        amount_raw = int(amount_usdc * 10 ** self.USDC_DECIMALS)

        if self.dry_run:
            log.info(
                f"  [DRY-RUN] splitPosition({condition_id_hex[:18]}…, {amount_usdc} USDC)"
            )
            return None

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                # ── Estimasi gas ─────────────────────────────────────────────
                try:
                    # Gunakan selector eksplisit untuk menghindari ambiguitas overload
                    gas_est = self.adapter.functions["splitPosition(bytes32,uint256)"](
                        cond_bytes, amount_raw
                    ).estimate_gas({"from": self.account.address})
                    gas_limit = int(gas_est * 1.35)  # +35% buffer untuk keamanan
                    log.debug(f"  Gas estimasi: {gas_est:,} → limit: {gas_limit:,}")
                except Exception as e_gas:
                    log.warning(
                        f"  Estimasi gas gagal (attempt {attempt}): {e_gas}\n"
                        f"  Menggunakan gas limit default 450_000"
                    )
                    gas_limit = 450_000

                # ── Build & kirim transaksi ───────────────────────────────────
                gas_price = self._gas_price()
                nonce = self._get_nonce()

                tx = self.adapter.functions["splitPosition(bytes32,uint256)"](
                    cond_bytes, amount_raw
                ).build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": nonce,
                        "gasPrice": gas_price,
                        "gas": gas_limit,
                        "chainId": self.CHAIN_ID,
                    }
                )

                tx_hash = self._signed_send(tx)

                log.info(f"  TX: https://polygonscan.com/tx/{tx_hash.hex()}")

                # ── Tunggu konfirmasi ─────────────────────────────────────────
                receipt = self._wait_receipt(tx_hash, timeout=300)
                if receipt.status == 1:
                    log.info(f"  OK  Gas used: {receipt.gasUsed:,}")
                    return tx_hash.hex()
                else:
                    raise RuntimeError(
                        f"Transaksi reverted on-chain.\n"
                        f"  TX: https://polygonscan.com/tx/{tx_hash.hex()}\n"
                        f"  Periksa tab 'Internal Txns' di Polygonscan untuk pesan revert."
                    )

            except RuntimeError:
                # Revert on-chain — tidak perlu retry, masalahnya bukan sementara
                raise
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    wait_s = 5 * attempt
                    log.warning(
                        f"  [Attempt {attempt}/{max_retries}] Error: {exc}\n"
                        f"  Retry dalam {wait_s} detik..."
                    )
                    time.sleep(wait_s)
                else:
                    raise RuntimeError(
                        f"Split gagal setelah {max_retries} percobaan.\n"
                        f"  Condition: {condition_id_hex}\n"
                        f"  Error: {last_error}"
                    ) from last_error

        return None  # tidak pernah dicapai

    # ─────────────────────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────────────────────
    # APPROVE CTF (ERC1155) — Diperlukan untuk convertPositions
    # ─────────────────────────────────────────────────────────────────────────

    def ensure_ctf_approved(self) -> Optional[str]:
        """
        Pastikan NegRiskAdapter diizinkan menarik ERC-1155 (NO tokens) dari wallet.
        Diperlukan sebelum memanggil convertPositions.

        Returns:
            TX hash jika approval baru dikirim, None jika sudah diizinkan.
        """
        operator = Web3.to_checksum_address(ADDR["NEG_RISK_ADAPTER"])

        already_approved = self.ctf.functions.isApprovedForAll(
            self.account.address, operator
        ).call()

        if already_approved:
            log.info("[CTF-APPROVE] NegRiskAdapter sudah diizinkan untuk ERC-1155, skip.")
            return None

        if self.dry_run:
            log.info("[DRY-RUN] Simulasi: setApprovalForAll CTF → NegRiskAdapter")
            return "dry-run-ctf-approve"

        log.info("[CTF-APPROVE] Mengizinkan NegRiskAdapter menarik NO tokens dari CTF...")
        gas_price = self._gas_price()
        nonce = self._get_nonce()

        tx = self.ctf.functions.setApprovalForAll(operator, True).build_transaction({
            "from":     self.account.address,
            "nonce":    nonce,
            "gasPrice": gas_price,
            "gas":      80_000,
            "chainId":  self.CHAIN_ID,
        })

        tx_hash = self._signed_send(tx)
        log.info(f"[CTF-APPROVE] TX: https://polygonscan.com/tx/{tx_hash.hex()}")

        receipt = self._wait_receipt(tx_hash, timeout=600)
        if receipt.status != 1:
            raise RuntimeError(f"setApprovalForAll gagal! TX: {tx_hash.hex()}")

        log.info(f"[CTF-APPROVE] Berhasil! Gas used: {receipt.gasUsed:,}")
        return tx_hash.hex()

    # ─────────────────────────────────────────────────────────────────────────
    # CONVERT STRATEGY — Cara paling efisien untuk dapat semua YES token
    # ─────────────────────────────────────────────────────────────────────────

    def run_convert_strategy(
        self,
        condition_ids: List[str],
        market_id_hex: str,
        amount_usdc: float,
    ) -> None:
        """
        Strategi Convert: mendapatkan YES token untuk SEMUA kondisi dengan biaya
        minimum — hanya 2 transaksi (bukan 30 transaksi seperti strategi Split).

        Mekanisme:
          Step 1: splitPosition(condition_0, amount)
                  → Bayar `amount` USDC, dapat YES_0 + NO_0
          Step 2: convertPositions(marketId, indexSet=0b1, amount)
                  → Berikan NO_0, dapat YES_1..YES_(N-1) GRATIS

        Hasil:
          - YES token untuk SEMUA N kondisi di wallet
          - Total USDC = hanya `amount` USDC (bukan amount × N)
          - Avg cost per YES = amount / (N × amount) = 1/N ≈ 3.4¢ (30 kondisi)

        Args:
            condition_ids : List conditionId (condition_0 harus di index [0])
            market_id_hex : NegRisk Market ID (bytes32 hex)
            amount_usdc   : Jumlah USDC untuk split awal (= jumlah YES per kondisi)
        """
        n = len(condition_ids)
        avg_cost = 100.0 / n if n > 0 else 0
        market_bytes = _hex_to_bytes32(market_id_hex)
        amount_raw = int(amount_usdc * 10 ** self.USDC_DECIMALS)

        # ── VALIDASI: pastikan NEG_RISK_MARKET_ID cocok dengan MARKET_SLUG ───
        log.info("[VALIDASI] Memeriksa kesesuaian NEG_RISK_MARKET_ID dengan MARKET_SLUG...")
        event_data = self.gamma.get_event_by_slug(MARKET_SLUG)
        if event_data:
            api_market_id = (event_data.get("negRiskMarketID") or "").lower().strip()
            env_market_id = market_id_hex.lower().strip()
            if api_market_id and api_market_id != env_market_id:
                log.error("=" * 65)
                log.error("  ABORT: NEG_RISK_MARKET_ID tidak cocok dengan MARKET_SLUG!")
                log.error(f"  Dari slug '{MARKET_SLUG}':")
                log.error(f"    API marketId  : {api_market_id}")
                log.error(f"    .env marketId : {env_market_id}")
                log.error("  Perbaiki NEG_RISK_MARKET_ID di .env agar sesuai slug,")
                log.error("  atau ubah MARKET_SLUG agar sesuai market yang dituju.")
                log.error("=" * 65)
                sys.exit(1)
            else:
                log.info(f"[VALIDASI] OK — marketId cocok dengan slug '{MARKET_SLUG}'")

        # ── Tentukan condition untuk split menggunakan NegRisk question index 0 ──
        # PENTING: Jangan gunakan condition_ids[0] dari Gamma API karena urutannya
        # bisa berbeda dengan NegRisk internal question index.
        # NegRisk question 0 = conditionId dari questionId dengan last byte = 0x00.
        # indexSet = 0b1 = bit 0 = user menyediakan NO untuk question 0.
        log.info("[VALIDASI] Menurunkan NegRisk question index 0 dari marketId...")
        negrisk_cond_ids = self._derive_condition_ids_from_market_id(market_id_hex, n=n)
        if not negrisk_cond_ids:
            log.error("Tidak bisa turunkan conditionId dari marketId!")
            sys.exit(1)
        cond_q0 = negrisk_cond_ids[0]  # selalu question index 0 di NegRisk
        log.info(f"[VALIDASI] NegRisk Q0 conditionId : {cond_q0[:22]}…")

        # Cek apakah question 0 sudah resolved di Gamma API
        if event_data:
            sub_mks = event_data.get("markets") or event_data.get("children") or []
            for _sm in sub_mks:
                if _sm.get("conditionId", "").lower() == cond_q0.lower():
                    _closed   = _sm.get("closed", False)
                    _resolved = _sm.get("resolved", False)
                    _title    = _sm.get("groupItemTitle") or _sm.get("question") or "?"
                    if _closed or _resolved:
                        log.error("=" * 65)
                        log.error(f"  ABORT: NegRisk question 0 sudah RESOLVED!")
                        log.error(f"  Kondisi  : {_title}  (closed={_closed}, resolved={_resolved})")
                        log.error("  Market ini sudah sebagian resolved — convert tidak bisa dilakukan.")
                        log.error("  Gunakan market baru yang belum ada kondisi resolved.")
                        log.error("=" * 65)
                        sys.exit(1)
                    log.info(f"[VALIDASI] OK — Q0 '{_title}' belum resolved")
                    break

        # indexSet = 1 (bit 0) → user berikan NO untuk question 0
        index_set = 1

        log.info("=" * 65)
        log.info("  STRATEGI: CONVERT (1 USDC → YES untuk semua kondisi)")
        log.info("=" * 65)
        log.info(f"  Market ID    : {market_id_hex[:18]}…")
        log.info(f"  Total kondisi: {n}")
        log.info(f"  Amount/set   : {amount_usdc} USDC.e")
        log.info(f"  Avg cost YES : ~{avg_cost:.2f}¢ per token")
        log.info(f"  Total USDC   : {amount_usdc} USDC (bukan {amount_usdc * n}!)")
        log.info("=" * 65)

        # ── Step 1: Split question 0 (NegRisk-derived) → YES_0 + NO_0 ────────
        # Gunakan cond_q0 (dari NegRisk derivation), BUKAN condition_ids[0] dari Gamma API.
        # Ini memastikan NO token yang kita pegang sesuai dengan indexSet=1 di convertPositions.
        cond_0 = cond_q0
        skip_split = False
        if not self.dry_run and event_data:
            import json as _json
            sub_markets_ev = event_data.get("markets") or event_data.get("children") or []
            no0_token_id = None
            for m in sub_markets_ev:
                if m.get("conditionId", "").lower() == cond_0.lower():
                    raw = m.get("clobTokenIds")
                    if raw:
                        ids = _json.loads(raw) if isinstance(raw, str) else raw
                        if len(ids) >= 2:
                            no0_token_id = int(ids[1])  # ids[1] = NO token
                    break
            if no0_token_id is not None:
                try:
                    bal = self._rpc_call(
                        self.ctf.functions.balanceOfBatch,
                        [self.account.address],
                        [no0_token_id],
                    )
                    if bal and bal[0] >= int(amount_usdc * 10 ** self.USDC_DECIMALS):
                        skip_split = True
                        log.info(f"[STEP 1/2] NO_0 sudah ada di wallet ({bal[0]/1e6:.2f} token) — SKIP splitPosition")
                except Exception:
                    pass  # Jika gagal cek, tetap lanjut split

        if not skip_split:
            log.info(f"\n[STEP 1/2] splitPosition(condition_0, {amount_usdc} USDC)")
            log.info(f"  Condition: {cond_0}")
            log.info("  Hasil: YES_0 + NO_0 masuk ke wallet")

            tx1 = self.split_single_condition(cond_0, amount_usdc)
            if tx1:
                log.info(f"  TX Split: https://polygonscan.com/tx/{tx1}")
                time.sleep(3)  # Tunggu TX dikonfirmasi sebelum lanjut

        # ── Step 2: convertPositions → berikan NO_0, dapat YES_1..YES_(N-1) ─
        log.info(f"\n[STEP 2/2] convertPositions(marketId, indexSet={index_set:#010b}, {amount_usdc} USDC)")
        log.info(f"  Memberikan : NO_0 untuk condition[0]")
        log.info(f"  Menerima   : YES untuk {n - 1} kondisi lainnya")

        if self.dry_run:
            log.info(f"  [DRY-RUN] convertPositions({market_id_hex[:18]}…, {index_set}, {amount_usdc} USDC)")
        else:
            try:
                # Gas untuk convertPositions iterasi 30 kondisi:
                # ~150k gas × 29 splits + overhead ≈ 4.5M minimum.
                # Pakai 6M untuk margin aman (biaya ~$0.12 di 200 gwei).
                gas_limit = 6_000_000
                log.info(f"  Gas limit  : {gas_limit:,} (fixed, tidak pakai estimate)")

                gas_price = self._gas_price()
                nonce = self._get_nonce()

                tx2_raw = self.adapter.functions.convertPositions(
                    market_bytes, index_set, amount_raw
                ).build_transaction({
                    "from":     self.account.address,
                    "nonce":    nonce,
                    "gasPrice": gas_price,
                    "gas":      gas_limit,
                    "chainId":  self.CHAIN_ID,
                })

                tx2_hash = self._signed_send(tx2_raw)
                log.info(f"  TX Convert: https://polygonscan.com/tx/{tx2_hash.hex()}")

                receipt2 = self._wait_receipt(tx2_hash, timeout=600)
                if receipt2.status == 1:
                    log.info(f"  OK  Gas used: {receipt2.gasUsed:,}")
                else:
                    raise RuntimeError(
                        f"convertPositions reverted!\n"
                        f"  TX: https://polygonscan.com/tx/{tx2_hash.hex()}\n"
                        f"  Kemungkinan: NO_0 token belum ada di wallet (Step 1 belum selesai),\n"
                        f"  atau indexSet salah, atau market ID tidak valid."
                    )
            except Exception as exc:
                log.error(f"  [GAGAL] convertPositions: {exc}")
                raise

        # ── Ringkasan ────────────────────────────────────────────────────────
        log.info("\n" + "=" * 65)
        log.info("  HASIL CONVERT STRATEGY")
        log.info("=" * 65)
        log.info(f"  YES token di wallet: {n} kondisi × {amount_usdc} token")
        log.info(f"  Total USDC dipakai : {amount_usdc} USDC")
        log.info(f"  Avg cost per YES   : {avg_cost:.2f}¢")
        log.info(f"  Efisiensi vs Split : hemat {(amount_usdc * (n-1)):.1f} USDC!")
        log.info("=" * 65)

    # ─────────────────────────────────────────────────────────────────────────
    # TRANSFER STRATEGY — kirim YES token dari EOA ke proxy wallet
    # ─────────────────────────────────────────────────────────────────────────

    def run_transfer_strategy(self, yes_token_ids: List[int], proxy_address: str,
                               include_no: bool = False) -> None:
        """
        Transfer YES token (dan opsional NO token) dari EOA ke proxy wallet Polymarket.

        Args:
            yes_token_ids: List token ID yang akan dicek dan ditransfer
            proxy_address: Alamat proxy wallet tujuan
            include_no:    Jika True, token_ids sudah mencakup YES+NO (dari extract_all_token_ids)
        """
        proxy = Web3.to_checksum_address(proxy_address)
        wallet = self.account.address
        n = len(yes_token_ids)

        log.info("=" * 65)
        log.info("  STRATEGI: TRANSFER YES TOKEN → PROXY WALLET")
        log.info("=" * 65)
        log.info(f"  Dari (EOA)   : {wallet}")
        log.info(f"  Ke (Proxy)   : {proxy}")
        log.info(f"  Total token  : {n}")
        log.info("=" * 65)

        # Cek saldo batch
        log.info("[STEP 1/2] Cek saldo YES token di EOA...")
        accounts = [wallet] * n
        balances = self._rpc_call(
            self.ctf.functions.balanceOfBatch, accounts, yes_token_ids
        )

        ids_to_send    = [yes_token_ids[i] for i, b in enumerate(balances) if b > 0]
        amounts_to_send = [b for b in balances if b > 0]

        if not ids_to_send:
            log.warning("Tidak ada YES token di EOA — tidak ada yang ditransfer.")
            return

        log.info(f"  Ditemukan {len(ids_to_send)} dari {n} YES token dengan saldo > 0")
        for tid, amt in zip(ids_to_send, amounts_to_send):
            log.info(f"    Token {str(tid)[:20]}…  saldo: {amt / 10**6:.6f}")

        # Transfer
        log.info(f"\n[STEP 2/2] safeBatchTransferFrom → {proxy[:22]}…")

        if self.dry_run:
            log.info("[DRY-RUN] Simulasi: safeBatchTransferFrom (tidak dikirim)")
            return

        tx = self.ctf.functions.safeBatchTransferFrom(
            wallet, proxy, ids_to_send, amounts_to_send, b""
        ).build_transaction({
            "from":     wallet,
            "chainId":  self.CHAIN_ID,
            "gas":      500_000 + 30_000 * len(ids_to_send),
            "maxFeePerGas":         Web3.to_wei("200", "gwei"),
            "maxPriorityFeePerGas": Web3.to_wei("100", "gwei"),
            "nonce": self._get_nonce(),
        })
        tx_hash = self._signed_send(tx)
        hex_hash = tx_hash.hex()
        log.info(f"  TX: https://polygonscan.com/tx/{hex_hash}")
        log.info("  Menunggu konfirmasi...")
        receipt = self._wait_receipt(tx_hash, timeout=600)
        if receipt["status"] == 1:
            log.info(f"  OK  Gas used: {receipt['gasUsed']:,}")
        else:
            log.error("  Transfer GAGAL! Cek TX di Polygonscan.")
            return

        log.info("")
        log.info("=" * 65)
        log.info("  TRANSFER SELESAI")
        log.info("=" * 65)
        log.info(f"  {len(ids_to_send)} YES token dikirim ke proxy wallet")
        log.info(f"  Cek porto: https://polymarket.com/portfolio")
        log.info("=" * 65)

    # ─────────────────────────────────────────────────────────────────────────
    # CANCEL STRATEGY — batalkan TX pending dengan nonce yang sama
    # ─────────────────────────────────────────────────────────────────────────

    def run_cancel_strategy(self, cancel_tx: Optional[str], cancel_nonce: Optional[int]) -> None:
        """
        Batalkan TX pending dengan mengirim TX kosong ke diri sendiri
        menggunakan nonce yang sama + gas 3× lebih tinggi.

        Args:
            cancel_tx:    TX hash pending yang ingin dibatalkan
            cancel_nonce: Nonce TX pending (alternatif jika TX hash tidak diketahui)
        """
        wallet = self.account.address

        # Cari nonce dari TX hash jika diberikan
        target_nonce = cancel_nonce
        if cancel_tx:
            tx_hash_bytes = cancel_tx if cancel_tx.startswith("0x") else "0x" + cancel_tx
            log.info(f"[CANCEL] Mencari nonce untuk TX: {tx_hash_bytes[:22]}…")
            try:
                pending_tx = self.w3.eth.get_transaction(tx_hash_bytes)
                target_nonce = pending_tx["nonce"]
                old_gas = pending_tx.get("gasPrice") or pending_tx.get("maxFeePerGas") or 0
                log.info(f"[CANCEL] Nonce TX pending : {target_nonce}")
                log.info(f"[CANCEL] Gas lama         : {Web3.from_wei(old_gas, 'gwei'):.0f} gwei")
            except Exception as exc:
                log.error(f"[CANCEL] Tidak bisa baca TX dari RPC: {exc}")
                log.error("[CANCEL] Coba gunakan --cancel-nonce <N> secara langsung.")
                sys.exit(1)

        if target_nonce is None:
            log.error("[CANCEL] Harus berikan --cancel-tx atau --cancel-nonce")
            sys.exit(1)

        # Gas untuk cancel: 500 gwei minimum (jauh lebih tinggi dari TX stuck)
        cancel_gas = max(Web3.to_wei("500", "gwei"), self._gas_price() * 3)
        log.info(f"[CANCEL] Gas cancel      : {Web3.from_wei(cancel_gas, 'gwei'):.0f} gwei")

        log.info("=" * 65)
        log.info("  CANCEL TX PENDING")
        log.info("=" * 65)
        log.info(f"  Wallet : {wallet}")
        log.info(f"  Nonce  : {target_nonce}")
        log.info(f"  Gas    : {Web3.from_wei(cancel_gas, 'gwei'):.0f} gwei")
        log.info(f"  Aksi   : kirim 0 MATIC ke diri sendiri (replace TX stuck)")
        log.info("=" * 65)

        if self.dry_run:
            log.info("[DRY-RUN] Simulasi cancel TX (tidak dikirim)")
            return

        cancel_tx_dict = {
            "from":     wallet,
            "to":       wallet,
            "value":    0,
            "data":     b"",
            "gas":      21_000,
            "gasPrice": cancel_gas,
            "nonce":    target_nonce,
            "chainId":  self.CHAIN_ID,
        }

        tx_hash = self._signed_send(cancel_tx_dict)
        hex_hash = tx_hash.hex()
        log.info(f"[CANCEL] TX cancel terkirim!")
        log.info(f"[CANCEL] TX: https://polygonscan.com/tx/{hex_hash}")
        log.info("[CANCEL] Menunggu konfirmasi...")

        receipt = self._wait_receipt(tx_hash, timeout=120)
        if receipt["status"] == 1:
            log.info(f"[CANCEL] Berhasil! TX pending dengan nonce {target_nonce} sudah dibatalkan.")
        else:
            log.error("[CANCEL] Cancel TX gagal. Cek Polygonscan.")

    # ─────────────────────────────────────────────────────────────────────────
    # BALANCE STRATEGY — tampilkan saldo YES dan NO token di wallet
    # ─────────────────────────────────────────────────────────────────────────

    def run_balance_strategy(self, condition_ids: List[str]) -> None:
        """Tampilkan saldo YES dan NO token untuk semua kondisi di wallet saat ini."""
        import json as _json
        wallet = self.account.address

        log.info("=" * 65)
        log.info("  SALDO TOKEN DI WALLET")
        log.info("=" * 65)
        log.info(f"  Wallet : {wallet}")
        log.info("=" * 65)

        event_data = self.gamma.get_event_by_slug(MARKET_SLUG)
        if not event_data:
            log.error(f"Tidak bisa ambil event data untuk slug: {MARKET_SLUG}")
            sys.exit(1)

        sub_markets = event_data.get("markets") or event_data.get("children") or []
        token_map: dict = {}
        title_map: dict = {}
        for m in sub_markets:
            cid = m.get("conditionId")
            raw = m.get("clobTokenIds")
            if cid and raw:
                ids = _json.loads(raw) if isinstance(raw, str) else raw
                if len(ids) >= 2:
                    token_map[cid] = (int(ids[0]), int(ids[1]))
                    title_map[cid] = m.get("groupItemTitle") or m.get("question") or cid[:20]

        cids = [c for c in condition_ids if c in token_map]
        yes_ids = [token_map[c][0] for c in cids]
        no_ids  = [token_map[c][1] for c in cids]

        accounts_list = [wallet] * len(yes_ids)
        yes_bals = self._rpc_call(self.ctf.functions.balanceOfBatch, accounts_list, yes_ids)
        no_bals  = self._rpc_call(self.ctf.functions.balanceOfBatch, accounts_list, no_ids)

        total_yes = total_no = 0
        for i, cid in enumerate(cids):
            y, n = yes_bals[i], no_bals[i]
            if y > 0 or n > 0:
                label = title_map.get(cid, cid[:20])
                log.info(f"  [{i:02d}] {label}")
                if y > 0:
                    log.info(f"        YES: {y / 1e6:.6f}  (token id: {str(yes_ids[i])[:20]}…)")
                if n > 0:
                    log.info(f"        NO : {n / 1e6:.6f}  (token id: {str(no_ids[i])[:20]}…)")
                total_yes += y
                total_no  += n

        if total_yes == 0 and total_no == 0:
            log.info("  (Tidak ada token YES/NO di wallet ini)")
        else:
            log.info("─" * 65)
            log.info(f"  Total YES : {total_yes / 1e6:.6f} USDC")
            log.info(f"  Total NO  : {total_no  / 1e6:.6f} USDC")
        log.info("=" * 65)

    # ─────────────────────────────────────────────────────────────────────────
    # MERGE STRATEGY — kembalikan YES+NO token ke USDC (kebalikan split)
    # ─────────────────────────────────────────────────────────────────────────

    def run_merge_strategy(self, condition_ids: List[str]) -> None:
        """
        Untuk setiap conditionId: cek saldo YES dan NO token di wallet.
        Jika keduanya ada, panggil mergePositions(conditionId, min(YES, NO))
        untuk mendapatkan kembali USDC.e.
        """
        wallet = self.account.address

        log.info("=" * 65)
        log.info("  STRATEGI: MERGE (YES + NO → USDC.e)")
        log.info("=" * 65)

        # Ambil YES dan NO token IDs dari Gamma API
        event_data = self.gamma.get_event_by_slug(MARKET_SLUG)
        if not event_data:
            log.error(f"Tidak bisa ambil event data untuk slug: {MARKET_SLUG}")
            sys.exit(1)

        import json as _json
        sub_markets = event_data.get("markets") or event_data.get("children") or []
        # Bangun map conditionId → (yes_token_id, no_token_id)
        token_map: dict = {}
        for m in sub_markets:
            cid = m.get("conditionId")
            raw = m.get("clobTokenIds")
            if cid and raw:
                ids = _json.loads(raw) if isinstance(raw, str) else raw
                if len(ids) >= 2:
                    token_map[cid] = (int(ids[0]), int(ids[1]))  # YES, NO

        # Cek saldo batch untuk YES dan NO tokens semua kondisi
        yes_ids = [token_map[c][0] for c in condition_ids if c in token_map]
        no_ids  = [token_map[c][1] for c in condition_ids if c in token_map]
        cids_ordered = [c for c in condition_ids if c in token_map]

        if not yes_ids:
            log.error("Tidak ada token ID ditemukan dari Gamma API.")
            return

        accounts_list = [wallet] * len(yes_ids)
        yes_bals = self._rpc_call(self.ctf.functions.balanceOfBatch, accounts_list, yes_ids)
        no_bals  = self._rpc_call(self.ctf.functions.balanceOfBatch, accounts_list, no_ids)

        # Temukan kondisi dengan YES+NO > 0
        to_merge = []
        for i, cid in enumerate(cids_ordered):
            y, n = yes_bals[i], no_bals[i]
            if y > 0 and n > 0:
                amount = min(y, n)
                to_merge.append((cid, amount))
                log.info(f"  [{i+1:02d}] {cid[:20]}…  YES={y/1e6:.2f}  NO={n/1e6:.2f}  → merge {amount/1e6:.2f} USDC")

        if not to_merge:
            log.warning("Tidak ada pasangan YES+NO di wallet — tidak ada yang di-merge.")
            return

        total_recover = sum(a for _, a in to_merge) / 1e6
        log.info(f"\n  Total USDC yang akan dikembalikan: {total_recover:.6f} USDC")
        log.info(f"  Jumlah kondisi: {len(to_merge)}")
        log.info("=" * 65)

        if self.dry_run:
            log.info("[DRY-RUN] Simulasi merge (tidak dikirim)")
            return

        success = 0
        for idx, (cid, amount_raw) in enumerate(to_merge, 1):
            cid_bytes = _hex_to_bytes32(cid)
            log.info(f"\n[MERGE {idx:02d}/{len(to_merge):02d}] {cid[:20]}…  {amount_raw/1e6:.6f} USDC")
            try:
                tx = self.adapter.functions.mergePositions(
                    cid_bytes, amount_raw
                ).build_transaction({
                    "from":     wallet,
                    "chainId":  self.CHAIN_ID,
                    "gas":      300_000,
                    "gasPrice": self._gas_price(),
                    "nonce":    self._get_nonce(),
                })
                tx_hash = self._signed_send(tx)
                log.info(f"  TX: https://polygonscan.com/tx/{tx_hash.hex()}")
                receipt = self._wait_receipt(tx_hash, timeout=600)
                if receipt["status"] == 1:
                    log.info(f"  OK  Gas: {receipt['gasUsed']:,}  → +{amount_raw/1e6:.2f} USDC kembali")
                    success += 1
                else:
                    log.error(f"  GAGAL! Cek TX di Polygonscan.")
            except Exception as exc:
                log.error(f"  Error: {exc}")

        log.info("")
        log.info("=" * 65)
        log.info("  MERGE SELESAI")
        log.info("=" * 65)
        log.info(f"  Berhasil : {success}/{len(to_merge)} kondisi")
        log.info(f"  USDC dikembalikan: ~{sum(a for _, a in to_merge[:success])/1e6:.2f} USDC")
        log.info("=" * 65)

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRYPOINT UTAMA
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, strategy: str = "split", transfer_to: Optional[str] = None,
            cancel_tx: Optional[str] = None, cancel_nonce: Optional[int] = None,
            include_no: bool = False) -> None:
        """
        Jalankan bot.

        Args:
            strategy:    "split"    → splitPosition × N (dapat YES+NO per kondisi)
                         "convert"  → Split 1 kondisi + convertPositions (YES-only semua)
                         "transfer" → Transfer YES token dari EOA ke proxy wallet
                         "cancel"   → Batalkan TX pending
            transfer_to: Alamat proxy wallet (hanya untuk strategy "transfer")
            cancel_tx:   TX hash pending (untuk strategy "cancel")
            cancel_nonce: Nonce TX pending (untuk strategy "cancel")
        """
        # ── CANCEL STRATEGY — tidak perlu conditionIds ───────────────────────
        if strategy == "cancel":
            self.run_cancel_strategy(cancel_tx=cancel_tx, cancel_nonce=cancel_nonce)
            return

        # 1. Ambil conditionIds + YES token IDs dari Gamma API
        condition_ids = self.fetch_condition_ids()
        condition_ids = condition_ids[:MAX_CONDITIONS]

        if not condition_ids:
            log.error("Tidak ada conditionId yang ditemukan. Batalkan eksekusi.")
            sys.exit(1)

        # 2. Diagnostik
        self.print_diagnostics(condition_ids)

        if strategy == "balance":
            # ── BALANCE STRATEGY ─────────────────────────────────────────────
            self.run_balance_strategy(condition_ids)
            return

        if strategy == "merge":
            # ── MERGE STRATEGY ───────────────────────────────────────────────
            log.info(f"\n[INFO] Strategi MERGE — kembalikan YES+NO → USDC.e")
            self.run_merge_strategy(condition_ids)
            return

        if strategy == "transfer":
            # ── TRANSFER STRATEGY ────────────────────────────────────────────
            if not transfer_to:
                log.error("--transfer-to <proxy_address> wajib untuk strategy transfer")
                sys.exit(1)
            event_data = self.gamma.get_event_by_slug(MARKET_SLUG)
            if not event_data:
                log.error(f"Tidak bisa ambil event data untuk slug: {MARKET_SLUG}")
                sys.exit(1)
            if include_no:
                log.info(f"\n[INFO] Strategi TRANSFER (YES + NO) → {transfer_to}")
                token_ids = self.gamma.extract_all_token_ids_from_event(event_data)
                token_ids = token_ids[:MAX_CONDITIONS * 2]
                log.info(f"  Ditemukan {len(token_ids)} token ID (YES+NO) dari Gamma API")
            else:
                log.info(f"\n[INFO] Strategi TRANSFER (YES only) → {transfer_to}")
                token_ids = self.gamma.extract_yes_token_ids_from_event(event_data)
                token_ids = token_ids[:MAX_CONDITIONS]
                log.info(f"  Ditemukan {len(token_ids)} YES token ID dari Gamma API")
            if not token_ids:
                log.error("Tidak ada token ID ditemukan dari Gamma API")
                sys.exit(1)
            self.run_transfer_strategy(token_ids, transfer_to, include_no=include_no)
            return

        if strategy == "convert":
            # ── CONVERT STRATEGY ────────────────────────────────────────────
            # Total USDC yang dibutuhkan = HANYA SPLIT_AMOUNT_USDC (bukan × N)
            total_usdc = SPLIT_AMOUNT_USDC

            log.info(
                f"\n[INFO] Strategi CONVERT — {len(condition_ids)} kondisi  |  "
                f"Total USDC: {total_usdc:.6f} USDC (avg ~{100/len(condition_ids):.1f}¢ per YES)"
            )

            # Validasi saldo — hanya jika NO_0 belum ada (split belum terjadi)
            # Jika NO_0 sudah ada di wallet, berarti split sudah dilakukan sebelumnya
            # dan kita hanya perlu convertPositions (tidak perlu USDC lagi).
            if not self.dry_run:
                import json as _json
                _ev = self.gamma.get_event_by_slug(MARKET_SLUG)
                _no0_exists = False
                if _ev:
                    _subs = _ev.get("markets") or _ev.get("children") or []
                    _cid0 = condition_ids[0] if condition_ids else None
                    for _m in _subs:
                        if _m.get("conditionId") == _cid0:
                            _raw = _m.get("clobTokenIds")
                            if _raw:
                                _ids = _json.loads(_raw) if isinstance(_raw, str) else _raw
                                if len(_ids) >= 2:
                                    try:
                                        _bal = self._rpc_call(
                                            self.ctf.functions.balanceOfBatch,
                                            [self.account.address], [int(_ids[1])]
                                        )
                                        if _bal and _bal[0] >= int(total_usdc * 10 ** self.USDC_DECIMALS):
                                            _no0_exists = True
                                    except Exception:
                                        pass
                            break
                if not _no0_exists:
                    usdc_raw = self._rpc_call(self.usdc.functions.balanceOf, self.account.address)
                    usdc_bal = usdc_raw / 10 ** self.USDC_DECIMALS
                    if usdc_bal < total_usdc:
                        log.error(f"Saldo tidak cukup: {usdc_bal:.2f} USDC (perlu {total_usdc:.2f})")
                        sys.exit(1)
                else:
                    log.info("[INFO] NO_0 sudah ada — skip validasi saldo USDC")

            # Approve USDC.e ke NegRiskAdapter (untuk Step 1: splitPosition)
            self.ensure_usdc_approved(total_usdc_needed=total_usdc * 1.02)
            # Approve CTF ERC-1155 ke NegRiskAdapter (untuk Step 2: convertPositions)
            self.ensure_ctf_approved()

            # Jalankan strategi convert
            self.run_convert_strategy(
                condition_ids=condition_ids,
                market_id_hex=NEG_RISK_MARKET_ID,
                amount_usdc=SPLIT_AMOUNT_USDC,
            )

        else:
            # ── SPLIT STRATEGY (default) ─────────────────────────────────────
            # Total USDC = SPLIT_AMOUNT_USDC × jumlah kondisi
            total_usdc = SPLIT_AMOUNT_USDC * len(condition_ids)
            log.info(
                f"\n[INFO] Strategi SPLIT — {len(condition_ids)} kondisi  |  "
                f"Total USDC: {total_usdc:.6f} USDC"
            )

            # Validasi saldo
            if not self.dry_run:
                usdc_raw = self._rpc_call(self.usdc.functions.balanceOf, self.account.address)
                usdc_bal = usdc_raw / 10 ** self.USDC_DECIMALS
                if usdc_bal < total_usdc:
                    log.error(
                        f"Saldo USDC.e tidak mencukupi!\n"
                        f"  Saldo  : {usdc_bal:.6f} USDC\n"
                        f"  Perlu  : {total_usdc:.6f} USDC\n"
                        f"  Kurangi SPLIT_AMOUNT_USDC atau MAX_CONDITIONS di .env"
                    )
                    sys.exit(1)

            # Approve USDC.e ke NegRiskAdapter
            self.ensure_usdc_approved(total_usdc_needed=total_usdc * 1.02)

            # Loop split per kondisi
            success_list: list = []
            failed_list: list = []

            for idx, cond_id in enumerate(condition_ids, start=1):
                log.info(
                    f"\n[SPLIT {idx:02d}/{len(condition_ids):02d}] "
                    f"{cond_id[:22]}…  |  {SPLIT_AMOUNT_USDC} USDC.e"
                )
                try:
                    tx_hash = self.split_single_condition(
                        condition_id_hex=cond_id,
                        amount_usdc=SPLIT_AMOUNT_USDC,
                    )
                    success_list.append({"condition_id": cond_id, "tx": tx_hash})
                    if not self.dry_run:
                        time.sleep(2)
                except Exception as exc:
                    log.error(f"  GAGAL: {exc}")
                    failed_list.append({"condition_id": cond_id, "error": str(exc)})

            _print_summary(condition_ids, success_list, failed_list)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _hex_to_bytes32(hex_str: str) -> bytes:
    """Konversi hex string ke bytes32, dengan padding leading zero jika perlu."""
    clean = hex_str.lstrip("0x")
    if len(clean) < 64:
        clean = clean.zfill(64)
    return bytes.fromhex(clean)


def _print_summary(
    condition_ids: List[str],
    success_list: list,
    failed_list: list,
) -> None:
    """Tampilkan ringkasan hasil split di akhir eksekusi."""
    log.info("\n" + "=" * 65)
    log.info("  HASIL SPLIT")
    log.info("=" * 65)
    log.info(f"  Total kondisi : {len(condition_ids)}")
    log.info(f"  Berhasil      : {len(success_list)}")
    log.info(f"  Gagal         : {len(failed_list)}")

    if success_list:
        log.info("\n  [OK] Transaksi berhasil:")
        for item in success_list:
            tx = item.get("tx") or "dry-run"
            cid = item["condition_id"]
            if tx and tx != "dry-run":
                log.info(f"    {cid[:20]}…  → https://polygonscan.com/tx/{tx}")
            else:
                log.info(f"    {cid[:20]}…  → [dry-run, tidak ada TX]")

    if failed_list:
        log.warning("\n  [FAIL] Kondisi yang gagal:")
        for item in failed_list:
            log.warning(f"    {item['condition_id'][:20]}…  → {item['error'][:80]}")
        log.warning(
            "\n  Tips debugging jika split gagal:\n"
            "    1. Pastikan conditionId valid — cek via Polygonscan NegRiskAdapter\n"
            "    2. Pastikan market belum ditutup/resolved\n"
            "    3. Cek tab 'Internal Txns' pada TX yang gagal untuk pesan revert\n"
            "    4. Coba tambah gas limit dengan mengubah nilai di fungsi split_single_condition\n"
            "    5. Pastikan MATIC mencukupi untuk biaya gas"
        )

    log.info("=" * 65)


# =============================================================================
# ARGPARSE & MAIN
# =============================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Negative Risk Split Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            "  # Dry run (simulasi)\n"
            "  python polymarket_split.py --dry-run\n\n"
            "  # Strategi CONVERT (DIREKOMENDASIKAN) — 2 TX, avg ~3.4¢ per YES\n"
            "  python polymarket_split.py --strategy convert --amount 8000 --dry-run\n"
            "  python polymarket_split.py --strategy convert --amount 8000\n\n"
            "  # Strategi SPLIT (lama) — N TX, dapat YES+NO per kondisi\n"
            "  python polymarket_split.py --strategy split --amount 5 --max 3\n\n"
            "  # Strategi TRANSFER — pindah YES token dari EOA ke proxy Polymarket\n"
            "  python polymarket_split.py --strategy transfer --transfer-to 0xProxyAddress --dry-run\n"
            "  python polymarket_split.py --strategy transfer --transfer-to 0xProxyAddress\n\n"
            "  # Strategi CANCEL — batalkan TX pending yang stuck\n"
            "  python polymarket_split.py --strategy cancel --cancel-tx 0xTxHash\n"
            "  python polymarket_split.py --strategy cancel --cancel-nonce 27\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulasi tanpa mengirim transaksi ke blockchain",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        choices=["split", "convert", "transfer", "merge", "cancel", "balance"],
        default="split",
        help=(
            "Pilih strategi:\n"
            "  split    = splitPosition × N kondisi → YES+NO per kondisi (N TX)\n"
            "  convert  = split 1 kondisi + convertPositions → YES-only semua kondisi (2 TX)\n"
            "             [DIREKOMENDASIKAN: lebih hemat, avg ~3.4¢ per YES]\n"
            "  transfer = kirim YES token dari EOA ke proxy wallet Polymarket\n"
            "  merge    = kembalikan YES+NO tokens → USDC.e (recovery modal)\n"
            "  cancel   = batalkan TX pending (gunakan bersama --cancel-tx atau --cancel-nonce)"
        ),
    )
    parser.add_argument(
        "--transfer-to",
        type=str,
        default=TRANSFER_TO,
        help="Alamat proxy wallet tujuan (wajib untuk --strategy transfer) [default dari .env TRANSFER_TO]",
    )
    parser.add_argument(
        "--cancel-tx",
        type=str,
        default=None,
        help="TX hash pending yang ingin dibatalkan (untuk --strategy cancel)",
    )
    parser.add_argument(
        "--cancel-nonce",
        type=int,
        default=None,
        help="Nonce TX pending yang ingin dibatalkan (alternatif --cancel-tx)",
    )
    parser.add_argument(
        "--market-id",
        type=str,
        default=NEG_RISK_MARKET_ID,
        help="NegRisk Market ID (bytes32 hex) [default dari .env]",
    )
    parser.add_argument(
        "--slug",
        type=str,
        default=MARKET_SLUG,
        help="Slug pasar untuk pencarian Gamma API [default dari .env]",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=SPLIT_AMOUNT_USDC,
        help=(
            "Jumlah USDC.e:\n"
            "  --strategy split  : USDC per kondisi (total = amount × N)\n"
            "  --strategy convert: USDC total untuk 1 complete set (mis: 8000)"
        ),
    )
    parser.add_argument(
        "--max",
        type=int,
        default=MAX_CONDITIONS,
        help="Maksimum jumlah kondisi [default: 30]",
    )
    parser.add_argument(
        "--include-no",
        action="store_true",
        default=False,
        help="(Untuk --strategy transfer) Transfer YES dan NO token sekaligus",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Override nilai global dari argumen CLI
    NEG_RISK_MARKET_ID = args.market_id
    MARKET_SLUG = args.slug
    SPLIT_AMOUNT_USDC = args.amount
    MAX_CONDITIONS = args.max

    bot = NegRiskSplitBot(dry_run=args.dry_run)
    bot.run(
        strategy=args.strategy,
        transfer_to=args.transfer_to,
        cancel_tx=args.cancel_tx,
        cancel_nonce=args.cancel_nonce,
        include_no=args.include_no,
    )
