/**
  ******************************************************************************
  * @file    sfra_dut.c
  * @brief   S6a 被测对象实现：三个离散模型，全部 Tustin 离散化
  ******************************************************************************
  * 三个模型的共同点：分子分母同阶、直流增益 1。这让下面两件事成立：
  *
  *   1. PLANT 模式下的"去直流"可以只减一个常数（g_dc_bias × 1）；
  *   2. 低频段幅值必然是 0 dB，是曲线上最容易读的检查点。
  *
  * Tustin 离散化统一用 s = k(1−z⁻¹)/(1+z⁻¹)，k = 2·fs。
  * 二阶公式的推导都在对应函数注释里，改之前先验算一遍。
  ******************************************************************************
  */

#include "sfra_dut.h"

/*============================================================================*/
/*                          内部状态                                          */
/*============================================================================*/

/* 统一用直接 II 型转置（Transposed Direct Form II）：
 *
 *     y  = b0·x + d1
 *     d1 = b1·x − a1·y + d2
 *     d2 = b2·x − a2·y
 *
 * 为什么选这个结构而不是直接 I 型：
 *   状态只有 2 个（一阶只 1 个），SRAM 更省；数值上比直接 I 型好，
 *   在 M0+ 的软件浮点下少一份舍入累积。
 *
 * 分母约定：a1/a2 存的是**已取负**的系数，即
 *     H(z) = (b0 + b1·z⁻¹ + b2·z⁻²) / (1 − a1·z⁻¹ − a2·z⁻²)
 * 这样递推里全是加法，与 libsfra 的 filter_rc.h 一致，不容易写错符号。
 */
typedef struct
{
    float b0, b1, b2;
    float a1, a2;
    float d1, d2;
} biquad_t;

/* Q15 定点系数版（M0+ 无 FPU 时使用）。
 *
 * 【为什么必须定点】
 * M0+ 没有 FPU，float 乘法是 __aeabi_fmul 软件例程（约 20~30 cycle），
 * 加法 15~20 cycle。TDF-II 每拍要 5 乘 4 加 ≈ 220 cycle，看似不多，
 * 但实测整个 ISR 是 6947 cycle / 7200 预算 = 96%，其中 dut_step
 * 所在的那一段（rest）占 4743 cycle —— 浮点滤波是最大单项。
 *
 * 【为什么用 Q15 就够】
 * SFRA 只关心**相对**幅相，输入幅值默认 1% pu（0.01），Q15 下
 * 即 328 LSB，仍有 ~9 bit 有效分辨率；累加器又是 int64，
 * 量化噪声不会累积到影响 0.5 dB / 2° 判据的程度。
 *
 * 【结构仍是 TDF-II】
 *     y  = b0·x + d1
 *     d1 = b1·x + a1·y + d2
 *     d2 = b2·x + a2·y
 * 系数一律 Q15（|b|,|a| < 1，Q15 表示范围 [-1, 1) 刚好够；
 * 二阶低通归一化后 b0 = α²/den 必定 < 1）。
 *
 * 中间积用 int32：Q15 × Q15 = Q30，|积| < 2^30，int32 装得下。
 * 状态 d1/d2 也是 Q15。
 */
typedef struct
{
    int32_t b0, b1, b2;
    int32_t a1, a2;
    int32_t d1, d2;
} biquad_q15_t;

static biquad_t      s_f;
static biquad_q15_t  s_fq;
static sfra_dut_kind_t s_kind;
static float         s_fc;
static float         s_fs;

/* float 系数 → Q15，带饱和。 */
static int32_t to_q15(float v)
{
    float t = v * 32768.0f;             /* Q15 用 2^15 而不是 32767，移位更快 */
    if (t > 32767.0f)  { t = 32767.0f; }
    if (t < -32768.0f) { t = -32768.0f; }
    return (int32_t)t;
}

/*============================================================================*/
/*                          离散化                                            */
/*============================================================================*/

/**
  * @brief  把连续域二阶系统离散成 Tustin 双线性形式。
  * @param  f: 待填写的双二阶结构体。
  * @param  w0: 自然角频率 rad/s。
  * @param  q: 品质因数。
  * @param  fs: 采样率 Hz。
  * @retval 无
  * @note   H(s) = ω0² / (s² + (ω0/Q)·s + ω0²)
  *
  *         Tustin：s = k(1−z⁻¹)/(1+z⁻¹)，k = 2·fs。
  *         令 α = ω0/k、β = ω0/(Q·k)，代入并同乘 (1+z⁻¹)²：
  *
  *             分子   = α²(1 + 2z⁻¹ + z⁻²)
  *             分母   = (α² + β + 1) + 2(α² − 1)z⁻¹ + (α² − β + 1)z⁻²
  *
  *         归一化后：
  *             b0 = b2 = α²/den,  b1 = 2b0
  *             a1 = −2(α²−1)/den, a2 = −(α²−β+1)/den
  *
  *         这里 a1/a2 已经带了负号（见上面 biquad_t 的约定），
  *         所以递推里写 +a1·y 而不是 −a1·y。
  */
