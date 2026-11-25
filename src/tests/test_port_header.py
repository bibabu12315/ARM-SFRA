"""适配层（`sfra_port.h`）的护栏测试。

要防的是这个局面：
    `sfra_port.h` 写得很漂亮（四个函数、示例代码、注意点），但**没有任何
    代码 include 它**。那样一来注释和现实会慢慢漂移：函数名改了没人知道，
    参数顺序变了也没人知道，直到用户拿它去移植 MCU 时才发现。
    S4 之前项目就是这个状态——`sfra_core.h` 的注释里引用 sfra_port.h，
    但文件根本不存在。

三条护栏：
    1. 文件存在，四个必需函数是"只有声明、没有定义"的适配约定；
    2. 假 MCU 真的 include 它、真的通过它收发字节（不是直接碰 stdout）；
    3. PC 参考实现把这四个函数全实现了，且与假 MCU 一起编译通过。

**不**做"删掉实现就该链接失败"那种测试——因为库本身不调用 port 函数
（零 HAL 依赖，见 sfra_port.h 的说明），删掉确实能链接成功。S4 的验收
是"上板 PING 通"，不是"编译能过"。曾经写了一条这样的用例，实测发现
它能链接成功，于是把它改成验证"约定被遵守"而不是"链接器会拦"。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FWM = ROOT / "firmware"
PORT_H = FWM / "sfra_inc" / "sfra_port.h"
SIM_MCU = FWM / "examples" / "pc" / "sfra_sim_mcu.c"
PORT_PC = FWM / "examples" / "pc" / "sfra_port_pc.c"

REQUIRED = (
    "sfra_port_uart_write",
    "sfra_port_uart_read_byte",
    "sfra_port_enter_critical",
    "sfra_port_exit_critical",
)


def test_port_header_exists():
    """S4 之前这个文件根本不存在（02 §2.2.3 描述了它，但没落地）。"""
    assert PORT_H.exists(), "sfra_port.h 必须存在——它是唯一要用户填的文件"


@pytest.mark.parametrize("fn", REQUIRED)
def test_required_functions_declared_only(fn):
    """四个必需函数必须**只有声明**：给了默认空实现就等于没护栏。"""
    text = PORT_H.read_text(encoding="utf-8")
    # 去掉注释，避免把注释里的示例代码当成声明
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    decl = re.search(rf"\b{fn}\s*\([^;{{]*\)\s*;", code)
    assert decl is not None, f"{fn} 未在 sfra_port.h 声明"
    body = re.search(rf"\b{fn}\s*\([^;{{]*\)\s*\{{", code)
    assert body is None, f"{fn} 在 sfra_port.h 里有定义——应当是纯声明"


def test_pc_reference_implements_all_required():
    """PC 参考实现必须把这四个函数全实现（否则假 MCU 链接不过）。"""
    code = PORT_PC.read_text(encoding="utf-8")
    for fn in REQUIRED:
        assert re.search(rf"\b{fn}\s*\([^;{{]*\)\s*\{{", code), \
            f"{fn} 未在 sfra_port_pc.c 实现"


def test_sim_mcu_goes_through_port_layer():
    """假 MCU 必须经 port 层收发，不许直接 fprintf(stdout) 写协议帧。

    这条是"让声明有用"的关键：只要假 MCU 走 port 层，`sfra_port.h` 里
    任何一个函数名/签名写错都会立刻编译失败。
    """
    code = SIM_MCU.read_text(encoding="utf-8")
    assert '#include "sfra_port.h"' in code
    assert "sfra_port_uart_write(" in code
    assert "sfra_port_uart_read_byte(" in code
    # 协议输出路径上不应再出现裸 fwrite(stdout) / putchar
    code_wo_comments = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    assert "fwrite(" not in code_wo_comments, \
        "假 MCU 不应直接 fwrite(stdout)，应走 sfra_port_uart_write"
    assert "putchar(" not in code_wo_comments
    assert "getchar(" not in code_wo_comments, \
        "假 MCU 不应直接 getchar()，应走 sfra_port_uart_read_byte"


def test_sim_mcu_does_not_redeclare_port_functions():
    """假 MCU 不得自己再定义一遍 port 函数（否则 PC 实现被绕过）。"""
    code = SIM_MCU.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    for fn in REQUIRED:
        assert not re.search(rf"\b{fn}\s*\([^;{{]*\)\s*\{{", code), \
            f"{fn} 在 sfra_sim_mcu.c 里被重新定义了"


@pytest.mark.crosscheck
def test_port_layer_compiles_with_library(tmp_path):
    """port 实现 + 库 + 假 MCU 一起编译：用 `-Werror=missing-prototypes` 逼出
    "声明与实现不一致"这类问题（比如把 uint16_t 写成 int 而两边都能编译）。

    不测"缺实现就链接失败"——库不调用 port 函数，缺了也能链上，这是
    零 HAL 依赖设计的必然结果，见 sfra_port.h 一节的说明。
    """
    import subprocess
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    import cross_check as cc

    gcc = cc.find_toolchain()
    if gcc is None:
        pytest.skip("找不到 x86 gcc")

    exe = tmp_path / "with_port.exe"
    cmd = [str(gcc), "-std=c99", "-O1",
           "-Werror=implicit-function-declaration",
           f"-I{FWM / 'sfra_inc'}",
           str(FWM / "sfra_src" / "sfra_core.c"),
           str(FWM / "sfra_src" / "sfra_proto.c"),
           str(PORT_PC),
           str(SIM_MCU),
           "-o", str(exe), "-lm"]

    # **必须**把 toolchain 的 bin 放进 PATH。
    # gcc 是驱动器，真正干活的是 cc1.exe，它依赖 libgmp-10.dll /
    # libmpc-3.dll / libisl-23.dll 等十来个大块头。这些 DLL 只在
    # tools/toolchain/w64/mingw64/bin 里，Windows 不会去别处找
    # （没有 rpath 概念）。找不到 DLL 时 cc1 直接 exit 127 且**不打印
    # 任何文字**，表象是"编译失败但 stderr 全空"——排查起来极费时间。
    # `link.SimMcuLink.build()` 里也是同样的处理。
    import os
    env = dict(os.environ)
    env["PATH"] = str(gcc.parent) + os.pathsep + env.get("PATH", "")

    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    detail = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, \
        f"port 层编译失败（rc={r.returncode}）：\n{detail[-1500:]}"
    assert exe.exists()


def test_port_header_documents_no_link_enforcement():
    """头文件必须写明"不做实现也能链接通过"，否则使用者会被误导。

    这是一条"文档言行一致"测试：S4 之前我们误以为声明能靠链接器兜底，
    实测发现库根本不调用 port 函数。把事实写进头文件，再用测试锁住，
    免得以后有人把它改回"缺一不可"的说法。
    """
    text = PORT_H.read_text(encoding="utf-8")
    assert "不在库的调用链上" in text
    assert "照样能编译链接通过" in text
