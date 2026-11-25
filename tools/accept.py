"""SFRA v1.0 一键自动化验收。

把 docs/07_v1.0验证清单.md 里 §1 的三条命令串起来跑，最后给一个总判定，
并把完整输出写成报告。

用法：
    # 最常用：全部跑（需要板子连着、串口空闲）
    .venv\\Scripts\\python.exe tools\\accept.py

    # 没接板子，只跑不依赖硬件的部分
    .venv\\Scripts\\python.exe tools\\accept.py --no-hw

    # 指定串口
    .venv\\Scripts\\python.exe tools\\accept.py --port COM14

    # 只跑某一项
    .venv\\Scripts\\python.exe tools\\accept.py --only pytest

判定规则（与验证清单 §8 一致）：
    pytest      必须 0 failed
    walkthrough 必须 0 failed
    health      必须 0 failed，且幅值/相位偏差都在判据内
    全部通过 -> v1.0 放行
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(_ROOT, ".venv", "Scripts", "python.exe")
REPORT = os.path.join(_ROOT, "docs", "08_验收报告.md")

#: 各环节的判据
MAG_LIMIT_DB = 0.5
PH_LIMIT_DEG = 2.0


@dataclass
class Step:
    key: str
    title: str
    cmd: list[str]
    needs_hw: bool
    expect: str
    ok: bool = False
    skipped: bool = False
    tail: str = ""
    elapsed: float = 0.0
    note: str = ""
    parsed: dict = field(default_factory=dict)


def wait_port_free(port: str, timeout: float = 15.0) -> tuple[bool, str]:
    """等串口出现在枚举列表里，并留一点时间给上一步彻底释放。

    【为什么不真的去 open 一下】
    最初这里用 QSerialPort 实际 open/close 来"验证可占用"，结果反而
    引入了 bug：父进程 open 再 close 之后，子进程（走查脚本）再去
    `open` 就会握手超时——实测稳定复现（accept.py 里 22/2，单独跑
    走查 24/0/0）。QSerialPort 在同一个父进程里先开一次，会让驱动
    层的句柄释放滞后，子进程随即打开就拿不到数据。

    所以改成纯只读检查：确认端口在枚举列表里 + 睡够时间。真被别的
    程序占着的话，子进程自己会报"端口可能被其他程序占用"，那条信息
    比这里猜的准。
    """
    if not port:
        return True, ""
    try:
        sys.path.insert(0, os.path.join(_ROOT, "src"))
        from app.comm.protocol import list_ports
    except Exception:                          # noqa: BLE001
        return True, ""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if port in list_ports():
                # 端口在，再等 1.2s 让上一个进程的句柄彻底落下去
                time.sleep(1.2)
                return True, ""
        except Exception:                      # noqa: BLE001
            pass
        time.sleep(0.4)
    return False, f"{port} 不在可用串口列表中"


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str, float]:
    """跑一条命令，返回 (returncode, 合并输出, 耗时)。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=_ROOT, capture_output=True,
                           timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out + err, time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s", time.perf_counter() - t0


# ---------------------------------------------------------------- 结果解析
def judge_pytest(text: str) -> tuple[bool, dict, str]:
    m = re.search(r"(\d+) passed", text)
    f = re.search(r"(\d+) failed", text)
    passed = int(m.group(1)) if m else 0
    failed = int(f.group(1)) if f else 0
    return failed == 0 and passed > 0, {"passed": passed, "failed": failed}, ""


def judge_walkthrough(text: str) -> tuple[bool, dict, str]:
    """期望 `通过 24 / 失败 0 / 警告 0` 这种形式。"""
    m = re.search(r"通过\s*(\d+)\s*/\s*失败\s*(\d+)(?:\s*/\s*警告\s*(\d+))?",
                  text)
    if not m:
        # 有的版本写成 PASS/FAIL 计数
        p = len(re.findall(r"\[PASS\]", text))
        f = len(re.findall(r"\[FAIL\]", text))
        return f == 0 and p > 0, {"passed": p, "failed": f, "warn": 0}, ""
    passed, failed = int(m.group(1)), int(m.group(2))
    warn = int(m.group(3)) if m.group(3) else 0
    return failed == 0 and passed > 0, \
        {"passed": passed, "failed": failed, "warn": warn}, ""


