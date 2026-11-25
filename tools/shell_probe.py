"""串口文本 shell 连通性探针。

用途：定位"上位机二进制协议通、但文本 shell 一个字都不回"这类故障。
它只做一件事——往串口发命令、把收到的原始字节打出来。

用法：
    python tools/shell_probe.py                 # 自动选第一个串口
    python tools/shell_probe.py COM14
    python tools/shell_probe.py COM14 --raw     # 连 PING 都不发，纯监听
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo
    from PyQt5.QtCore import QCoreApplication
except ImportError:  # pragma: no cover
    print("需要 PyQt5（QSerialPort）", file=sys.stderr)
    raise


def list_ports() -> list[str]:
    return [p.portName() for p in QSerialPortInfo.availablePorts()]


def probe(port: str, raw: bool = False) -> int:
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)  # noqa: F841

    sp = QSerialPort()
    sp.setPortName(port)
    sp.setBaudRate(921600)
    sp.setDataBits(QSerialPort.Data8)
    sp.setParity(QSerialPort.NoParity)
    sp.setStopBits(QSerialPort.OneStop)
    sp.setFlowControl(QSerialPort.NoFlowControl)

    rx = bytearray()

    def on_ready() -> None:
        rx.extend(bytes(sp.readAll()))

    sp.readyRead.connect(on_ready)

    if not sp.open(QSerialPort.ReadWrite):
        print(f"打开 {port} 失败: {sp.errorString()}")
        print("  很可能被 SecureCRT / 串口助手占用，先关掉它们")
        return 2

    print(f"已打开 {port} @921600")

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

    # ① 先不发送，纯监听 0.6 s：上电横幅/提示符如果还在缓冲里会在这里出现
    pump(0.6)
    print(f"[监听] 收到 {len(rx)} 字节: {bytes(rx)!r}")

    if raw:
        sp.close()
        return 0 if rx else 1

    # ② 敲两个回车，看提示符
    for payload in (b"\r\n", b"\r\n", b"help\r\n", b"\r\n", b"sfra\r\n"):
        rx.clear()
        n = sp.write(payload)
        sp.flush()
        pump(1.0)
        print(f"发送 {payload!r} (写入 {n} B) -> 收到 {len(rx)} B: {bytes(rx)!r}")

    sp.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default=None)
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()

    ports = list_ports()
    print(f"可用串口: {ports or '(无)'}")
    port = a.port or (ports[0] if ports else None)
    if not port:
        print("没有可用串口")
        return 3
    return probe(port, a.raw)


if __name__ == "__main__":
    raise SystemExit(main())
