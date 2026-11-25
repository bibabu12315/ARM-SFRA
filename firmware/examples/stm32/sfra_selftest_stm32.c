/* ---------------------------------------------------------------------------
 * sfra_selftest_stm32.c - S6a 上板自测示例（L2 gate）
 *
 * ===========================================================================
 * 这个文件是干什么的
 * ===========================================================================
 *
 * S6a 的验收标准（见 00 文档 §0.5.4 与 04 文档 §4.4）：
 *
 *   1. 扫一个**已知答案**的对象（一阶低通），Bode 与解析解误差 < 1 dB / 3°；
 *   2. 相位必须 0 → −90°（符号错就是 180° 全反，一眼看出来）；
 *   3. 用 DWT->CYCCNT 实测 inject+monitor 的周期数，折算成 ISR 占用率；
 *   4. 定点/浮点在真芯片上都不溢出（int64 累加器最坏情况）。
 *
 * 所以这个文件**故意不接功率级**：它把一个一阶低通塞进 ISR 当被测对象，
 * 闭环跑完整的"上位机 → 协议 → 扫频 → 回数据"链路。
 * 功率级测试是 S6b，另一个文件。
 *
 * ===========================================================================
 * 为什么用一阶低通当被测对象
 * ===========================================================================
 *
 * 因为它有闭式解，不需要任何模型拟合就能判对错：
 *
 *     H(s) = 1 / (1 + s/ωc),  ωc = 2π·fc
 *     |H(f)| = 1 / √(1 + (f/fc)²)          → fc 处 −3 dB，之后 −20 dB/dec
 *     ∠H(f)  = −arctan(f/fc)               → 0° → −45°(fc) → −90°
 *
 * 而且它在三个频段各有一个明确的检查点：
 *   低频（f << fc）  ≈ 0 dB / 0°
 *   转折（f = fc）   = −3 dB / −45°      ← 最重要，斜率和相位同时变化
 *   高频（f >> fc）  ≈ −20 dB/dec / −90°
 *
 * 如果这三个点都对，说明：整周期取整、θ 递推、复数约定、累加器位数、
 * 频率轴映射全都对。任何一处错都会让至少一个点明显偏离。
 *
 * ===========================================================================
 * 被测对象用 Tustin 还是后向欧拉
 * ===========================================================================
 *
 * 用 **Tustin（双线性）**，与 `app/sim/plants.py::FirstOrderLP` 和
 * libsfra 的 `filter_rc.h` 同源。这样上位机可以拿 Python 的解析解
 * 直接对，不需要为相位差找借口。
 *
 * Tustin：s → k(1−z⁻¹)/(1+z⁻¹)，k = 2·fs
 *     b0 = b1 = ωc/(ωc+k)
 *     a1 = (ωc−k)/(ωc+k)        （注意这里是 −a1·y[n−1]）
 *
 * 代入 s = jω 后离散频率响应的相位在高频会偏离连续域解析解
 * （双线性翘曲），但**在 f_s/5 以内误差 < 1°**，够用。
 * 若你想严格对标连续域，把 fc 取低一点（本示例取 f_s/100）。
 *
 * ===========================================================================
 * ISR 计时怎么做（这是 S6a 最硬的证据）
 * ===========================================================================
 *
 * 三种做法，按可信度排序：
 *
 *   A. DWT->CYCCNT（推荐，Cortex-M3/M4/M7 有，M0+ 没有）
 *      在 inject() 前后各读一次，差值就是精确周期数。
 *      优点：不用示波器、分辨率 1 周期、能直接算出占用率。
 *      缺点：DWT 需要先使能（DEMCR |= TRCENA, DWT->CTRL |= CYCCNTENA）；
 *            M0+ 上没有这个外设，只能用 B。
 *
 *   B. GPIO 翻转 + 示波器（最硬，任何 MCU 都行）
 *      在 ISR 入口拉高、出口拉低，示波器量高电平宽度。
 *      优点：包含真实的中断延迟/流水线/访存，谁也没法反驳。
 *      缺点：要示波器，且 GPIO 翻转本身有几周期开销（约 2~5 周期）。
 *
 *   C. 掐 ISR 周期（最粗糙，只用于粗估）
 *      逐步缩短 ISR 周期直到丢拍，那就是极限。
 *      优点：零工具。缺点：只能给上界，且要小心别把板子烧了。
 *
 * 本文件实现了 A 和 B 两个钩子，默认两个都关（`SFRA_SELFTEST_TIMING`）。
 * 打开后会把最大值/累加值打到 stderr（或你配置的调试串口）。
 *
 * **注意**：A 和 B 都要在你的真实 ISR 里做，而不是在主循环里。
 * 本文件的"ISR"是一个软件模拟的调用点（`control_isr_tick()`），
 * 因为自测对象在软件里，没有真实的 PWM 定时器中断。移植到你的固件时，
 * 计时钩子要搬进真正的 PWM_ISR。
 *
 * ===========================================================================
 * 移植到你的板子要改哪几处（共 4 处，都在文件末尾的 USER CODE 区）
 * ===========================================================================
 *
 *   ① `sfra_port_uart_write()`      —— 改成你的 UART 发送（或 DMA 启动）
 *   ② `sfra_port_uart_read_byte()`  —— 通常直接 `return -1`，字节由 RX 中断塞
 *   ③ `sfra_port_enter/exit_critical()` —— 关/开中断（保护累加器）
 *   ④ `selftest_uart_rx_isr()`      —— UART RX 中断里调 `sfra_proto_rx()`
 *
 * 其余代码（协议、状态机、扫频）一行都不用改——这正是"零 HAL 依赖"
 * 换来的好处：同一份 sfra_core.c/sfra_proto.c 在 PC 和 MCU 上跑。
 *
 * ===========================================================================
 * 怎么跑
 * ===========================================================================
 *
 *   1. 把三个 .c 加进工程：sfra_core.c / sfra_proto.c / 本文件
 *   2. 改上面 ①~④ 四处
 *   3. 编译烧录，串口接 921600 8N1
 *   4. PC 端跑：
 *
 *          python tools/selftest_s6a.py --port COM7 --fc 1000
 *
 *      （--fc 必须与下面 SELFTEST_FC_HZ 一致，脚本会拿它算解析解）
 *
 *   5. 脚本会打印逐点误差表和 PASS/FAIL
 *
 * ===========================================================================
 * 常见失败与含义
 * ===========================================================================
 *
 *   相位 0 → +90°（而不是 −90°）
 *       复数约定实现反了。全项目只应在 PC 的 bode_data.py 一处实现
 *       `Z = S_c − j·S_s`，检查那里（D18）。
 *
 *   相位正确但幅值整体差一个常数
 *       pu 归一化不一致（D15）。检查 `x` 和 `y` 是否都在同一 pu 基准上。
 *
 *   低频段幅值/相位乱跳
 *       settle 时间不够，或注入幅值低于噪声。加大幅值或升精度档。
 *
 *   高频段相位比预期更"负"
 *       正常，是 Tustin 翘曲。f_s/5 以内应 < 1°。
 *
 *   STATUS 一直 IDLE，扫不动
 *       主循环没调 `sfra_background_task()`，或 ISR 没在跑。
 *       最容易犯的：主循环只调了 `sfra_proto_poll()`。
 *
 *   一 PING 就断线
 *       `sfra_port_uart_write()` 没发全（DMA 未等完成就返回），
 *       或 UART 波特率不对。先确认单个字节能通。
 * ------------------------------------------------------------------------- */

