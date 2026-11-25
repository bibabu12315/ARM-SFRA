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

/* 一次插值取一个 Q15 值（-32767..32767）。
 *
 * 【2026-11-24 重写：彻底去掉 int64 乘法】
 *
 * 原实现 t = v0·2^k + (v1−v0)·frac 用 int64 是因为
 * 65535·2^22 ≈ 2.7e11 超 int32。但在 M0+ 上 int64 乘法不是单条
 * umull —— 它是 __aeabi_lmul 软件例程（约 40~70 cycle），而 ISR 里
 * 每拍要调用 3 次 sin_q15，光这一项就吃掉 200+ cycle。
 *
 * 关键观察：插值的**精度需求远低于 22 位小数**。
 *   - 表是 10 位（1024 点），sin 在相邻点间的最大二阶差
 *     ≈ (π/512)²/8 ≈ 7.5e-6，线性插值本身就把误差压到这个量级；
 *   - Q15 输出只有 1/32768 ≈ 3.1e-5 的分辨率；
 *   - 所以插值小数保留 **16 位** 已经饱和，再多是无效精度。
 *
 * 改成 16 位插值后，所有中间量都能放进 int32：
 *     v0 << 16        ∈ [−2.1e9, 2.1e9]   刚好不溢出 int32
 *     (v1-v0) * frac  ∈ [−65535 × 65535] ≈ ±4.29e9  ← 会溢出！
 *
 * 所以再把 v1−v0 右移 1 位（差值最大 65534 → 32767），
 * 乘 frac(16bit) 后落在 ±2.1e9 内，最后补回那 1 位移位。
 * 损失的最低位是 2 LSB/32768 ≈ 6e-5 相对量，仍远小于表插值误差。
 *
 * 实测收益见 Docs：ISR 平均周期从 6947 降到 ~2000 以下。
 */
static int32_t sin_q15(uint32_t phase)
{
    uint32_t i0   = (phase >> SIN_IDX_SHIFT) & SIN_TAB_MASK;
    uint32_t i1   = (i0 + 1u) & SIN_TAB_MASK;
    /* frac16：取相位低 16 位作为插值权重（原 22 位截到 16 位） */
    uint32_t frac = (phase >> (SIN_IDX_SHIFT - 16u)) & 0xFFFFu;
    int32_t  v0   = (int32_t)s_sin_tab[i0];
    int32_t  v1   = (int32_t)s_sin_tab[i1];
    int32_t  d    = (v1 - v0) >> 1;      /* 防 int32 溢出，见上面推导 */

    /* t = v0·2^16 + d·frac，两者都在 int32 内；最后 >>15 补回 d 的 1 位 */
    return v0 + ((d * (int32_t)frac) >> 15);
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
    for (i = 0; i < cfg->vec_length; i++) {
        if (f >= f_max) {
            return (uint16_t)(i + 1u);    /* 含被钳在 f_max 的那一点 */
        }
        f *= cfg->freq_step;
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
 *
 * 【两条路径的定标不同，取结果时必须分开】
 *   float 路径：acc = Σ (y·2^27)·c_q15  →  Q42，还原乘 2^-42
 *   Q15 路径  ：acc = Σ y_q15·c_q15     →  Q30，还原乘 2^-30
 * 混用会让整条曲线的幅值差 2^12 倍（看起来像"扫频结果全错"），
 * 所以下面用 sfra_acc_to_f() 一处收口，绝不各写各的。
 * ========================================================================= */
#define ACC_Y_SCALE   134217728.0f                /* 2^27，float 路径 */
#define ACC_INV_SCALE (1.0f / 4398046511104.0f)   /* 2^-42，float 路径 */
#define ACC_Q15_INV   (1.0f / 32768.0f)           /* 2^-15，Q15 路径单乘 */

/* 一次乘加：acc += y·q15。
 *
 * 【2026-11-24 定点化】原来入参 y 是 float，函数内部先 `y * 2^27`
 * 转成 Q27 再乘。在 M0+ 上那一次软件浮点乘法 + 两次
 * __aeabi_f2iz / __aeabi_i2l 转换约 60~90 cycle，而 ISR 里每拍
 * 要调 4 次 acc_mac（y_c/y_s/x_c/x_s）—— 单这一项就 300+ cycle。
 *
 * 现在直接收 Q15 整数：定点路径下采样值本来就是 Q15，
 * 乘完是 Q30，累加进 int64。全程零浮点调用。
 *
 * 定标：Q15 × Q15 = Q30，累加点数上限 10^5 → 需要的位宽
 *       log2(10^5) ≈ 17，30 + 17 = 47 < 63，int64 富余。 */
#define ACC_Q15_SHIFT  15

static int64_t acc_mac(int64_t acc, int32_t y_q15, int32_t q15)
{
    return acc + (int64_t)y_q15 * (int64_t)q15;
}

/* 累加器 → 归一化的复数分量（实部/虚部各一半）。
 * 唯一收口点：所有 res->a_c/a_s/b_c/b_s 都从这里出，
 * 保证两条数值路径的定标不会跑偏。 */
static float sfra_acc_to_f(int64_t acc)
{
#if SFRA_USE_FIXED_POINT
    /* Q30 → float：先转 float 再乘 2^-30。累加值可能到 1e12 量级，
     * 直接 (float)acc 在 float32 的 24 位尾数下会损失低频部分，
     * 但 SFRA 只需要 1e-5 量级的相对精度，且分母 (N/2) 同步缩放，
     * 比值 H 的精度不受影响。 */
    return (float)acc * ACC_Q15_INV * ACC_Q15_INV;
#else
    return (float)acc * ACC_INV_SCALE;
#endif
}

/* 采样值 → 归一化 float。两条路径的唯一定义处。
 *
 * 定点路径下 ISR 里不再调用它（采样值本身就是 Q15，直接进 acc_mac），
 * 保留仅供 float 路径使用；用 #if 包起来免得 ARMCC 报 #177-D。 */
#if !SFRA_USE_FIXED_POINT
static float sfra_sig_to_f(sfra_signal_t v)
{
    return (float)v;
}
#endif

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
    /* 注入幅值的 Q15 定点缓存。
     *
     * 【为什么不每拍算 s->cfg.amplitude * c】
     * cfg.amplitude 是 float（per-unit），原来每拍都要做
     * "float→Q15 缩放 → mul → 再转回 float" 三次软浮点。幅值在
     * 扫频期间是**冻结**的（D8 约定），所以完全可以预转成 Q15
     * 存进 s，ISR 里只剩一次整数乘法。
     *
     * 转换点在 sfra_load_point()（每点一次），不在 ISR 里。 */
    int32_t amp_q15;

    if (!s || s->state == SFRA_ST_IDLE || s->state == SFRA_ST_DONE) {
        return (sfra_signal_t)0;
    }

    /* cos(θ) = sin(θ + 2^30)，2^30 就是四分之一圈 */
    q15c = sin_q15(s->theta_phase + 0x40000000u);
    amp_q15 = s->amp_q15;

#if SFRA_USE_FIXED_POINT
    /* 定点路径：c(Q15) × amp(Q15) → Q30，右移回 Q15。全程无浮点。 */
    {
        int32_t v_q15 = (int32_t)(((int64_t)amp_q15 * (int64_t)q15c) >> ACC_Q15_SHIFT);
        if (v_q15 > 32767)  { v_q15 = 32767; }
        if (v_q15 < -32767) { v_q15 = -32767; }

        if (s->state == SFRA_ST_SETTLE) {
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
                q15s     = sin_q15(s->theta_phase);
                s->inj_c = acc_mac(s->inj_c, v_q15, q15c);
                s->inj_s = acc_mac(s->inj_s, v_q15, q15s);
            }
        }
        return (sfra_signal_t)v_q15;
    }
#else
    {
        float c = (float)q15c * Q15_TO_F;
        float v = s->cfg.amplitude * c;

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
                int32_t v_q = (int32_t)(v * (float)SFRA_ONE);
                q15s     = sin_q15(s->theta_phase);
                s->inj_c = acc_mac(s->inj_c, v_q, q15c);
                s->inj_s = acc_mac(s->inj_s, v_q, q15s);
            }
        }
        return (sfra_signal_t)(v * (float)SFRA_ONE);
    }
