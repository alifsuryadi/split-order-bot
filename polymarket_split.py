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
]

CTF_ABI: list = [
    # Hanya untuk diagnostik — cek status kondisi di CTF utama
    {
        "name": "getOutcomeSlotCount",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
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
    # DIAGNOSTIK
    # ─────────────────────────────────────────────────────────────────────────

    def print_diagnostics(self, condition_ids: List[str]) -> None:
        """Tampilkan saldo, allowance, dan status kondisi untuk debugging."""
        log.info("[DIAGNOSTIK] ─────────────────────────────────────────────")

        # Saldo MATIC (untuk biaya gas)
        matic_wei = self.w3.eth.get_balance(self.account.address)
        log.info(f"  MATIC (gas) : {Web3.from_wei(matic_wei, 'ether'):.6f} MATIC")

        # Saldo USDC.e
        usdc_raw = self.usdc.functions.balanceOf(self.account.address).call()
        usdc_bal = usdc_raw / 10 ** self.USDC_DECIMALS
        log.info(f"  USDC.e      : {usdc_bal:.6f} USDC")

        # Allowance USDC.e → NegRiskAdapter
        allowance_raw = self.usdc.functions.allowance(
            self.account.address,
            Web3.to_checksum_address(ADDR["NEG_RISK_ADAPTER"]),
        ).call()
        log.info(
            f"  Allowance   : {allowance_raw / 10**self.USDC_DECIMALS:.6f} USDC"
            f" → NegRiskAdapter"
        )

        # Cek beberapa kondisi pertama di CTF utama (harusnya = 0, ini normal)
        log.info("  Status kondisi di CTF utama (slotCount=0 = NORMAL untuk NegRisk):")
        for cid in condition_ids[:3]:
            cid_bytes = _hex_to_bytes32(cid)
            try:
                slot_count = self.ctf.functions.getOutcomeSlotCount(cid_bytes).call()
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
        return self._derive_condition_ids_from_market_id(NEG_RISK_MARKET_ID, n=30)

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
        gas_price = self.w3.eth.gas_price
        nonce = self.w3.eth.get_transaction_count(self.account.address)

        tx = self.usdc.functions.approve(spender, MAX_UINT256).build_transaction(
            {
                "from": self.account.address,
                "nonce": nonce,
                "gasPrice": int(gas_price * 1.2),  # +20% buffer agar tidak stuck
                "gas": 80_000,
                "chainId": self.CHAIN_ID,
            }
        )

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

        log.info(f"[APPROVE] TX terkirim  : {tx_hash.hex()}")
        log.info(f"[APPROVE] Polygonscan  : https://polygonscan.com/tx/{tx_hash.hex()}")
        log.info("[APPROVE] Menunggu konfirmasi...")

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
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
                gas_price = self.w3.eth.gas_price
                nonce = self.w3.eth.get_transaction_count(self.account.address)

                tx = self.adapter.functions["splitPosition(bytes32,uint256)"](
                    cond_bytes, amount_raw
                ).build_transaction(
                    {
                        "from": self.account.address,
                        "nonce": nonce,
                        "gasPrice": int(gas_price * 1.2),
                        "gas": gas_limit,
                        "chainId": self.CHAIN_ID,
                    }
                )

                signed = self.account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

                log.info(f"  TX: https://polygonscan.com/tx/{tx_hash.hex()}")

                # ── Tunggu konfirmasi ─────────────────────────────────────────
                receipt = self.w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=300
                )
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
    # ENTRYPOINT UTAMA
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Jalankan proses split lengkap:
          fetch conditionIds → diagnostik → validasi saldo → approve → split loop
        """

        # 1. Ambil conditionIds
        condition_ids = self.fetch_condition_ids()
        condition_ids = condition_ids[:MAX_CONDITIONS]

        if not condition_ids:
            log.error("Tidak ada conditionId yang ditemukan. Batalkan eksekusi.")
            sys.exit(1)

        total_usdc = SPLIT_AMOUNT_USDC * len(condition_ids)
        log.info(
            f"[INFO] {len(condition_ids)} kondisi akan di-split  |  "
            f"Total USDC diperlukan: {total_usdc:.6f} USDC"
        )

        # 2. Diagnostik
        self.print_diagnostics(condition_ids)

        # 3. Validasi saldo USDC.e
        if not self.dry_run:
            usdc_raw = self.usdc.functions.balanceOf(self.account.address).call()
            usdc_bal = usdc_raw / 10 ** self.USDC_DECIMALS
            if usdc_bal < total_usdc:
                log.error(
                    f"Saldo USDC.e tidak mencukupi!\n"
                    f"  Saldo  : {usdc_bal:.6f} USDC\n"
                    f"  Perlu  : {total_usdc:.6f} USDC\n"
                    f"  Kurang : {total_usdc - usdc_bal:.6f} USDC\n"
                    f"  Kurangi SPLIT_AMOUNT_USDC atau MAX_CONDITIONS di .env"
                )
                sys.exit(1)

        # 4. Approve USDC.e ke NegRiskAdapter (satu kali, unlimited)
        self.ensure_usdc_approved(total_usdc_needed=total_usdc * 1.02)

        # 5. Loop split per kondisi
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
                # Jeda kecil antar transaksi untuk menghindari nonce collision
                if not self.dry_run:
                    time.sleep(2)
            except Exception as exc:
                log.error(f"  GAGAL: {exc}")
                failed_list.append({"condition_id": cond_id, "error": str(exc)})

        # 6. Ringkasan
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
        description="Polymarket Negative Risk Split Position Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Contoh:\n"
            "  python polymarket_split.py --dry-run\n"
            "  python polymarket_split.py --amount 10 --max 5\n"
            "  python polymarket_split.py --market-id 0x11ab... --slug elon-musk-...\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulasi tanpa mengirim transaksi ke blockchain",
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
        help="Jumlah USDC.e per kondisi [default dari .env atau 5]",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=MAX_CONDITIONS,
        help="Maksimum jumlah kondisi yang di-split [default: 30]",
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
    bot.run()
