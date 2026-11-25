/* ---------------------------------------------------------------------------
 * sfra_dump.c - S2 阶段 PC 交叉验证的 C 端
 *
 * 目的：证明 C 实现与 Python 实现（src/app/sim/sfra_sim.py）是同一个东西。
 * 做法：喂完全相同的激励序列，比对**累加器原值**（不是 mag/phase）。
 *
 * 用法：
 *     sfra_dump <script.txt>
 *
 * script.txt（纯文本，Python 端生成）：
 *     line 1 : isr_freq freq_start freq_step amplitude vec_length mode cyc_mode
 *     line 2+: 每行一个 float，是逐拍的 in 值（PLANT 模式下是 x，
 *              LOOP 模式下忽略，C 端自己按 inj 闭环）
 *
 * 输出到 stdout，每频点一行：
 *     f_act a_c a_s b_c b_s
 * ------------------------------------------------------------------------- */
#include "sfra_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_VEC 512

/* 二阶 LC（与 plants.py SecondOrderLC 同一组系数公式） */
typedef struct {
    float b0, b1, b2, a1, a2;
    float x1, x2, y1, y2;
} lc_t;

static void lc_setup(lc_t *p, float f0, float q, float fs)
{
    double k   = 2.0 * (double)fs;
    double w0  = 2.0 * 3.14159265358979323846 * (double)f0;
    double den = w0 * w0 + k * w0 / (double)q + k * k;

    p->b0 = (float)(w0 * w0 / den);
    p->b1 = (float)(2.0 * w0 * w0 / den);
    p->b2 = p->b0;
    p->a1 = (float)(2.0 * (w0 * w0 - k * k) / den);
    p->a2 = (float)((w0 * w0 - k * w0 / (double)q + k * k) / den);
    p->x1 = p->x2 = p->y1 = p->y2 = 0.0f;
}

static float lc_step(lc_t *p, float x)
{
    float y = p->b0 * x + p->b1 * p->x1 + p->b2 * p->x2
            - p->a1 * p->y1 - p->a2 * p->y2;
    p->x2 = p->x1;
    p->x1 = x;
    p->y2 = p->y1;
    p->y1 = y;
    return y;
}

/* 第 idx 个频点的几何，与 sfra_load_point 内部算的一致。
 * 单独算一遍是为了让主循环知道总共要跑多少拍。 */
static void point_geom(const sfra_setup_t *cfg, uint16_t idx,
                       uint32_t *dc, uint32_t *settle)
{
    float    foi = sfra_freq_at(cfg, idx);
    uint32_t cyc = sfra_calc_cycles(foi, cfg->cycles_mode, cfg->isr_freq);
    float    rad = 0.0f;
    float    sec, per;

    *dc = sfra_point_geometry(cfg->isr_freq, foi, cyc, &rad);

    sec = 0.020f;
    per = (foi > 0.0f) ? (2.0f / foi) : 0.0f;
    if (per > sec) {
        sec = per;
    }
    *settle = (uint32_t)(sec * cfg->isr_freq + 0.999f);
}

/* 归一化 float → sfra_signal_t。
 *
 * 定点模式下 sfra_signal_t 是 int16（Q15），直接写
 * `sfra_monitor(&s, wave[k], y)` 会被 C 的隐式转换截成 0 或 1——
 * 整个 PLANT 累加器变成 0，交叉验证会报"1.00 相对误差"却还 PASS
 * （分母是 0 时 compare 会跳过）。所以这里显式过一道转换，
 * 与固件里 sfra_inject 的返回一致。 */
static sfra_signal_t to_sig(float v)
{
#if SFRA_USE_FIXED_POINT
    return (sfra_signal_t)(v * 32767.0f);
#else
    return (sfra_signal_t)v;
#endif
}

