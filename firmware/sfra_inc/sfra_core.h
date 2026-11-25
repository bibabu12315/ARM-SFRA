/* ---------------------------------------------------------------------------
 * sfra_core.h - SFRA 扫频算法核心（纯 C99，零外部依赖）
 *
 * 与 Python 参考实现 src/app/sim/sfra_sim.py 逐行等价。
 * S2 阶段在 PC 上用 gcc 编译，喂相同激励序列，比对累加器原值。
 *
 * 设计边界（00 文档 D5 / D18）：
 *   - 本库**不做任何幅相运算**：不算 atan2、不算 log10、不算 abs(sqrt)。
 *     只累加 4~6 个相关量，幅相全部在 PC 端 bode_data.py 里算。
 *   - 本库**不碰任何 HAL**：PWM/ADC/UART 全在用户代码里。
 *   - 用户只需在 ISR 里插两行：inj = sfra_inject(); sfra_monitor(in, out);
 *
 * 时序约定（00 文档 D23，S1 阶段踩了 8 个坑总结出来的）：
 *   1. theta 必须在 sfra_monitor() **之后**推进（用 sfra_advance()）
 *   2. 采集窗起点**不要复位 theta**（整周期取整已保证窗内是整数个周期）
 *   3. 采满 data_count 点后 monitor() 立即停手
 *   4. 闭环系统一拍计算延迟由用户的调用顺序保证（见 sfra_port.h 注释）
 * ------------------------------------------------------------------------- */
#ifndef SFRA_CORE_H
#define SFRA_CORE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------- 版本 */
#define SFRA_VERSION_MAJOR 1
#define SFRA_VERSION_MINOR 0

/* ---------------------------------------------------------------- 模式 */
#define SFRA_MODE_PLANT 0
#define SFRA_MODE_LOOP  1

/* ---------------------------------------------------------------- 周期数档位 */
#define SFRA_CYC_FAST    0
#define SFRA_CYC_NORMAL  1
#define SFRA_CYC_PRECISE 2

/* ---------------------------------------------------------------- 状态机 */
#define SFRA_ST_IDLE      0
#define SFRA_ST_SETTLE    1
#define SFRA_ST_COLLECT   2
#define SFRA_ST_NEXT      3
#define SFRA_ST_DONE      4

/* ---------------------------------------------------------------- 正弦表精度
 *
 * SFRA_SIN_TAB_BITS 决定正弦表大小 2^N 点。
 *
 * 线性插值误差 ≈ (π/N_max)^2 / 8，N_max = 每象限点数 = 2^N/4：
 *   N=8  (256 点) ：1.2e-4  →  -78 dB，M0+ 省 RAM 时用
 *   N=10 (1024 点)：7.5e-6  →  -102 dB，**默认**，满足 S2 的 1e-5 判据
 *   N=12 (4096 点)：4.7e-7  →  -126 dB，M4F 追求极限精度时用
 *
 * RAM 占用 = (2^N + 1) × 2 字节：256→514B，1024→2KB，4096→8KB。
 * 表可以放 flash（const），这时只占 flash 不占 RAM。
 */
#ifndef SFRA_SIN_TAB_BITS
#define SFRA_SIN_TAB_BITS 10
#endif

/* ---------------------------------------------------------------- 数学路径
 *
 * SFRA_USE_FIXED_POINT = 0 (默认)：float32 路径，M4F/M7 等带 FPU 的片子用。
 * SFRA_USE_FIXED_POINT = 1      ：Q15 定点路径，M0/M0+ 用。
 *
 * 定点路径的累加器必须用 int64（10^5 点 × int32 × 2^15 已达 2^52，
 * int32 必溢出，见 00 文档 0.6 风险表）。
 */
#ifndef SFRA_USE_FIXED_POINT
#define SFRA_USE_FIXED_POINT 0
#endif

#if SFRA_USE_FIXED_POINT
typedef int16_t sfra_signal_t;      /* Q15：[-1, 1) 映射到 [-32768, 32767] */
typedef int32_t sfra_acc_t;         /* 单次乘加结果，int16*int16 */
#define SFRA_ONE 32767
#else
typedef float   sfra_signal_t;
typedef float   sfra_acc_t;
#define SFRA_ONE 1.0f
#endif

/* ---------------------------------------------------------------- 配置
 *
 * 频率单位一律 Hz，幅值一律 per-unit（0,1）。不要把占空比算成 0~10000
 * 的整数——全链路 pu 归一化是 00 文档 D15 的硬约定。
 */
