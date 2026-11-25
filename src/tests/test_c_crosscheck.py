"""S2 · C 库与 Python 参考实现的交叉验证（pytest 封装）。

真正的逻辑在 `tools/cross_check.py`（也能单独 `python tools/cross_check.py`
跑，输出更好看）。这里只做三件事：

  1. 把 10 个用例接进 pytest，防止后续改 core.c 时悄悄跑偏；
  2. 默认**跳过**——它要现编 C 代码、跑子进程，全套约 15s，
     不适合每次 `pytest` 的快速通道。用 `pytest -m crosscheck` 显式开启；
  3. 挡住"假通过"。定点路径曾经因为 dump 程序把 float 直接塞给 int16
     参数，C 端累加器全 0，而旧的 compare() 在分母为 0 时直接跳过，
     输出 "0.0000 dB / 0.0000° PASS"。现在 compare() 遇到全零分量会判失败。

跑法：
    pytest -m crosscheck -v          # 全套 10 个用例
    python tools/cross_check.py      # 同样的东西，带统计表
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: tools/ 不在 pythonpath 里（pytest.ini 只配了 src），手工加进去。
_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import cross_check as cc  # noqa: E402

#: 模块默认全部属于 crosscheck 慢通道（要 gcc + 子进程）。
#: **例外**：`test_sin_tab_has_no_dc_bias` 是纯 Python 的，
#: 它是坑 A12（sin_q15 直流偏置）唯一的守门人，必须进快速通道。
#: 用 hook 在收集后单独摘掉，比给它加一个不存在的 mark 干净。
pytestmark = pytest.mark.crosscheck

#: 一次编译、多次复用。build_dump() 每次都会重编，10 个用例编 10 次
#: 太慢，这里把编译交给 fixture。
_CACHE: dict[bool, Path] = {}


@pytest.fixture(scope="module")
def dumps() -> dict[bool, Path]:
    if not _CACHE:
        _CACHE[False] = cc.build_dump(fixed=False)
        _CACHE[True] = cc.build_dump(fixed=True)
    return _CACHE


BASE = dict(isr_freq=100_000.0, freq_start=100.0, freq_step=10 ** (1 / 20),
            amplitude=0.01, vec_length=30, cycles_mode=1)

#: (用例名, 配置, 是否定点)
CASES = [
    ("plant_normal",  dict(BASE, mode=0), False),
    ("loop_normal",   dict(BASE, mode=1), False),
    ("plant_fast",    dict(BASE, mode=0, cycles_mode=0), False),
    ("plant_precise", dict(BASE, mode=0, cycles_mode=2, vec_length=10), False),
    ("loop_fast",     dict(BASE, mode=1, cycles_mode=0), False),
    ("loop_precise",  dict(BASE, mode=1, cycles_mode=2, vec_length=10), False),
    # 定点（M0+）路径：DoD 是 < 0.05 dB / 0.3°
    ("plant_normal_q15",  dict(BASE, mode=0), True),
    ("loop_normal_q15",   dict(BASE, mode=1), True),
    ("plant_precise_q15", dict(BASE, mode=0, cycles_mode=2, vec_length=10), True),
    ("loop_precise_q15",  dict(BASE, mode=1, cycles_mode=2, vec_length=10), True),
]


@pytest.mark.parametrize("name,cfg,fixed", CASES,
                         ids=[c[0] for c in CASES])
def test_c_matches_python(dumps, name, cfg, fixed, tmp_path):
    """C 端累加器与 Python 参考实现折算后需一致。

    浮点路径卡 0.01 dB / 0.05°，定点路径卡 DoD 的 0.05 dB / 0.3°。
    拿浮点的尺子量定点是错的——Q15 输入本身就带 3e-5 的量化噪声。
    """
    exe = dumps[fixed]
    script = tmp_path / "script.txt"
    cc.gen_script(cfg, script)
    c_res = cc.run_c(exe, script)
    p_res = cc.run_py(cfg, fixed=fixed)

    tol = (0.05, 0.3) if fixed else (0.01, 0.05)
    rep = cc.compare(c_res, p_res, cfg["mode"], *tol)
    assert rep.passed, str(rep)
    # 点数必须对上：少一个点说明某一侧提前收敛/提前停了
    assert rep.n_points == cfg["vec_length"], str(rep)
