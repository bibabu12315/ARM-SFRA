/**
  ******************************************************************************
  * @file    sfra_local_cfg.h
  * @brief   本工程的 SFRA 本地配置覆盖（**调试用，测完记得改回去**）
  ******************************************************************************
  * 为什么要有这个文件：
  *   `SFRA_MEASURE_ISR_COST` 这类开关原来靠 PVCharger.uvprojx 的 <Define>
  *   传，但那个文件会被 Keil GUI 覆盖（已经踩过：手加的宏保存一次就没了，
  *   编译仍 0 Error，运行时才发现功能缺失）。所以改成源码头文件，
  *   改这里绝不会被 IDE 吃掉。
  *
  * 用法：把某项改成 1 → 重新编译 → 烧录 → 用 shell 的 `sfra cost` 读数
  *       → 测完改回 0 → 再编译一次。
  ******************************************************************************
  */
#ifndef __SFRA_LOCAL_CFG_H
#define __SFRA_LOCAL_CFG_H

/* ISR 开销测量。
 *
 * 打开后 ISR 里多一次 SysTick 差值统计，统计本身会让周期数偏大
 * 5~10 周期，所以**不要常态化开着**。测完把下面这行删掉或改 0。
 *
 * 读数方法：PC 端 shell 里 `sfra cost`。
 * 判据：72 MHz 下 10 kHz 节拍 = 每拍 7200 周期，ISR 占用应远小于此。
 *
 * 【命名必须注意】这里是 SFRA_LOCAL_MEASURE_ISR_COST，不是
 * SFRA_MEASURE_ISR_COST。后者在 sfra_app.h 里已有 #ifndef 默认值，
 * 直接定义它会被那个默认值挡住，出现"改了不生效、Code 大小一点没变"。
 *
 * 【2026-11-24 实测结论：已测完，量产构建保持 0】
 * 定点化之后测得（fs=10 kHz，预算 7200 cyc）：
 *     avg=1312 (18%)  max=1475 (20%)  ovr=0
 * 数据已经记录在 Docs 里，不需要常态化打开测量。
 * 打开本项会让 ISR 每拍多两次 SysTick 读取 + 几次比较，
 * 实测约 +30~40 cycle，测完就该关掉。
 *
 * 【命名必须注意】这里是 SFRA_LOCAL_MEASURE_ISR_COST，不是
 * SFRA_MEASURE_ISR_COST。后者在 sfra_app.h 里已有 #ifndef 默认值，
 * 直接定义它会被那个默认值挡住，出现"改了不生效、Code 大小一点没变"。
 *
 * 【2026-11-24 曾误判"打开后板子失联"】实际原因与本题无关：Keil 命令行
 * `-f` 烧录后不复位，芯片停在 HALTED（PC=0x20000000），串口自然无声。
 * 用 tools/flash_and_run.py 烧录（内部会用 pyOCD 补一次 reset+resume）
 * 即可正常运行。 */
#define SFRA_LOCAL_MEASURE_ISR_COST   0

/* 数值路径：1 = Q15 定点，0 = float32。
 *
 * 【必须开 1】这块板是 PY32F071（Cortex-M0+），**没有 FPU**，
 * 所有 float 运算都是 __aeabi_fmul / __aeabi_fadd 软件例程。
 * 2026-11-24 实测 float 路径下整个 SFRA ISR：
 *
 *     总耗时  avg=6947  max=8148   （预算 7200 → 96% / 113%）
 *     inject  avg=2204  max=2217
 *     rest    avg=4743  max=5931   ← dut_step + monitor 的浮点
 *
 * 已超时（ovr 计数 78301），扫频期间控制节拍被拖垮。
 * 切到定点后同一套动作降到 ~1500 cycle 以内，见 Docs 的实测记录。
 *
 * 只有在带 FPU 的片子（M4F/M7）上才应该置 0——那时 float 比
 * 定点更快且精度更好。 */
#define SFRA_LOCAL_USE_FIXED_POINT    1

/* 文本 shell 通路（分流器）。1 = 保留，0 = 关掉。
 *
 * 【为什么有这个开关】
 * 开发阶段为了在串口上同时跑"人打的文本命令"（app_log.c 的 help/status）
 * 和"二进制协议帧"，加了一个按帧头 AA 55 分流的 mux。量产板只需要二进制
 * 协议，文本通路纯属脚手架。
 *
 * 【关掉会发生什么】
 * sfra_app_feed_byte() 不再判断帧头，所有字节直接进 sfra_proto_rx()。
 * 上位机行为完全不变（上位机本来就只发二进制帧，从没有以非 AA 字节打头
 * 的流量）。shell 的 `help` / `sfra` 命令失效。
 *
 * 【v1.0 取值】1 —— 保持现状不动。这份代码要上传存档，暂时不为了删
 * 100 行而改动主循环契约；等真实功率级联调时再决定是否置 0。
 * 置 0 前请确认没人依赖 app_log 输出。 */
#ifndef SFRA_LOCAL_USE_TEXT_SHELL
#define SFRA_LOCAL_USE_TEXT_SHELL     1
#endif

#endif /* __SFRA_LOCAL_CFG_H */
