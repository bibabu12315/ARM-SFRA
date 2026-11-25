"""S2 · C 库与 Python 参考实现的交叉验证（L1）。

目标（04 文档 4.2）：证明 `firmware/sfra_src/sfra_core.c` 与
`src/app/sim/sfra_sim.py` 是同一个东西。

**比的是累加器原值，不是 mag/phase**。原因：
  - mag/phase 只能暴露"结果错了"，累加器能暴露"哪一步错了"；
  - 一旦比 mag/phase，相位约定的实现差异会被混进来，定位困难。

结构：
    1. `gen_script()`   —— 按 C 端的算法算好逐拍激励，写成文本
    2. `run_c()`        —— 跑编译好的 sfra_dump.exe，读回累加器
    3. `run_py()`       —— 用 SfraSim 跑同一套激励
    4. `compare()`      —— 逐点比对，给出相对误差统计

C 端自己算几何（cycles/data_count），Python 端也自己算，
两边独立实现同一套公式——这才是真正的交叉验证，而不是把 Python
算好的 data_count 喂给 C 端。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: 工程根。本文件在 <root>/tools/ 下，parents[1] 才是 <root>。
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 数据模型
@dataclass
class PointResult:
    """一个频点的累加器原值（与 C 端 printf 的 5 个数一一对应）。"""
    f_act: float
    a_c: float
    a_s: float
    b_c: float
    b_s: float


@dataclass
class CompareReport:
    """一次交叉验证的结论。"""
    mode: int
    n_points: int
    max_rel_db: float          # 折算到 dB 的最大偏差
    max_rel_deg: float         # 折算到角度的最大偏差
    max_acc_rel: float         # 累加器的最大相对误差
    worst_freq: float
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return (f"[{'PASS' if self.passed else 'FAIL'}] mode={self.mode} "
                f"points={self.n_points} "
                f"累加器相对误差 max={self.max_acc_rel:.2e} "
                f"→ {self.max_rel_db:.4f} dB / {self.max_rel_deg:.4f}° "
                f"@ {self.worst_freq:.0f}Hz")


# ---------------------------------------------------------------- 调用 C 端
def find_toolchain() -> Path | None:
    """找一个能用的 x86 gcc。优先项目内置的，其次 PATH 里的。"""
    local = ROOT / "tools" / "toolchain" / "w64" / "mingw64" / "bin" / "gcc.exe"
    if local.exists():
        return local
    import shutil
    exe = shutil.which("gcc") or shutil.which("gcc.exe")
    return Path(exe) if exe else None


def build_dump(out_exe: Path | None = None, fixed: bool = False) -> Path:
    """编译 sfra_dump.exe。每次交叉验证都重编，避免跑旧二进制。

    fixed=True 时加 `-DSFRA_USE_FIXED_POINT=1`，走 M0+ 的 Q15 定点路径。
    两条路径必须都能与 Python 参考对上——定点路径多了"信号归一化"和
    "Q15 截断"两处量化，只要这两处在 Python 里有一处没仿对，
    折算到相位上就会是 0.1° 量级的系统性偏差（DoD 是 0.3°）。
    """
    gcc = find_toolchain()
    if gcc is None:
        raise RuntimeError(
            "找不到 x86 gcc。请把 gcc 放到 tools/toolchain/w64/mingw64/bin/，"
            "或把它加入 PATH。")

    out_exe = out_exe or (ROOT / "tools" /
                          ("sfra_dump_fixed.exe" if fixed else "sfra_dump.exe"))
    cmd = [
        str(gcc), "-std=c99", "-Wall", "-Wextra", "-O2",
        f"-I{ROOT / 'firmware' / 'sfra_inc'}",
    ]
    if fixed:
        cmd.append("-DSFRA_USE_FIXED_POINT=1")
    cmd += [
        str(ROOT / "firmware" / "sfra_src" / "sfra_core.c"),
        str(ROOT / "firmware" / "examples" / "pc" / "sfra_dump.c"),
        "-o", str(out_exe), "-lm",
    ]
    env = dict(os.environ)
    env["PATH"] = str(gcc.parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"编译失败:\n{r.stdout}\n{r.stderr}")
    return out_exe


def run_c(exe: Path, script: Path) -> list[PointResult]:
    env = dict(os.environ)
    if exe.parent.exists():
        env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run([str(exe), str(script)],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"sfra_dump 失败:\n{r.stderr}")
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        out.append(PointResult(*[float(x) for x in parts]))
    return out


# ---------------------------------------------------------------- 生成激励
def _pick_cycles(isr: float, foi: float, cyc_mode: int) -> int:
    """与 C 端 sfra_calc_cycles 逐条对应的 Python 参考（不调用 sfra_sim，
    避免用同一份代码自己验自己）。

    **所有中间量按 float32 复算**：C 端 `0.020f * 1000.000061f` 在 float32
    下恰好等于 20.0，而双精度是 20.0000012，ceil 之后一个取 20、一个取 21——
    cyc 差 1，整个频点的 data_count 和角步进就全变了。交叉验证要暴露的是
    算法逻辑错误，不是"Python 用 double、C 用 float"这种表示差异。
    """
    f32 = np.float32
    FAST_MIN_CYCLES = 8
    FAST_MIN_SECONDS = 0.020
    MIN_SPC = 16.0

    if foi <= 0:
        return 1

    if cyc_mode == 0:                       # FAST
        by_time = int(np.ceil(f32(f32(FAST_MIN_SECONDS) * f32(foi))))
        n = max(FAST_MIN_CYCLES, by_time)
    elif cyc_mode == 2:                     # PRECISE
        n = 20 if foi < 10.0 else 200
    else:                                   # NORMAL
        if foi < 10.0:
            n = 10
        elif foi <= 100.0:
            n = max(10, int(math.floor(foi + 0.5)))     # 对齐 C 的 ceilf(x-0.5)
        else:
            n = 100

    spc = f32(f32(isr) / f32(foi))
    if spc < f32(MIN_SPC):
        lo = int(np.ceil(f32(f32(MIN_SPC) / spc)))
        if lo > n:
            n = lo
    return n


def _point_geom(isr: float, foi: float, cycles: int) -> tuple[int, float]:
    """与 C 端 sfra_point_geometry 一致：用 double 算再向上取整。

    这里刻意用 double——C 端也正是 `(double)isr * (double)cycles / (double)f`，
    因为走 float 会把 1e5×100/8000 这种整数顶成 +1。
    """
    dc = int(math.ceil(float(isr) * cycles / float(foi)))
    dc = max(dc, cycles)
    rad = cycles / dc * 2.0 * math.pi
    return dc, rad


def _p(name: str, x: float) -> str:
    """把 float 按 float32 能精确回读的位数写进文本。

    float32 只有 7 位有效数字，`%.12g` 会把 1.1220184543019634 写成
    1.122018454302，C 端读成 float 后又变成 1.1220184564590454，
    与 Python 的双精度值差 2e-9。单个点无所谓，但 freq_step 会被
    连乘 30 次，频率累计偏 2e-7，df 一偏相位就整体错开。
    所以一切都按 "Python 端先量化成 float32，再写 9 位" 的规则来。
    """
    return f"{float(np.float32(x)):.9g}"


def gen_script(cfg: dict, path: Path) -> int:
    """按 **float32 语义** 生成逐拍激励，返回总拍数。

    固件里一切都是 float32：freq_step、isr_freq、theta、amplitude 全部
    单精度累加。Python 端若继续用 double 算，两边输入从一开始就不一样，
    比出来的差异全是"数据表示"，不是算法。所以这里：

      - freq_step 量化成 float32 再写文本（C 端读进来逐位相同）
      - 每个频点的名义频率用 **float32 逐次累乘**（与 sfra_freq_at 一致）
      - settle / data_count / 角步进 全部按量化后的值算
      - 注入值由查表 sin 生成（与 C 端同一张表），不是 math.cos

    这样两边的激励序列逐位相同，剩下的差异只可能是算法逻辑。
    """
    f32 = np.float32
    isr = float(f32(cfg["isr_freq"]))
    fs_start = float(f32(cfg["freq_start"]))
    step = float(f32(cfg["freq_step"]))
    amp = float(f32(cfg["amplitude"]))
    nvec = cfg["vec_length"]
    mode = cfg["mode"]
    cyc_mode = cfg["cycles_mode"]

    tab = _SinTab()

    lines = []
    # 相位**跨频点连续**：固件的 theta_phase 是 uint32 自由累加，
    # 换频点时只换 step_phase，不断章（这是坑 A2 的正面要求）。
    # 早期这里每个频点把 phase 归零，导致从第 2 个点起激励波形
    # 整体错开一个相位，C 端算出的 a_s 与 Python 完全对不上。
    phase = 0
    for i in range(nvec):
        # 与 sfra_freq_at 一致：float32 逐次乘
        foi32 = f32(fs_start)
        for _ in range(i):
            foi32 = f32(foi32 * f32(step))
        foi = float(foi32)

        cyc = _pick_cycles(isr, foi, cyc_mode)
        dc, rad = _point_geom(isr, foi, cyc)
        # 角步进也要量化：C 端 theta_step 是 float
        us = tab.rad_to_phase(float(f32(rad)))

        sec = max(0.020, 2.0 / foi)
        settle = int(math.ceil(sec * isr))

        # settle 期注入照常推进，但不采集。
        # 相位用 Q32 定点推进，与固件 sfra_advance 完全一致。
        for _ in range(settle + dc):
            c, _s = tab.sincos(phase)
            v = float(f32(amp * c))
            # PLANT：激励是执行量 x = 0.5 + inj（开环，注入叠加在占空比上）
            # LOOP ：激励是参考 r = inj（Python 侧的"参考"本身就没有直流，
            #        这与它对应的 y = 0.5·inj 配套，见 run_py 里的说明）
            lines.append(f"{float(f32(0.5 + v)) if mode == 0 else v:.9g}")
            phase = (phase + us) & 0xFFFFFFFF

    header = (f"{_p('isr', isr)} {_p('start', fs_start)} {_p('step', step)} "
              f"{_p('amp', amp)} {nvec} {mode} {cyc_mode}")
    path.write_text(header + "\n" + "\n".join(lines) + "\n", encoding="ascii")
    return len(lines)


# ---------------------------------------------------------------- Python 端
Q27 = 134217728                       # 2^27，acc_mac 里 y 的定标
INV_Q42 = 1.0 / (2.0 ** 42)           # 累加器 → pu

f32 = np.float32
#: **必须是精确的 2^27**：写成 1.34e8 的 float32 值是 133999992，
#: 定标错 1.6e-6，100 点累加后就是 1.6e-6 相对量，虽然折算成 dB 很小，
#: 但会让"定点路径是否逐位一致"这个结论变得不可靠。
Q27_F = f32(Q27)


def _acc_mac(acc: int, y: float, q15: int) -> int:
    """与 C 的 `acc_mac()` 逐位一致。

        yy = (int32_t)(y * 2^27f)
        acc += (int64_t)yy * q15

    注意 C 端是 `(int32_t)(y * ACC_Y_SCALE)`，向零截断；Python 的 int()
    也是向零截断，语义相同。这里不引入任何 round()。
    """
    return acc + int(f32(y) * Q27_F) * q15


class _IntAcc:
    """六个整数累加器 + 计数。对应固件 sfra_t 的 y_c..inj_s。"""

    __slots__ = ("y_c", "y_s", "x_c", "x_s", "inj_c", "inj_s", "n")

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.y_c = self.y_s = 0
        self.x_c = self.x_s = 0
        self.inj_c = self.inj_s = 0
        self.n = 0

    def to_float(self) -> "PointResult":
        return PointResult(0.0,
                           self.y_c * INV_Q42, self.y_s * INV_Q42,
                           self.x_c * INV_Q42, self.x_s * INV_Q42)
class _SinTab:
    """与 C 端 `sfra_init_all()` + `sin_q15()` 逐位等价的查表实现。

    为什么要在这里重做一遍查表？
      交叉验证的目标是"**算法结构**两边一致"，不是"sin 的求值精度"。
      C 端为了跑得动 M0+ 必然用查表（1024 点 + 线性插值，误差 7.5e-6），
      Python 端若用 math.cos 精确值，两边累加器必然差 1e-6 量级——
      这个差异会淹没真正的 bug（比如相位推进顺序、采样点数差一个）。
      所以这里按 C 的**同一张表、同一套整数插值、同一个 Q32 相位**复算，
      剩下的残差才是 float32 舍入，量级 1e-7，任何 1e-4 以上的偏差
      都必然是逻辑错误。

    **整数插值不是可选项**：C 端 `sin_q15()` 最后一步是 `t >> shift`，
    结果只能取整到 1 LSB（3.05e-5）。Python 端若用浮点公式
    `(v0·FS + Δv·frac)/(32767·FS)`，会多出最高 1.5e-5 的细分量；
    1024 点里约有 3/4 的相位点落在不同 LSB 上，1e5 拍累加的偏差
    可达 1e-4 相对量 —— 看起来像"算法不一致"，其实只是量化方式不同。
    """

    def __init__(self, bits: int = 10):
        self.n = 1 << bits
        self.mask = self.n - 1
        self.shift = 32 - bits
        self.frac_scale = 1 << self.shift
        self.frac_mask = self.frac_scale - 1
        # C 端：s_sin_tab[k] = (int16_t)(32767.0 * sin(2πk/N))，k 取 0..N
        self.tab = [int(32767.0 * math.sin(2.0 * math.pi * k / self.n))
                    for k in range(self.n + 1)]

    def rad_to_phase(self, rad: float) -> int:
        """弧度 → Q32 归一化相位（与 C 的 sfra_rad_to_phase 一致）。"""
        t = rad / (2.0 * math.pi) * 4294967296.0
        if t < 0.0:
            t += 4294967296.0
        return int(t + 0.5) & 0xFFFFFFFF

    def sin_q15(self, phase: int) -> int:
        """与 C 的 `sin_q15()` 逐位一致：int64 乘加后**四舍五入**回 Q15。"""
        p = phase & 0xFFFFFFFF
        i0 = (p >> self.shift) & self.mask
        i1 = (i0 + 1) & self.mask
        frac = p & self.frac_mask
        v0, v1 = self.tab[i0], self.tab[i1]
        t = v0 * self.frac_scale + (v1 - v0) * frac
        # +半LSB 再右移。**不能省**：直接 `t >> shift` 是 floor，对零均值
        # 正弦会留下 −0.5 LSB 的直流偏置，PLANT 模式下 x = 0.5 + inj，
        # 大直流乘上这个偏置会在 x_s 里造出伪分量（详见 sfra_core.c 坑 A12）。
        return (t + (1 << (self.shift - 1))) >> self.shift

    def sincos(self, phase: int) -> tuple[float, float]:
        """返回 (cos, sin)，与 C 的 inject/monitor 里两次 sin_q15 调用一致。"""
        s = self.sin_q15(phase)
        c = self.sin_q15(phase + 0x40000000)
        q2f = np.float32(1.0 / 32767.0)
        # (float)q15 * Q15_TO_F，全程 float32
        return (float(np.float32(np.float32(c) * q2f)),
                float(np.float32(np.float32(s) * q2f)))


class _Q32Phase:
    """把 SimSfra 的浮点 theta 换成 Q32 相位，行为与固件完全一致。

    SimSfra 本身用 `theta += step` 的浮点累加；固件用 uint32 定点。
    直接拿浮点版去比，100 Hz 一个频点 1e5 拍之后 theta 到 6.3e2，
    float32 的 ulp 已经是步进的 1%，累积漂移几十度——比出来的差异
    全是相位表示方式，不是算法。这里注入同样的定点推进。
    """

    def __init__(self, tab: _SinTab):
        self.tab = tab
        self.phase = 0
        self.us = 0          # 每拍相位增量（Q32），plot 时由调用方设好

    def sincos(self, _theta_ignored: float) -> tuple[float, float]:
        return self.tab.sincos(self.phase)

    def advance(self) -> None:
        self.phase = (self.phase + self.us) & 0xFFFFFFFF


def run_py(cfg: dict, use_sin_tab: bool = True,
           fixed: bool = False) -> list[PointResult]:
    """用 SfraSim 的**时序逻辑** + 固件的**整数累加器**导出累加器原值。

    S2 第二阶段（A11 之后）固件把所有累加器换成 int64 Q42 定点，
    所以这里也不能再复用 `PointAccumulator`（它是 float 累加）。
    复用的只是 SimSfra 的状态机语义：settle 递减、采满即停、
    LOOP 的 Inj 只在与 Y 同一轮里累加——这些是算法约定，与数值表示无关。

    fixed=True 时复现定点路径的两处额外量化：
      1. `sfra_inject()` 返回 int16：`(int16)(v * 32767)`
      2. `sfra_sig_to_f()` 再把 int16 除回 32767 进累加器
    这两步不能省——少了它们，定点路径的"输入被量化成 1/32767 网格"
    这个事实就没了，比出来的差异会假性偏小，DoD 那关等于白过。

    频率表、角步进、注入乘法全部按 float32 语义复算，理由见 gen_script。
    """
    isr = float(f32(cfg["isr_freq"]))
    fs_start = float(f32(cfg["freq_start"]))
    fstep = float(f32(cfg["freq_step"]))
    amp = float(f32(cfg["amplitude"]))
    nvec = cfg["vec_length"]
    mode = cfg["mode"]
    cyc_mode = cfg["cycles_mode"]

    q2f = f32(1.0 / 32767.0)

    def to_sig(v: float) -> float:
        """sfra_signal_t 往返：进出都把值压到 I/O 类型上。"""
        if not fixed:
            return float(f32(v))
        return float(f32(f32(int(f32(v) * f32(32767.0))) * q2f))

    tab = _SinTab()
    phase = 0                     # Q32，跨频点连续（A2/A12）
    out: list[PointResult] = []

    for i in range(nvec):
        foi32 = f32(fs_start)
        for _ in range(i):
            foi32 = f32(foi32 * f32(fstep))
        foi = float(foi32)

        cyc = _pick_cycles(isr, foi, cyc_mode)
        dc, rad = _point_geom(isr, foi, cyc)
        f_act = cyc / dc * isr
        us = tab.rad_to_phase(float(f32(rad)))
        settle = int(math.ceil(max(0.020, 2.0 / foi) * isr))

        acc = _IntAcc()
        collecting = False
        for k in range(settle + dc + 2):
            # ---- inject() 的等价展开 ----
            c15 = tab.sin_q15(phase + 0x40000000)
            s15 = tab.sin_q15(phase)
            c = float(f32(f32(c15) * q2f))
            v = to_sig(float(f32(f32(amp) * f32(c))))

            if k < settle:
                if k == settle - 1:
                    collecting = True
            else:
                collecting = True

            if collecting and acc.n < dc and mode == 1:
                # LOOP：Inj 与 Y 同一轮采集（D19）
                acc.inj_c = _acc_mac(acc.inj_c, v, c15)
                acc.inj_s = _acc_mac(acc.inj_s, v, s15)

            # ---- monitor() 的等价展开 ----
            if collecting and acc.n < dc:
                if mode == 0:
                    y = to_sig(float(f32(0.7) * f32(v)))   # 与 C 逐字一致
                else:
                    y = to_sig(float(f32(f32(0.5) * f32(v))))
                acc.y_c = _acc_mac(acc.y_c, y, c15)
                acc.y_s = _acc_mac(acc.y_s, y, s15)
                if mode == 0:
                    x = to_sig(float(f32(f32(0.5) + f32(v))))
                    acc.x_c = _acc_mac(acc.x_c, x, c15)
                    acc.x_s = _acc_mac(acc.x_s, x, s15)
                acc.n += 1

            phase = (phase + us) & 0xFFFFFFFF

        r = acc.to_float()
        out.append(PointResult(f_act, r.a_c, r.a_s,
                               r.b_c if mode == 0 else acc.inj_c * INV_Q42,
                               r.b_s if mode == 0 else acc.inj_s * INV_Q42))
    return out


# ---------------------------------------------------------------- 比对
def _acc_ratio(c: PointResult, p: PointResult) -> float:
    """累加器相对误差，**按复振幅模长归一化**，并对绝对小量设底。

    两个坑：
      1. 不能逐分量比。某个分量理论值就是 0（y 与注入同相时 a_s ≡ 0），
         1e-7 的舍入会被算成 100% 误差。
      2. 也不能只按模长比。低频点 |Inj| ≈ 500，其虚部只有 6e-5，
         两边差 1e-6 看似微不足道，除以 6e-5 却是 1.7e-2。

    所以取
        err = |ΔS| / max(|S_c|, |S_p|,  sqrt(N)·eps)
    那个下限 sqrt(N)·eps 是"float 累加本来就会有的噪声水平"：
    N 次累加、每次相对误差 eps≈6e-8（float32 的半个 ulp），
    按随机游走累加就是 sqrt(N)·eps ≈ 2e-5（N=1e5）。
    低于这个量级的差异没有物理意义，不该被判成算法不一致。
    """
    n_est = 1e5                                 # 单频点典型采样点数
    floor = math.sqrt(n_est) * 6e-8
    out = 0.0
    for ac, as_, bc, bs in ((c.a_c, c.a_s, p.a_c, p.a_s),
                            (c.b_c, c.b_s, p.b_c, p.b_s)):
        dc, ds = ac - bc, as_ - bs
        d_amp = math.hypot(dc, ds)
        ref = max(math.hypot(ac, as_), math.hypot(bc, bs), floor)
        out = max(out, d_amp / ref)
    return out


def compare(c_res: list[PointResult], p_res: list[PointResult],
            mode: int, tol_db: float = 0.01, tol_deg: float = 0.05) -> CompareReport:
    """逐点比对，判据用**折算后的 dB / 度**，不用累加器相对误差。

    为什么不用累加器误差当判据：`|ΔS|/|S|` 在 |S| 小时会被放大（低频
    |Inj|=500、虚部只有 6e-5，两边差 1e-6 就是 1.7e-2，折算成相位
    只有 0.0001°）。DoD 关心的是"扫出来的点准不准"，就直接比 dB 与度。

    门槛来历（S2 DoD，00 文档 §0.5.2）：
      定点路径折算误差 < 0.05 dB / 0.3°   ← DoD 的硬指标
      浮点路径比它严一个量级，取 0.01 dB / 0.05°
    实测两边都是 float32 运算，量化噪声下限约 0.003 dB / 0.02°，
    所以 0.01 dB 是"确实一致"而不是"凑巧过关"的合理界线。
    """
    n = min(len(c_res), len(p_res))
    if n == 0:
        return CompareReport(mode, 0, 0, 0, 0, 0, False, "没有可比对的点")

    worst_db = 0.0
    worst_deg = 0.0
    worst_acc = 0.0
    worst_f = 0.0
    skipped = 0

    for i in range(n):
        c, p = c_res[i], p_res[i]
        # 用复振幅比：|S_c|/|S_p| 与 arg(S_c)-arg(S_p)
        # 这样比出来直接就是"这个频点的增益/相位差多少"
        for sc, sp in ((complex(c.a_c, c.a_s), complex(p.a_c, p.a_s)),
                       (complex(c.b_c, c.b_s), complex(p.b_c, p.b_s))):
            if abs(sp) <= 0 or abs(sc) <= 0:
                skipped += 1
                continue
            d_db = abs(20.0 * math.log10(abs(sc) / abs(sp)))
            d_ph = abs(math.degrees(math.atan2(sc.imag, sc.real)
                                    - math.atan2(sp.imag, sp.real)))
            while d_ph > 180.0:
                d_ph = abs(360.0 - d_ph)
            if d_db > worst_db or d_ph > worst_deg:
                worst_db = max(worst_db, d_db)
                worst_deg = max(worst_deg, d_ph)
                worst_f = p.f_act
        worst_acc = max(worst_acc, _acc_ratio(c, p))

    # 一个点都不该被跳过：C 端出 0 说明激励没进到累加器（典型是
    # 定点模式下误把 float 直接塞给 int16 参数），这比"有偏差"更严重，
    # 必须在判据里挡死，否则会以 "0.0000 dB / 0.0000°" 的形式假 PASS。
    if skipped:
        return CompareReport(mode, n, worst_db, worst_deg, worst_acc,
                             worst_f, False,
                             f"{skipped} 个分量为零，C 端未采到有效数据")

    return CompareReport(
        mode=mode,
        n_points=n,
        max_rel_db=worst_db,
        max_rel_deg=worst_deg,
        max_acc_rel=worst_acc,
        worst_freq=worst_f,
        passed=(worst_db <= tol_db and worst_deg <= tol_deg),
    )


# ---------------------------------------------------------------- 主入口
def run_case(name: str, cfg: dict, fixed: bool = False) -> CompareReport:
    exe = build_dump(fixed=fixed)
    script = ROOT / "tools" / "_sfra_script.txt"
    gen_script(cfg, script)
    c_res = run_c(exe, script)
    p_res = run_py(cfg, fixed=fixed)
    rep = compare(c_res, p_res, cfg["mode"])
    rep.detail = name
    return rep


def main() -> int:
    base = dict(isr_freq=100_000.0, freq_start=100.0, freq_step=10 ** (1 / 20),
                amplitude=0.01, vec_length=30, cycles_mode=1)
    cases = [
        # (名字, 配置, 是否定点路径)
        ("PLANT Normal",  dict(base, mode=0), False),
        ("LOOP  Normal",  dict(base, mode=1), False),
        ("PLANT Fast",    dict(base, mode=0, cycles_mode=0), False),
        ("PLANT Precise", dict(base, mode=0, cycles_mode=2, vec_length=10), False),
        ("LOOP  Fast",    dict(base, mode=1, cycles_mode=0), False),
        ("LOOP  Precise", dict(base, mode=1, cycles_mode=2, vec_length=10), False),
        # 定点（M0+）路径：DoD 要求折算误差 < 0.05 dB / 0.3°
        ("PLANT Normal Q15", dict(base, mode=0), True),
        ("LOOP  Normal Q15", dict(base, mode=1), True),
        ("PLANT Precise Q15", dict(base, mode=0, cycles_mode=2, vec_length=10), True),
        ("LOOP  Precise Q15", dict(base, mode=1, cycles_mode=2, vec_length=10), True),
    ]
    ok = True
    for name, cfg, fixed in cases:
        # 定点路径的判据放宽到 DoD 的 0.05 dB / 0.3°（Q15 输入本身就有
        # 3e-5 量化噪声，拿浮点的 0.01 dB 去卡它是拿错了尺子）
        tol = (0.05, 0.3) if fixed else (0.01, 0.05)
        try:
            exe = build_dump(fixed=fixed)
            script = ROOT / "tools" / "_sfra_script.txt"
            gen_script(cfg, script)
            c_res = run_c(exe, script)
            p_res = run_py(cfg, fixed=fixed)
            rep = compare(c_res, p_res, cfg["mode"], *tol)
            rep.detail = name
            print(f"{name:<18} {rep}")
            ok = ok and rep.passed
        except Exception as e:
            print(f"{name:<18} ERROR: {e}")
            ok = False
    print("\n总计:", "全部通过" if ok else "存在偏差")
    return 0 if ok else 1


# ---------------------------------------------------------------- 计时
BENCH_N = 1_000_000


def bench_dump(out_exe: Path | None = None) -> float:
    """跑 100 万次 inject+monitor+advance，返回每次的纳秒数。

    这是 S2 DoD 要求"回写 01/02 文档开销表"的实测值。注意它是在
    PC 上（x86-64、-O2、double 累加路径）跑的，**不是 MCU 上的真实
    开销**——能说明的是"算法本身有多少次乘加"，绝对周期数还得在
    目标板上用 DWT->CYCCNT 量。文档里会把两者分开写。
    """
    gcc = find_toolchain()
    if gcc is None:
        raise RuntimeError("找不到 x86 gcc")
    exe = out_exe or (ROOT / "tools" / "sfra_bench.exe")
    cmd = [str(gcc), "-std=c99", "-O2", "-DNDEBUG",
           f"-I{ROOT / 'firmware' / 'sfra_inc'}",
           str(ROOT / "firmware" / "sfra_src" / "sfra_core.c"),
           str(ROOT / "firmware" / "examples" / "pc" / "sfra_bench.c"),
           "-o", str(exe), "-lm"]
    env = dict(os.environ)
    env["PATH"] = str(gcc.parent) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"编译失败:\n{r.stdout}\n{r.stderr}")
    r = subprocess.run([str(exe), str(BENCH_N)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    # 输出格式：ns_per_frame
    return float(r.stdout.strip().split()[-1])


if __name__ == "__main__":
    raise SystemExit(main())