typedef struct {
    float    isr_freq;     /* 控制 ISR 频率 Hz，也是采样率 f_s */
    float    freq_start;   /* 扫频起点 Hz */
    float    freq_step;    /* 每点倍数 = 10^(1/pts_per_dec)，>1 递增 */
    float    amplitude;    /* 注入幅值 pu，扫频期间冻结（D8） */
    uint16_t vec_length;   /* 频点个数（调用方保证 freq 表长度 >= 此值） */
    uint8_t  cycles_mode;  /* SFRA_CYC_* */
    uint8_t  mode;         /* SFRA_MODE_* */
} sfra_setup_t;

/* ---------------------------------------------------------------- 结果
 *
 * 每频点 5 个 float：实际频率 + 4 个累加器原值。
 * 调用方自己分配 5 个长度为 vec_length 的数组。
 *
 * b_c/b_s 的含义随模式变化：
 *   PLANT：X(in) 的累加器        → H = Y/X
 *   LOOP ：Inj 的累加器            → GH = Y/(Inj−Y)
 */
typedef struct {
    float *freq_vect;   /* 输出：实际频率 f_act（不是名义值，D7） */
    float *a_c;         /* 输出：Σ y·cosθ */
    float *a_s;         /* 输出：Σ y·sinθ */
    float *b_c;         /* 输出：PLANT=Σ x·cosθ | LOOP=Σ inj·cosθ */
    float *b_s;         /* 输出：PLANT=Σ x·sinθ | LOOP=Σ inj·sinθ */
} sfra_result_t;

/* ---------------------------------------------------------------- 内部状态 */
typedef struct {
    /* --- 配置快照 --- */
    sfra_setup_t cfg;

    /* --- 相位 ---
     *
     * theta_phase 是 Q32 归一化相位：整圈 = 2^32，也就是 sin 表索引
     * 直接取高 SFRA_SIN_TAB_BITS 位，**不需要任何乘除和取模**。
     *
     * 为什么不用 float 弧度累加（S2 交叉验证踩到的坑）：
     *   100kHz/100Hz 一个频点要累加 1e5 次，θ 长到 6.3e2，
     *   float32 在那个量级的 ulp 是 6.1e-5 rad，而步进本身只有
     *   6.28e-3 —— 每拍误差 1%，累积几十拍后相位就飘到 30° 以外。
     *   更糟的是 theta_step 只有在"整周期取整"后才是精确的
     *   cycles/N·2π，float32 存下来又丢 1e-7 相对量。
     *   Q32 定点把整圈量化成 2^32 份，最小步进 2^-32 圈，
     *   100kHz/200kHz 下每个点也只要几十拍，累积漂移 < 1e-9 圈。
     *
     * 换算：theta_phase = θ_rad × 2^32 / 2π  （在 sfra_load_point 里一次算好）
     */
    uint32_t theta_phase;   /* 归一化相位 [0, 2^32) */
    uint32_t step_phase;    /* 每采样点相位增量（同一单位） */
    /* 保留弧度步进仅供 PC 端打印/核对，不参与运算 */
    float    theta_step;

    /* --- 状态机 --- */
    uint8_t  state;
    uint16_t point_idx;     /* 当前频点序号 0 .. vec_length-1 */
    uint32_t settle_left;   /* 还要静默多少拍 */
    uint32_t data_count;    /* 本频点要采多少点 */
    uint32_t collected;     /* 已采点数 */

    /* --- 累加器 ---
     *
     * 一律 int64，**不用 double**（坑 A11 的延伸）：
     *   M4F 没有硬件双精度，`acc += (double)y * (double)c` 会展开成
     *   __aeabi_f2d / __aeabi_dmul / __aeabi_dadd 三条软浮点调用，
     *   单拍约 90 cycle，100kHz ISR 里占 0.9%、200kHz 占 1.8%。
     *   改成 int64 后每拍只有 umull + adds + adc，约 6 cycle。
     *
     * 也不用 float 累加：10^5 点累加误差可达 1e-3 相对量（float32 尾数
     * 只有 24 位），会污染 60dB 以下的点。
     *
     * 定点含义（Q27）：
     *   输入 y 是 [-1,1] 的 float，乘 2^27 后取整存进 int32，
     *   再与 Q15 的 cos/sin 相乘，得到 Q42 的积，累加进 int64。
     *   量程：|y|·2^27 ≤ 1.34e8，|c|·32767 ≤ 32767，
     *         积 ≤ 4.4e12；10^6 点累加 ≤ 4.4e18 ≈ 2^62，int64 装得下。
     *   读出时除以 2^42 回到 pu（见 sfra_store_point）。
     */
    int64_t y_c, y_s, x_c, x_s, inj_c, inj_s;
} sfra_t;

