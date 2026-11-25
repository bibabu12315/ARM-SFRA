/* ---------------------------------------------------------------------------
 * sfra_core.c - SFRA 扫频算法核心实现
 *
 * 与 src/app/sim/sfra_sim.py 逐行等价，所有"为什么这么写"的注释都指向
 * 那一边或 docs/00 的对应编号，改这里之前先看那边。
 * ------------------------------------------------------------------------- */
#include "sfra_core.h"

#include <math.h>
#include <string.h>

/* =========================================================================
 * 正弦查找表
 *
 * 2^SFRA_SIN_TAB_BITS 点 + 线性插值。查表比调 sinf 快一个数量级：
 *   M4F  sinf  约 60~100 cycle
 *   M0+  sinf  约 1500 cycle（软件浮点），完全不能用
 *   查表+插值  约 8 cycle
 *
 * 精度由 SFRA_SIN_TAB_BITS 决定（见头文件）：
 *   N=8  → 1.2e-4（-78dB），S2 的 1e-5 判据过不了，仅够看 60dB 动态范围
 *   N=10 → 7.5e-6（-102dB），默认，满足 1e-5
 *   N=12 → 4.7e-7（-126dB）
 * ========================================================================= */
#define SIN_TAB_SIZE  (1u << SFRA_SIN_TAB_BITS)
#define SIN_TAB_MASK  (SIN_TAB_SIZE - 1u)

/* 多一格放 sin(2π)=0，插值时不必回绕到 0 号元素。
 *
 * 现在用运行时填表（sfra_init_all），占 RAM；若想省 RAM，
 * 用 tools/gen_sin_table.py 生成 const 静态表替换下面的 s_sin_tab，
 * 就能放进 flash（M4F 推荐，省 2KB RAM）。 */
static int16_t s_sin_tab[SIN_TAB_SIZE + 1];

void sfra_init_all(void)
{
    uint32_t k;
    for (k = 0; k <= SIN_TAB_SIZE; k++) {
        double a = 2.0 * 3.14159265358979323846 * (double)k / (double)SIN_TAB_SIZE;
        s_sin_tab[k] = (int16_t)(32767.0 * sin(a));
    }
}

/* 一次算出 (cosθ, sinθ)，θ 是 Q32 归一化相位（整圈 = 2^32）。
 *
 * 表索引直接从高 SFRA_SIN_TAB_BITS 位取，插值小数取剩下的低位：
 *     i0   = (phase >> (32-N)) & MASK
 *     frac = phase & (2^(32-N) − 1)          —— 归一化到 [0,1)
 * 全程整数，不用 fmod/floor/浮点除法，M0+ 也跑得动。
 *
 * cos(θ) = sin(θ + π/2) = sin(θ + 2^30)，只是加常数。
 *
 * **为什么必须是整数插值而不是 double（坑 A11）**：
 *   最初写成 `(v0·FS + (v1−v0)·frac) / (32767·FS)` 的 double 版本。
 *   M4F 没有硬件双精度，编译器把它展开成 __aeabi_ui2d / __aeabi_dmul /
 *   __aeabi_dadd / __aeabi_d2f 一串软浮点调用，单次 sincos 实测约 500
 *   cycle —— 100kHz ISR 里占 5%，等于把 SFRA 塞进了控制回路预算。
 *   整数版只有 32×32→64 的 umull（M0+ 也有），约 20 cycle。
 *
 * **精度**：Q15 表 + 22 位小数插值，理论误差 (π/512)²/8 ≈ 7.5e-6；
 *   整数插值额外误差来自最后 t → float32 的舍入，1 ulp(1.0)=1.2e-7，
 *   远小于插值误差本身，不影响 1e-5 判据。
 */
