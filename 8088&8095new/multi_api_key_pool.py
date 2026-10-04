ALLTICK_TOKEN = 'e4432003c7fb8ef16dce2c8fbcf1ae57-c-app'
ITICK_TOKEN   = '7111eb89b6364381bad7d4a507b5573ac880fb5d6c0e47bda0cae76a453c1bb0'

"""
===============================================================================
MULTI-SOURCE INSTITUTIONAL API KEY ROTATION POOL
XAUUSD SPOT GOLD REAL-TIME FEED WITH DYNAMIC FAILOVER
===============================================================================
Active Keys:
- TwelveData: 13 Active Production Keys (Direct Spot XAU/USD)
- RealMarketAPI: 9 Active Production Keys (Spot XAUUSD with Bid/Ask/Spread)
- Binance: PAXG/USDT Zero-Key Fallback Stream
===============================================================================
"""

import urllib.request
import urllib.parse
import json
import time
import ssl
import threading
import logging

logger = logging.getLogger("ApiKeyPool")

# SSL Context
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# 13 TWELVEDATA API KEYS
TWELVEDATA_KEYS = [
    "7b4a5b6feaf2429180934a74c8d88905",
    "a77eb531fb46450088346ca8ed8d2658",
    "0c541a88560d4a6dbf169a37db04fcb6",
    "2d261575188c46678bf2b3d50a528408",
    "9a881e3b6cba435fb5b02a868473c9c1",
    "09db8bad69204b4d907bbbbbe1cc540f",
    "044603710a24444bbff60912e365cf7e",
    "e6a9bbe1a68946479fdb526b83c3ccd6",
    "67b7138f49964313b96520c505e3c78f",
    "53aa8f7c4390483b81fd9095dd4fd7eb",
    "f8f735ebca8e4b9fbe58de09ee1f81e2",
    "37456a60b8484cfa89d85a83ce9e0ab9",
    "cd70ff3ef4114ce394a2d446d0ab8d9b"
]

# 9 VERIFIED REALMARKET API KEYS
REALMARKET_KEYS = [
    "G9Nq09nXaBZLR0zcZDVW0kg4IOrhYB4KkvYoIvk5plh7ioOp",
    "xZxlI1aC0UhJZY9jZdfh3gqYQE3Qm9jeAuptPZy1n5jNYIzL",
    "ZTsYWWk1VYaGfuYUzWhvTGxj0ketmClqm5tBCcjCwSUIC9Wi",
    "dD6DPvOpxZONPk8j9mARQrJmhQ2L6h7dk3T7NiaZBUILMdBc",
    "gQP3ZSkBKPQYOlKevntEluhAOqUp9NUFNTXGZbrwSf5s6LaJ",
    "oTICKn5MJZdgA3MLbq0cqAgDKnBMWbPyO3yIIdFGgPHiYayF",
    "oVD0LtmS8BS1LUJ1YrRv44OQ4oNf7PDr7i3BYtkhk8h844ck",
    "lmbOvLJNkLC0dhL3U7Qcysf1HMgvU703EPy2XkcQj4l4OwcF",
    "FY0oUU0xGykL8CryEwXRbIuut2uwXPr4Qf7jJSzKCSrzL4ei"
]