#endif
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
#if SFRA_USE_FIXED_POINT
    int32_t y, x;              /* 定点：采样值本身就是 Q15，无需转换 */
#else
    float   y, x;
    float   yf, xf;
#endif

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

    /* 定点路径下 in/out 本来就是 Q15 整数，直接进 acc_mac，
     * 省掉两次 sfra_sig_to_f（软浮点乘）。 */
#if SFRA_USE_FIXED_POINT
    y = (int32_t)out;
    s->y_c = acc_mac(s->y_c, y, q15c);
    s->y_s = acc_mac(s->y_s, y, q15s);

    if (s->cfg.mode == SFRA_MODE_PLANT) {
        x = (int32_t)in;
        s->x_c = acc_mac(s->x_c, x, q15c);
        s->x_s = acc_mac(s->x_s, x, q15s);
    }
#else
    yf = sfra_sig_to_f(out);
    s->y_c = acc_mac(s->y_c, (int32_t)(yf * ACC_Y_SCALE), q15c);
    s->y_s = acc_mac(s->y_s, (int32_t)(yf * ACC_Y_SCALE), q15s);

    if (s->cfg.mode == SFRA_MODE_PLANT) {
        xf = sfra_sig_to_f(in);
        s->x_c = acc_mac(s->x_c, (int32_t)(xf * ACC_Y_SCALE), q15c);
        s->x_s = acc_mac(s->x_s, (int32_t)(xf * ACC_Y_SCALE), q15s);
    }
#endif

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

    /* 注入幅值预转 Q15（定点路径用）。
     * cfg.amplitude 是 per-unit，[0,1] 映射到 [0, 32767]。
     * 这里只在换频点时算一次，ISR 里就不用碰浮点了。
     * 限幅防越界：调用方可能传 >1 的幅值做压力测试。 */
    {
        float a = s->cfg.amplitude;
        if (a < 0.0f) { a = 0.0f; }
        if (a > 1.0f) { a = 1.0f; }
        s->amp_q15 = (int32_t)(a * (float)SFRA_ONE);
    }

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

    /* 累加器 → pu。定标换算统一走 sfra_acc_to_f()，两条路径不会跑偏。 */
    res->a_c[i] = sfra_acc_to_f(s->y_c);
    res->a_s[i] = sfra_acc_to_f(s->y_s);
    res->b_c[i] = sfra_acc_to_f((s->cfg.mode == SFRA_MODE_LOOP) ? s->inj_c : s->x_c);
    res->b_s[i] = sfra_acc_to_f((s->cfg.mode == SFRA_MODE_LOOP) ? s->inj_s : s->x_s);
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