#include <stdint.h>
#include <string.h>

#include "sfra_core.h"
#include "sfra_port.h"
#include "sfra_proto.h"

/* =========================================================================
 * 用户配置（改这里，不用改下面的逻辑）
 * ========================================================================= */

/* 被测一阶低通的转折频率。必须与 PC 端 `--fc` 一致。
 *
 * 取 f_s/100 的理由：这样整条扫频带（100 Hz ~ f_s/5）覆盖转折点两侧
 * 各一个多 decade，既能验证低频 0 dB，也能验证高频 −20 dB/dec 斜率。
 * 若取太高（比如 f_s/10），高频段会被 f_s/5 截断，看不到 −90° 平台。 */
#ifndef SELFTEST_FC_HZ
#define SELFTEST_FC_HZ      1000.0f
#endif

/* ISR 频率 = 你的 PWM 开关频率。 */
#ifndef SELFTEST_ISR_HZ
#define SELFTEST_ISR_HZ     100000.0f
#endif

/* 打开 ISR 计时（DWT 或 GPIO）。默认关，因为会略微影响周期数。 */
#ifndef SFRA_SELFTEST_TIMING
#define SFRA_SELFTEST_TIMING 0
#endif

/* 用 DWT 还是 GPIO 计时。DWT 只在 M3/M4/M7 上有。 */
#ifndef SFRA_TIMING_USE_DWT
#define SFRA_TIMING_USE_DWT  1
#endif