/* ---------------------------------------------------------------- 生命周期 */

/* 建立正弦表。必须在任何其他调用之前、且只调一次（可在 main 里调）。 */
void sfra_init_all(void);

/* 初始化一个实例（不分配内存，用户自己提供 sfra_result_t 的数组） */
void sfra_init(sfra_t *s, const sfra_setup_t *cfg, const sfra_result_t *res);

/* 开始扫频（非阻塞）。返回 0 成功，非 0 为参数错误码。 */
int  sfra_start(sfra_t *s);

/* 立即停止。安全第一优先级：先把相位幅度置 0，再改状态。 */
void sfra_abort(sfra_t *s);

uint8_t sfra_is_running(const sfra_t *s);
uint8_t sfra_is_done(const sfra_t *s);
uint8_t sfra_state(const sfra_t *s);

/* 取走全部结果后清 DONE，允许下一轮 start */
void sfra_clear_done(sfra_t *s);

/* ---------------------------------------------------------------- ISR 内 */

/* 返回本拍注入值 A·cosθ。内部按需累加 Inj（LOOP 模式、采集中）。 */
sfra_signal_t sfra_inject(sfra_t *s);

/* 推进相位。**必须**在 sfra_monitor() 之后调用，每个 ISR 周期恰好一次。
 *
 * 单独拆出来的原因（S1 踩过的坑 A1）：
 *   若在 inject() 内部先加 step，monitor() 用的就是一个周期之后的 θ，
 *   整条曲线被强行加了 360/data_count 度的相位偏移（低频 −6°、
 *   12 点/周期时 −30°）。这是最容易抄错的一行。
 */
void sfra_advance(sfra_t *s);

/* 采集一拍。
 *   PLANT：累加 Y(out) 与 X(in)
 *   LOOP ：只累加 Y(out)，同时累加 Inj（由 inject 完成）
 * 采满 data_count 点后自动停手（坑 A3）。
 */
void sfra_monitor(sfra_t *s, sfra_signal_t in, sfra_signal_t out);

/* ---------------------------------------------------------------- 主循环 */

/* 推进状态机：SETTLE→COLLECT→NEXT→...→DONE，并在 NEXT 时把结果写进 res。
 *
 * res 只在 sfra_store_point 时被读，指针由调用方在每次 background_task
 * 里传入（允许用户把结果数组放在外部 RAM 或换 buffer）。 */
void sfra_background_task(sfra_t *s, const sfra_result_t *res);

/* ---------------------------------------------------------------- 纯计算工具
 *
 * 暴露出来是为了 PC 交叉验证能逐函数比对，以及上位机估算耗时。
 */

/* 选周期数（对齐 libsfra calc_cycles，三档 + 每周期采样点下限） */
uint32_t sfra_calc_cycles(float foi_hz, uint8_t cyc_mode, float isr_freq);

/* 整周期取整：返回 data_count，*out_rad 得到角步进。
 * 保证 data_count * rad == cycles * 2π（精确），泄漏被消掉。 */
uint32_t sfra_point_geometry(float isr_freq, float foi_hz, uint32_t cycles,
                             float *out_rad);

/* f_act = cycles / data_count * f_s */
float sfra_actual_freq(float isr_freq, uint32_t data_count, uint32_t cycles);

/* 名义频率表第 i 个点：freq_start * freq_step^i，**已含奈奎斯特钳制**
 * （上限 isr_freq/5，越界后钉在上限不再增长）。 */
float sfra_freq_at(const sfra_setup_t *cfg, uint16_t i);

/* 实际有效点数：从 0 数到第一次触顶（含触发点）。
 * PC 端进度条与"扫完了吗"都用它，别用 cfg.vec_length。 */
uint16_t sfra_effective_points(const sfra_setup_t *cfg);

#if SFRA_USE_FIXED_POINT
/* 定点结果的换算：累加器原值 → float 幅度/相位不便在此处算，
 * 提供"定点累加器 → float 等效复数分量"的纯换算，方便 PC 比对。 */
void sfra_fixed_to_float(int64_t c, int64_t s, float *out_c, float *out_s);
#endif

#ifdef __cplusplus
}
#endif

#endif /* SFRA_CORE_H */
