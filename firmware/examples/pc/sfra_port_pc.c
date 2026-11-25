/* ---------------------------------------------------------------------------
 * sfra_port_pc.c - `sfra_port.h` 的 PC 参考实现（假 MCU 用）
 *
 * 这个文件把四个必需函数映射到 PC 的 stdin/stdout：
 *     uart_write       → fwrite(stdout)（二进制模式，由 main 负责 setmode）
 *     uart_read_byte   → getchar()，EOF 返回 -1
 *     enter/exit_crit  → 空（单线程，没有竞态）
 *
 * 为什么要单独抽出来（而不是留在 sfra_sim_mcu.c 里）：
 *     ① 让 `sfra_port.h` 的声明**真的有用**——一份只有注释、没有实现
 *        的头文件，改错一个字都不会有人发现；
 *     ② 给移植的人一个**可编译的最小样例**：把这里的函数体换成
 *        HAL_UART_Transmit / __disable_irq，就是 STM32 的 sfra_port.c。
 *        调用顺序、缓冲语义都不用重新想。
 *
 * 注意：本文件不属于交付给 MCU 的库，只用于 `firmware/examples/pc/`。
 * ------------------------------------------------------------------------- */
#include "sfra_port.h"

#include <stdio.h>
#include <stdlib.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <windows.h>    /* PeekNamedPipe：非阻塞探测管道有没有数据 */
#endif

/* 管道状态（定义在本文件末尾）。read_byte 里要用，先声明。 */
void sfra_port_mark_stdin_closed(void);
int  sfra_port_stdin_closed(void);
static int16_t sfra_port_pc_poll_byte(void);

/* ---------------------------------------------------------------- 传输层 */

