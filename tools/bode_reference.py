"""产出"真实 Bode 图"基准 —— 回答"我该看到什么形状"。

用户原话：

    首先我得知道我 mcu 内软件实现的二阶 LC，结合 mcu 性能下真实的
    伯德图长什么样子，然后拿扫频出来的和真实的对比，从而来判断可行性。

扫频本身只回答"测到了什么"。要判断"测得对不对"，必须有一份**独立于
测量过程**的基准。本脚本把三条曲线画在同一张图上，并导出 CSV：

============  =====================================================
基准线         含义
============  =====================================================
连续域解析解    物理对象本身  H(s) = K/(1 + s/(Q·ω0) + (s/ω0)²)
Tustin 离散     MCU 数字滤波器实际在算的  H(z)，z = e^{jωTs}
ZOH + 一拍延迟  真实数字环路（含零阶保持器与计算延迟）—— S6b 用
============  =====================================================

另外还叠一条**扫频仿真**（走 `sfra_sim` 的真实算法，含整周期取整、
settle、DFT 累加），这条代表"上位机此刻会画出来的东西"。它的偏差
就是从算法/量化来的，不是数学。

用法::

    # 默认：二阶 LC，f0=1300, Q=2, fs=100k，画图 + 存 CSV/PNG
    .venv\\Scripts\\python.exe tools\\bode_reference.py

    # 换成真实 Buck（Vin=12V，直流增益 +21.6dB）
    .venv\\Scripts\\python.exe tools\\bode_reference.py --vin 12

    # 一阶低通（S6a 那个被测对象）
    .venv\\Scripts\\python.exe tools\\bode_reference.py --kind lp1 --fc 1000

    # 只打印数值表，不出图（CI / 快速核对）
    .venv\\Scripts\\python.exe tools\\bode_reference.py --no-plot

关键判据（脚本会自动检查并打印）
--------------------------------
1. 离散域与连续域在 f ≤ f0 处的偏差应 < 0.1 dB / 1°  —— Tustin 在
   低频几乎无翘曲，对不上就是系数算错。
2. 离散域与连续域在 f_s/5 处的偏差应是**负的幅值**（幅值偏低），
   这来自频率翘曲，不是 bug。脚本把最大偏差与所在频点打印出来。
3. 直流增益必须等于 K（归一化形式 0 dB，Vin 形式 20log10(Vin) dB）。
4. f0 处离散域幅值应为 K·Q（±0.1 dB），相位应为 −90°（±1°）。
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np                                       # noqa: E402

from app.sim import theory                               # noqa: E402
from app.sim.theory import (AnalogLC, AnalogLP1, TustinLC,  # noqa: E402
                            TustinLP1, ZohPlant)


# ---------------------------------------------------------------- 频率网格
def make_grid(f0: float, fs: float, ppd: int = 40) -> np.ndarray:
    """比扫频用的 ppd=20 更密，才看得见谐振峰的形状。

    末尾加一点 f_s/2 附近的点，把 Tustin 的零点显出来（f_s/2 处
    二阶 LC 的离散幅值理论上为 −inf）。
    """
    f_lo = max(f0 / 1000.0, 1.0)
    f_hi = min(fs / 2.0 * 0.98, f0 * 200.0)
    n = int(math.log10(f_hi / f_lo) * ppd) + 1
    base = np.logspace(math.log10(f_lo), math.log10(f_hi), max(n, 40))
    extra = np.asarray([fs / 5.0, fs / 4.0, fs / 3.0, fs * 0.45])
    return np.unique(np.concatenate([base, extra]))


# ---------------------------------------------------------------- 扫频仿真
def run_sweep(freqs, kind: str, params: dict, fs: float,
              amp: float = 0.01) -> tuple:
    """用**真实的扫频算法**跑一遍，返回 (f_act, mag, phase)。

    刻意走 `SimWorker.run()` 而不是直接算解析解：这里要的是
    "上位机画出来的曲线"，包含整周期取整、settle 时间、DFT 累加、
    float32 上报等全部环节。
    """
    from app.model.bode_data import MODE_PLANT, h_from_raw, mag_db, phase_deg
    from app.sim.engine import SimWorker

    if kind == "lp1":
        cfg = {"dut": "first", "fc": params["fc"]}
    else:
        cfg = {"dut": "lc", "f0": params["f0"], "q": params["q"]}

    d = {"isr_freq": fs, "mode": MODE_PLANT, "amp_pu": amp, "cyc": 1,
         "freqs": np.asarray(freqs, dtype=float), "ref_dc": 0.5,
         "comp": {"kp": 0.5, "ki": 1200.0}}
    d.update(cfg)

    out_f, out_m, out_p = [], [], []

    def on_point(p):
        h = h_from_raw(p, MODE_PLANT)
        out_f.append(p.f_act)
        out_m.append(mag_db(h))
        out_p.append(phase_deg(h))

    err = {}
    w = SimWorker(d)
    w.pointReady.connect(on_point)
    w.failed.connect(lambda m: err.setdefault("m", m))
    w.run()
    if err:
        raise RuntimeError(f"仿真失败：{err['m']}")
    return np.asarray(out_f), np.asarray(out_m), np.asarray(out_p)


# ---------------------------------------------------------------- 判据检查
def check(a_an, a_tu, freqs, q: float, f0: float, dc_db_expected: float,
          fs: float, kind: str = "lc") -> list[tuple[str, bool, str]]:
    """四条自洽判据。返回 [(名称, 是否通过, 说明)]。

    `kind="lp1"` 时第 4 条换成"fc 处 −3 dB / −45°"——一阶低通的
    特征点是 fc，而二阶 LC 是 f0 处的 K·Q 凸峰与 −90°。两者不能共用。
    """
    res = []

    # 1. 低频段两域一致
    low = [f for f in freqs if f <= f0]
    dm = max((abs(a_tu.mag_db(f) - a_an.mag_db(f)) for f in low), default=0.0)
    dp = max((abs(a_tu.phase_deg(f) - a_an.phase_deg(f)) for f in low),
             default=0.0)
    res.append(("低频两域一致（f ≤ f_target）",
                dm < 0.10 and dp < 1.0,
                f"max Δ{dm:.3f} dB / Δ{dp:.3f}°"))

    # 2. f_s/5 处偏差应为负（幅值偏低）
    f_lim = fs / 5.0
    d_lim = a_tu.mag_db(f_lim) - a_an.mag_db(f_lim)
    res.append((f"f_s/5 处翘曲为负（{f_lim:.0f} Hz）",
                d_lim < 0.0,
                f"Δ{d_lim:+.3f} dB（离散域偏低，来自双线性频率翘曲）"))

    # 3. 直流增益
    dc = a_an.mag_db(1e-6)
    res.append(("直流增益",
                abs(dc - dc_db_expected) < 0.01,
                f"{dc:+.3f} dB（期望 {dc_db_expected:+.3f} dB）"))

    # 4. 特征点
    if kind == "lp1":
        m0, p0 = a_tu.mag_db(f0), a_tu.phase_deg(f0)
        m_exp = dc_db_expected - 3.0103
        res.append(("fc 处幅相（离散域）",
                    abs(m0 - m_exp) < 0.10 and abs(p0 + 45.0) < 1.0,
                    f"{m0:+.3f} dB / {p0:+.3f}°"
                    f"（期望 {m_exp:+.3f} dB / −45°）"))
    else:
        m0, p0 = a_tu.mag_db(f0), a_tu.phase_deg(f0)
        m_exp = dc_db_expected + 20.0 * math.log10(q)
        res.append(("f0 处幅相（离散域）",
                    abs(m0 - m_exp) < 0.10 and abs(p0 + 90.0) < 1.0,
                    f"{m0:+.3f} dB / {p0:+.3f}°"
                    f"（期望 {m_exp:+.3f} dB / −90°）"))
    return res


# ---------------------------------------------------------------- 输出
def write_csv(path: Path, rows) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["f_Hz",
                    "analog_mag_dB", "analog_phase_deg",
                    "tustin_mag_dB", "tustin_phase_deg",
                    "zoh_mag_dB", "zoh_phase_deg",
                    "warp_dB", "warp_deg"])
        for r in rows:
            w.writerow([f"{r[0]:.6g}"] + [f"{v:.6f}" if math.isfinite(v)
                                          else "nan" for v in r[1:]])


def print_table(rows, limit: int = 200) -> None:
    print()
    print(f"  {'f (Hz)':>10} {'连续域 dB':>10} {'°':>9} "
          f"{'Tustin dB':>10} {'°':>9} {'ΔdB':>8} {'Δ°':>7}")
    print("  " + "-" * 72)
    step = max(1, len(rows) // limit)
    for i, r in enumerate(rows):
        if i % step and i != len(rows) - 1:
            continue
        print(f"  {r[0]:10.1f} {r[1]:10.3f} {r[2]:9.2f} "
              f"{r[3]:10.3f} {r[4]:9.2f} "
              f"{r[3] - r[1]:+8.3f} {r[4] - r[2]:+7.2f}")
    print(f"  （共 {len(rows)} 行，此处隔行显示）" if step > 1 else "")


def plot(path: Path, rows, title: str, f0: float, fs: float,
         sweep=None) -> bool:
    """用 pyqtgraph 离屏出图（项目没装 matplotlib，风格也与上位机一致）。"""
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        import pyqtgraph as pg
        from app import theme
    except Exception as e:                              # noqa: BLE001
        print(f"  跳过出图（{e}）")
        return False

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pg.setConfigOptions(antialias=True, background="w", foreground=theme.T2)

    win = QtWidgets.QWidget()
    win.resize(1200, 900)
    lay = QtWidgets.QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    glw = pg.GraphicsLayoutWidget()
    lay.addWidget(glw)

    pm = glw.addPlot(row=0, col=0)
    pp = glw.addPlot(row=1, col=0)
    pp.setXLink(pm)

    for p in (pm, pp):
        p.showGrid(x=True, y=True, alpha=0.18)
        p.setLogMode(x=True, y=False)
        p.addLegend(offset=(-10, 10))
        p.getAxis("left").setTextPen(theme.T2)
        p.getAxis("bottom").setTextPen(theme.T2)

    pm.setLabel("left", "|H| (dB)")
    pp.setLabel("left", "∠H (°)")
    pp.setLabel("bottom", "频率 (Hz)")

    f = np.asarray([r[0] for r in rows], dtype=float)
    series = [
        ("连续域解析解 H(s)", theme.BLUE, [r[1] for r in rows],
         [r[2] for r in rows], 2.4, None),
        ("Tustin 离散 H(z)（MCU 实际）", theme.AMBER, [r[3] for r in rows],
         [r[4] for r in rows], 2.0, QtCore.Qt.DashLine),
        ("ZOH + 一拍延迟（真实环路）", theme.PURPLE, [r[5] for r in rows],
         [r[6] for r in rows], 1.6, QtCore.Qt.DotLine),
    ]
    for name, color, mag, pha, width, style in series:
        pen = pg.mkPen(color, width=width)
        if style is not None:
            pen.setStyle(style)
        pm.plot(f, np.asarray(mag, dtype=float), pen=pen, name=name)
        pp.plot(f, np.asarray(pha, dtype=float), pen=pen, name=None)

    if sweep is not None:
        sf, sm, sp = sweep
        pen = pg.mkPen(theme.GREEN, width=0)
        pm.plot(sf, sm, pen=None, symbol="o", symbolSize=5,
                symbolBrush=theme.GREEN, symbolPen=pg.mkPen(None),
                name="扫频仿真（上位机会画这条）")
        pp.plot(sf, sp, pen=None, symbol="o", symbolSize=5,
                symbolBrush=theme.GREEN, symbolPen=pg.mkPen(None))

    # 参考线：0 dB、f0、f_s/5
    for p in (pm, pp):
        p.addItem(pg.InfiniteLine(pos=f0, angle=90,
                                  pen=pg.mkPen(theme.RED, width=1,
                                               style=QtCore.Qt.DashLine)))
        p.addItem(pg.InfiniteLine(pos=fs / 5.0, angle=90,
                                  pen=pg.mkPen(theme.GREY_CURVE, width=1,
                                               style=QtCore.Qt.DotLine)))
    pm.addItem(pg.InfiniteLine(pos=0.0, angle=0,
                               pen=pg.mkPen(theme.GREY_CURVE, width=1)))
    pp.addItem(pg.InfiniteLine(pos=-90.0, angle=0,
                               pen=pg.mkPen(theme.GREY_CURVE, width=1)))
    pp.addItem(pg.InfiniteLine(pos=-180.0, angle=0,
                               pen=pg.mkPen(theme.GREY_CURVE, width=1)))

    tt = pg.LabelItem(justify="left")
    glw.addItem(tt, row=2, col=0)
    tt.setText(f'<span style="color:{theme.T1};font-size:13px;'
               f'font-weight:600">{title}</span>'
               f'<br><span style="color:{theme.T3};font-size:11px">'
               f'红虚线 = f0 {f0:.0f} Hz　灰点线 = f_s/5 '
               f'{fs/5:.0f} Hz　绿点 = 扫频仿真</span>')

    win.show()
    app.processEvents()
    pix = glw.grab()
    ok = bool(pix.save(str(path)))
    win.close()
    return ok


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="产出真实 Bode 基准（连续域 / Tustin / ZOH+延迟 / 扫频仿真）")
    ap.add_argument("--kind", choices=("lc", "lp1"), default="lc",
                    help="lc=二阶 LC（默认），lp1=一阶低通")
    ap.add_argument("--f0", type=float, default=1300.0, help="谐振/转折频率 Hz")
    ap.add_argument("--fc", type=float, default=1000.0, help="一阶低通 fc")
    ap.add_argument("--q", type=float, default=2.0, help="品质因数")
    ap.add_argument("--fs", type=float, default=100_000.0, help="ISR 频率 Hz")
    ap.add_argument("--vin", type=float, default=None,
                    help="给定时 K=Vin（真实 Buck 直流增益 = Vin）")
    ap.add_argument("--ppd", type=int, default=40, help="基准网格每 decade 点数")
    ap.add_argument("--out", default="tools/_ref_out", help="输出目录")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--no-sweep", action="store_true", help="跳过扫频仿真")
    args = ap.parse_args()

    fs = args.fs
    K = float(args.vin) if args.vin else 1.0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("  真实 Bode 基准  ·  连续域 / Tustin / ZOH+延迟 / 扫频仿真")
    print("=" * 78)

    if args.kind == "lc":
        f0 = args.f0
        freqs = make_grid(f0, fs, args.ppd)
        a_an = AnalogLC(f0=f0, Q=args.q, K=K)
        a_tu = TustinLC(f0=f0, Q=args.q, fs=fs, K=K)
        zoh = ZohPlant(f0=f0, Q=args.q, fs=fs, K=K, delay=1)
        params = {"f0": f0, "q": args.q}
        dc_exp = 20.0 * math.log10(K)
        title = (f"二阶 LC  f0={f0:.0f} Hz  Q={args.q:g}  "
                 f"K={K:g}" + (f" (=Vin {K:g} V)" if args.vin else " (归一化)")
                 + f"  fs={fs/1000:.0f} kHz")
    else:
        f0 = args.fc
        freqs = make_grid(f0, fs, args.ppd)
        a_an = AnalogLP1(fc=f0, gain=K)
        a_tu = TustinLP1(fc=f0, fs=fs, gain=K)
        zoh = None
        params = {"fc": f0}
        dc_exp = 20.0 * math.log10(K)
        title = (f"一阶低通  fc={f0:.0f} Hz  K={K:g}  fs={fs/1000:.0f} kHz")

    rows = []
    for f in freqs:
        h_an = a_an.h(float(f))
        h_tu = a_tu.h(float(f))
        m_an = theory._db(abs(h_an))
        m_tu = theory._db(abs(h_tu))
        p_an = math.degrees(math.atan2(h_an.imag, h_an.real))
        p_tu = math.degrees(math.atan2(h_tu.imag, h_tu.real))
        if zoh is not None:
            h_z = zoh.h(float(f))
            m_z = theory._db(abs(h_z))
            p_z = math.degrees(math.atan2(h_z.imag, h_z.real))
        else:
            m_z, p_z = float("nan"), float("nan")
        rows.append((float(f), m_an, p_an, m_tu, p_tu, m_z, p_z,
                     m_tu - m_an, p_tu - p_an))

    csv_path = out / "bode_reference.csv"
    write_csv(csv_path, rows)
    print(f"\n  CSV → {csv_path}")

    print_table(rows)

    print("\n  ---- 自洽判据 ----")
    ok_all = True
    checks = check(a_an, a_tu, list(freqs), args.q, f0, dc_exp, fs,
                   args.kind)
    for name, ok, note in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<26} {note}")
        ok_all = ok_all and ok

    # 扫频仿真
    sweep = None
    if not args.no_sweep:
        from app.model.sweep_config import make_freqs
        sf = make_freqs(max(f0 / 20.0, 10.0), fs / 5.0, 20, fs)
        print(f"\n  ---- 扫频仿真（{len(sf)} 点，ppd=20，PLANT 模式）----")
        try:
            sf_a, sm, sp = run_sweep(sf, args.kind, params, fs)
            sweep = (sf_a, sm, sp)
            d_m, d_p = [], []
            for ff, mm, pp2 in zip(sf_a, sm, sp):
                h_an = a_an.h(float(ff))
                if abs(h_an) <= 0:
                    continue
                d_m.append(mm - theory._db(abs(h_an)))
                d_p.append(pp2 - math.degrees(math.atan2(h_an.imag,
                                                         h_an.real)))
            d_m = np.asarray(d_m)
            d_p = np.asarray(d_p)
            print(f"  vs 连续域：max {np.nanmax(np.abs(d_m)):.3f} dB / "
                  f"{np.nanmax(np.abs(d_p)):.3f}°"
                  f"（含翘曲；低频段应 < 0.1 dB）")
            lowmask = sf_a <= f0
            if lowmask.any():
                print(f"  f ≤ f0 段：max {np.nanmax(np.abs(d_m[lowmask])):.3f} dB"
                      f" / {np.nanmax(np.abs(d_p[lowmask])):.3f}°")
        except Exception as e:                         # noqa: BLE001
            print(f"  扫频仿真失败：{e}")
            ok_all = False

    if not args.no_plot:
        png = out / "bode_reference.png"
        if plot(png, rows, title, f0, fs, sweep):
            print(f"\n  PNG → {png}")

    print()
    print("=" * 78)
    print(f"  {'全部判据通过' if ok_all else '有判据未通过，见上'}")
    print("=" * 78)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