int main(int argc, char **argv)
{
    FILE         *fp;
    sfra_setup_t  cfg;
    sfra_result_t res;
    sfra_t        s;
    float         freq[MAX_VEC], ac[MAX_VEC], as_[MAX_VEC], bc[MAX_VEC], bs[MAX_VEC];
    float        *wave = NULL;
    long          wave_n = 0, wave_cap = 0;
    int           i;

    if (argc < 2) {
        fprintf(stderr, "usage: %s <script.txt>\n", argv[0]);
        return 2;
    }
    fp = fopen(argv[1], "r");
    if (!fp) {
        fprintf(stderr, "cannot open %s\n", argv[1]);
        return 2;
    }

    {
        int mode = 0, cyc = SFRA_CYC_NORMAL;
        if (fscanf(fp, "%f %f %f %f %hu %d %d",
                   &cfg.isr_freq, &cfg.freq_start, &cfg.freq_step,
                   &cfg.amplitude, &cfg.vec_length, &mode, &cyc) != 7) {
            fprintf(stderr, "bad header\n");
            fclose(fp);
            return 2;
        }
        cfg.mode        = (uint8_t)mode;
        cfg.cycles_mode = (uint8_t)cyc;
    }
    if (cfg.vec_length == 0 || cfg.vec_length > MAX_VEC) {
        fprintf(stderr, "bad vec_length\n");
        fclose(fp);
        return 2;
    }

    /* 读全部激励样本（数量由 Python 端按同样公式算好，我们照单全收） */
    {
        char  line[64];
        float v;
        while (fgets(line, sizeof(line), fp) != NULL) {
            if (sscanf(line, "%f", &v) != 1) {
                continue;
            }
            if (wave_n == wave_cap) {
                wave_cap = wave_cap ? wave_cap * 2 : 65536;
                wave = (float *)realloc(wave, (size_t)wave_cap * sizeof(float));
                if (!wave) {
                    fprintf(stderr, "oom\n");
                    fclose(fp);
                    return 2;
                }
            }
            wave[wave_n++] = v;
        }
    }
    fclose(fp);

    /* 需要的总拍数 */
    {
        long need = 0;
        for (i = 0; i < (int)cfg.vec_length; i++) {
            uint32_t dc, st;
            point_geom(&cfg, (uint16_t)i, &dc, &st);
            need += (long)st + (long)dc;
        }
        if (wave_n < need) {
            fprintf(stderr, "wave too short: %ld < %ld\n", wave_n, need);
            free(wave);
            return 2;
        }
    }

    res.freq_vect = freq;
    res.a_c = ac;
    res.a_s = as_;
    res.b_c = bc;
    res.b_s = bs;

    sfra_init_all();
    sfra_init(&s, &cfg, &res);
    if (sfra_start(&s) != 0) {
        fprintf(stderr, "sfra_start failed\n");
        free(wave);
        return 2;
    }

    /* 主循环：每拍 inject → 推对象 → monitor → advance → background */
    {
        lc_t   plant;
        long   k = 0;

        lc_setup(&plant, 2000.0f, 0.8f, cfg.isr_freq);

        while (!sfra_is_done(&s) && k < wave_n) {
            /* inject 必须有，它负责推进 settle→collect 的状态切换；
             * 激励本身用 wave[k]，保证两边输入逐位相同。 */
            (void)sfra_inject(&s);

            if (cfg.mode == SFRA_MODE_PLANT) {
                /* 激励用 Python 给的 wave[k]（= 0.5 + inj），保证两边输入
                 * 逐位相同；响应取**零直流增益**：y = 0.5·(x − 0.5)。
                 *
                 * 不能让 y 带直流：DFT 只对注入频率相关，直流在整周期窗口
                 * 里贡献本应为 0，但有限精度累加会留下 N·DC·Σcosθ 的残差。
                 * 交叉验证只比交流通路，直流残差另见 01 文档。 */
                float x = wave[k];
                float y = 0.7f * (x - 0.5f);
                sfra_monitor(&s, to_sig(x), to_sig(y));
            } else {
                /* LOOP：Python 侧直接把 inj 当参考送进来（wave[k] = inj），
                 * 响应取零直流增益的 y = 0.5·inj。
                 *
                 * 注意这里不能写 y = 0.5·(wave[k] − 0.5)：wave 是 float32，
                 * 0.5 也是 float32，相减会丢最低几位有效数字，而 Python 端
                 * 用的是 inj 原值。两边公式必须逐字相同，否则 0.8° 级别的
                 * 相位差会混进"算法是否正确"的结论里。 */
                float r = wave[k];
                float y = 0.5f * r;
                sfra_monitor(&s, to_sig(r), to_sig(y));
            }

            sfra_advance(&s);
            sfra_background_task(&s, &res);
            k++;
        }
        fprintf(stderr, "pushed %ld frames, done=%d n=%u y_c=%.6f\n", k, sfra_is_done(&s), s.collected, s.y_c);
    }

    for (i = 0; i < (int)cfg.vec_length; i++) {
        printf("%.9g %.9g %.9g %.9g %.9g\n",
               freq[i], ac[i], as_[i], bc[i], bs[i]);
    }

    free(wave);
    return 0;
}