#define SIN_IDX_SHIFT  (32u - SFRA_SIN_TAB_BITS)
#define SIN_FRAC_MASK  ((1u << SIN_IDX_SHIFT) - 1u)
/* 四舍五入用的半个 LSB。
 *
 * **为什么必须加它（坑 A12）**：
 *   `t >> k` 是算术右移 = 向下取整（floor）。正弦是零均值信号，
 *   对它做单向 floor 等于引入 **−0.5 LSB 的直流偏置**——实测扫一整圈
 *   平均偏差 −0.499928 LSB，不是随机误差，是常数偏移。
 *
 *   这个偏置在 LOOP 模式下几乎无害（inj 也是本表生成的，两边同步偏），
 *   但在 **PLANT 模式**下会放大成严重的相位误差：
 *       x = 0.5 + inj                      ← 大直流 0.5
 *       x_s = Σx·q15s = 0.5·Σq15s + Σinj·q15s
 *                     ↑ 理论为 0，实际 = 0.5·(−dc/2)
 *   `Σq15s` 是整周期求和，每个点偏 −0.5，N 点就是 −N/2，于是
 *   x_s 凭空多出 −0.25·N/32767，相对 x_c = A·N/2·32767 就是
 *   −0.5/(A·32767)。A=0.01 时 = −1.526e-3 → **+0.087° 的注入相位误差**。
 *
 *   而且这个误差随 dc 线性增长，所以高频（dc 小）看似没事、低频也小，
 *   中间频段最脏——实测 10 kHz（dc=1000）相位偏 +5.4°、幅值偏 0.79 dB，
 *   20 kHz 偏 +23°。**这就是"高频段系统性相位偏差"的唯一根因。**
 *
 *   加上半个 LSB 后平均偏差降到 −0.000000 LSB，10 kHz 的相位误差
 *   从 −5.39° 变成 0.000°（见 tools/_probe_q15.py 的对照实验）。
 *
 * 代价：多一次 int64 加法（M0+ 上 2 条 adds/adc），可忽略。 */
#define SIN_ROUND_HALF ((int64_t)1 << (SIN_IDX_SHIFT - 1u))

/* 一次插值取一个 Q15 值（-32767..32767）。
 *
 * t = v0·2^k + (v1−v0)·frac ∈ [−32767·2^k, 32767·2^k]
 * 65535·2^22 ≈ 2.7e11 超 int32，必须 int64（M0+ 也是单条 umull）。
 * 最后 (t + 半LSB) >> k 回到 Q15，误差 ≤ 0.5 LSB = 1.6e-5 绝对值。
 *
 * 不写成 `(float)t * 常数`：int64 → float 在 M0+ 上要调 __aeabi_l2f
 * （约 40 cycle），而先右移回 int32 再 cvt 只要几个 cycle。
 */
static int32_t sin_q15(uint32_t phase)
{
    uint32_t i0   = (phase >> SIN_IDX_SHIFT) & SIN_TAB_MASK;
    uint32_t i1   = (i0 + 1u) & SIN_TAB_MASK;
    uint32_t frac = phase & SIN_FRAC_MASK;
    int32_t  v0   = (int32_t)s_sin_tab[i0];
    int32_t  v1   = (int32_t)s_sin_tab[i1];
    int64_t  t    = (int64_t)v0 * (int64_t)(1u << SIN_IDX_SHIFT)
                  + (int64_t)(v1 - v0) * (int64_t)frac;
    /* +半LSB 再右移 = 四舍五入，消掉单向 floor 带来的 −0.5 LSB 直流 */
    return (int32_t)((t + SIN_ROUND_HALF) >> SIN_IDX_SHIFT);
}

/* 归一化到 [-1, 1) 的浮点输出 */
#define Q15_TO_F   (1.0f / 32767.0f)

/* =========================================================================
 * 纯计算工具（与 Python 一一对应）
 * ========================================================================= */

#define SFRA_FAST_MIN_CYCLES   8u
#define SFRA_FAST_MIN_SECONDS  0.020f
#define SFRA_MIN_SPC           16.0f    /* 每周期采样点下限，见 Python 同名常量注释 */

uint32_t sfra_calc_cycles(float foi_hz, uint8_t cyc_mode, float isr_freq)
{
    uint32_t n;
    float    spc, lo_f;

    if (foi_hz <= 0.0f) {
        return 1u;
    }

    if (cyc_mode == SFRA_CYC_FAST) {
        n = SFRA_FAST_MIN_CYCLES;
        {
            uint32_t by_time = (uint32_t)ceilf(SFRA_FAST_MIN_SECONDS * foi_hz);
            if (by_time > n) {
                n = by_time;
            }
        }
    } else if (cyc_mode == SFRA_CYC_PRECISE) {
        n = (foi_hz < 10.0f) ? 20u : 200u;
    } else {                                   /* SFRA_CYC_NORMAL */
        if (foi_hz < 10.0f) {
            n = 10u;
        } else if (foi_hz <= 100.0f) {
            long r = (long)ceilf(foi_hz - 0.5f);   /* round() 的 C99 行为 */
            n = (r > 10) ? (uint32_t)r : 10u;
        } else {
            n = 100u;
        }
    }

    /* 每周期采样点下限：spc = f_s/f 与周期数无关，
     * 这里只在"周期数太少导致总点数不足"时补周期。 */
    spc = isr_freq / foi_hz;
    if (spc < SFRA_MIN_SPC) {
        lo_f = ceilf(SFRA_MIN_SPC / spc);
        if ((uint32_t)lo_f > n) {
            n = (uint32_t)lo_f;
        }
    }
    return n;
}

