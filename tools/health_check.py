"""对已烧录的固件做一次完整体检（真串口）。

用法：
    .venv\\Scripts\\python.exe tools/health_check.py [COM口]

做的事：
    连接 → 握手(PING/CAPS/CONDITIONS) → 一次完整扫频
    → 数据完整性检查 → 与二阶 Sallen-Key 的 Tustin 解析解比对

已知事实（用于比对的基准）：
    固件 v1.0.1，Fs(TIM7) = 10 kHz，max_vec_len = 256
    软件 DUT = 二阶 Sallen-Key，fc = 1 kHz，Q = 1/3
"""
from __future__ import annotations

import math
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PyQt5 import QtWidgets  # noqa: E402

#: DUT 理论参数，与 PV-charger 的 sfra_dut.c 一致
DUT_FC_HZ = 1000.0
DUT_Q = 1.0 / 3.0


def theory_2nd_order(f: float, fs: float, fc: float = DUT_FC_HZ,
                     q: float = DUT_Q):
    """二阶低通的 Tustin 离散化频率响应。

    固件里用的是双线性变换（Tustin），所以这里必须也用同一套离散化，
    否则在接近 fs/10 的地方会看到"看起来是 bug 的偏差"，其实是
    双线性变换的频率翘曲（warping）。s → (2/T)·tan(wT/2)。
    """
    wc = 2.0 * math.pi * fc
    w = 2.0 * math.pi * f
    s = 2.0 * fs * 1j * math.tan(w / (2.0 * fs))
    h = (wc * wc) / (s * s + (s * wc / q) + (wc * wc))
    mag_db = 20.0 * math.log10(max(abs(h), 1e-30))
    phase = math.degrees(math.atan2(h.imag, h.real))
    return mag_db, phase


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM14"
    app = QtWidgets.QApplication(sys.argv[:1])

    from app.comm.controller import LinkController
    from app.comm.link import SerialLink
    from app.model.sweep_config import SweepConfig

    link = SerialLink()
    ctrl = LinkController(link)

    events = []
    link.errorText.connect(lambda s: events.append(("ERROR", s)))
    link.logText.connect(lambda s: events.append(("LOG", s)))

    print(f"=== 连接 {port} @ 921600 ===")
    t_conn = time.time()
    if not link.open(port, 921600):
        print("!! 连接失败")
        return 1
    print("已连接")

    # ---- 握手 ----
    got = {"caps": False, "cond": False}
    link.capsReceived.connect(lambda c: got.__setitem__("caps", True))
    link.conditionsReceived.connect(lambda d: got.__setitem__("cond", True))
    ctrl.handshake()

    deadline = time.time() + 3.0
    while time.time() < deadline and not (got["caps"] and got["cond"]):
        app.processEvents()
        time.sleep(0.01)
    t_hs = (time.time() - t_conn) * 1000

    print(f"\n=== 握手（{t_hs:.0f} ms）===")
    caps = ctrl.caps
    if caps is None:
        print("  !! CAPS 未收到")
        link.close()
        return 1
    print(f"  固件版本 : {caps.version_str}")
    print(f"  ISR 频率 : {caps.isr_freq:.1f} Hz")
    print(f"  最大点数 : {caps.max_vec_len}")
    print(f"  默认幅值 : {caps.default_amp * 100:.3f} %pu")

    cond = ctrl.conditions
    if cond:
        print("  运行条件 :")
        for k, v in cond.items():
            print(f"    {k:12s} = {v:12.4f}" if isinstance(v, float)
                  else f"    {k:12s} = {v}")
    else:
        print("  !! CONDITIONS 未收到")

    # ---- 扫频 ----
    fs = float(caps.isr_freq)
    print(f"\n=== 扫频 PLANT（10 Hz ~ 5 kHz，21 点/dec，Fs={fs:.0f} Hz）===")
    print("  模式：mode=0 PLANT 则 H=Y/X；mode=1 LOOP 则 GH=Y/(Inj-Y)")
    print("  S6a 阶段 DUT 是开环软件模型，必须用 PLANT。")
    cfg = SweepConfig(
        isr_freq=fs, f_start=10.0, f_stop=5000.0, ppd=21,
        cyc_mode=0, mode=0, amp_pu=0.01)

    fin = {"ok": None, "msg": ""}
    ctrl.sigDone.connect(lambda ok, msg: fin.update(ok=ok, msg=msg))

    t0 = time.time()
    if not ctrl.start(cfg):
        print("  !! start() 返回 False")
        link.close()
        return 1

    deadline = time.time() + 40.0
    while time.time() < deadline and fin["ok"] is None:
        app.processEvents()
        time.sleep(0.005)
    el = time.time() - t0

    print(f"  结果     : ok={fin['ok']}  msg={fin['msg']!r}")
    print(f"  耗时     : {el:.2f} s")

    res = ctrl.result
    n = len(res) if res is not None else 0
    print(f"  收到点数 : {n} / {cfg.n_points}")

    if n < 2:
        print("\n!! 点数不足，无法分析")
        link.close()
        return 1

    freq = res.freq
    mag = res.mag
    phase = res.phase

    # ---- 数据完整性 ----
    print("\n=== 数据完整性 ===")
    bad_f = int((freq[1:] <= freq[:-1]).sum())
    print(f"  频率严格递增 : {'是' if bad_f == 0 else f'否，{bad_f} 处逆序'}")
    print(f"  频率范围     : {freq[0]:.1f} ~ {freq[-1]:.1f} Hz")
    print(f"  幅值范围     : {mag.min():.2f} ~ {mag.max():.2f} dB")
    print(f"  相位范围     : {phase.min():.1f} ~ {phase.max():.1f} °")

    sat = getattr(res, "sat_mask", None)
    if sat is not None:
        nsat = int(sum(bool(x) for x in sat()))
        print(f"  饱和点       : {nsat} / {n}")

    # ---- 与理论比对 ----
    print("\n=== 与理论值比对（二阶 Sallen-Key, fc=1kHz, Q=1/3, Tustin）===")
    hdr = (f"  {'频率Hz':>9s} {'实测dB':>8s} {'理论dB':>8s} {'差dB':>7s}"
           f" {'实测°':>8s} {'理论°':>8s} {'差°':>7s}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    max_db = max_ph = 0.0
    max_db_f = max_ph_f = 0.0
    for i in range(n):
        f = float(freq[i])
        m = float(mag[i])
        p = float(phase[i])
        tdb, tph = theory_2nd_order(f, fs)
        ddb = m - tdb
        dph = ((p - tph + 180.0) % 360.0) - 180.0
        print(f"  {f:9.1f} {m:8.3f} {tdb:8.3f} {ddb:7.3f}"
              f" {p:8.2f} {tph:8.2f} {dph:7.2f}")
        # f <= fs/10 才算"可信区间"，更高频点受双线性翘曲与
        # 每周期采样点太少双重影响，不作为判据
        if f <= fs / 10.0:
            if abs(ddb) > max_db:
                max_db, max_db_f = abs(ddb), f
            if abs(dph) > max_ph:
                max_ph, max_ph_f = abs(dph), f

    print(f"\n  f <= Fs/10（{fs/10:.0f} Hz）区间：")
    print(f"    最大幅值偏差 {max_db:.3f} dB @ {max_db_f:.1f} Hz")
    print(f"    最大相位偏差 {max_ph:.3f} °  @ {max_ph_f:.1f} Hz")

    # ---- 判据 ----
    print("\n=== 判据 ===")
    ok_db = max_db <= 0.5
    ok_ph = max_ph <= 2.0
    print(f"  幅值偏差 <= 0.5 dB : {'通过' if ok_db else '不通过'}"
          f"（实测 {max_db:.3f}）")
    print(f"  相位偏差 <= 2.0 °  : {'通过' if ok_ph else '不通过'}"
          f"（实测 {max_ph:.3f}）")

    errs = [m for t, m in events if t == "ERROR"]
    if errs:
        print("\n=== 错误事件 ===")
        for m in errs:
            print("  ", m)

    link.close()
    print("\n=== 完 ===")
    return 0 if (ok_db and ok_ph) else 2


if __name__ == "__main__":
    sys.exit(main())
