import asyncio
import json
import time
from collections import OrderedDict
from app.errors import AppError


class MemoryControls:
    """Single-process bounded development controls; production configuration forbids this backend."""
    def __init__(self, limit=60):
        self.limit, self.rates, self.cache = limit, OrderedDict(), OrderedDict()
        self.lock = asyncio.Lock()

    async def rate(self, key):
        now = time.monotonic()
        async with self.lock:
            start, count = self.rates.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            count += 1
            self.rates[key] = (start, count)
            self.rates.move_to_end(key)
            while len(self.rates) > 10000:
                self.rates.popitem(last=False)
            if count > self.limit:
                raise AppError("rate_limited", "Too many requests. Try again shortly.", 429)

    async def get(self, key):
        item = self.cache.get(key)
        if not item or item[0] <= time.monotonic():
            self.cache.pop(key, None)
            return None
        return item[1]

    async def set(self, key, value, ttl):
        self.cache[key] = (time.monotonic() + ttl, value)
        self.cache.move_to_end(key)
        while len(self.cache) > 1000:
            self.cache.popitem(last=False)

    async def ping(self):
        return True

    async def close(self):
        pass


RATE_LUA = """
local tm = redis.call('TIME')
local bucket = math.floor(tonumber(tm[1]) / 60)
local key = KEYS[1] .. ':' .. bucket
local count = redis.call('INCR', key)
if count == 1 then redis.call('EXPIRE', key, 120) end
return count
"""


class RedisControls:
    def __init__(self, url, limit=60, client=None):
        self.limit = limit
        if client is None:
            from redis.asyncio import from_url
            client = from_url(url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1,
                              health_check_interval=30)
        self.client = client

    async def rate(self, key):
        try:
            count = int(await self.client.eval(RATE_LUA, 1, "rag:rate:" + key))
        except Exception:
            raise AppError("rate_service_unavailable", "Request admission is temporarily unavailable.", 503) from None
        if count > self.limit:
            raise AppError("rate_limited", "Too many requests. Try again shortly.", 429)

    async def get(self, key):
        try:
            value = await self.client.get("rag:retrieval:" + key)
            return json.loads(value) if value else None
        except Exception:
            return None  # Optional cache can fail open; request admission cannot.

    async def set(self, key, value, ttl):
        try:
            await self.client.set("rag:retrieval:" + key, json.dumps(value), ex=ttl)
        except Exception:
            pass

    async def ping(self):
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self):
        await self.client.aclose()