uint32_t sfra_point_geometry(float isr_freq, float foi_hz, uint32_t cycles,
                             float *out_rad)
{
    uint32_t data_count;
    double   dc;

    if (foi_hz <= 0.0f || isr_freq <= 0.0f || cycles == 0u) {
        if (out_rad) {
            *out_rad = 0.0f;
        }
        return 0u;
    }

    /* data_count = ceil(f_s * cycles / f)   —— 用 double 防 float 舍入
     * 直接算出 ceil-1，避免 1e5*100/8000 这类整值被浮点误差顶到 +1 */
    dc = (double)isr_freq * (double)cycles / (double)foi_hz;
    data_count = (uint32_t)ceil(dc);
    if (data_count < cycles) {
        data_count = cycles;                 /* 至少每周期 1 点 */
    }

    if (out_rad) {
        *out_rad = (float)((double)cycles / (double)data_count
                           * 2.0 * 3.14159265358979323846);
    }
    return data_count;
}

float sfra_actual_freq(float isr_freq, uint32_t data_count, uint32_t cycles)
{
    if (data_count == 0u) {
        return 0.0f;
    }
    return (float)((double)cycles / (double)data_count * (double)isr_freq);
}

/* 奈奎斯特上限：f_max = isr_freq / SFRA_NYQ_DIV。
 * 取 5 而不是 2，理由是 SFRA 测的是 sin 注入的**单频响应**：
 *   1. f 接近 f_s/2 时，每个周期只有 2 个采样点，sin_q15 的查表插值
 *      相位误差会被放大到几度；
 *   2. 电源环路在 f_s/2 附近本来就没有意义（那是开关纹波频段）；
 *   3. TI 的 SFRA GUI 同样把扫频上限限制在 f_s/5 附近。
 * 05 文档的 SOP 也按这个值写给用户。 */
#define SFRA_NYQ_DIV 5.0f

float sfra_freq_at(const sfra_setup_t *cfg, uint16_t i)
{
    /* 逐点累乘，和 Python 的 power(freq_step, arange) 有微小舍入差异，
     * 但 f_act 由整周期取整反算，不受名义值影响，差异 < 1e-7 相对量。 */
    float    f = cfg->freq_start;
    float    f_max = cfg->isr_freq / SFRA_NYQ_DIV;
    uint16_t k;

    for (k = 0; k < i; k++) {
        f *= cfg->freq_step;
        if (f >= f_max) {
            /* 到顶就停：后面所有点都钉在 f_max 上，不再继续累乘。
             * 不钳的话频率会一路涨过 f_s/2，data_count 撞到下限、
             * f_act 被反算成同一个值，PC 端看到的是"末尾一长串
             * 100kHz 的假点"——S3 端到端第一次跑就撞上了。 */
            return f_max;
        }
    }
    return (f > f_max) ? f_max : f;
}

uint16_t sfra_effective_points(const sfra_setup_t *cfg)
{
    float f_max;
    float f;
    uint16_t i;

    if (!cfg || cfg->isr_freq <= 0.0f || cfg->vec_length == 0u) {
        return 0u;
    }
    f_max = cfg->isr_freq / SFRA_NYQ_DIV;
    f = cfg->freq_start;
    /* 首点就已经到顶：只保留这一点。 */
    if (f >= f_max) {
        return 1u;
    }
    for (i = 1; i < cfg->vec_length; i++) {
        f *= cfg->freq_step;
        if (f >= f_max) {
            return (uint16_t)(i + 1u);    /* 含被钳在 f_max 的那一点 */
        }
    }
    return cfg->vec_length;
}

/* =========================================================================
 * 生命周期
 * ========================================================================= */

static void sfra_clear_acc(sfra_t *s)
{
    s->y_c = s->y_s = 0;
    s->x_c = s->x_s = 0;
    s->inj_c = s->inj_s = 0;
}

