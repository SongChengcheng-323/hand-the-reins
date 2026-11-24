#!/usr/bin/env python3
"""常驻会话 —— 保持一条连接，读指令文件，变了就下发

为什么这样设计：
  - 这类设备大多「连接一断就停」，断开重连会把一切归零
  - 所以整场只连一次，之后只改 state/toy_cmd.json，脚本自己去同步

指令文件格式（state/toy_cmd.json）：
    {"outin": 0, "shake": 20, "suck": 5, "rotate": 0}
改文件即生效，不需要重启脚本。

启动：
    nohup python3 play_session.py > /tmp/play_session.log 2>&1 &   # macOS / Linux
停止：
    pkill -f play_session.py        # 断开即全停

Windows 用户：`nohup` / `pkill` 不可用，用任务计划或直接开一个终端窗口跑，
结束时 Ctrl-C（进程退出即断连，设备会自己停）。
"""
import asyncio
import json
import os
import sys
import time

from bleak import BleakClient, BleakScanner

# 显式设成空串 = 故意不用名字匹配（配合 TOY_ADDRESS）；没设才用默认值
TOY_HINT = (os.environ["TOY_NAME_HINT"] if "TOY_NAME_HINT" in os.environ else "mizzzee").strip().lower()
TOY_ADDR = (os.environ.get("TOY_ADDRESS") or "").strip().upper()   # 可选：直接指定地址，跳过名字匹配
CH_WRITE = "0000ddd1-0000-1000-8000-00805f9b34fb"
CH_HEAT = "0000ffe3-0000-1000-8000-00805f9b34fb"
HEAT_ON = bytes.fromhex("AA060101B2")
STATE_DIR = os.environ.get("TOY_STATE_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))
CMD = os.path.join(STATE_DIR, "toy_cmd.json")
POLL = 0.25
RESEND_EVERY = 6.0          # 无 ACK 的写入，每隔几秒无条件重发一次，防止丢包后状态错位
VAL_MAX = 99


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(CMD):
        with open(CMD, "w", encoding="utf-8") as f:
            json.dump({"outin": 0, "shake": 0, "suck": 0, "rotate": 0}, f)
        log(f"没有指令文件，已建一份空的：{CMD}")


def clamp(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, min(VAL_MAX, v))


def frame(outin=0, shake=0, suck=0, rotate=0) -> bytes:
    body = [0xAA, 0x08, 4, clamp(outin), clamp(shake), clamp(suck), clamp(rotate)]
    return bytes(body + [sum(body) & 0xFF])


_bad_cmd_logged = False


def read_cmd(last):
    """读指令文件；解析失败就沿用上一次的有效值（不要突然全停）"""
    global _bad_cmd_logged
    try:
        with open(CMD, encoding="utf-8") as f:
            d = json.load(f)
        return (clamp(d.get("outin", 0)), clamp(d.get("shake", 0)),
                clamp(d.get("suck", 0)), clamp(d.get("rotate", 0))), True
    except FileNotFoundError:
        if not _bad_cmd_logged:
            log(f"⚠️ 指令文件不见了：{CMD}（继续用上一份值）")
            _bad_cmd_logged = True
        return last, False
    except Exception as e:
        if not _bad_cmd_logged:
            log(f"⚠️ 指令文件解析失败：{e}（继续用上一份值，改好会自动恢复）")
            _bad_cmd_logged = True
        return last, False


async def find_toy(timeout=12):
    devs = await BleakScanner.discover(timeout=timeout)
    if TOY_ADDR:
        for d in devs:
            if (d.address or "").upper() == TOY_ADDR:
                return d
        log(f"指定了地址 {TOY_ADDR}，但这次没扫到")
        return None
    if not TOY_HINT:
        log("❌ TOY_NAME_HINT 是空的，拒绝连接（否则可能连到旁边的任何设备）")
        return None
    hits = [d for d in devs if TOY_HINT in (d.name or "").lower()]
    if len(hits) > 1:
        log(f"扫到 {len(hits)} 台同名设备，取信号最强的那台；不确定就设 TOY_ADDRESS")
        hits.sort(key=lambda d: getattr(d, "rssi", -999) or -999, reverse=True)
    return hits[0] if hits else None


async def main():
    if not TOY_ADDR and not TOY_HINT:
        # 空匹配串会匹配到附近任何一台 BLE 设备，必须在扫描前就拦住
        log("❌ 既没有 TOY_NAME_HINT 也没有 TOY_ADDRESS，拒绝连接")
        return 1
    ensure_state()
    toy = await find_toy()
    if not toy:
        log("❌ 没扫到设备（先确认它开着，且没被别的程序占着）")
        return 1
    async with BleakClient(toy.address, timeout=25) as cli:
        log(f"已连 {toy.name or toy.address}")

        # 加热/附加功能是可选通道：设备没有就别让整场连不进来
        try:
            await cli.write_gatt_char(CH_HEAT, HEAT_ON, response=False)
            log("附加通道已触发（有的型号是加热，连接不断就一直生效）")
        except Exception as e:
            log(f"附加通道不可用，跳过：{e}")

        cur = (0, 0, 0, 0)
        last_sent = 0.0
        try:
            await cli.write_gatt_char(CH_WRITE, frame(), response=False)
        except Exception as e:
            log(f"❌ 首帧写入失败：{e}")
            return 1
        log(f"就绪，开始轮询 {CMD}")

        while True:
            if not cli.is_connected:
                log("连接断了，退出（设备会自己停）")
                break
            nxt, _ok = read_cmd(cur)
            now = time.time()
            need = (nxt != cur) or (now - last_sent >= RESEND_EVERY)
            if need:
                f = frame(*nxt)
                try:
                    await cli.write_gatt_char(CH_WRITE, f, response=False)
                except Exception as e:
                    log(f"写入失败：{e} —— 连接可能已经掉了，退出")
                    break
                if nxt != cur:
                    log(f"→ {f.hex().upper()}  {nxt}")
                cur = nxt
                last_sent = now
            await asyncio.sleep(POLL)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        log("退出")