/* 结果数组长度上限。固件要求 ≤ 256（SFRA_FW_VEC_MAX），
 * f_s/5 截断后实际点数会更少。 */
#ifndef SELFTEST_MAX_POINTS
#define SELFTEST_MAX_POINTS  256
#endif

/* =========================================================================
 * 被测对象：一阶低通（Tustin 离散化）
 *
 * 与 app/sim/plants.py::FirstOrderLP 逐行对应，方便两边结果对齐。
 * ========================================================================= */
typedef struct {
    float b0, b1, a1;
    float x1, y1;
} lp1_t;

static void lp1_init(lp1_t *f, float fc, float fs)
{
    /* k = 2·fs；注意用 double 算，避免 float 在 fs 较大时丢精度 */
    double k   = 2.0 * (double)fs;
    double w   = 2.0 * 3.14159265358979323846 * (double)fc;
    double den = w + k;

    f->b0 = (float)(w / den);
    f->b1 = (float)(w / den);
    f->a1 = (float)((w - k) / den);
    f->x1 = 0.0f;
    f->y1 = 0.0f;
}

static float lp1_step(lp1_t *f, float x)
{
    float y = f->b0 * x + f->b1 * f->x1 - f->a1 * f->y1;
    f->x1 = x;
    f->y1 = y;
    return y;
}

/* =========================================================================
 * 全局状态
 *
 * 都放 static：这几个数组加起来约 5.2 KB（256 点 × 5 × f32 = 5120 B），
 * 大一点的 MCU 放 RAM 没问题；SRAM 紧张时可以把 `SELFTEST_MAX_POINTS`
 * 降到 64（f_s/5 截断后一般也就 40~70 点），省到 1.3 KB。
 * ========================================================================= */
static sfra_t        g_sfra;
static sfra_proto_t  g_proto;
static sfra_result_t g_res;

static float g_freq[SELFTEST_MAX_POINTS];
static float g_ac[SELFTEST_MAX_POINTS];
static float g_as[SELFTEST_MAX_POINTS];
static float g_bc[SELFTEST_MAX_POINTS];
static float g_bs[SELFTEST_MAX_POINTS];

static lp1_t g_lp;
static float g_dc_gain = 1.0f;      /* 一阶低通的直流增益 = 1，算一次存着 */
static float g_dc_bias = 0.0f;      /* PLANT 工作点（见下） */

static sfra_setup_t g_def;

/* =========================================================================
 * ISR 计时钩子
 *
 * 下面两个宏默认指向 CMSIS 的 DWT / GPIO 操作。如果你的工程没有包含
 * CMSIS 头（比如裸机自己定义寄存器），在编译命令行里预先定义这两个宏
 * 即可覆盖：
 *
 *     -DDWT_CYCCNT_READ()=(\*(volatile uint32_t\*)0xE0001004)
 *
 * DWT->CYCCNT 的使能（这一步只需在 init 里做一次）：
 *
 *     CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
 *     DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
 *     DWT->CYCCNT = 0;
 *
 * 没使能的话 CYCCNT 恒为 0，量出来是 0 周期——看起来"零开销"，
 * 其实什么都没测到。这是这套方法最常见的假通过。
 * ========================================================================= */
#if SFRA_SELFTEST_TIMING

    /* 读 DWT 周期计数器。M3/M4/M7 有，M0+ 没有。 */
    #if SFRA_TIMING_USE_DWT
        #ifndef DWT_CYCCNT_READ
            /* CMSIS 里 DWT->CYCCNT 是 32 位循环计数器，读一次 1~2 周期 */
            #if defined(__has_include)
                #if __has_include("stm32_hal.h")
                    #include "stm32_hal.h"
                #endif
            #endif
            #ifndef DWT_CYCCNT_READ
                /* 退化成直接读内存映射寄存器，不依赖任何头文件 */
                #define DWT_CYCCNT_READ()  (*(volatile uint32_t *)0xE0001004u)
            #endif
        #endif
    #else
        /* GPIO + 示波器法：这里返回的是"本周期高电平宽度（周期数）"，
         * 由用户在 update 里测量。若你不想在代码里测量，改成
         * `#define GPIO_PULSE_WIDTH_CYCLES() 0u` 并在示波器上读。 */
        #ifndef GPIO_PULSE_WIDTH_CYCLES
            #define GPIO_PULSE_WIDTH_CYCLES()  0u
        #endif
    #endif

    static uint32_t g_cyc_max  = 0;
    static uint64_t g_cyc_sum  = 0;
    static uint32_t g_cyc_n    = 0;