/* =========================================================================
 * 累加器定标
 *
 * 累加 = Σ (y·2^27) · (c·32767)，其中 y,c ∈ [-1,1]。积是 Q42。
 * 两个常量都必须是编译期常量：写成 `(int32_t)(y * 134217728.0f)` 这种，
 * 编译器会直接生成 vcvt.f32.s32，不会去调 double。
 * ========================================================================= */
#define ACC_Y_SCALE   134217728.0f        /* 2^27 */
#define ACC_INV_SCALE (1.0f / 4398046511104.0f)   /* 2^-42 */

/* 一次乘加：acc += y·q15，y 是归一化到 [-1,1] 的**单精度**，q15 是 ±32767。
 *
 * 只做整数乘加，M4F/M0+ 都是 umull + adds + adc 三条。
 * 为什么入参是 float 而不是 sfra_signal_t：
 *   定点路径（M0+）下 sfra_signal_t = int16，采样值本身就是 Q15，
 *   走 sfra_sig_to_f() 换成 [-1,1] 再进这里，两条路径只剩一套数值代码。
 *   多出的一次 mul 在 M0+ 软件浮点上约 20 cycle，但换来的是
 *   "只有一份累加逻辑"，不会出现两条路径慢慢走偏的老问题。 */
static int64_t acc_mac(int64_t acc, float y, int32_t q15)
{
    int32_t yy = (int32_t)(y * ACC_Y_SCALE);  /* Q27，向零截断 */
    return acc + (int64_t)yy * (int64_t)q15;
}

/* 采样值 → 归一化 float。两条路径的唯一定义处。 */
static float sfra_sig_to_f(sfra_signal_t v)
{
#if SFRA_USE_FIXED_POINT
    return (float)v * Q15_TO_F;
#else
    return (float)v;
#endif
}

void sfra_init(sfra_t *s, const sfra_setup_t *cfg, const sfra_result_t *res)
{
    if (!s) {
        return;
    }
    memset(s, 0, sizeof(*s));
    if (cfg) {
        s->cfg = *cfg;
    }
    (void)res;                 /* 结果数组不在这里存，交给 background_task 的调用方 */
    s->state = SFRA_ST_IDLE;
    s->theta_phase = 0u;
}

/* 弧度角步进 → Q32 归一化相位增量。
 * 整圈 = 2^32，所以 inc = rad / 2π × 2^32。用 double 算，
 * 保证 200kHz/100Hz 这种"极大 dc"下也不会舍入到 0。 */
static uint32_t sfra_rad_to_phase(float rad)
{
    double t = (double)rad / (2.0 * 3.14159265358979323846) * 4294967296.0;
    if (t < 0.0) {
        t += 4294967296.0;
    }
    return (uint32_t)(int64_t)(t + 0.5);
}

int sfra_start(sfra_t *s)
{
    uint32_t dc;
    float    rad;

    if (!s) {
        return -1;
    }
    if (s->cfg.isr_freq <= 0.0f || s->cfg.freq_start <= 0.0f ||
        s->cfg.freq_step <= 0.0f || s->cfg.vec_length == 0u) {
        return -2;
    }
    if (s->cfg.mode > SFRA_MODE_LOOP) {
        return -3;
    }

    /* 奈奎斯特钳制：把 vec_length 裁到"触顶前最后一个点"。
     * 不裁的话，越过 f_s/5 之后每一点都钉在上限，扫出来是一长串
     * 完全重复的假数据（S3 端到端第一次跑就是 60~67 全是 20kHz）。
     * 裁在 start 里做，GET_STATUS 的 freq_index / GET_DATA 的 avail
     * 就都自动跟着对齐，不需要每处各判一次。 */
    {
        uint16_t eff = sfra_effective_points(&s->cfg);
        if (eff == 0u) {
            return -2;
        }
        s->cfg.vec_length = eff;
    }

    s->point_idx  = 0;
    s->collected  = 0;
    s->theta_phase = 0u;

    /* 第一个频点的几何 */
    {
        uint32_t cyc = sfra_calc_cycles(sfra_freq_at(&s->cfg, 0),
                                        s->cfg.cycles_mode, s->cfg.isr_freq);
        dc = sfra_point_geometry(s->cfg.isr_freq,
                                 sfra_freq_at(&s->cfg, 0), cyc, &rad);
    }
    s->data_count  = dc;
    s->theta_step  = rad;
    s->step_phase  = sfra_rad_to_phase(rad);

    /* settle：max(20ms, 2 个注入周期)，按采样率换算成拍数。
     * 不按采集窗比例截断——那是 S1 的坑 A4。 */
    {
        float    foi = sfra_freq_at(&s->cfg, 0);
        float    sec = 0.020f;
        float    per = (foi > 0.0f) ? (2.0f / foi) : 0.0f;
        if (per > sec) {
            sec = per;
        }
        s->settle_left = (uint32_t)ceilf(sec * s->cfg.isr_freq);
    }

    sfra_clear_acc(s);
    s->state = SFRA_ST_SETTLE;
    return 0;
}

