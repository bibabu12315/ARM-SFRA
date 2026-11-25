/**
  ******************************************************************************
  * @file    sfra_pend.c
  * @brief   TIM7 控制节拍：ISR 里跑 sfra_inject/monitor/advance
  ******************************************************************************
  * ISR 里的**唯一**工作就是那三条调用，别的都往主循环扔。
  *
  * 调用顺序（任何一处顺序错都会毁掉整条曲线）：
  *
  *     inj = sfra_inject(&s)        ① 产本拍正弦，不推进相位
  *     sfra_dut_step(...)           ② 被测对象/真实控制律（本拍用①的 inj）
  *     sfra_monitor(&s, x, y)       ③ 用**本拍**θ 累加，必须在 advance 之前
  *     sfra_advance(&s)             ④ 推进相位，每拍恰好一次
  *
  * 把 ④ 提前到 ③ 之前，monitor 用的就是下一拍的 θ，整条曲线被强行加上
  * 360/data_count 度的相位偏移——低频看不出来，高频直接跑飞。这是
  * docs/02 A1 坑，也是本文件唯一容易抄错的地方。
  *
  * background_task 不在 ISR 里：它要写 res 数组、要调 calc_cycles，
  * 放 ISR 里会把周期数顶上去。放在主循环 sfra_app_poll() 里，代价是
  * 状态机从 COLLECT 到 NEXT 可能晚一拍——不影响数据正确性，因为
  * monitor() 采满 data_count 后自己就停手了（sfra_core.c 的 A3 坑处理）。
  ******************************************************************************
  */

#include "sfra_pend.h"
#include "sfra_app.h"
#include "sfra_dut.h"

/*============================ 内部状态 ====================================*/

static bool     s_running;
static float    s_actual_freq;
static uint32_t s_overrun;

/* ISR 开销统计。只在 SFRA_MEASURE_ISR_COST=1 时更新。 */
#if SFRA_MEASURE_ISR_COST
static uint32_t s_cyc_sum;
static uint32_t s_cyc_max;
static uint32_t s_cyc_n;
/** 被丢弃的异常样本数（单次测得 ≥ 1 ms，说明测量被打断）。 */
static uint32_t s_cyc_drop;

/* 分阶段统计：sfra_inject 段（sin_q15 + 幅度缩放）。 */
static uint32_t s_inj_sum;
static uint32_t s_inj_max;
static uint32_t s_inj_n;

/* ---------------------------------------------------------------- 计时方式
 *
 * 【不要用 HAL_GetTick() 拼时间戳】
 * 最初的实现是  ts = ms * (LOAD+1) + (LOAD - VAL)，想做 32 位单调时钟。
 * 实测输出 `avg=1 max=4294903495` —— max 就是 0xFFFF0007 这种回绕值。
 * 原因：ms 是**开机毫秒数**，跑一分钟就到 60000，60000 × 72000 早已
 * 溢出 32 位，之后做差得到的是垃圾。而且它还在 ISR 里读 HAL 的全局
 * 变量，和主循环更新 uwTick 存在竞争。
 *
 * 【改用 SysTick 的 COUNTFLAG + VAL 差值】
 * Cortex-M0+ 的 SysTick 在 1 ms 中断模式（LOAD=71999）下：
 *   - VAL 每个内核时钟减 1，从 LOAD 减到 0 后重装；
 *   - 重装时硬件置 CTRL.COUNTFLAG。
 * 我们要测的是**一次 ISR 内部**的时长，最多几十微秒，远小于 1 ms，
 * 所以正常情况下期间**根本不会发生重装**。于是：
 *
 *   dt = (VAL_start - VAL_end)   // 直接相减，无符号，回绕自然正确
 *
 * 只有在极端情况下（ISR 被打断、或单次执行超过 1 ms）才会跨越重装，
 * 那时用 COUNTFLAG 把 LOAD+1 补回去。这样：
 *   - 不需要 ms 计数，不可能溢出；
 *   - 不读 HAL 全局变量，没有竞争；
 *   - VAL 是 24 位递减计数器，相减天然处理 0 附近回绕。
 *
 * 代价：M0+ 不能关中断太久，所以这里只读 CTRL/SysTick->VAL 两个
 * 寄存器，开销几个周期，对被测对象的影响可以忽略（本来就要 --).
 */
#define SYSTICK_LOAD_DEFAULT   72000U   /* 72 MHz / 1 kHz，LOAD = 71999 */

/**
  * @brief  读 SysTick 当前计数值，并清掉 COUNTFLAG。
  * @param  flagged: 输出，本次读取时是否发生过重装。
  * @retval 当前 VAL（递减计数器的瞬时值）。
  * @note   先读 CTRL 清 COUNTFLAG，再读 VAL：顺序反了会漏掉
  *         "读 VAL 与读 CTRL 之间"发生的重装。
  */
