"""S6a · 上板自测验证（L2 gate）。

对应 04 文档 §4.4 / 00 文档 §0.5.4 的 S6a 验收：
把 `firmware/examples/stm32/sfra_selftest_stm32.c` 烧到真板上，
用**一阶低通**当被测对象扫一条 Bode，与解析解逐点比对。

判据（DoD）：
    1. 幅值误差 < 1.0 dB、相位误差 < 3.0°（Normal 档，f_s/5 以内）
    2. 相位单调 0° → −90°，**不得出现正相位**（符号反了会 +90°）
    3. fc 处 −3 dB / −45°（±1.5 dB / ±6°）
    4. 高频段斜率 ≈ −20 dB/dec（−18 ~ −22）

用法::

    # 真板
    python tools/selftest_s6a.py --port COM7 --fc 1000

    # 没有板子时自检脚本本身（走假 MCU，被测对象换成一阶低通）
    python tools/selftest_s6a.py --fake --fc 200

设计要点
--------
**为什么拿解析解而不是"另一条实测曲线"当基准**：一阶低通有闭式解，
`|H| = 1/√(1+(f/fc)²)`、`∠H = −arctan(f/fc)`，没有任何拟合参数。
拿它当基准，误差可以直接归因到固件，不需要为"模型不准"找借口。

**注意 Tustin 翘曲**：固件用 Tustin 离散化，高频相位会比连续域解析解
更"负"一点。在 f_s/5 以内差值 < 1°，落在 3° 容差内不必特殊处理；
但如果你把 fc 取得接近 f_s/2，这个偏差会吃掉全部容差，
脚本会在报告里单独标出"翘曲占比"，免得你误以为是固件错了。
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------- 解析解
@dataclass
class Theory:
    """一阶低通的连续域解析解。

    连续域而非 Tustin 域的解析解：因为"物理对象"就是一阶 RC，
    固件的 Tustin 离散化只是实现手段。用连续域当基准，
    才能同时抓到"离散化实现错"和"算法错"。
    """

    fc: float

    def mag_db(self, f: float) -> float:
        return -10.0 * math.log10(1.0 + (f / self.fc) ** 2)

    def phase_deg(self, f: float) -> float:
        return -math.degrees(math.atan(f / self.fc))

    def tustin_phase_deg(self, f: float, fs: float) -> float:
        """Tustin 域的相位，用于估计翘曲占比（不是判据）。

        双线性变换把 s → k(1−z⁻¹)/(1+z⁻¹)，k = 2fs。
        代入 z⁻¹ = e^{−jθ} 得数字域频率响应，比连续域更"超前/滞后"。
        """
        return math.degrees(math.atan2(
            self._tustin_h(f, fs).imag, self._tustin_h(f, fs).real))

    def tustin_mag_db(self, f: float, fs: float) -> float:
        """Tustin 域的幅值响应（dB）。见 `warp_db`。"""
        return 20.0 * math.log10(abs(self._tustin_h(f, fs)))

    def _tustin_h(self, f: float, fs: float) -> complex:
        """一阶低通的 Tustin 离散传递函数，在数字频率 f 处求值。

        与固件 `lp1_init()` / `app/sim/plants.py::FirstOrderLP` 同源：
            b0 = b1 = ωc/(ωc+k),  a1 = (ωc−k)/(ωc+k)
            H(z) = (b0 + b1·z⁻¹) / (1 + a1·z⁻¹)
        注意分母是 **1 + a1·z⁻¹**（a1 本身已含负号），
        写成 `1 − a1·z⁻¹` 会把直流增益算成 1/159。
        """
        k = 2.0 * fs
        w = 2.0 * math.pi * self.fc
        b0 = w / (w + k)
        a1 = (w - k) / (w + k)
        th = 2.0 * math.pi * f / fs
        zm = complex(math.cos(th), -math.sin(th))
        return (b0 + b0 * zm) / (1.0 + a1 * zm)

    def warp_db(self, f: float, fs: float) -> float:
        """Tustin 域与连续域的幅值差（dB）。**是负的**，高频越来越大。

        这不是 bug，是数学：双线性变换把整个 jω 轴压缩到单位圆内，
        频率轴被"翘曲"（frequency warping）。一阶低通在 f_s/5 处
        已经有约 −1.26 dB 的幅值偏差，相位偏差极小（< 0.1°）。
        """
        return self.tustin_mag_db(f, fs) - self.mag_db(f)


# ---------------------------------------------------------------- 报告
@dataclass
class Row:
    f: float
    mag: float
    pha: float
    mag_th: float
    pha_th: float
    d_mag: float
    d_pha: float
    dpc: float          # 每周期采样点


def build_report(freqs, mags, phases, th: Theory, fs: float,
                 mag_tol=1.0, pha_tol=3.0,
                 check_tustin=True) -> tuple[list[Row], dict]:
    """逐点比对，返回 (明细行, 汇总判定)。

    **两条基准线，不能混用**：

    1. **Tustin 离散域**（`tustin_mag_db` / `tustin_phase_deg`）——
       "固件应该算出什么"的精确答案（固件就是一阶 Tustin，
       `lp1_init()` 里 b0=b1=ωc/(ωc+k)）。**默认判据**，全频段
       0.5 dB / 1.5°。固件实现与它不符才是真 bug。

    2. **连续域**（`mag_db` / `phase_deg`）——"物理对象"的定义。
       它和 Tustin 在 20 kHz（f_s/5）处差 **1.26 dB**，这是双线性变换
       的固有翘曲，不是缺陷。所以连续域只判**低频段**（f ≤ 10·fc，
       此处翘曲 < 0.02 dB）。

    为什么必须分开：若全程只跟连续域比，一个数学完全正确的固件会被
    判 FAIL；若全程只跟 Tustin 比，就看不到"离散化偏离物理对象多远"。
    两者各判一段，报告里都列出来。

    参数 `check_tustin=False` 用于校准脚本自身的判据逻辑
    （喂连续域理论数据进去时，A 段必然不通过且无意义）。
    """
    rows: list[Row] = []
    for f, m, p in zip(freqs, mags, phases):
        mt = th.mag_db(f)
        pt = th.phase_deg(f)
        rows.append(Row(
            f=float(f), mag=float(m), pha=float(p),
            mag_th=mt, pha_th=pt,
            d_mag=float(m) - mt, d_pha=float(p) - pt,
            dpc=(fs / f) if f > 0 else 0.0,
        ))

    if not rows:
        return [], {"passed": False, "reason": "没有数据"}

    # ---- 判据 A：与 Tustin 离散域比（全频段，真 bug 判据）----
    d_tus_mag = [r.mag - th.tustin_mag_db(r.f, fs) for r in rows]
    d_tus_pha = [r.pha - th.tustin_phase_deg(r.f, fs) for r in rows]
    max_tus_mag = max(abs(x) for x in d_tus_mag)
    max_tus_pha = max(abs(x) for x in d_tus_pha)
    a_ok = (max_tus_mag < 0.5 and max_tus_pha < 1.5) if check_tustin else True

    # ---- 判据 B：与连续域比，只看低频段（翘曲可忽略的部分）----
    low = [r for r in rows if r.f <= 10.0 * th.fc]
    max_low_mag = max((abs(r.d_mag) for r in low), default=0.0)
    max_low_pha = max((abs(r.d_pha) for r in low), default=0.0)
    max_dm = max(abs(r.d_mag) for r in rows)
    max_dp = max(abs(r.d_pha) for r in rows)

    # 判据 2：相位必须全负且单调下降（0 → −90°）
    phas = [r.pha for r in rows]
    any_positive = any(p > 0.5 for p in phas)
    monotonic = all(phas[i + 1] <= phas[i] + 0.5
                    for i in range(len(phas) - 1))

    # 判据 3：fc 处 −3 dB / −45°
    near_fc = [r for r in rows if 0.9 * th.fc <= r.f <= 1.1 * th.fc]
    fc_ok, fc_note = True, "未覆盖 fc（扫频范围不包含转折点）"
    if near_fc:
        r = min(near_fc, key=lambda x: abs(x.f - th.fc))
        fc_ok = abs(r.d_mag) < 1.5 and abs(r.d_pha) < 6.0
        fc_note = (f"最近 fc 点 {r.f:.1f} Hz: "
                   f"{r.mag:.2f} dB / {r.pha:.1f}° "
                   f"(理论 {r.mag_th:.2f} / {r.pha_th:.1f})")

    # 判据 4：高频斜率 −20 dB/dec
    #
    # **必须量实测曲线，不能量理论曲线**——量理论等于把判据变成"我抄了
    # 一遍解析解"，无论固件输出什么，只要扫的频点一样，斜率就永远是
    # −20.8，判据恒 PASS。这条错误被 test_high_freq_slope_violation_is_caught
    # 抓出来过，记在这里免得再犯。
    slope_ok, slope = True, float("nan")
    hi = [r for r in rows if r.f >= 5.0 * th.fc]
    if len(hi) >= 3:
        f1, f2 = hi[0].f, hi[-1].f
        m1, m2 = hi[0].mag, hi[-1].mag          # 实测值
        if f2 > f1 and m1 != m2:
            slope = (m2 - m1) / math.log10(f2 / f1)
            slope_ok = -22.0 <= slope <= -18.0

    warp = max((abs(th.warp_db(r.f, fs)) for r in rows), default=0.0)

    summary = {
        "passed": (a_ok and max_low_mag < mag_tol and max_low_pha < pha_tol
                   and not any_positive and monotonic and fc_ok and slope_ok),
        "check_tustin": check_tustin,
        "a_ok": a_ok,
        "n": len(rows),
        "max_dm": max_dm,
        "max_dp": max_dp,
        "max_tus_mag": max_tus_mag,
        "max_tus_pha": max_tus_pha,
        "max_low_mag": max_low_mag,
        "max_low_pha": max_low_pha,
        "any_positive": any_positive,
        "monotonic": monotonic,
        "fc_ok": fc_ok,
        "fc_note": fc_note,
        "slope": slope,
        "slope_ok": slope_ok,
        "warp": warp,
        "worst_mag": max(rows, key=lambda r: abs(r.d_mag)),
        "worst_pha": max(rows, key=lambda r: abs(r.d_pha)),
    }
    return rows, summary


def print_report(rows: list[Row], s: dict, th: Theory, fs: float) -> None:
    print()
    print(f"  被测对象  一阶低通  fc = {th.fc:.1f} Hz")
    print(f"  ISR 频率  {fs:.0f} Hz   实用上限 f_s/5 = {fs/5:.0f} Hz")
    print(f"  频点数    {s['n']}")
    print()
    print(f"  {'f (Hz)':>9} {'|H| (dB)':>9} {'∠H (°)':>9} "
          f"{'理论dB':>9} {'理论°':>9} {'ΔdB':>7} {'Δ°':>7} {'点/周':>6}")
    print("  " + "-" * 78)
    for r in rows:
        flag = ""
        if abs(r.d_mag) >= 1.0 or abs(r.d_pha) >= 3.0:
            flag = " !"
        print(f"  {r.f:9.1f} {r.mag:9.2f} {r.pha:9.2f} "
              f"{r.mag_th:9.2f} {r.pha_th:9.2f} "
              f"{r.d_mag:+7.2f} {r.d_pha:+7.2f} {r.dpc:6.1f}{flag}")
    print("  （! 标记 = 与连续域解析解偏差超过 1 dB / 3°，"
          "高频段属正常翘曲）")
    print()

    def ok(b: bool) -> str:
        return "PASS" if b else "FAIL"

    print("  ---- 判据 A：与 Tustin 离散域比（真 bug 判据）----")
    if not s.get("check_tustin", True):
        print("  [SKIP] 本次数据本身来自连续域模型，A 段不适用")
    else:
        print(f"  [{ok(s['max_tus_mag'] < 0.5)}] 幅值  max {s['max_tus_mag']:.3f} dB"
              f"  (需 < 0.5，全频段)")
        print(f"  [{ok(s['max_tus_pha'] < 1.5)}] 相位  max {s['max_tus_pha']:.3f}°"
              f"   (需 < 1.5，全频段)")
    print()
    print("  ---- 判据 B：与连续域比（物理对象，只看低频段）----")
    print(f"  [{ok(s['max_low_mag'] < 1.0)}] 幅值  max {s['max_low_mag']:.3f} dB"
          f"  (需 < 1.0，f ≤ {10*th.fc:.0f} Hz)")
    print(f"  [{ok(s['max_low_pha'] < 3.0)}] 相位  max {s['max_low_pha']:.3f}°"
          f"   (需 < 3.0，f ≤ {10*th.fc:.0f} Hz)")
    print(f"       全频段 vs 连续域：{s['max_dm']:.2f} dB / {s['max_dp']:.2f}°"
          f"（含翘曲，仅供参考）")
    print()
    print("  ---- 形状检查 ----")
    print(f"  [{ok(not s['any_positive'])}] 相位符号  全程为负（0° → −90°）")
    print(f"  [{ok(s['monotonic'])}] 相位单调  无上翘")
    print(f"  [{ok(s['fc_ok'])}] fc 判据   {s['fc_note']}")
    if s["slope"] != s["slope"]:
        print("  [SKIP] 高频斜率  频点数不足（需 ≥3 点超过 5·fc）")
    else:
        print(f"  [{ok(s['slope_ok'])}] 高频斜率  {s['slope']:.1f} dB/dec"
              f"  (需 −18 ~ −22)")
    print()
    print(f"  离散化翘曲最大 {s['warp']:.2f} dB（Tustin vs 连续域）")
    print()
    print(f"  ==== {'S6a PASS' if s['passed'] else 'S6a FAIL'} ====")

    if s["any_positive"]:
        print()
        print("  相位出现正值 → 复数约定实现反了。全项目只应在")
        print("  src/app/model/bode_data.py 实现 Z = S_c − j·S_s（D18）。")
    if s["max_tus_mag"] >= 0.5 and s["max_low_mag"] < 1.0:
        print()
        print("  低频对得上、高频对不上 → 查整周期取整与 θ 递推：")
        print("  高频每周期点数少，取整误差和相位步进误差被放大最快。")


# ---------------------------------------------------------------- 取数据
def run_fake(fc: float, isr: float, cyc: str = "N") -> tuple:
    """不连板子，用 sim 引擎跑一遍（验证本脚本的判据逻辑本身）。

    被测对象用 `plants.FirstOrderLP`——**与 S6a 固件的 Tustin 一阶低通
    同一套离散化**，所以 --fake 通过意味着"判据和解析解是对的"，
    真机 FAIL 就只可能是固件/接线的问题，不是脚本的问题。

    直接驱动 `SimWorker.run()` 而不经 `SweepController`：后者会把
    结果 emit 到 Qt 信号上，脚本里还得泵事件循环，没必要。
    """
    from app.sim.engine import SimWorker
    from app.model.sweep_config import SweepConfig
    from app.model.bode_data import MODE_PLANT, h_from_raw, mag_db, phase_deg

    cfg_obj = SweepConfig(isr_freq=isr, f_start=100.0, f_stop=isr / 5.0,
                          ppd=20, cyc_mode=1 if cyc == "N" else 0,
                          mode=MODE_PLANT, amp_pu=0.01, dut="first",
                          f0c=0.0, q=0.0, kp=0.0, ki=0.0)
    d = cfg_obj.to_sim_cfg()
    d["dut"] = "first"
    d["fc"] = fc
    d["comp"] = {"kp": 0.5, "ki": 1200.0}

    freqs_out, mags, phases = [], [], []

    w = SimWorker(d)

    def on_point(p):
        # RawPoint 只有累加器原值，mag/phase 必须经 h_from_raw 换算——
        # 那是全项目唯一的复数约定实现处（D18）。这里刻意不自己算，
        # 与真机路径保持同一份代码。
        h = h_from_raw(p, MODE_PLANT)
        freqs_out.append(p.f_act)
        mags.append(mag_db(h))
        phases.append(phase_deg(h))

    err = {}

    def on_fail(msg):
        err["m"] = msg

    w.pointReady.connect(on_point)
    w.failed.connect(on_fail)
    w.run()                      # 同步跑，不开线程

    if err:
        raise SystemExit(f"仿真失败：{err['m']}")
    return freqs_out, mags, phases


def run_serial(port: str, baud: int, isr=None) -> tuple:
    """连真板跑一次扫频。返回 (freqs, mags, phases, isr_freq_from_caps)。"""
    from PyQt5 import QtCore, QtWidgets
    from app.comm.controller import SerialLinkController
    from app.model.sweep_config import SweepConfig

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ctrl = SerialLinkController()
    if not ctrl.connect_port(port, baud):
        raise SystemExit(f"打不开串口 {port}")

    loop = QtCore.QEventLoop()
    done = {}

    def on_done(ok, msg):
        done["v"] = (ok, msg)
        QtCore.QTimer.singleShot(50, loop.quit)

    ctrl.sigDone.connect(on_done)

    # 等握手完成（CAPS 到达后 ctrl.caps 才有值）
    def start_when_ready(attempt=0):
        if ctrl.caps is None:
            if attempt > 40:
                print("  ✗ 握手超时：CAPS 未到达")
                print("    检查：波特率 / TX 是否发全 / 固件是否在跑")
                loop.quit()
                return
            QtCore.QTimer.singleShot(100, lambda: start_when_ready(attempt + 1))
            return
        c = ctrl.caps
        fs = float(getattr(c, "isr_freq", 0.0)) or 100000.0
        print(f"  握手成功  fw={c.fw_ver_str if hasattr(c,'fw_ver_str') else '-'}"
              f"  isr={fs:.0f} Hz")
        cfg = SweepConfig(isr_freq=fs, f_start=100.0, f_stop=fs / 5.0,
                          ppd=20, cyc_mode=1, mode=0, amp_pu=0.01,
                          dut="first", f0c=0.0, q=0.0, kp=0.0, ki=0.0)
        print(f"  开始扫频  {cfg.n_points} 点  预计 {cfg.estimate_seconds():.1f} s")
        done["isr"] = fs
        ctrl.start(cfg)

    QtCore.QTimer.singleShot(300, start_when_ready)
    QtCore.QTimer.singleShot(600000, loop.quit)     # 10 min 硬上限
    loop.exec_()

    if "v" not in done:
        raise SystemExit("扫频未完成（超时或未开始）")
    ok, msg = done["v"]
    if not ok:
        raise SystemExit(f"扫频失败：{msg}")

    res = ctrl.result
    fs = done.get("isr", isr or 100000.0)
    freqs = list(res.freq)
    mags = list(res.mag)
    phases = list(res.phase)
    ctrl.disconnect_port()
    return freqs, mags, phases, fs


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="S6a 上板自测：一阶低通 Bode vs 解析解")
    ap.add_argument("--port", help="串口，如 COM7")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--fc", type=float, default=1000.0,
                    help="被测一阶低通转折频率，须与固件 SELFTEST_FC_HZ 一致")
    ap.add_argument("--isr", type=float, default=100000.0,
                    help="ISR 频率（仅 --fake 用，真机从 CAPS 读）")
    ap.add_argument("--fake", action="store_true",
                    help="不连板子，用 sim 引擎跑（验证脚本判据本身）")
    ap.add_argument("--mag-tol", type=float, default=1.0)
    ap.add_argument("--pha-tol", type=float, default=3.0)
    args = ap.parse_args()

    print("=" * 80)
    print("  S6a · 上板自测（L2 gate）  一阶低通 vs 解析解")
    print("=" * 80)

    if args.fake:
        print("  模式：--fake（sim 引擎，不连板）")
        freqs, mags, phases = run_fake(args.fc, args.isr)
        fs = args.isr
    else:
        if not args.port:
            print("  错误：需要 --port COMx，或用 --fake 自检")
            return 2
        print(f"  模式：真板 {args.port} @ {args.baud}")
        freqs, mags, phases, fs = run_serial(args.port, args.baud, args.isr)

    th = Theory(fc=args.fc)
    rows, summary = build_report(freqs, mags, phases, th, fs,
                                 args.mag_tol, args.pha_tol,
                                 check_tustin=True)
    print_report(rows, summary, th, fs)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