def judge_health(text: str) -> tuple[bool, dict, str]:
    """解析幅值/相位偏差，与判据比。"""
    mv = re.search(r"幅值偏差[^0-9\-]*([0-9.]+)", text)
    pv = re.search(r"相位偏差[^0-9\-]*([0-9.]+)", text)
    mag = float(mv.group(1)) if mv else None
    ph = float(pv.group(1)) if pv else None
    d = {"mag_db": mag, "phase_deg": ph}
    if mag is None or ph is None:
        # 解析不到数值 -> **不能算通过**。之前这里返回 True，
        # 结果握手失败、一个数都没测到时反而报 PASS，是很危险的假绿。
        return False, d, "未能解析到偏差数值（握手可能未完成）"
    okmag = mag <= MAG_LIMIT_DB
    okph = ph <= PH_LIMIT_DEG
    note = ""
    if not okmag:
        note += f"幅值超判据({mag:.3f}>{MAG_LIMIT_DB}) "
    if not okph:
        note += f"相位超判据({ph:.3f}>{PH_LIMIT_DEG}) "
    return okmag and okph, d, note.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hw", action="store_true",
                    help="不跑需要硬件的项（walkthrough / health）")
    ap.add_argument("--port", default="", help="串口号，例如 COM14")
    ap.add_argument("--only", default="",
                    help="只跑某一项：pytest | walk / health")
    ap.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = ap.parse_args()

    steps = [
        Step("pytest", "上位机单元测试", [PY, "-m", "pytest", "-q"],
             False, "0 failed"),
        Step("walk", "界面端到端走查",
             [PY, os.path.join("tools", "v1_walkthrough.py")]
             + ([args.port] if args.port else []),
             True, "失败 0"),
        Step("health", "真板精度校验",
             [PY, os.path.join("tools", "health_check.py")]
             + ([args.port] if args.port else []),
             True, "幅值 <=0.5dB 且 相位 <=2.0°"),
    ]

    if args.only:
        pick = args.only.lower()
        alias = {"pytest": "pytest", "walk": "walk",
                 "walkthrough": "walk", "health": "health"}
        want = alias.get(pick, pick)
        steps = [s for s in steps if s.key == want]
        if not steps:
            print(f"未知的 --only 取值：{args.only}")
            return 2

    print("=" * 66)
    print("SFRA v1.0 自动化验收")
    print(f"工程：{_ROOT}")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 66)

    for s in steps:
        if s.needs_hw and args.no_hw:
            s.skipped = True
            s.note = "按 --no-hw 跳过"
            print(f"\n>>> {s.title}：跳过（--no-hw）")
            continue

        # 需要硬件的项，先把串口等到能打开，避免上一步的残留占用
        # 造成"握手超时"这种假失败。
        if s.needs_hw and args.port:
            ready, why = wait_port_free(args.port)
            if not ready:
                s.ok = False
                s.note = f"{args.port} 无法打开：{why}"
                print(f"\n>>> {s.title}")
                print(f"    [FAIL] {s.note}")
                continue

        print(f"\n>>> {s.title}")
        print(f"    $ {' '.join(s.cmd)}")
        rc, out, dt = run(s.cmd)
        s.elapsed = dt
        s.tail = out

        if s.key == "pytest":
            s.ok, s.parsed, s.note = judge_pytest(out)
        elif s.key == "walk":
            s.ok, s.parsed, s.note = judge_walkthrough(out)
        else:
            s.ok, s.parsed, s.note = judge_health(out)

        # returncode 非 0 时，只有在解析明确通过的情况下才容忍
        if rc != 0 and not s.ok:
            s.ok = False

        status = "PASS" if s.ok else "FAIL"
        print(f"    [{status}] {dt:.1f}s  {s.parsed}")
        if s.note:
            print(f"    注：{s.note}")
        # 打印尾部，方便当场看
        for line in out.strip().splitlines()[-8:]:
            print("      " + line)

    # ---------------- 汇总 ----------------
    ran = [s for s in steps if not s.skipped]
    failed = [s for s in ran if not s.ok]
    passed = [s for s in ran if s.ok]

    print("\n" + "=" * 66)
    print("汇总")
    print("=" * 66)
    for s in steps:
        if s.skipped:
            mark, detail = "SKIP", s.note
        elif s.ok:
            mark, detail = "PASS", str(s.parsed)
        else:
            mark, detail = "FAIL", (s.note or str(s.parsed))
        print(f"  {mark:5s} {s.title:18s} {detail}")

    all_ok = len(failed) == 0 and len(passed) > 0
    n_skip = len([s for s in steps if s.skipped])
    if all_ok:
        if n_skip:
            # 跳过项不能算通过。只跑一部分时给"部分通过"，避免误放行。
            print(f"\n*** 已跑的 {len(passed)} 项全部通过，"
                  f"但还有 {n_skip} 项未跑 —— 不构成完整验收 ***")
            all_ok = False
        else:
            print("\n*** v1.0 验收通过 —— 可以放行 ***")
    else:
        print("\n*** 未通过，见上面 FAIL 项 ***")

    if not args.no_report:
        write_report(steps, all_ok, args)

    return 0 if all_ok else 1


def write_report(steps: list[Step], all_ok: bool, args) -> None:
    lines = [
        "# SFRA v1.0 验收报告（自动生成）",
        "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> 生成命令：`python tools/accept.py"
        + (f" --no-hw" if args.no_hw else "")
        + (f" --port {args.port}" if args.port else "")
        + (f" --only {args.only}" if args.only else "")
        + "`  ",
        f"> 结论：**{'通过' if all_ok else '未通过'}**",
        "",
        "## 结果",
        "",
        "| 项目 | 结果 | 关键数字 | 耗时 |",
        "| --- | --- | --- | --- |",
    ]
    for s in steps:
        if s.skipped:
            res = "跳过"
            num = s.note
        else:
            res = "通过" if s.ok else "**失败**"
            num = ", ".join(f"{k}={v}" for k, v in s.parsed.items()) or s.note
        lines.append(f"| {s.title} | {res} | {num} | {s.elapsed:.1f}s |")

    lines += ["", "## 判据", "",
              f"- 幅值偏差 ≤ {MAG_LIMIT_DB} dB",
              f"- 相位偏差 ≤ {PH_LIMIT_DEG} °",
              "- 单元测试与界面走查均 0 失败",
              "", "## 原始输出", ""]

    for s in steps:
        if s.skipped:
            continue
        lines += [f"### {s.title}", "", "```", s.tail.strip()[-6000:],
                  "```", ""]

    try:
        os.makedirs(os.path.dirname(REPORT), exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")
        print(f"\n报告已写入：{REPORT}")
    except OSError as e:
        print(f"\n报告写入失败：{e}")


if __name__ == "__main__":
    sys.exit(main())