void sfra_abort(sfra_t *s)
{
    if (!s) {
        return;
    }
    /* 安全第一：先让注入归零（inject 看到 state 就返回 0），再清状态。
     * 顺序反了会有一个 ISR 周期仍在注入。 */
    s->state   = SFRA_ST_IDLE;
    s->theta_phase = 0u;
    s->collected = 0;
    sfra_clear_acc(s);
}

uint8_t sfra_is_running(const sfra_t *s)
{
    return (uint8_t)(s && (s->state == SFRA_ST_SETTLE ||
                           s->state == SFRA_ST_COLLECT));
}

uint8_t sfra_is_done(const sfra_t *s)
{
    return (uint8_t)(s && s->state == SFRA_ST_DONE);
}

uint8_t sfra_state(const sfra_t *s)
{
    return s ? s->state : SFRA_ST_IDLE;
}

void sfra_clear_done(sfra_t *s)
{
    if (s && s->state == SFRA_ST_DONE) {
        s->state = SFRA_ST_IDLE;
    }
}

/* =========================================================================
 * ISR 内
 * ========================================================================= */

sfra_signal_t sfra_inject(sfra_t *s)
{
    int32_t q15c, q15s;
    float   c, v;

    if (!s || s->state == SFRA_ST_IDLE || s->state == SFRA_ST_DONE) {
        return (sfra_signal_t)0;
    }

    /* cos(θ) = sin(θ + 2^30)，2^30 就是四分之一圈 */
    q15c = sin_q15(s->theta_phase + 0x40000000u);
    c    = (float)q15c * Q15_TO_F;
    v    = s->cfg.amplitude * c;

    if (s->state == SFRA_ST_SETTLE) {
        /* settle 计数在 ISR 里递减——inject 是每周期唯一必经点。
         * 减到 0 就切 COLLECT（background 只负责兜底翻译）。 */
        if (s->settle_left > 0u) {
            s->settle_left--;
            if (s->settle_left == 0u) {
                s->state = SFRA_ST_COLLECT;
            }
        } else {
            s->state = SFRA_ST_COLLECT;
        }
    } else if (s->state == SFRA_ST_COLLECT) {
        if (s->cfg.mode == SFRA_MODE_LOOP && s->collected < s->data_count) {
            /* LOOP：注入复振幅与 Y 同一轮采集（D19 单 pass） */
            q15s     = sin_q15(s->theta_phase);
            s->inj_c = acc_mac(s->inj_c, v, q15c);
            s->inj_s = acc_mac(s->inj_s, v, q15s);
        }
    }
    return (sfra_signal_t)(v * (float)SFRA_ONE);
}

void sfra_advance(sfra_t *s)
{
    if (!s) {
        return;
    }
    /* Q32 相位自然回绕（uint32 加溢出即整圈取模），
     * 没有浮点漂移，也就不需要阈值归零。 */
    s->theta_phase += s->step_phase;
}

void sfra_monitor(sfra_t *s, sfra_signal_t in, sfra_signal_t out)
{
    int32_t q15c, q15s;
    float   y, x;

    if (!s || s->state != SFRA_ST_COLLECT) {
        return;
    }
    /* 采满即停（坑 A3）：调用方常会多跑几拍收尾，
     * 继续累加会让窗口尾部多出不属于整周期的点。 */
    if (s->collected >= s->data_count) {
        return;
    }

    /* cos(θ) = sin(θ + 2^30)，与 sfra_inject 的分工必须一致：
     * q15c 走 +2^30（余弦），q15s 走 θ（正弦）。写反了幅值对、
     * 相位整整差 90°——S2 第二次回归就是这么翻车的。 */
    q15c = sin_q15(s->theta_phase + 0x40000000u);
    q15s = sin_q15(s->theta_phase);

    y = sfra_sig_to_f(out);
    s->y_c = acc_mac(s->y_c, y, q15c);
    s->y_s = acc_mac(s->y_s, y, q15s);

    if (s->cfg.mode == SFRA_MODE_PLANT) {
        x = sfra_sig_to_f(in);
        s->x_c = acc_mac(s->x_c, x, q15c);
        s->x_s = acc_mac(s->x_s, x, q15s);
    }

    s->collected++;
}

