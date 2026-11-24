#!/usr/bin/env python3
"""心率常驻监听 —— 断了自动重连，写到 jsonl 供读取

设计：
  - 死循环扫手环，扫到就连，连上就订阅心率
  - 连接断了/心率停了 > 20 秒，自动断开重连
  - 心率写到 state/hr_live.jsonl（每行 {"t": 时间戳, "bpm": 数}）
  - 只保留最近 30 分钟，防止无限增长

启动（后台常驻）：
    nohup python3 hr_service.py > /tmp/hr_service.log 2>&1 &
查看：
    tail -f /tmp/hr_service.log
停止：
    pkill -f hr_service.py
"""
import asyncio
import json
import os
import signal
import sys
import time

from bleak import BleakClient, BleakScanner

HR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
STATE_DIR = os.environ.get("TOY_STATE_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))
OUT = os.path.join(STATE_DIR, "hr_live.jsonl")
SCAN_SECS = 8
STALE_SECS = 20          # 多久没心跳就判定连死了
KEEP_SECS = 1800         # 文件里只留最近 30 分钟


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_hr(data: bytes) -> int:
    return int.from_bytes(data[1:3], "little") if data[0] & 0x01 else data[1]


def append(bpm: int):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 2), "bpm": bpm}) + "\n")


def prune():
    """只留最近 KEEP_SECS 秒"""
    try:
        cutoff = time.time() - KEEP_SECS
        kept = []
        with open(OUT, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("t", 0) >= cutoff:
                        kept.append(line)
                except Exception:
                    continue
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, OUT)
    except FileNotFoundError:
        pass


HR_HINT = (os.environ["HR_NAME_HINT"] if "HR_NAME_HINT" in os.environ else "band").strip().lower()
HR_ADDR = (os.environ.get("HR_ADDRESS") or "").strip().upper()


async def find_band(timeout=SCAN_SECS):
    devs = await BleakScanner.discover(timeout=timeout)
    if HR_ADDR:
        for d in devs:
            if (d.address or "").upper() == HR_ADDR:
                return d
        return None
    hits = [d for d in devs if HR_HINT and HR_HINT in (d.name or "").lower()]
    if len(hits) > 1:
        log(f"扫到 {len(hits)} 台同名设备，取第一台；不确定就设 HR_ADDRESS")
    return hits[0] if hits else None


async def session(band) -> bool:
    """连一次，直到断开。返回 False 表示进气正常但断了"""
    state = {"last": 0.0, "n": 0, "connected_at": time.time()}

    def on_hr(_c, data: bytes):
        try:
            bpm = parse_hr(data)
        except Exception:
            return
        state["last"] = time.time()
        state["n"] += 1
        append(bpm)

    async with BleakClient(band.address, timeout=25) as cli:
        log(f"已连 {band.name} rssi 附近")
        await cli.start_notify(HR_UUID, on_hr)
        log("订阅心率成功")
        while True:
            await asyncio.sleep(3)
            if not cli.is_connected:
                log("连接断了")
                return False
            # 收到过数据：超过 STALE_SECS 没更新 ⇒ 断连
            if state["last"] and time.time() - state["last"] > STALE_SECS:
                log(f"心率 {STALE_SECS}s 没更新，判定连死了")
                return False
            # 连上了却一直没数据（没戴 / 没开广播）⇒ 也要退出来重试，别挂着不动
            if not state["last"] and time.time() - state["connected_at"] > 60:
                log("连上 60 秒还没收到任何心率，重连试试（是不是没戴或者没开广播）")
                return False


async def main():
    global _STOP
    _STOP = asyncio.Event()
    log(f"心率服务启动，输出 → {OUT}")
    while True:
        if _STOP is not None and _STOP.is_set():
            log("退出")
            return
        try:
            band = await find_band()
            if not band:
                log("没扫到手环，5 秒后重试")
                await asyncio.sleep(5)
                continue
            await session(band)
            prune()                    # 每次重连都裁一次（按时间判断，不靠计数）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"异常 {type(e).__name__}: {str(e)[:80]}")
        await asyncio.sleep(2)


_STOP = None


def on_term(*_):
    # 在信号处理器里 sys.exit 会打断 await，可能留下「进程没了但设备还连着」
    log("收到退出信号")
    if _STOP is not None:
        _STOP.get_loop().call_soon_threadsafe(_STOP.set)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