void sfra_port_init_stdio(void)
{
#ifdef _WIN32
    /* **必须**：Windows 上 stdout 默认文本模式，0x0A 会被翻译成 0x0D 0x0A，
     * 二进制帧就这样毁了。协议里 float 低位出现 0x0A 的概率很高，
     * 不设这个的话大约每 3 帧就有一帧 CRC 错。 */
    _setmode(_fileno(stdin),  _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif

    /* **管道必须无缓冲**（2026-11-24 修）。
     *
     * 直接 `fprintf(stderr, ...)` 往管道写 4 KB 的块缓冲，而假 MCU 的
     * 调试输出频率高（每 ISR 拍一行），缓冲写满之前 PC 端一个字节都
     * 看不到；更糟的是 `fprintf` 本身在临界区里被反复调用，会把
     * "每拍 10 µs" 的虚拟时序彻底打乱。
     *
     * stdout 同理但要小心：它承载协议字节，无缓冲意味着每帧一次
     * write() 系统调用。PC 端按 200 ms 轮询、每次最多 143 B，
     * 这个开销完全可以接受，换来的是"写完就出发"的确定性时序。
     *
     * 真机上没有这两行——UART 是硬件 FIFO，不存在 stdio 缓冲。 */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
}

void sfra_port_uart_write(const uint8_t *buf, uint16_t len)
{
    size_t w;

    if (buf == NULL || len == 0u) {
        return;
    }
    w = fwrite(buf, 1u, (size_t)len, stdout);
    fflush(stdout);
    if (w != (size_t)len) {
        fprintf(stderr, "[port] stdout write short: %zu/%u\n", w, (unsigned)len);
    }
}

int16_t sfra_port_uart_read_byte(void)
{
    /* **非阻塞**：读不到就立刻返回 -1，让主循环继续去推进扫频。
     *
     * 为什么不能用 getchar()：管道上的 getchar 会一直阻塞到下一个字节。
     * 上位机是按 200 ms 轮询的，等于要求固件"只在被发命令的那一刻
     * 才活一下"——两个字节之间 200 ms 里一拍 ISR 都不推进，扫频速度
     * 被 PC 的轮询节奏死死绑住（100 kHz 档 20 个频点要跑几分钟）。
     * 真板上 TIM7 自由运行，主循环卡多久都不影响 ISR，所以这里必须
     * 模拟出同样的"串口可以空着"的语义。
     *
     * Windows 上用 `_read()` 直接读二进制句柄：它在管道上会**阻塞**，
     * 所以先用 `_kbhit()`-之外的可靠办法——`PeekNamedPipe` 探一次。
     * 用标准库更省事的做法是 `_setmode` + `_read` 配 `select()`，
     * 但 Windows 没有 select()，这里用 CRT 的 `_getch` 也不行（走控制台）。
     * 实际做法见下：`sfra_port_pc_poll_byte()` 用 PeekNamedPipe。 */
    return sfra_port_pc_poll_byte();
}

/* 非阻塞读一个字节：管道里没有数据就立刻返回 -1。
 *
 * PeekNamedPipe 是 Win32 上唯一"不消费就能看有没有数据"的 API，
 * 对匿名管道同样有效。跨平台时换成 select()/poll() 即可，语义一致：
 *   · 有数据 → 返回 1，读走一字节
 *   · 没数据 → 返回 -1（不是 EOF，继续跑）
 *   · 管道关了 → 置 g_stdin_closed，返回 -1
 */
static int16_t sfra_port_pc_poll_byte(void)
{
#ifdef _WIN32
    HANDLE h = (HANDLE)_get_osfhandle(_fileno(stdin));
    DWORD avail = 0;
    char  c;

    if (h == INVALID_HANDLE_VALUE) {
        sfra_port_mark_stdin_closed();
        return (int16_t)-1;
    }
    if (!PeekNamedPipe(h, NULL, 0, NULL, &avail, NULL)) {
        /* 不是管道（例如重定向到文件/控制台）：退回阻塞读。 */
        int ch = getchar();
        if (ch == EOF) {
            sfra_port_mark_stdin_closed();
            return (int16_t)-1;
        }
        return (int16_t)ch;
    }
    if (avail == 0) {
        /* 管道里没数据。**注意**：匿名管道在写端关闭时 PeekNamedPipe
         * 仍然成功且 avail == 0，无法区分"暂时没数据"和"对端关了"。
         * 对假 MCU 来说这没关系——上位机正常关闭时会先 kill 进程，
         * 不需要靠 EOF 收尾。
         *
         * 【为什么这里只等 1 ms 而不是直接返回】
         * 主循环是"补中断 → 等字节"的循环。如果这里立刻返回 -1，
         * 上位机 200 ms 才发一条命令，这期间主循环会空转上万次去补
         * 中断——虚拟时间一口气跑完整轮扫频，PC 一个点都拿不到
         * （实测就是这样：`vec=21`，PC 只收到前 20 点）。
         * 所以没数据时用 WaitForSingleObject 小睡 1 ms：既把 CPU 让出去，
         * 又让"虚拟时间 ≈ 墙钟时间"，与真板 TIM7 的节奏一致。 */
        if (!sfra_port_stdin_closed()) {
            Sleep(1u);
        }
        return (int16_t)-1;
    }
    {
        int n = _read(_fileno(stdin), &c, 1);
        if (n != 1) {
            sfra_port_mark_stdin_closed();
            return (int16_t)-1;
        }
    }
    return (int16_t)(unsigned char)c;
#else
    {
        int ch = getchar();
        if (ch == EOF) {
            sfra_port_mark_stdin_closed();
            return (int16_t)-1;
        }
        return (int16_t)ch;
    }
#endif
}

/* ------------------------------------------------- 管道状态（PC 专用）
 *
 * 真板上没有这两个函数：UART 一直在线，不存在"输入源关闭"这回事。
 *
 * 为什么要显式记状态：假 MCU 改成非阻塞读之后，`read_byte()` 返回 -1
 * 有两种含义——"这一轮没数据"（要继续跑扫频）和"管道关了"（要退出）。
 * 混在一起会导致两种情况之一：
 *   · 当成没数据 → 上位机关了它还在这里空转；
 *   · 当成关了     → 上位机每 200 ms 才发一条命令，中间 399 次空读
 *                    全被误判为 EOF，扫频刚开始就退出。
 * 所以 EOF 单独落一个标志，只有它才代表真的结束。 */
static int g_stdin_closed = 0;

void sfra_port_mark_stdin_closed(void)
{
    g_stdin_closed = 1;
}

int sfra_port_stdin_closed(void)
{
    return g_stdin_closed;
}

/* 前向声明：read_byte 里要用，两个函数都在本文件内定义。 */
void sfra_port_mark_stdin_closed(void);
int  sfra_port_stdin_closed(void);

/* ---------------------------------------------------------------- 临界区 */

void sfra_port_enter_critical(void)
{
    /* PC 上是单线程假 MCU：协议层在 main 循环里跑，ISR 由同一个循环
     * 模拟，两者不可能交错。真机上这里必须关 ISR。 */
}

void sfra_port_exit_critical(void)
{
}
