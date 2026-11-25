"""导出功能测试。"""
import json
import os

import numpy as np
import pytest

from app.exporters import export
from app.model.bode_data import MODE_LOOP, MODE_PLANT, RawPoint, SweepResult


def _result(mode=MODE_LOOP, n=10) -> SweepResult:
    """造一份可用的结果（用已知的 H = 1∠-45° 便于校验导出内容）。"""
    res = SweepResult(mode=mode, amplitude_pu=0.01, isr_freq=100_000.0,
                      timestamp="2026-01-01 12:00:00")
    for i in range(n):
        f = 100.0 * (2 ** i)
        # GH = 2∠-60°
        gh = 2.0 * np.exp(-1j * np.radians(60))
        inj = complex(1.0, 0.0)
        y = gh / (1 + gh)
        k = 1000 / 2.0
        res.add(RawPoint(f,
                         k * abs(y) * np.cos(np.angle(y)),
                         -k * abs(y) * np.sin(np.angle(y)),
                         k, 0.0))
    return res


def test_csv_header(tmp_path):
    p = tmp_path / "a.csv"
    export.to_csv(_result(), str(p))
    lines = p.read_text(encoding="utf-8-sig").strip().split("\n")
    assert lines[0].startswith("Freq(Hz),Mag(dB),Phase(deg)")
    assert len(lines) == 11          # 表头 + 10 行


def test_csv_values(tmp_path):
    p = tmp_path / "b.csv"
    export.to_csv(_result(), str(p))
    lines = p.read_text(encoding="utf-8-sig").strip().split("\n")
    cols = lines[1].split(",")
    assert float(cols[0]) == pytest.approx(100.0)
    assert float(cols[1]) == pytest.approx(20 * np.log10(2.0), abs=0.05)
    assert float(cols[2]) == pytest.approx(-60.0, abs=0.05)


# ------------------------------------------------------------------ 饱和列
def test_csv_has_saturated_column(tmp_path):
    """CSV 必须带 Saturated 列（W2），下游才能自己决定要不要排除。"""
    p = tmp_path / "sat.csv"
    export.to_csv(_result(), str(p))
    head = p.read_text(encoding="utf-8-sig").strip().split("\n")[0]
    assert "Saturated" in head


def test_csv_marks_saturated_rows(tmp_path):
    """饱和点标 1，其余标 0。"""
    from app.model.bode_data import MODE_PLANT, RawPoint, SweepResult

    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    for i in range(4):
        res.add(RawPoint(100.0 * (i + 1), 2.0, 0.0, 1.0, 0.0),
                saturated=(i == 2))
    p = tmp_path / "sat2.csv"
    export.to_csv(res, str(p))
    lines = p.read_text(encoding="utf-8-sig").strip().split("\n")
    cols = lines[0].split(",")
    j = cols.index("Saturated")
    vals = [lines[k].split(",")[j] for k in range(1, len(lines))]
    assert vals == ["0", "0", "1", "0"]


def test_ti_csv_four_columns(tmp_path):
    p = tmp_path / "c.csv"
    export.to_ti_csv(_result(), str(p))
    lines = p.read_text(encoding="utf-8-sig").strip().split("\n")
    assert lines[0].count(",") == 3


def test_json_roundtrip(tmp_path):
    p = tmp_path / "d.json"
    res = _result()
    export.to_json(res, str(p), extra={"meta": {"拓扑": "Buck"}})

    back = export.from_json(str(p))
    assert back.mode == res.mode
    assert back.isr_freq == pytest.approx(res.isr_freq)
    assert len(back) == len(res)
    np.testing.assert_allclose(back.freq, res.freq)
    np.testing.assert_allclose(back.mag, res.mag, atol=1e-4)
    np.testing.assert_allclose(back.phase, res.phase, atol=1e-4)


def test_json_keeps_raw_accumulators(tmp_path):
    """原始累加器必须存下来——以后发现公式错了能重算，不用重扫。"""
    p = tmp_path / "e.json"
    export.to_json(_result(), str(p))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert "points" in d
    assert set(d["points"][0]) >= {"f_act", "y_c", "y_s", "b_c", "b_s"}
    assert "stability" in d


def test_json_roundtrip_preserves_saturated_flags(tmp_path):
    """饱和标记必须过 JSON 往返（W2）。

    不存的话，从 JSON 读回的历史会话会"忘记"哪些点被限幅污染，
    重算出的 PM/GM 会把不可信点算进去——用户只看到"数值和上次不一样"。
    """
    from app.model.bode_data import MODE_PLANT, RawPoint, SweepResult

    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    for i in range(5):
        res.add(RawPoint(100.0 * (i + 1), 2.0, 0.0, 1.0, 0.0),
                saturated=(i in (1, 3)))
    p = tmp_path / "sat.json"
    export.to_json(res, str(p))

    back = export.from_json(str(p))
    assert back.sat_mask().tolist() == [False, True, False, True, False]


def test_markdown_has_stability_table(tmp_path):
    p = tmp_path / "f.md"
    export.to_markdown(_result(), str(p), meta={"拓扑": "Buck"})
    txt = p.read_text(encoding="utf-8")
    assert "相位裕度 PM" in txt
    assert "增益裕度 GM" in txt
    assert "Buck" in txt
    assert "## 数据" in txt


def test_markdown_with_multiple_curves(tmp_path):
    p = tmp_path / "g.md"
    export.to_markdown(_result(), str(p),
                       curves=[("实测", _result()),
                               ("预测", _result(MODE_PLANT))])
    txt = p.read_text(encoding="utf-8")
    assert "曲线对比" in txt
    assert "实测" in txt and "预测" in txt


def test_ensure_ext(tmp_path):
    assert export.ensure_ext("a", "csv").endswith(".csv")
    assert export.ensure_ext("a.csv", "csv") == "a.csv"
    assert export.ensure_ext("a.b", "csv").endswith(".csv")


def test_default_name():
    name = export.default_name("sfra", "csv")
    assert name.startswith("sfra_") and name.endswith(".csv")