static uint32_t systick_read(bool *flagged)
{
    /* CTRL.COUNTFLAG 的读取会清零它（写 0 也能清） */
    *flagged = ((SysTick->CTRL & SysTick_CTRL_COUNTFLAG_Msk) != 0U);
    return SysTick->VAL;
}
#endif /* SFRA_MEASURE_ISR_COST */

/*============================ 初始化 ======================================*/

/**
  * @brief  配置 TIM7 为周期性更新中断，作为控制 ISR 节拍。
  * @param  无
  * @retval BSP_OK 表示成功；时钟不满足要求或频率无法产生返回 BSP_ERROR。
  * @note   定时器时钟的判定：
  *           APB 预分频 = 1 时，TIMxCLK = PCLK1；
  *           APB 预分频 > 1 时，TIMxCLK = PCLK1 × 2。
  *         本工程 APB 不分频，所以走第一支。这里显式判断而不是"反正
  *         我们不分频"，是因为将来调主频时忘了这一层的代价很高——
  *         整个扫频带的横轴会错一个常数比例，而曲线形状完全正常。
  */
bsp_status_t sfra_pend_init(void)
{
    uint32_t pclk1 = HAL_RCC_GetPCLK1Freq();
    uint32_t hclk  = HAL_RCC_GetHCLKFreq();
    uint32_t timclk;
    uint32_t isr_hz;
    uint32_t psc = 0U;
    uint32_t arr;
    uint32_t ticks_per_irq;

    if (pclk1 == 0U)
    {
        return BSP_ERROR;
    }
    /* APB1 预分频为 1 → 定时器时钟等于 PCLK1 */
    timclk = (pclk1 == hclk) ? pclk1 : (pclk1 * 2U);

    isr_hz = (uint32_t)(SFRA_ISR_FREQ_HZ + 0.5f);
    if (isr_hz == 0U)
    {
        return BSP_ERROR;
    }

    /* 先在 timclk 内找一个 ≤ 65536 的预分频，让 (timclk/(psc+1))/isr_hz
     * 落进 16 位 ARR。72 MHz / 10 kHz = 7200，PSC=0 就够；100 kHz 需要
     * ARR=720，也够。留通用算法是为了改频率时不用手算。 */
    ticks_per_irq = timclk / isr_hz;
    if (ticks_per_irq == 0U)
    {
        return BSP_ERROR;           /* ISR 频率高于定时器时钟 */
    }
    while ((ticks_per_irq > 65536U) && (psc < 65536U))
    {
        psc++;
        ticks_per_irq = timclk / ((psc + 1U) * isr_hz);
    }
    if ((ticks_per_irq == 0U) || (ticks_per_irq > 65536U))
    {
        return BSP_ERROR;
    }
    arr = ticks_per_irq - 1U;

    /* 报告真值而不是标称值：f = timclk/((psc+1)(arr+1))。
     * 除不尽时这个值与 SFRA_ISR_FREQ_HZ 有微小差异，必须用真值去填
     * cfg.isr_freq，否则整条曲线的横轴有个常数比例误差。 */
    s_actual_freq = (float)timclk
                  / ((float)(psc + 1U) * (float)(arr + 1U));

    __HAL_RCC_TIM7_CLK_ENABLE();
    __HAL_RCC_TIM7_FORCE_RESET();
    __HAL_RCC_TIM7_RELEASE_RESET();

    TIM7->CR1  = 0U;                /* 先停，避免配置过程产生中断 */
    TIM7->PSC  = psc;
    TIM7->ARR  = arr;
    TIM7->CNT  = 0U;
    TIM7->SR   = 0U;
    /* 产生一次更新事件把 PSC/ARR 从影子寄存器装进去。
     * 不装的话第一次中断会等一个"旧 ARR"周期，时序对不上。 */
    TIM7->EGR  = TIM_EGR_UG;
    TIM7->SR   = 0U;                /* UG 会置 UIF，清掉 */
    TIM7->DIER = TIM_DIER_UIE;      /* 只开更新中断 */

    HAL_NVIC_SetPriority(TIM7_IRQn, 1U, 0U);
    HAL_NVIC_EnableIRQ(TIM7_IRQn);

    s_running = false;
    s_overrun = 0U;
    return BSP_OK;
}

/**
  * @brief  启动控制节拍。
  * @param  无
  * @retval BSP_OK；已在运行时返回 BSP_BUSY。
  */
bsp_status_t sfra_pend_start(void)
{
    if (s_running)
    {
        return BSP_BUSY;
    }
    TIM7->CNT = 0U;
    TIM7->SR  = 0U;
    s_running = true;
    TIM7->CR1 |= TIM_CR1_CEN;
    return BSP_OK;
}