#endif

/* =========================================================================
 * 控制 ISR —— 三行调用的正确顺序
 *
 * 这是整个接入工作**唯一容易出错**的地方，所以单独写成函数并标好注释。
 * 你在真固件里要做的就是把这三行插进自己的 PWM_ISR。
 * ========================================================================= */
static void control_isr_tick(void)
{
#if SFRA_SELFTEST_TIMING && SFRA_TIMING_USE_DWT
    uint32_t t0 = DWT_CYCCNT_READ();
#endif

    /* ① 产正弦 + 计时 + （LOOP 模式下）累加 Inj
     *
     * 注意：这个函数**不推进 θ**。θ 由 ③ 推进。
     * 拆开的原因见 02 文档 A1 坑：若在这里先加 step，monitor 用的是
     * 下一拍的 θ，整条曲线偏 360/data_count 度。 */
    float inj = (float)sfra_inject(&g_sfra);

    /* ② 用户的控制律（这里是自测对象，真固件里是你的 PI/补偿器）
     *
     * 两种模式的注入位置不同：
     *   LOOP ：加在**参考值**上，输出取反馈 → 测环路增益 GH
     *   PLANT：加在**执行量**上，输出取反馈 → 测功率级 H
     *
     * 本自测两种都支持，方便你对比：LOOP 应该给出 H·1（对象本身），
     * PLANT 也应给出 H，但分母是注入后的 x。 */
    float x, y;

    if (g_sfra.cfg.mode == SFRA_MODE_PLANT) {
        /* 开环：注入叠在输入上，对象真实响应。
         *
         * g_dc_bias 是工作点（比如占空比 0.5）。这里必须**减掉直流**：
         * DFT 只要交流通路，直流分量会在整周期窗里留下 N·DC·Σcosθ
         * 的残差。真实硬件里这一步由硬件高通或软件均值扣除完成。
         *
         * 本示例的 dc_gain = 1（一阶低通直流增益为 1），所以减 1·bias。
         * 若你的对象直流增益不是 1，必须先用 plant_dc_gain() 实测。 */
        x = g_dc_bias + inj;
        y = lp1_step(&g_lp, x) - g_dc_gain * g_dc_bias;
    } else {
        /* 闭环：把对象当成"参考 → 反馈"的整条通路。
         * 注入加在参考上，反馈是对象输出。
         *
         * 这里**不需要**减直流：Y 和 Inj 都含同一个直流分量，
         * 在 `Inj−Y` 里相减抵消。减了反而引入额外误差源。 */
        x = inj;
        y = lp1_step(&g_lp, g_dc_bias + x) ;
    }

    /* ③ 采集 + 推进相位（顺序不能变！）
     *
     * monitor 累加 Y(out)，PLANT 模式还累加 X(in)，LOOP 模式忽略 in。
     * advance 必须在 monitor 之后，每周期恰好一次。 */
    sfra_monitor(&g_sfra, x, y);
    sfra_advance(&g_sfra);

#if SFRA_SELFTEST_TIMING
    {
        uint32_t dt;
    #if SFRA_TIMING_USE_DWT
        dt = DWT_CYCCNT_READ() - t0;
    #else
        dt = GPIO_PULSE_WIDTH_CYCLES();   /* 用户实现 */
    #endif
        g_cyc_sum += dt;
        g_cyc_n++;
        if (dt > g_cyc_max) { g_cyc_max = dt; }
    }
#endif
}

/* =========================================================================
 * 初始化
 * ========================================================================= */
void sfra_selftest_init(void)
{
    memset(&g_def, 0, sizeof(g_def));
    g_def.isr_freq    = SELFTEST_ISR_HZ;
    g_def.freq_start  = 100.0f;
    g_def.freq_step   = 1.1220184543019633f;   /* 20 pts/dec */
    g_def.amplitude   = 0.01f;                 /* 1 %pu，远小于固件上限 0.25 */
    g_def.vec_length  = 68u;
    g_def.cycles_mode = SFRA_CYC_NORMAL;
    g_def.mode        = SFRA_MODE_PLANT;

    g_res.freq_vect = g_freq;
    g_res.a_c = g_ac;
    g_res.a_s = g_as;
    g_res.b_c = g_bc;
    g_res.b_s = g_bs;

    /** 建正弦表。必须是第一步，且只调一次。 */
    sfra_init_all();

    sfra_init(&g_sfra, &g_def, &g_res);
    sfra_proto_init(&g_proto, &g_def);
    g_proto.cfg = g_def;              /* 让 GET_CAPS 报出的默认值与上面一致 */

    lp1_init(&g_lp, SELFTEST_FC_HZ, SELFTEST_ISR_HZ);

    /* 一阶低通的直流增益恒为 1（H(0) = 1/(1+0) = 1），不用实测。
     * 若换成二阶 LC 或带增益的对象，这里要改成实测值：
     *
     *     g_dc_gain = 灌 1.0f 跑 20000 拍后的稳态输出;
     *
     * 并在扫频开始前把对象状态清零，否则 DC 标定会污染第一个频点。 */
    g_dc_gain = 1.0f;

    /* PLANT 工作点：一阶低通是线性的，工作点取 0 或 0.5 都一样。
     * 真功率级必须取实际工作点（如 Buck 的 D=0.5），因为功率级
     * 在小信号意义上依赖工作点。 */
    g_dc_bias = 0.0f;
}