/* =========================================================================
 * 主循环
 * ========================================================================= */

/* 装载第 idx 个频点：算几何、装 settle、清累加器。
 *
 * 注意相位 **不复位**（坑 A2）：
 * 整周期取整保证 data_count * step == cycles*2π，采集窗内恰好是整数个
 * 周期，起点相位任意都成立。强行复位反而会在注入波形上造一个相位台阶，
 * 频谱泄漏到整个频带（实测 20kHz 偏 +28dB / +128°）。
 */
static void sfra_load_point(sfra_t *s, uint16_t idx)
{
    float    foi = sfra_freq_at(&s->cfg, idx);
    uint32_t cyc = sfra_calc_cycles(foi, s->cfg.cycles_mode, s->cfg.isr_freq);
    float    rad = 0.0f;
    uint32_t dc  = sfra_point_geometry(s->cfg.isr_freq, foi, cyc, &rad);
    float    sec, per;

    s->point_idx   = idx;
    s->data_count  = dc;
    s->theta_step  = rad;
    s->step_phase  = sfra_rad_to_phase(rad);
    s->collected   = 0;
    sfra_clear_acc(s);

    sec = 0.020f;
    per = (foi > 0.0f) ? (2.0f / foi) : 0.0f;
    if (per > sec) {
        sec = per;
    }
    s->settle_left = (uint32_t)ceilf(sec * s->cfg.isr_freq);

    s->state = SFRA_ST_SETTLE;
}

/* 把当前累加器写进结果数组。
 * 定点路径在这里折算成 float 等效值，让 PC 端与浮点路径用同一套公式。 */
static void sfra_store_point(sfra_t *s, const sfra_result_t *res)
{
    uint16_t i = s->point_idx;
    uint32_t cyc;

    if (!res || !res->freq_vect) {
        return;
    }

    cyc = sfra_calc_cycles(sfra_freq_at(&s->cfg, i),
                           s->cfg.cycles_mode, s->cfg.isr_freq);
    res->freq_vect[i] = sfra_actual_freq(s->cfg.isr_freq, s->data_count, cyc);

    /* Q42 累加器 → pu。两条路径共用这一处换算。 */
    res->a_c[i] = (float)s->y_c * ACC_INV_SCALE;
    res->a_s[i] = (float)s->y_s * ACC_INV_SCALE;
    res->b_c[i] = (float)((s->cfg.mode == SFRA_MODE_LOOP) ? s->inj_c : s->x_c)
                  * ACC_INV_SCALE;
    res->b_s[i] = (float)((s->cfg.mode == SFRA_MODE_LOOP) ? s->inj_s : s->x_s)
                  * ACC_INV_SCALE;
}

void sfra_background_task(sfra_t *s, const sfra_result_t *res)
{
    if (!s) {
        return;
    }

    switch (s->state) {
    case SFRA_ST_SETTLE:
        /* settle 计数由 inject 递减（ISR 是唯一必经点）。
         * 这里只负责把"减到 0"翻译成 COLLECT。 */
        if (s->settle_left == 0u) {
            s->state = SFRA_ST_COLLECT;
        }
        break;

    case SFRA_ST_COLLECT:
        s->settle_left = 0;
        if (s->collected >= s->data_count) {
            s->state = SFRA_ST_NEXT;
        }
        break;

    case SFRA_ST_NEXT:
        sfra_store_point(s, res);
        if ((uint16_t)(s->point_idx + 1u) >= s->cfg.vec_length) {
            s->state = SFRA_ST_DONE;
        } else {
            sfra_load_point(s, (uint16_t)(s->point_idx + 1u));
        }
        break;

    default:
        break;
    }
}

/* settle 计数必须在 ISR 里递减，而 inject 是唯一必经点。
 * 为不破坏"inject 只做注入"的语义，这里给一个显式入口，
 * 由 inject 内部调用（见 sfra_inject 的 state 分支）。 */