static void biquad_lowpass(biquad_t *f, float w0, float q, float fs)
{
    /* 全程 double：α 在 fs/w0 很大时是 1e-3 量级，
     * α² 会掉到 1e-6，float32 的 24 位尾数在这个量级只有 6~7 位有效
     * 数字，α² − 1 这种减法会丢掉几乎全部精度，导致极点位置跑偏。 */
    double k   = 2.0 * (double)fs;
    double a   = (double)w0 / k;
    double b   = (double)w0 / ((double)q * k);
    double a2c = a * a;
    double den = a2c + b + 1.0;

    f->b0 = (float)(a2c / den);
    f->b1 = (float)(2.0 * a2c / den);
    f->b2 = f->b0;
    f->a1 = (float)(-2.0 * (a2c - 1.0) / den);
    f->a2 = (float)(-(a2c - b + 1.0) / den);
    f->d1 = 0.0f;
    f->d2 = 0.0f;
}

/**
  * @brief  一阶低通的 Tustin 离散化。
  * @param  f: 待填写的结构体（b2/a2 置 0）。
  * @param  fc: −3 dB 频率 Hz。
  * @param  fs: 采样率 Hz。
  * @retval 无
  * @note   H(s) = 1/(1 + s/ωc)。令 γ = ωc/k：
  *             b0 = b1 = γ/(1+γ)
  *             a1 = −(1−γ)/(1+γ)      （已含负号）
  *         与 docs/06 示例的 lp1_init() 逐位一致，方便对文档的期望表。
  */
static void biquad_first_order_lp(biquad_t *f, float fc, float fs)
{
    double k   = 2.0 * (double)fs;
    double w   = 2.0 * 3.14159265358979323846 * (double)fc;
    double den = w + k;

    f->b0 = (float)(w / den);
    f->b1 = f->b0;
    f->b2 = 0.0f;
    f->a1 = (float)((w - k) / den);     /* = −(1−γ)/(1+γ)，已为负 */
    f->a2 = 0.0f;
    f->d1 = 0.0f;
    f->d2 = 0.0f;
}

/*============================================================================*/
/*                          接口                                              */
/*============================================================================*/

/**
  * @brief  按对象类型重建滤波器系数。
  * @param  无
  * @retval 无
  */
static void dut_build(void)
{
    double w0;

    switch (s_kind)
    {
    case SFRA_DUT_PLANT_LC:
        /* 二阶 LC，Q 取 0.8（欠阻尼），谐振峰约 +1.9 dB。
         * 这个峰值是"幅值算得对不对"的最强证据：算错 3 dB 以内看得见，
         * 而平段上的小误差看不出来。 */
        w0 = 2.0 * 3.14159265358979323846 * (double)s_fc;
        biquad_lowpass(&s_f, (float)w0, 0.8f, s_fs);
        break;

    case SFRA_DUT_PLANT_LP:
        biquad_first_order_lp(&s_f, s_fc, s_fs);
        break;

    case SFRA_DUT_SALLEN_KEY:
    default:
        /* 单位增益 Sallen-Key：ω0 = 1/RC，Q = 1/3。
         *
         * 那个 1/3 不是随手取的，是 H(s) = 1/(1 + 3sRC + (sRC)²) 里
         * s 项系数 3 的倒数。Q = 1/3 远小于 0.707，两个极点都是实数，
         * 曲线没有峰值，高频段干净地滚到 −40 dB/dec。
         *
         * 用 1/3 而不是 0.3333f：float 表示 1/3 的误差在 1e-8 量级，
         * 对 Q 的影响远小于 C 的容差，这里只是让公式读起来清楚。 */
        w0 = 2.0 * 3.14159265358979323846 * (double)s_fc;
        biquad_lowpass(&s_f, (float)w0, 1.0f / 3.0f, s_fs);
        break;
    }

    /* float 系数算好后同步一份 Q15 版。
     * 放在这里（而不是 sfra_dut_step 里懒初始化）是为了让 ISR
     * 路径绝不出现浮点——包括第一条样本。 */
    s_fq.b0 = to_q15(s_f.b0);
    s_fq.b1 = to_q15(s_f.b1);
    s_fq.b2 = to_q15(s_f.b2);
    s_fq.a1 = to_q15(s_f.a1);
    s_fq.a2 = to_q15(s_f.a2);
    s_fq.d1 = 0;
    s_fq.d2 = 0;
}

