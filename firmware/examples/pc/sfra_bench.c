/* ---------------------------------------------------------------------------
 * sfra_bench.c - S2 开销实测
 *
 * 跑 N 次 inject + monitor + advance，报告每拍的平均耗时。
 * 目的是把 01/02 文档里"理论估算"的那张开销表换成实测值。
 *
 * **注意这是 PC 上的数字，不是 MCU 的**：
 *   - x86-64 有硬件 double，M4F 是单精度 FPU（double 走软件）
 *   - M0+ 连单精度 FPU 都没有
 * 所以这里能证明的只有"每拍做多少次乘加"，绝对周期数必须上板用
 * DWT->CYCCNT 量。文档里两个数字分开列。
 *
 * 用法：
 *     sfra_bench <N>
 * 输出：
 *     n_per_frame ns_per_frame
 * ------------------------------------------------------------------------- */
#include "sfra_core.h"

#include <stdio.h>
#include <stdlib.h>

#ifdef _WIN32
#include <windows.h>
/* Windows 的 clock() 只有 1ms 分辨率，测 1e6 次 10ns 级的操作完全不够。
 * QueryPerformanceCounter 是 100ns 甚至更高分辨率。 */
static double now_sec(void)
{
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
#else
#include <time.h>
static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}
#endif

int main(int argc, char **argv)
{
    sfra_setup_t cfg;
    sfra_result_t res;
    sfra_t s;
    float f[4], ac[4], as_[4], bc[4], bs[4];
    long n = 1000000;
    long k;
    double t0, t1, us;
    volatile float sink = 0.0f;

    if (argc > 1) {
        n = atol(argv[1]);
        if (n <= 0) {
            n = 1000000;
        }
    }

    cfg.isr_freq    = 100000.0f;
    cfg.freq_start  = 100.0f;
    cfg.freq_step   = 1.12201846f;
    cfg.amplitude   = 0.01f;
    /* 频点给足，保证注入侧一直在忙（LOOP 模式要算 Inj） */
    cfg.vec_length  = 4;
    cfg.cycles_mode = SFRA_CYC_PRECISE;   /* 每点 200 周期，采集期最长 */
    cfg.mode        = SFRA_MODE_LOOP;     /* LOOP 每拍多 2 次乘加，算上界 */

    res.freq_vect = f;
    res.a_c = ac;
    res.a_s = as_;
    res.b_c = bc;
    res.b_s = bs;

    sfra_init_all();
    sfra_init(&s, &cfg, &res);
    sfra_start(&s);

    /* 预热：让 cache / 分支预测进入稳态 */
    for (k = 0; k < 10000; k++) {
        sink += (float)sfra_inject(&s);
        sfra_monitor(&s, 0.5f, 0.25f);
        sfra_advance(&s);
        sfra_background_task(&s, &res);
        if (sfra_is_done(&s)) {
            sfra_clear_done(&s);
            sfra_start(&s);
        }
    }

    t0 = now_sec();
    for (k = 0; k < n; k++) {
        sink += (float)sfra_inject(&s);
        sfra_monitor(&s, 0.5f, 0.25f);
        sfra_advance(&s);
        sfra_background_task(&s, &res);
        if (sfra_is_done(&s)) {
            sfra_clear_done(&s);
            sfra_start(&s);
        }
    }
    t1 = now_sec();

    us = (t1 - t0) * 1e6 / (double)n;
    printf("%ld %.1f\n", n, us * 1000.0);   /* ns per frame */
    (void)sink;
    return 0;
}