/* =========================================================================
 * 主循环
 * ========================================================================= */
void sfra_selftest_loop(void)
{
    /* ① 收字节：真机上由 UART RX 中断调 sfra_proto_rx()，
     *    所以这里**通常什么都不做**。若你的串口没有中断/DMA，
     *    可以在这里轮询 `while ((c = uart_getc()) >= 0) sfra_proto_rx(...)`。 */
    {
        int ch = sfra_port_uart_read_byte();
        if (ch >= 0) {
            uint8_t b = (uint8_t)ch;
            sfra_proto_rx(&g_proto, &b, 1u);
        }
    }

    /* ② 推进协议：解析命令、装应答帧、检查 500 ms 通信超时。
     *    必须在主循环调用，绝不能放进 ISR（事务太长会拖垮控制环）。 */
    sfra_proto_poll(&g_proto, &g_sfra, &g_res, /*now_ms=*/0u);

    /* ③ 把应答倒出去。
     *
     *    **一个字节都不能留在缓冲里**：如果 tx_len 不为 0 就返回，
     *    协议层会认为上一帧还没发完，之后所有的应答都会积压，
     *    表现为"上位机第一次能连上，之后全超时"。
     *
     *    真机的 uart_write 若用 DMA，这里应当**等 DMA 完成**再 tx_done，
     *    否则下一次覆写会破坏正在发送的缓冲。 */
    if (g_proto.tx_len != 0u) {
        sfra_port_uart_write(g_proto.tx_buf, g_proto.tx_len);
        sfra_proto_tx_done(&g_proto);
    }

    /* ④ 推进扫频状态机。
     *
     *    **必须无条件调用**，不能写成 `if (sfra_is_running())`。
     *    COLLECT 采满后 state 立刻变 NEXT，此时 is_running 为假，
     *    若用它当条件，background_task 再没人调，状态机永远卡在 NEXT。
     *    （S3 阶段踩过这个坑，见 00 文档 §0.5.7。） */
    sfra_background_task(&g_sfra, &g_res);

    /* ⑤ ISR 演示：自测对象在软件里，没有真实定时器中断，
     *    所以在这里模拟 ISR 节拍。
     *
     *    真固件里**删掉这一段**——ISR 由 PWM 定时器硬件驱动，
     *    和主循环无关。保留它只是为了本示例能单机跑起来。 */
    if (g_sfra.state != SFRA_ST_IDLE && g_sfra.state != SFRA_ST_DONE) {
        control_isr_tick();
    }
}

/* =========================================================================
 * USER CODE —— 移植到你的板子只改这几处
 *
 * 下面四个函数是 sfra_port.h 声明的。**库里不调用它们**，只有你的
 * 应用代码会用，所以不实现它们库照样能编译链接（见 sfra_port.h 的说明）。
 * ========================================================================= */

/* ① UART 发送：发 n 个字节。
 *
 * 必须**发全**：若用 HAL_UART_Transmit 阻塞模式最省心（一帧最多 149 B，
 * 921600 下约 1.6 ms，能接受）。用 DMA 时必须等完成，或改成
 * "把指针和长度存下来，等 TX 完成中断里再调 sfra_proto_tx_done()"。
 *
 * 千万不要在这里 printf —— 会把二进制帧和文本混在一起。 */
void sfra_port_uart_write(const uint8_t *buf, uint16_t len)
{
    /* ===== 你的代码 =====
     * HAL_UART_Transmit(&huart2, (uint8_t *)buf, len, 10);
     * ==================== */
    (void)buf;
    (void)len;
}

