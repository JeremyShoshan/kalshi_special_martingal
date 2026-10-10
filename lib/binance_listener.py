import json
import websockets
import asyncio
from datetime import datetime, timezone

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

def now_utc_unix():
    return int(datetime.now(timezone.utc).timestamp())

def normalize_timestamp(ms):
    """Convert ms → seconds (float)."""
    if ms is None:
        return None
    return float(ms) / 1000.0

def format_price(price):
    try:
        return f"{float(price):.2f}"
    except:
        return None

async def listen_btc():
    queue = asyncio.Queue(maxsize=1)

    async def reader():
        async with websockets.connect(BINANCE_WS, ping_interval=100) as ws:
            print("✅ Connected to Binance WS")

            count = 0
            last_print_time = now_utc_unix()

            while True:
                try:
                    msg = await ws.recv()
                    received_ts = now_utc_unix()
                    local_ts = now_utc_unix()

                    count += 1
                    if received_ts - last_print_time >= 10:
                        #print(f"📊 Received {count} messages in the last {received_ts - last_print_time}s")
                        count = 0
                        last_print_time = received_ts

                    item = (msg, received_ts, local_ts)
                    while queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await queue.put(item)
                except websockets.ConnectionClosed:
                    print("[ERROR] Connection closed")
                    while queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await queue.put(None)
                    break
                except Exception as e:
                    print(f"[ERROR] {e}")
                    while queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await queue.put(None)
                    break

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            msg, received_ts, local_ts = item
            data = json.loads(msg)

            price = format_price(data.get("p"))
            source_ts = normalize_timestamp(data.get("T"))

            yield price, source_ts, received_ts, local_ts
    finally:
        reader_task.cancel()
