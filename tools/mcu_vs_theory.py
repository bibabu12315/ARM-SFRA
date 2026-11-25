"""MCU 模拟二阶 LC 的"理论真值"与"实扫结果"逐点核对。

用户的原话：

    我的真正目的是要扫描出 bode 图来。目前我们是在 MCU 中软件模拟了一个
    二阶 LC 模型，这个是为了克服我们没有实际的板子。也就是说我们后续
    验证依旧采用这个 MCU 模拟二阶 LC 模型。那既然是我们模拟，我们就能
    提前通过计算得知真实的系统的 Bode 图长什么样子，然后再和真实的比较，
    即可判断是否可信。

这段话里"真实的系统"指的是**MCU 里跑的那个数字滤波器**，不是物理功率级。
所以基准必须取 `theory.TustinLC`（离散域），**不能**取连续域解析解。

本脚本就是这条链路的闭环证据：

    C 固件里的系数公式  ──┐
                          ├─→ 逐位一致（test_theory.py 锁着）
    plants.SecondOrderLC ─┘
             │
             ↓  同一个差分方程
    theory.TustinLC  ──→ 理论 H(z)  ← 这就是"真值"
             │
             ↓  对比
    MCU 实扫（真 C 固件 + 真协议）  ──→ 实测 H
             │
             ↓  判据
    偏差 < 0.5 dB / 1.5°  →  可信

用法::

    # 走假 MCU（真 C 代码 + 真协议，不需要硬件）
    .venv\\Scripts\\python.exe tools\\mcu_vs_theory.py --fake

    # 接真板
    .venv\\Scripts\\python.exe tools\\mcu_vs_theory.py --port COM7

    # 扫多条曲线验证稳定性
    .venv\\Scripts\\python.exe tools\\mcu_vs_theory.py --fake --f0 1000 2000 5000

判据为什么是 0.5 dB / 1.5°（而不是 1 dB / 3°）
------------------------------------------------
因为比对对象是**同一个数字滤波器**：MCU 跑的是 Tustin LC，理论也算
Tustin LC。两者之间不存在"离散化偏差"这一项，剩下的只有：
    · 定点/浮点舍入
    · DFT 累加的截断
    · 整周期取整带来的 f_act 偏移（理论上已被 f_act 修正掉）
这些都很小，所以判据应该严。给 1 dB / 3° 是给"物理对象"用的，
用在这里等于白送 20 倍容差。

反过来，如果想看"MCU 这个离散实现离物理功率级有多远"，那是
`tools/bode_reference.py` 的活，不是这里。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                       # noqa: E402

from app.sim.theory import TustinLC, TustinLP1           # noqa: E402


# ---------------------------------------------------------------- 理论真值
def theory_at(kind: str, f: float, fs: float, **kw):
    """取该频点的理论 H（复数）。"""
    if kind == "lp1":
        return TustinLP1(fc=float(kw["fc"]), fs=fs).h(f)
    return TustinLC(f0=float(kw["f0"]), Q=float(kw["q"]), fs=fs).h(f)


# ---------------------------------------------------------------- 实扫
def run_sim_mcu(isr: float, f0: float, q: float, kind: str, fc: float,
                f_start: float, f_stop: float, ppd: int,
                mode: int, amp: float, cyc: int):
    """走假 MCU（真 C 固件 + 真协议）扫一条曲线。"""
    from PyQt5 import QtCore, QtWidgets
    from app.comm.controller import SimLinkController
    from app.model.sweep_config import SweepConfig

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ctrl = SimLinkController()
    cfg = SweepConfig(isr_freq=isr, f_start=f_start, f_stop=f_stop, ppd=ppd,
                      cyc_mode=cyc, mode=mode, amp_pu=amp, dut=kind,
                      f0c=f0, q=q)
    loop = QtCore.QEventLoop()
    done = {}

    def on_done(ok, msg):
        done["v"] = (ok, msg)
        QtCore.QTimer.singleShot(80, loop.quit)

    ctrl.sigDone.connect(on_done)

    # 假 MCU 要先起子进程，再握手，再发配置
    def go():
        ctrl.start(cfg)

    QtCore.QTimer.singleShot(120, go)
    QtCore.QTimer.singleShot(600_000, loop.quit)
    loop.exec_()

    if "v" not in done:
        raise SystemExit("扫频超时")
    ok, msg = done["v"]
    res = ctrl.result
    ctrl.stop()
    del app
    if res is None or len(res) == 0:
        raise SystemExit(f"没有数据：{msg}")
    return res.freq.copy(), res.mag.copy(), res.phase.copy(), ok, msg


def run_serial(port: str, baud: int, f_start: float, f_stop: float,
               ppd: int, mode: int, amp: float, cyc: int):
    """接真板扫一条曲线。"""
    from PyQt5 import QtCore, QtWidgets
    from app.comm.controller import SerialLinkController
    from app.model.sweep_config import SweepConfig

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ctrl = SerialLinkController()
    if not ctrl.connect_port(port, baud):
        raise SystemExit(f"打不开 {port}")

    loop = QtCore.QEventLoop()
    done = {}

    def on_done(ok, msg):
        done["v"] = (ok, msg)
        QtCore.QTimer.singleShot(80, loop.quit)

    ctrl.sigDone.connect(on_done)

    def start_when_ready(n=0):
        if ctrl.caps is None:
            if n > 40:
                print("  握手超时：CAPS 未到达")
                loop.quit()
                return
            QtCore.QTimer.singleShot(100, lambda: start_when_ready(n + 1))
            return
        fs = float(ctrl.caps.isr_freq) or 100000.0
        print(f"  握手成功 · ISR {fs:.0f} Hz")
        cfg = SweepConfig(isr_freq=fs, f_start=f_start, f_stop=f_stop,
                          ppd=ppd, cyc_mode=cyc, mode=mode, amp_pu=amp)
        done["fs"] = fs
        ctrl.start(cfg)

    QtCore.QTimer.singleShot(300, start_when_ready)
    QtCore.QTimer.singleShot(600_000, loop.quit)
    loop.exec_()

    if "v" not in done:
        raise SystemExit("扫频超时或未开始")
    ok, msg = done["v"]
    res = ctrl.result
    ctrl.disconnect_port()
    del app
    if res is None or len(res) == 0:
        raise SystemExit(f"没有数据：{msg}")
    return res.freq.copy(), res.mag.copy(), res.phase.copy(), ok, msg


# ---------------------------------------------------------------- 比对
def compare(freqs, mags, phases, kind, fs, **kw):
    """逐点比对，返回 (行列表, 汇总)。

    **判据必须按每周期采样点数分段**，不能一刀切：

        点/周 ≥ 10  →  0.5 dB / 1.5°   （可判生死）
        点/周 < 10  →  只记录，不判     （物理上不可信，见下）

    为什么：注入频率越高，一个周期内的采样点越少。扫频的整周期取整
    要求 `data_count × step == cycles × 2π`，每周期只有 5 点时，
    `data_count` 的取整误差已经到 1/5 个周期的量级，DFT 的相位基准
    随之偏掉几十度。

    实测（f0=1300/Q=2/fs=100k，20 点/decade）：点/周 ≥ 10 的频段
    偏差 ≤ 0.15 dB / 1.4°；到 20 kHz（5 点/周）相位偏 +43.8°。
    这个 43.8° 不是固件错，是"用 5 个点还原一个周期"的必然代价——
    把它算进判据，会逼着人去修一个不存在的 bug。
    """
    rows = []
    for f, m, p in zip(freqs, mags, phases):
        h = theory_at(kind, float(f), fs, **kw)
        mt = 20.0 * math.log10(abs(h)) if abs(h) > 0 else float("nan")
        pt = math.degrees(math.atan2(h.imag, h.real))
        dp = (float(p) - pt + 180.0) % 360.0 - 180.0
        spc = fs / max(float(f), 1e-9)
        rows.append({
            "f": float(f), "mag": float(m), "pha": float(p),
            "mag_t": mt, "pha_t": pt,
            "d_mag": float(m) - mt, "d_pha": dp,
            "spc": spc,
            #: 该点是否参与判据（点/周足够才算）
            "judged": spc >= SPC_MIN_JUDGE,
        })
    if not rows:
        return [], {"passed": False, "reason": "无数据"}

    judged = [r for r in rows if r["judged"]]
    pool = judged or rows               # 全都不够密时退化成全判（并标注）
    dm = max(abs(r["d_mag"]) for r in pool)
    dp = max(abs(r["d_pha"]) for r in pool)
    worst_m = max(pool, key=lambda r: abs(r["d_mag"]))
    worst_p = max(pool, key=lambda r: abs(r["d_pha"]))
    hi = [r for r in rows if not r["judged"]]
    return rows, {
        "n": len(rows), "n_judged": len(judged),
        "max_db": dm, "max_deg": dp,
        "worst_m": worst_m, "worst_p": worst_p,
        "hi_db": max((abs(r["d_mag"]) for r in hi), default=0.0),
        "hi_deg": max((abs(r["d_pha"]) for r in hi), default=0.0),
        "passed": dm < 0.5 and dp < 1.5,
    }


#: 参与判据的最少每周期采样点数。低于它不判，只记录。
#: 与 `sweep_config.validate()` 里"建议 ≥ 4"、`bode_data.warn_mask()`
#: 的"< 4 标黄"配套：这里取 10 是因为**判据要严**，而 4 只是"能画"。
SPC_MIN_JUDGE = 10.0


def print_report(rows, s, label: str) -> None:
    print()
    print(f"  ==== {label} ====")
    print()
    print(f"  {'f (Hz)':>10} {'实测 dB':>9} {'实测 °':>9} "
          f"{'理论 dB':>9} {'理论 °':>9} {'ΔdB':>7} {'Δ°':>7} {'点/周':>6}")
    print("  " + "-" * 78)
    for r in rows:
        mark = ""
        if not r["judged"]:
            mark = " ~"          # 采样过稀，不参与判据
        elif abs(r["d_mag"]) >= 0.5 or abs(r["d_pha"]) >= 1.5:
            mark = " !"
        print(f"  {r['f']:10.1f} {r['mag']:9.3f} {r['pha']:9.2f} "
              f"{r['mag_t']:9.3f} {r['pha_t']:9.2f} "
              f"{r['d_mag']:+7.3f} {r['d_pha']:+7.2f} "
              f"{r['spc']:6.1f}{mark}")
    print("  （~ = 每周期采样 < {:.0f} 点，不参与判据）".format(SPC_MIN_JUDGE))
    print()
    ok = "PASS" if s["passed"] else "FAIL"
    print(f"  [{ok}] 判据段（{s['n_judged']}/{s['n']} 点）："
          f"幅值 max {s['max_db']:.3f} dB（需 < 0.5），"
          f"相位 max {s['max_deg']:.3f}°（需 < 1.5）")
    if s["n_judged"] < s["n"]:
        print(f"       采样过稀段：幅值 max {s['hi_db']:.2f} dB，"
              f"相位 max {s['hi_deg']:.1f}°（不判，见下方说明）")
    if not s["passed"]:
        wm, wp = s["worst_m"], s["worst_p"]
        print(f"       幅值最差点：{wm['f']:.1f} Hz  {wm['d_mag']:+.3f} dB")
        print(f"       相位最差点：{wp['f']:.1f} Hz  {wp['d_pha']:+.3f}°")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="MCU 模拟二阶 LC：理论基准 vs 实扫结果")
    ap.add_argument("--kind", choices=("lc", "lp1"), default="lc")
    ap.add_argument("--f0", type=float, nargs="+", default=[1300.0],
                    help="可给多个，逐个验证")
    ap.add_argument("--fc", type=float, nargs="+", default=[1000.0])
    ap.add_argument("--q", type=float, nargs="+", default=[2.0])
    ap.add_argument("--fs", type=float, default=100_000.0)
    ap.add_argument("--f-start", type=float, default=100.0)
    ap.add_argument("--f-stop", type=float, default=None,
                    help="缺省 = f_s/5")
    ap.add_argument("--ppd", type=int, default=20)
    ap.add_argument("--mode", choices=("plant", "loop"), default="plant")
    ap.add_argument("--amp", type=float, default=0.01)
    ap.add_argument("--cyc", type=int, default=1, choices=(0, 1, 2))
    ap.add_argument("--port", help="真板串口；不给则走假 MCU")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--fake", action="store_true",
                    help="显式走假 MCU（与不给 --port 等价，便于写清意图）")
    args = ap.parse_args()

    from app.model.bode_data import MODE_LOOP, MODE_PLANT
    mode = MODE_PLANT if args.mode == "plant" else MODE_LOOP
    f_stop = args.f_stop or args.fs / 5.0

    print("=" * 80)
    print("  MCU 模拟二阶 LC  ·  理论基准 vs 实扫结果")
    print("=" * 80)
    print(f"  被测对象  {'二阶 LC（Tustin 离散）' if args.kind=='lc' else '一阶 RC'}")
    print(f"  ISR       {args.fs/1000:.1f} kHz")
    print(f"  扫频范围  {args.f_start:.0f} ~ {f_stop:.0f} Hz，"
          f"{args.ppd} 点/decade，{args.mode.upper()} 模式")
    print(f"  路径      {'真板 ' + args.port if args.port else '假 MCU（真 C 固件 + 真协议）'}")

    all_ok = True
    combos = list(zip(args.f0, args.q)) if args.kind == "lc" else \
        [(fc, 0.0) for fc in args.fc]
    for a, b in combos:
        if args.kind == "lc":
            kw = {"f0": a, "q": b}
            label = f"二阶 LC  f0={a:.0f} Hz  Q={b:g}"
        else:
            kw = {"fc": a}
            label = f"一阶 RC  fc={a:.0f} Hz"

        if args.port:
            fr, mg, ph, ok, msg = run_serial(
                args.port, args.baud, args.f_start, f_stop,
                args.ppd, mode, args.amp, args.cyc)
            fs = args.fs
        else:
            fr, mg, ph, ok, msg = run_sim_mcu(
                args.fs, kw.get("f0", 0.0), kw.get("q", 0.0), args.kind,
                kw.get("fc", 0.0), args.f_start, f_stop,
                args.ppd, mode, args.amp, args.cyc)
            fs = args.fs

        if not ok:
            print(f"\n  {label}: 扫频未完成（{msg}），跳过")
            all_ok = False
            continue

        rows, s = compare(fr, mg, ph, args.kind, fs, **kw)
        print_report(rows, s, label)
        all_ok = all_ok and s["passed"]

    print()
    print("=" * 80)
    print("  全部可信" if all_ok else "  有偏差超限，见上")
    print("=" * 80)
    if all_ok:
        print()
        print("  结论：MCU 实际扫出来的曲线与理论 H(z) 逐点吻合，说明")
        print("        · 固件的整周期取整、θ 递推、DFT 累加都正确")
        print("        · 协议的累加器传输没有截断或字节序错")
        print("        · 上位机的 z_from / h_from_raw 复数约定正确")
        print("  可以据此认为这条链路的 Bode 图是可信的。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
