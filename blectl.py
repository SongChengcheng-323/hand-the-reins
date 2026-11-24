#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""醉清风 / 谜姬（cn.intofun.mizzzee）蓝牙设备的自助控制台

协议由 APK 逆向得出（2026-11-21）：
    帧 = AA | CMD | LEN | DATA... | CHECKSUM
      · CMD  06 = 模式（多路马达开关/模式组合）
             08 = 强度（末两字节为 16 位强度值）
      · LEN  数据字节个数（暂按 "0" + 个数 的写法，data<=15 时有效）
      · CHECK 所有字节相加取低 8 位
    写通道 service 0000DDDD-0000-1000-8000-00805F9B34FB
           char    0000DDD1-0000-1000-8000-00805F9B34FB
    通知     char    0000DDD2-...
    备用服务 FFE0/FFE1、AE30

⚠️ 静态分析产物，真机可能需要微调。用法：

    python3 blectl.py scan                      # 扫附近的 BLE 设备
    python3 blectl.py probe <地址前缀>           # 连上枚举 service/characteristic
    python3 blectl.py raw <地址前缀> AABBCC      # 发原始 hex
    python3 blectl.py mode <地址前缀> 1        # 发 06（1=开，0=关；只验证过单字节）
    python3 blectl.py power <地址前缀> 500       # 发 08（强度，0-1000 试）
    python3 blectl.py demo <地址前缀>            # 演示：停 → 弱 → 中 → 强 → 停
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

SVC = "0000DDDD-0000-1000-8000-00805F9B34FB"
CH_WRITE = "0000DDD1-0000-1000-8000-00805F9B34FB"
CH_NOTIFY = "0000DDD2-0000-1000-8000-00805F9B34FB"


def frame(cmd: str, args) -> bytes:
    """AA | CMD | LEN | DATA | CHECK"""
    body = [int(cmd, 16)] + [int(a) & 0xFF for a in args]
    n = len(args)
    hdr = 0xAA
    payload = body
    checksum = (hdr + sum(payload)) & 0xFF
    # LEN 字节也要进校验
    checksum = (checksum + n) & 0xFF
    return bytes([hdr, int(cmd, 16), n] + [int(a) & 0xFF for a in args] + [checksum])


async def scan(sec=6.0):
    print(f"扫描 {sec:.0f} 秒…")
    devs = await BleakScanner.discover(timeout=sec)
    if not devs:
        print("没扫到设备。检查：① 设备开机了吗 ② 系统设置里的终端蓝牙权限 ③ 别被 App 连着")
        return
    def _rssi(d):
        r = getattr(d, "rssi", None)
        if r is None:
            ad = getattr(d, "advertisement_data", None)
            r = getattr(ad, "rssi", None) if ad else None
        return r if r is not None else -999

    for d in sorted(devs, key=lambda x: -_rssi(x)):
        print(f"  {d.address}  rssi={_rssi(d)}  {d.name or '(无名)'}")


async def find(target: str, sec=10.0):
    devs = await BleakScanner.discover(timeout=sec)
    for d in devs:
        if d.address.replace(":", "").lower().startswith(target.replace(":", "").lower()):
            return d
    for d in devs:
        if d.name and target.lower() in d.name.lower():
            return d
    return None


async def probe(target: str):
    d = await find(target)
    if not d:
        print("没找到该设备，先 scan 看看。")
        return
    print(f"连接 {d.address} ({d.name}) …")
    async with BleakClient(d.address) as c:
        print("已连接:", c.is_connected)
        for svc in c.services:
            print(f"  service {svc.uuid}")
            for ch in svc.characteristics:
                mark = "  ← 写通道" if ch.uuid.upper() == CH_WRITE.upper() else ""
                print(f"    char {ch.uuid}  props={ch.properties}{mark}")
        if "write" in " ".join(ch.properties for s in c.services for ch in s.characteristics):
            pass


async def send_hex(target: str, hexstr: str, notify_wait=1.5):
    d = await find(target)
    if not d:
        print("没找到设备。")
        return
    try:
        data = bytes.fromhex(hexstr.replace(" ", ""))
    except ValueError:
        raise SystemExit(f"不是合法的十六进制串：{hexstr}")
    print(f"→ {data.hex().upper()}")
    async with BleakClient(d.address) as c:
        def cb(_h, b):
            print(f"  ← 通知 {b.hex().upper()}")
        try:
            await c.start_notify(CH_NOTIFY, cb)
        except Exception as e:
            print(f"  （这台设备没有可订阅的通知通道，跳过监听：{e}）")
        await c.write_gatt_char(CH_WRITE, data, response=False)
        await asyncio.sleep(notify_wait)


async def main():
    a = sys.argv[1:]
    if not a or a[0] in ("scan", "--scan"):
        await scan()
        return
    if not a[0] in ("probe", "raw", "mode", "power", "demo"):
        print(__doc__)
        return
    if not a[1:2]:
        raise SystemExit(f"{a[0]} 需要设备名或地址，例：python3 blectl.py {a[0]} <target> …")

    if a[0] == "probe":
        await probe(a[1])
    elif a[0] == "raw":
        if len(a) < 3:
            raise SystemExit("raw 需要两段：设备 + 十六进制串")
        await send_hex(a[1], a[2])
    elif a[0] == "mode":
        # 已验证的开关帧是 AA 06 01 01 B2 —— 数据只有 1 字节
        vals = [int(x) for x in a[2:]] or [1]
        if len(vals) > 1:
            print("⚠️ 这条命令的数据只验证过 1 个字节，多传的部分设备可能不认")
        f = frame("06", vals)
        print("帧:", f.hex().upper())
        await send_hex(a[1], f.hex())
    elif a[0] == "power":
        if len(a) < 3:
            raise SystemExit("power 需要两段：设备 + 数值（0~65535）")
        val = max(0, min(65535, int(a[2])))
        f = frame("08", [0, 0, (val >> 8) & 0xFF, val & 0xFF])
        print("帧:", f.hex().upper())
        await send_hex(a[1], f.hex())
    elif a[0] == "demo":
        for label, v in (("停", 0), ("弱 200", 200), ("中 500", 500), ("强 900", 900), ("停", 0)):
            print(f"— {label} —")
            f = frame("08", [0, 0, (v >> 8) & 0xFF, v & 0xFF])
            print("帧:", f.hex().upper())
            await send_hex(a[1], f.hex(), notify_wait=2.5)
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