/* ② UART 读字节：返回 -1 表示当前没有数据。
 *
 * **推荐直接 `return -1`**：字节交给 UART RX 中断处理（ISR 里调
 * `sfra_proto_rx(&g_proto, &b, 1)`），主循环不必轮询。
 * 这样既省 CPU，也避免"主循环卡在等字符"导致状态机不推进。 */
int16_t sfra_port_uart_read_byte(void)
{
    return -1;
}

/* ③ 临界区：保护 int64 累加器。
 *
 * 何时真正需要：
 *   - 上位机 ABORT 时，主循环会写 `s->cfg` / 状态，而 ISR 正在读；
 *   - 上位机 GET_DATA 读 a_c/a_s 数组时，ISR 可能正在写同一频点。
 *
 * 在 Cortex-M 上，最简单的是 `__disable_irq()` / `__enable_irq()`，
 * 但**必须保存并恢复**原状态——否则嵌套调用会把中断提前打开：
 *
 *     static uint32_t g_primask;
 *     void sfra_port_enter_critical(void) {
 *         g_primask = __get_PRIMASK(); __disable_irq();
 *     }
 *     void sfra_port_exit_critical(void) {
 *         __set_PRIMASK(g_primask);
 *     }
 *
 * 用 BASEPRI 屏蔽特定优先级更好（不影响更紧急的中断），
 * 但要保证 FOC/PWM ISR 的优先级高于被屏蔽的那一级。 */
void sfra_port_enter_critical(void)
{
    /* ===== 你的代码 ===== */
}

void sfra_port_exit_critical(void)
{
    /* ===== 你的代码 ===== */
}

/* ④ UART RX 中断回调：字节进协议层。
 *
 * 在 `HAL_UART_RxCpltCallback` 或你自己的 RX 中断里调用。
 * 注意一次中断可能来多个字节（DMA 模式），按实际长度传 n。 */
void sfra_selftest_uart_rx_isr(const uint8_t *data, uint16_t n)
{
    sfra_proto_rx(&g_proto, data, n);
}

/* ⑤ 可选弱符号：把测试条件报给上位机（Vin/Vout/Iout/温度等）。
 *
 * 不实现也能跑，上位机只会显示"未回读"。但**强烈建议实现**：
 * meta.json 里的测试条件是"这条曲线在什么工况下测的"的唯一记录，
 * 复测时对不上工况，曲线差异就无法归因（见 02 文档 §2.5）。 */
int sfra_proto_get_conditions(float out[8])
{
    out[0] = 0.0f;    /* Vin  */
    out[1] = 0.0f;    /* Vout */
    out[2] = 0.0f;    /* Iout */
    out[3] = SELFTEST_ISR_HZ;   /* f_sw */
    out[4] = 0.0f;    /* deadtime ns */
    out[5] = 25.0f;   /* temp °C */
    out[6] = 0.0f;    /* adc full scale V */
    out[7] = 0.0f;    /* shunt gain */
    return 8;
}

/* =========================================================================
 * 计时统计输出
 *
 * 打开 SFRA_SELFTEST_TIMING 后，这两组数就是 S6a DoD 第 3 条的证据：
 * ISR 附加时间占 ISR 周期的百分比。
 *
 * 怎么算占用率：
 *     core_clock 是内核主频（不是 ISR 频率！）
 *     isr_period_cycles = core_clock / isr_freq
 *     占用率 = avg_cycles / isr_period_cycles × 100%
 *
 * 例：M4F @168 MHz，ISR 100 kHz → 1680 周期/ISR；
 *     若 avg = 130 周期 → 占用 7.7%，满足 < 10% 的 DoD。
 *
 * 注意 avg 里**不含**中断进入/退出开销（压栈、取向量约 12~16 周期）。
 * 所以真实占用比这个数高一点点——报数时把这一点说清楚，别把
 * "测到 7.7%" 说成 "总共 7.7%"，那会低估真实成本。
 * ========================================================================= */
#if SFRA_SELFTEST_TIMING
uint32_t sfra_selftest_cyc_max(void)  { return g_cyc_max; }
uint32_t sfra_selftest_cyc_avg(void)
{
    return g_cyc_n ? (uint32_t)(g_cyc_sum / g_cyc_n) : 0u;
}
uint32_t sfra_selftest_cyc_count(void) { return g_cyc_n; }

void sfra_selftest_cyc_reset(void)
{
    g_cyc_max = 0u;
    g_cyc_sum = 0u;
    g_cyc_n   = 0u;
}
#endif
