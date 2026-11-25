"""
固件烧录 + 自动复位运行 + 串口自检。

为什么需要这个脚本
------------------
Keil 命令行 `UV4.exe -f project.uvprojx` 只做"擦除 → 编程 → 校验"，
**烧完不复位**。芯片会停在 HALTED 状态（PC = 0x20000000，即 SRAM
起始地址，不是 Flash），表现是：

    - 烧录日志显示 "Verify OK"，看起来一切正常；
    - 连上串口一个字节都收不到，文本 shell 和二进制协议同时失声；
    - 用调试器看 PC = 0x20000000、state = HALTED。

2026-11-24 在这上面浪费了大半天，误判成"新固件把板子烧死了"。

Keil IDE 里勾了 "Reset and Run" 才会自动复位，命令行没有等价开关。
本脚本用 pyOCD 补上这一步：

    1. 调 UV4 -b 编译（可选，--no-build 跳过）
    2. 调 UV4 -f 烧录（擦除/编程/校验）
    3. 用 pyOCD 连上，读向量表确认 Flash 内容合法
    4. reset_and_halt() → resume()，让芯片真正跑起来
    5. 打开串口监听，确认能看到启动横幅

用法：
    python tools/flash_and_run.py                 # 编译+烧录+复位+自检
    python tools/flash_and_run.py --no-build      # 只烧录现有的 axf
    python tools/flash_and_run.py --verify-only   # 只复位+自检（不烧）
    python tools/flash_and_run.py --port COM14
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 路径常量

HERE = Path(__file__).resolve().parent
PROJ_ROOT = HERE.parent
FW_ROOT = Path(r"D:\project_code\PV-charger")
MDK_DIR = FW_ROOT / "MDK-ARM"
UVPROJX = MDK_DIR / "PVCharger.uvprojx"
AXF = MDK_DIR / "Objects" / "PVCharger.axf"
HEX = MDK_DIR / "Objects" / "PVCharger.hex"
UV4 = Path(r"D:\Work_APP\Keil\UV4\UV4.exe")

BAUD = 921600


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 构建/烧录


def build() -> bool:
    """调 Keil 命令行编译，返回是否 0 Error。"""
    if not UV4.exists():
        log(f"找不到 Keil: {UV4}")
        return False
    logpath = MDK_DIR / "build_flash.log"
    r = subprocess.run(
        [str(UV4), "-b", str(UVPROJX), "-j0", "-o", str(logpath)],
        cwd=str(MDK_DIR), capture_output=True, text=True, timeout=600,
    )
    txt = logpath.read_text(encoding="utf-8", errors="replace") if logpath.exists() else ""
    log("--- build ---")
    log(txt.strip() or "(无编译输出)")
    ok = "0 Error(s)" in txt
    if not ok:
        log(f"编译失败 (exit={r.returncode})")
    return ok


def flash() -> bool:
    """调 Keil 命令行烧录。注意：这步**不会**复位芯片。"""
    logpath = MDK_DIR / "flash_flash.log"
    r = subprocess.run(
        [str(UV4), "-f", str(UVPROJX), "-j0", "-o", str(logpath)],
        cwd=str(MDK_DIR), capture_output=True, text=True, timeout=600,
    )
    txt = logpath.read_text(encoding="utf-8", errors="replace") if logpath.exists() else ""
    log("--- flash ---")
    log(txt.strip() or "(无烧录输出)")
    ok = "Verify OK" in txt
    if not ok:
        log(f"烧录失败 (exit={r.returncode})")
    return ok


# ---------------------------------------------------------------- 复位运行


def reset_and_run() -> bool:
    """用 pyOCD 复位并让芯片跑起来。

    pyOCD 没有 PY32F071 的内置 target，用通用的 `cortex_m` 即可——
    我们只需要"读向量表 + 复位 + resume"，这几件事和具体型号无关。
    真正要小心的是关掉 `auto_unlock`，避免 pyOCD 去动选项字节。
    """
    try:
        from pyocd.core.helpers import ConnectHelper
    except ImportError:
        log("未安装 pyocd：pip install pyocd")
        return False

    sess = ConnectHelper.session_with_chosen_probe(
        target_override="cortex_m",
        options={"auto_unlock": False, "resume_on_disconnect": True},
    )
    if sess is None:
        log("找不到调试探针（PY-LINK / CMSIS-DAP 是否插好？）")
        return False

    with sess:
        t = sess.board.target

        # ① 先读向量表，确认 Flash 里确实是我们的固件
        try:
            mem = bytes(t.read_memory_block8(0x08000000, 8))
            vec_sp = int.from_bytes(mem[0:4], "little")
            vec_pc = int.from_bytes(mem[4:8], "little")
            log(f"向量表: SP=0x{vec_sp:08X} Reset=0x{vec_pc:08X}")
            if not (0x20000000 <= vec_sp <= 0x20010000):
                log(f"  [!] 初始 SP 不像合法 SRAM，Flash 可能没写对")
            if not (0x08000000 <= (vec_pc & ~1) < 0x08020000):
                log(f"  [!] Reset 向量不在 Flash 范围内")
        except Exception as e:  # noqa: BLE001
            log(f"读向量表失败（不致命）: {e}")

        # ② 记录复位前的状态，便于诊断
        try:
            before = t.get_state()
            before_pc = t.read_core_register("pc")
            log(f"复位前: state={before}  PC=0x{before_pc:08X}")
        except Exception as e:  # noqa: BLE001
            log(f"复位前状态读取失败（不致命）: {e}")

        # ③ 复位并运行
        t.reset_and_halt()
        pc0 = t.read_core_register("pc")
        log(f"复位后停在: PC=0x{pc0:08X}")
        t.resume()
        time.sleep(0.3)
        state = t.get_state()
        log(f"运行状态: {state}")

        if str(state).endswith("RUNNING"):
            log("✅ 芯片已进入 RUNNING")
            return True
        log("❌ 芯片没有跑起来")
        return False


# ---------------------------------------------------------------- 串口自检


def serial_selftest(port: str | None = None, timeout_s: float = 3.0) -> bool:
    """打开串口，监听启动横幅，再敲一条 help 确认 shell 双向通。"""
    try:
        from PyQt5.QtCore import QCoreApplication
        from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo
    except ImportError:
        log("需要 PyQt5 才能做串口自检")
        return False

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    if port is None:
        ports = [p.portName() for p in QSerialPortInfo.availablePorts()]
        if not ports:
            log("没有可用串口")
            return False
        port = ports[0]
        log(f"自动选择串口: {port}")

    sp = QSerialPort()
    sp.setPortName(port)
    sp.setBaudRate(BAUD)
    sp.setDataBits(QSerialPort.Data8)
    sp.setParity(QSerialPort.NoParity)
    sp.setStopBits(QSerialPort.OneStop)
    sp.setFlowControl(QSerialPort.NoFlowControl)

    rx = bytearray()
    sp.readyRead.connect(lambda: rx.extend(bytes(sp.readAll())))

    if not sp.open(QSerialPort.ReadWrite):
        log(f"打开 {port} 失败: {sp.errorString()}")
        log("  → 可能被 SecureCRT / 串口助手占用")
        return False

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

    ok = True

    # ① 启动横幅（芯片刚复位，横幅应该还在发出）
    pump(min(timeout_s, 1.5))
    banner = bytes(rx)
    if banner:
        log(f"启动输出: {banner!r}")
    else:
        log("启动输出: (无) —— 芯片可能已跑过头，继续敲命令验证")

    # ② 敲 help，确认 shell 活着
    rx.clear()
    sp.write(b"\r\nhelp\r\n")
    sp.flush()
    pump(1.2)
    resp = bytes(rx)
    log(f"help 应答 ({len(resp)} B): {resp!r}")
    if b"List commands" not in resp:
        log("  [!] help 没有正常应答")
        ok = False

    # ③ 敲 sfra，确认 SFRA 命令挂上了
    rx.clear()
    sp.write(b"sfra\r\n")
    sp.flush()
    pump(1.0)
    resp = bytes(rx)
    log(f"sfra 应答 ({len(resp)} B): {resp!r}")
    if b"sfra" not in resp:
        log("  [!] sfra 命令没有应答")
        ok = False

    sp.close()
    return ok


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="烧录 + 复位运行 + 串口自检")
    ap.add_argument("--no-build", action="store_true", help="跳过编译")
    ap.add_argument("--verify-only", action="store_true", help="只复位+自检，不烧录")
    ap.add_argument("--port", default=None, help="串口名，默认自动选第一个")
    ap.add_argument("--no-serial", action="store_true", help="不做串口自检")
    a = ap.parse_args()

    if not a.verify_only:
        if not a.no_build and not build():
            return 1
        if not AXF.exists():
            log(f"找不到固件: {AXF}")
            return 1
        if not flash():
            return 1
    else:
        log("跳过编译与烧录")

    # 关键一步：Keil 命令行烧完不复位，必须自己来
    if not reset_and_run():
        log("复位失败。手动按板上 RESET 键，或拔插 USB 供电后再试。")
        return 2

    if a.no_serial:
        return 0

    time.sleep(0.8)          # 让固件跑过初始化
    ok = serial_selftest(a.port)
    log("=" * 50)
    log("✅ 全部通过" if ok else "⚠️ 复位成功，但串口自检有问题（见上）")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