/**
  * @brief  停止控制节拍。注入由 sfra_abort() 负责归零，这里只管定时器。
  * @param  无
  * @retval 无
  */
void sfra_pend_stop(void)
{
    TIM7->CR1 &= ~TIM_CR1_CEN;
    s_running = false;
}

/**
  * @brief  节拍是否在跑。
  * @param  无
  * @retval true 表示 TIM7 已启动。
  */
bool sfra_pend_is_running(void)
{
    return s_running;
}

/**
  * @brief  实际 ISR 频率。
  * @param  无
  * @retval Hz。
  */
float sfra_pend_actual_freq(void)
{
    return s_actual_freq;
}

/**
  * @brief  节拍溢出计数。
  * @param  无
  * @retval 溢出次数。
  */
uint32_t sfra_pend_overrun_count(void)
{
    return s_overrun;
}

/*============================ ISR =========================================*/

/**
  * @brief  TIM7 更新中断：控制节拍。
  * @param  无
  * @retval 无
  * @note   这里的执行时间是 S6a DoD 第 3 条要量的东西。
  *         不加 __disable_irq()：NVIC 已经保证同级不嵌套，而 sfra_t
  *         的累加器只被本 ISR 写、被主循环的 GET_DATA 读，那个方向由
  *         sfra_port 的临界区保护。
  */
void sfra_pend_irq_handler(void)
{
    sfra_t *s = (sfra_t *)sfra_app_handle();

    /* 清更新标志。必须在读/写任何状态之前做，否则处理超时后
     * 出来会立刻又进一次中断（表现为系统假死）。 */
    if ((TIM7->SR & TIM_SR_UIF) == 0U)
    {
        return;                     /* 不是更新中断（不该发生，防御） */
    }
    TIM7->SR = ~TIM_SR_UIF;

#if SFRA_MEASURE_ISR_COST
    {
        bool     wrap0;
        uint32_t v0 = systick_read(&wrap0);   /* 清掉 COUNTFLAG 并取起点 */
#if SFRA_USE_FIXED_POINT
        int16_t  inj;
        int16_t  y;
#else
        float    inj;
        float    x, y;
#endif
        uint32_t v1;
        bool     wrap1;
        uint32_t dt;
        uint32_t v_mid;
        bool     wrap_mid;

#if SFRA_USE_FIXED_POINT
        inj = (int16_t)sfra_inject(s);
        v_mid = systick_read(&wrap_mid);
        y = sfra_dut_step_q15(inj);
        sfra_monitor(s, inj, y);
#else
        inj = (float)sfra_inject(s);
        v_mid = systick_read(&wrap_mid);
        /* 被测对象：S6a 阶段是软件模型，将来接功率级时替换成
         * "注入叠到占空比/参考上 → 读 ADC → 算控制量" 那一段。 */
        x = inj;
        y = sfra_dut_step(inj);
        sfra_monitor(s, (sfra_signal_t)x, (sfra_signal_t)y);
#endif
        sfra_advance(s);

        v1 = systick_read(&wrap1);
        if (wrap1)
        {
            /* 期间发生过重装：VAL 从 0 跳回 LOAD，补一个整周期 */
            dt = (v0 - v1) + SYSTICK_LOAD_DEFAULT;
        }
        else
        {
            /* 正常情况：VAL 单调递减，直接相减即为经过的内核周期数 */
            dt = v0 - v1;
        }

        /* 防御：单次 ISR 超过 1 ms 说明有严重问题（或测量被中断打断），
         * 这种样本不能污染 max 和 avg，直接丢弃并计数。 */
        if (dt < SYSTICK_LOAD_DEFAULT)
        {
            uint32_t dt_inj;
            s_cyc_sum += dt;
            s_cyc_n++;
            if (dt > s_cyc_max) { s_cyc_max = dt; }

            /* 分阶段统计：inject 段 vs 其余段（dut_step+monitor+advance）。
             * 这两段的和约等于 dt（中间一次 systick_read 的几条指令
             * 算进 inject 段）。有了这两个数就能直接定位"是不是
             * sin_q15 的 64 位乘法在吃时间"。 */
            dt_inj = wrap_mid ? ((v0 - v_mid) + SYSTICK_LOAD_DEFAULT)
                              : (v0 - v_mid);
            if (dt_inj < SYSTICK_LOAD_DEFAULT)
            {
                s_inj_sum += dt_inj;
                s_inj_n++;
                if (dt_inj > s_inj_max) { s_inj_max = dt_inj; }
            }
        }
        else
        {
            s_cyc_drop++;
        }
    }
#else
#if SFRA_USE_FIXED_POINT
    {
        int16_t inj;

        /* 定点路径：从 inject 到 monitor 全程不碰浮点。
         * PLANT/LOOP 两个分支在数值上等价（这里都是开环模型），
         * 保留分支只为将来接真功率级时 LOOP 取真实反馈量。 */
        inj = (int16_t)sfra_inject(s);
        sfra_monitor(s, inj, sfra_dut_step_q15(inj));
        sfra_advance(s);
    }
#else
    {
        float inj;

        inj = (float)sfra_inject(s);
        /* PLANT 模式下 monitor 自己会累加 x；LOOP 模式只用 y。
         * 本阶段跑 PLANT（被测对象是开环模型），x 就是注入本身。 */
        if (s->cfg.mode == SFRA_MODE_PLANT)
        {
            /* PLANT：注入叠在 x 上，对象真实响应。
             * 减去 dc_gain × 0 = 0（本对象直流增益 1、工作点 0），
             * 所以这里不减直流——若将来换成有工作点的对象，
             * 这一步要改成 (dut_step(bias+inj) − dc_gain*bias)。 */
            sfra_monitor(s, (sfra_signal_t)inj, (sfra_signal_t)sfra_dut_step(inj));
        }
        else
        {
            /* LOOP：注入加在参考上，输出取反馈。
             * Y 与 Inj 含同一个直流，在 Inj−Y 里抵消，不需要减直流。
             *
             * 【S6a 阶段的现实】这里的"被测对象"只是一个开环软件模型
             * sfra_dut_step()，**没有闭环**。上位机在 LOOP 模式下按
             * GH = Y/(Inj−Y) 计算，而 Y ≈ Inj（单位增益低通），
             * 分母趋于 0 → 算出来的曲线整体抬高几十 dB、相位乱跳。
             *
             * 实测过一次：同一块板 PLANT 模式最大偏差 0.05 dB，
             * LOOP 模式整条曲线 +30 dB，看着像"算法坏了"，其实是
             * 模式选错。所以这里保留分支只为将来接真功率级（那时
             * y 是真的反馈量），**S6a 请一律用 PLANT 模式**。 */
            float y = sfra_dut_step(inj);
            sfra_monitor(s, (sfra_signal_t)inj, (sfra_signal_t)y);
        }
        sfra_advance(s);
    }
#endif
#endif

    /* 溢出检测：如果退出时 UIF 又置位，说明本拍执行时间超过了 ISR 周期，
     * 下一拍已经在等。数据不可信，但**不能**就此停机——扫频还在跑，
     * 让它跑完，由上位机从 STATUS.flags 或本计数看出来。 */
    if ((TIM7->SR & TIM_SR_UIF) != 0U)
    {
        s_overrun++;
        TIM7->SR = ~TIM_SR_UIF;
    }
}