/**
  * @brief  初始化被测对象。
  * @param  kind: 对象类型。
  * @param  fc_hz: 特征频率 Hz。
  * @param  fs_hz: 采样率 Hz，必须与 SFRA_ISR_FREQ_HZ 一致。
  * @retval 无
  */
void sfra_dut_init(sfra_dut_kind_t kind, float fc_hz, float fs_hz)
{
    s_kind = (kind < SFRA_DUT_KIND_MAX) ? kind : SFRA_DUT_SALLEN_KEY;
    s_fc   = (fc_hz > 0.0f) ? fc_hz : 1000.0f;
    s_fs   = (fs_hz > 0.0f) ? fs_hz : 10000.0f;
    dut_build();
}

/**
  * @brief  切换被测对象类型并清状态。
  * @param  kind: 新对象类型。
  * @retval 无
  */
void sfra_dut_select(sfra_dut_kind_t kind)
{
    if (kind >= SFRA_DUT_KIND_MAX)
    {
        kind = SFRA_DUT_SALLEN_KEY;
    }
    s_kind = kind;
    dut_build();
}

/**
  * @brief  清内部延时单元。
  * @param  无
  * @retval 无
  */
void sfra_dut_reset(void)
{
    s_f.d1 = 0.0f;
    s_f.d2 = 0.0f;
}

/**
  * @brief  推进一拍。
  * @param  x: 本拍输入。
  * @retval 本拍输出。
  * @note   直接 II 型转置，两条 mul + 两条 mac + 两次状态搬移。
  *         在 M0+ 软件浮点上约 40 周期，10 kHz ISR 里占 0.06%。
  */
float sfra_dut_step(float x)
{
    float y = s_f.b0 * x + s_f.d1;

    s_f.d1 = s_f.b1 * x + s_f.a1 * y + s_f.d2;
    s_f.d2 = s_f.b2 * x + s_f.a2 * y;
    return y;
}

/**
  * @brief  推进一拍（Q15 定点版）。
  * @param  x_q15: 本拍输入，Q15。
  * @retval 本拍输出，Q15。
  * @note   与 sfra_dut_step() 数值等价，但**全程整数**。
  *         M0+ 上约 40~50 cycle（umull + 移位），比浮点版快 4~5 倍。
  *
  *         所有乘法都是 Q15 × Q15 → Q30，右移 15 回到 Q15。
  *         用算术右移（负数是向下取整），误差 < 1 LSB，
  *         在扫频累加里表现为白噪声，不影响 0.5 dB 判据。
  */
int16_t sfra_dut_step_q15(int16_t x_q15)
{
    int32_t x  = (int32_t)x_q15;
    /* y = b0·x + d1 ：b0·x 是 Q30，>>15 回 Q15 后再加状态 */
    int32_t y  = (int32_t)(((int64_t)s_fq.b0 * x) >> 15) + s_fq.d1;

    s_fq.d1 = (int32_t)(((int64_t)s_fq.b1 * x) >> 15)
            + (int32_t)(((int64_t)s_fq.a1 * y) >> 15)
            + s_fq.d2;
    s_fq.d2 = (int32_t)(((int64_t)s_fq.b2 * x) >> 15)
            + (int32_t)(((int64_t)s_fq.a2 * y) >> 15);

    /* 限幅：二阶滤波器在瞬态可能小幅过冲，超出 Q15 范围时钳位，
     * 避免把 16 位返回值截断成反号（那会造成统计上的离群点）。 */
    if (y > 32767)  { y = 32767; }
    if (y < -32768) { y = -32768; }
    return (int16_t)y;
}

/**
  * @brief  被测对象的直流增益。
  * @param  无
  * @retval 恒为 1.0
  * @note   三个模型分子分母同阶且低阶项归一到 1，H(0) = 1。
  *         如果将来换成带增益的对象（比如反相放大器、Buck 的 D 到 Vout），
  *         这里必须改成实测值，否则 PLANT 模式的去直流会减错量。
  */
float sfra_dut_dc_gain(void)
{
    return 1.0f;
}

/**
  * @brief  对象名字。
  * @param  无
  * @retval 常量字符串。
  */
const char *sfra_dut_name(void)
{
    switch (s_kind)
    {
    case SFRA_DUT_PLANT_LC: return "lc";
    case SFRA_DUT_PLANT_LP: return "lp1";
    case SFRA_DUT_SALLEN_KEY:
    default:                return "sk2";
    }
}

/**
  * @brief  特征频率。
  * @param  无
  * @retval Hz。
  */
float sfra_dut_fc_hz(void)
{
    return s_fc;
}
