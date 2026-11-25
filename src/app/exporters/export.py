"""SFRA Studio - 导出（CSV / JSON / PNG / Markdown 报告）。

CSV 列名刻意对齐 TI SFRA GUI，方便直接拷进 Spreadsheet 或与 TI 结果对比：

    Freq | Mag(dB) | Phase(deg) | MagLin | Real | Imag

TI 的 "Phase" 是 -180~180 的原始值，这里也一样，另外多给一列 unwrapped 相位。
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import numpy as np

from ..model.analysis import analyze, unwrap_phase
from ..model.bode_data import SweepResult


# ---------------------------------------------------------------- CSV
def to_csv(res: SweepResult, path: str, include_unwrapped: bool = True) -> str:
    f = res.freq
    h = res.h
    mag = res.mag
    ph = res.phase
    ph_u = unwrap_phase(ph)

    lines = []
    head = "Freq(Hz),Mag(dB),Phase(deg),MagLin,Real,Imag,Saturated"
    if include_unwrapped:
        head += ",PhaseUnwrapped(deg)"
    lines.append(head)

    # 饱和列（W2）：让下游（Excel/Python 分析）能自己决定要不要排除。
    # 只写 1/0 而不是文字，方便直接当数值列筛选用。
    sat = res.sat_mask() if hasattr(res, "sat_mask") else None

    for i in range(len(f)):
        row = [f"{f[i]:.4f}",
               f"{mag[i]:.6f}",
               f"{ph[i]:.6f}",
               f"{abs(h[i]):.8f}",
               f"{h[i].real:.8f}",
               f"{h[i].imag:.8f}"]
        row.append("1" if (sat is not None and sat[i]) else "0")
        if include_unwrapped:
            row.append(f"{ph_u[i]:.6f}")
        lines.append(",".join(row))

    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def to_ti_csv(res: SweepResult, path: str) -> str:
    """TI Compensation Designer 风格的 4 列（Freq, Mag, Phase, MagLinear）。"""
    lines = ["Freq(Hz),Gain(dB),Phase(deg),Gain(linear)"]
    f, mag, ph, h = res.freq, res.mag, res.phase, res.h
    for i in range(len(f)):
        lines.append(f"{f[i]:.4f},{mag[i]:.6f},{ph[i]:.6f},{abs(h[i]):.8f}")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- JSON
def to_json(res: SweepResult, path: str,
            extra: Optional[dict] = None) -> str:
    """完整会话：原始累加器 + 派生曲线 + 判据。

    存原始累加器是为了"以后发现相位公式错了，能重算而不用重扫"（02-2.5）。
    """
    st = analyze(res.freq, res.mag, res.phase)
    satm = res.sat_mask() if hasattr(res, "sat_mask") else None
    payload = {
        "format": "sfra-studio/session",
        "version": 1,
        "created": res.timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": res.mode,
        "mode_name": res.mode_name,
        "amplitude_pu": res.amplitude_pu,
        "isr_freq": res.isr_freq,
        "notes": list(res.notes),
        "stability": {
            "fc": st.fc, "pm": st.pm, "f180": st.f180, "gm": st.gm,
            "pm_note": st.pm_note, "gm_note": st.gm_note,
        },
        "points": [
            {
                "f_act": p.f_act,
                "y_c": p.a_c, "y_s": p.a_s,
                "b_c": p.b_c, "b_s": p.b_s,
                "mag_db": float(res.mag[i]),
                "phase_deg": float(res.phase[i]),
                "phase_unwrapped": float(unwrap_phase(res.phase)[i]),
                # W2：逐点饱和标记必须一起存。不存的话，从 JSON 读回的
                # 历史会话会"忘记"哪些点曾被限幅污染，重新算出的 PM/GM
                # 会把不可信点算进去——而用户看到的只是"数值和上次不一样"。
                "saturated": bool(satm[i]) if satm is not None else False,
            }
            for i, p in enumerate(res.raw)
        ],
    }
    if extra:
        payload.update(extra)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def from_json(path: str) -> SweepResult:
    """读回会话（UART 没接也能重看历史数据）。"""
    from ..model.bode_data import RawPoint

    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)

    res = SweepResult(
        mode=int(d.get("mode", 1)),
        amplitude_pu=float(d.get("amplitude_pu", 0.01)),
        isr_freq=float(d.get("isr_freq", 100_000.0)),
        timestamp=d.get("created", ""),
        notes=list(d.get("notes", [])),
    )
    pts = d.get("points", [])
    for p in pts:
        res.add(RawPoint(float(p["f_act"]), float(p["y_c"]), float(p["y_s"]),
                         float(p["b_c"]), float(p["b_s"])),
                saturated=bool(p.get("saturated", False)))
    return res


# ---------------------------------------------------------------- Markdown 报告
def to_markdown(res: SweepResult, path: str,
                meta: Optional[dict] = None,
                curves: Optional[list] = None) -> str:
    """测试报告（可直接贴进周报/评审文档）。

    meta    : 拓扑、工作点、被测环路等
    curves  : [(名字, SweepResult), ...] 多条曲线时一起汇总
    """
    st = analyze(res.freq, res.mag, res.phase)
    now = res.timestamp or time.strftime("%Y-%m-%d %H:%M:%S")

    def fmt(v, unit="", dec=2):
        return "不可信" if v is None else f"{v:.{dec}f}{unit}"

    L = []
    L.append(f"# SFRA 测试报告\n")
    L.append(f"- 时间：{now}")
    L.append(f"- 模式：**{res.mode_name}**")
    L.append(f"- ISR 频率：{res.isr_freq/1000:.1f} kHz")
    L.append(f"- 注入幅值：{res.amplitude_pu*100:.2f} % pu")
    L.append(f"- 频点：{len(res)} 个，"
             f"{res.freq.min():.1f} Hz ~ {res.freq.max()/1000:.2f} kHz\n")

    if meta:
        L.append("## 工作点\n")
        L.append("| 项 | 值 |")
        L.append("| --- | --- |")
        for k, v in meta.items():
            L.append(f"| {k} | {v} |")
        L.append("")

    L.append("## 稳定性判据\n")
    L.append("| 指标 | 值 | 判据 | 结论 |")
    L.append("| --- | --- | --- | --- |")
    pm_c = "✅ 通过" if st.pm_ok else ("⚠️ 偏低" if st.pm is not None else "—")
    gm_c = "✅ 通过" if st.gm_ok else ("⚠️ 偏低" if st.gm is not None else "—")
    L.append(f"| 相位裕度 PM | {fmt(st.pm, '°', 1)} | > 45° | {pm_c} |")
    L.append(f"| 增益裕度 GM | {fmt(st.gm, ' dB', 1)} | > 6 dB | {gm_c} |")
    L.append(f"| 穿越频率 fc | "
             f"{'不可信' if st.fc is None else f'{st.fc:.1f} Hz'} | — | — |")
    L.append(f"| −180° 频率 | "
             f"{'不可信' if st.f180 is None else f'{st.f180:.1f} Hz'} | — | — |")
    L.append("")

    msgs = [m for m in (st.pm_note, st.gm_note) if m]
    msgs += list(res.notes)
    if res.invalid_mask().any():
        msgs.append(f"{int(res.invalid_mask().sum())} 个频点每周期采样 < 2，已剔除")
    if res.warn_mask().any():
        msgs.append(f"{int(res.warn_mask().sum())} 个频点每周期采样 < 4，数据可信度低")
    if msgs:
        L.append("## 告警\n")
        for m in msgs:
            L.append(f"- {m}")
        L.append("")

    if curves and len(curves) > 1:
        L.append("## 曲线对比\n")
        L.append("| 曲线 | PM | GM | fc | 点数 |")
        L.append("| --- | --- | --- | --- | --- |")
        for name, r in curves:
            s = analyze(r.freq, r.mag, r.phase)
            L.append(f"| {name} | {fmt(s.pm, '°', 1)} | {fmt(s.gm, ' dB', 1)} "
                     f"| {'—' if s.fc is None else f'{s.fc:.0f} Hz'} | {len(r)} |")
        L.append("")

    L.append("## 数据\n")
    L.append("| Freq (Hz) | Mag (dB) | Phase (°) |")
    L.append("| --- | --- | --- |")
    for i in range(len(res)):
        L.append(f"| {res.freq[i]:.1f} | {res.mag[i]:.2f} | {res.phase[i]:.1f} |")
    L.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return path


# ---------------------------------------------------------------- 工具
def default_name(prefix: str, ext: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"


def ensure_ext(path: str, ext: str) -> str:
    root, e = os.path.splitext(path)
    if e.lower() != "." + ext.lower():
        return path + "." + ext
    return path