class KeyRotationPool:
    def __init__(self):
        self.lock = threading.Lock()
        self.td_index = 0
        self.rm_index = 0
        self.last_spot_price = 4335.0
        self.last_bid = 4334.8
        self.last_ask = 4335.2
        self.last_source = "INITIALIZING"
        self.last_update = time.time()
        self.stats = {
            "td_requests": 0,
            "rm_requests": 0,
            "binance_requests": 0,
            "errors": 0
        }

    def _get_next_td_key(self):
        with self.lock:
            key = TWELVEDATA_KEYS[self.td_index]
            self.td_index = (self.td_index + 1) % len(TWELVEDATA_KEYS)
            return key, self.td_index

    def _get_next_rm_key(self):
        with self.lock:
            key = REALMARKET_KEYS[self.rm_index]
            self.rm_index = (self.rm_index + 1) % len(REALMARKET_KEYS)
            return key, self.rm_index

    def fetch_twelvedata_spot(self):
        """Fetch spot price from TwelveData rotating pool"""
        key, idx = self._get_next_td_key()
        url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={key}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GoldFlow/2.0"})
            with urllib.request.urlopen(req, timeout=3.5, context=ssl_ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if "close" in data and data.get("close"):
                    price = float(data["close"])
                    op = float(data.get("open", price))
                    hi = float(data.get("high", price))
                    lo = float(data.get("low", price))
                    ch = float(data.get("change", 0.0))
                    spread = 0.20
                    bid = round(price - (spread / 2), 2)
                    ask = round(price + (spread / 2), 2)
                    
                    self.last_spot_price = price
                    self.last_bid = bid
                    self.last_ask = ask
                    self.last_source = f"TwelveData (Key #{idx})"
                    self.last_update = time.time()
                    self.stats["td_requests"] += 1
                    
                    return {
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "spread": spread,
                        "open": op,
                        "high": hi,
                        "low": lo,
                        "close": price,
                        "change": ch,
                        "source": self.last_source,
                        "timestamp": time.time()
                    }
        except Exception as e:
            self.stats["errors"] += 1
        return None

    def fetch_realmarket_spot(self):
        """Fetch spot price from RealMarketAPI rotating pool"""
        key, idx = self._get_next_rm_key()
        url = f"https://api.realmarketapi.com/api/v1/price?symbolCode=XAUUSD&timeFrame=M1&apiKey={key}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GoldFlow/2.0"})
            with urllib.request.urlopen(req, timeout=3.5, context=ssl_ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if "closePrice" in data or "bid" in data:
                    bid = float(data.get("bid", 0))
                    ask = float(data.get("ask", 0))
                    close = float(data.get("closePrice", (bid + ask) / 2 if bid else 0))
                    mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else round(close, 2)
                    spread = round(ask - bid, 3) if (bid > 0 and ask > 0) else 0.20
                    
                    if mid > 0:
                        self.last_spot_price = mid
                        self.last_bid = bid if bid > 0 else round(mid - 0.10, 2)
                        self.last_ask = ask if ask > 0 else round(mid + 0.10, 2)
                        self.last_source = f"RealMarket (Key #{idx})"
                        self.last_update = time.time()
                        self.stats["rm_requests"] += 1
                        
                        return {
                            "price": mid,
                            "bid": self.last_bid,
                            "ask": self.last_ask,
                            "spread": spread,
                            "open": float(data.get("openPrice", mid)),
                            "high": float(data.get("highPrice", mid)),
                            "low": float(data.get("lowPrice", mid)),
                            "close": mid,
                            "change": round(mid - float(data.get("openPrice", mid)), 2),
                            "source": self.last_source,
                            "timestamp": time.time()
                        }
        except Exception as e:
            self.stats["errors"] += 1
        return None


    def get_live_spot_gold(self):
        """
        Master Multi-Source Ingestion Pipeline:
        1. Try TwelveData (13 keys rotating)
        2. Failover to RealMarketAPI (9 keys rotating)
        """
        # Alternate primary source between TwelveData and RealMarket to conserve both
        if (self.stats["td_requests"] + self.stats["rm_requests"]) % 2 == 0:
            res = self.fetch_twelvedata_spot()
            if not res:
                res = self.fetch_realmarket_spot()
        else:
            res = self.fetch_realmarket_spot()
            if not res:
                res = self.fetch_twelvedata_spot()
                
        return res

# Global Singleton Pool
KEY_POOL = KeyRotationPool()

def get_spot_gold_live():
    return KEY_POOL.get_live_spot_gold()