#if SFRA_MEASURE_ISR_COST
/**
  * @brief  取 ISR 开销统计。
  * @param  avg_cycles: 接收平均周期数。
  * @param  max_cycles: 接收最大周期数。
  * @param  count: 接收统计拍数。
  * @retval 无
  */
void sfra_pend_cost(uint32_t *avg_cycles, uint32_t *max_cycles,
                    uint32_t *count)
{
    if (avg_cycles)  { *avg_cycles = (s_cyc_n ? (s_cyc_sum / s_cyc_n) : 0U); }
    if (max_cycles)  { *max_cycles = s_cyc_max; }
    if (count)       { *count      = s_cyc_n; }
}

/**
  * @brief  清零开销统计。
  * @param  无
  * @retval 无
  */
void sfra_pend_cost_reset(void)
{
    s_cyc_sum  = 0U;
    s_cyc_max  = 0U;
    s_cyc_n    = 0U;
    s_cyc_drop = 0U;
    s_inj_sum  = 0U;
    s_inj_max  = 0U;
    s_inj_n    = 0U;
}

/**
  * @brief  取分阶段统计：sfra_inject 段耗时。
  * @param  avg_cycles: 接收 inject 段平均周期数。
  * @param  max_cycles: 接收 inject 段最大周期数。
  * @retval 无
  * @note   与总耗时相减即得 dut_step + monitor + advance 段。
  *         用来定位"时间到底花在查表正弦还是浮点滤波上"。
  */
void sfra_pend_cost_split(uint32_t *avg_cycles, uint32_t *max_cycles)
{
    if (avg_cycles) { *avg_cycles = (s_inj_n ? (s_inj_sum / s_inj_n) : 0U); }
    if (max_cycles) { *max_cycles = s_inj_max; }
}

/**
  * @brief  取被丢弃的异常样本数。
  * @param  无
  * @retval 单次测得 ≥ 1 ms 而被丢弃的样本数。
  * @note   非 0 说明测量期间 ISR 被打断过（比如更高优先级中断抢占），
  *         此时 avg/max 只是"没被打断的那些拍"的统计，仍可用但要留意。
  */
uint32_t sfra_pend_cost_dropped(void)
{
    return s_cyc_drop;
}
#endif
