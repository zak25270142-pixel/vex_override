#ifndef __COMMUNICATION_H__
#define __COMMUNICATION_H__

#include "my_main.h"

// 传输通道说明（重要，别和Smart口搞混）：
//   V5主控USB连电脑会枚举两个口：通信口(VEXcode下载用)和"User Port"。
//   User Port在用户程序里有两条newlib通道：
//     - fd 0/1/2 (stdin/stdout/stderr)：终端加工通道，printf能打去VEXcode终端，
//       但输出被加CRLF、输入走行编辑且未建立终端会话时RX被丢弃，不能传二进制；
//     - /dev/serial1：原始FIFO，二进制透传，本协议就用它（open后read/write）。
//   vexGenericSerial*那套API的index 0~20对应的是21个Smart口(RS485)，与USB无关。
//   User Port是USB虚拟串口，波特率无意义；走/dev/serial1时字节原样收发，无需转义。
//
// 上位机使用前提（实测两个坑）：
//   1. VEX扩展的"Enable User Terminal"必须设为Disable，否则Interactive Terminal
//      独占User Port，第三方程序(网页/PowerShell)发的数据主控收不到；
//   2. /dev/serial1的read()实测是阻塞的（O_NONBLOCK在V5 newlib上不生效），
//      所以接收必须放独立任务里挂等，数据到达即唤醒，不占CPU。
//
// 帧格式（字节流里怎么分出一条完整消息）：
//   A5 | Cmd | Payload... | XOR
//     A5      固定帧头，每一帧都以它开头，收信方靠它在乱码的字节流里找到帧的起点
//     Cmd     1B命令字，说明这帧是干什么的（见下面两个枚举）
//     Payload 0~128B数据，有些命令没有数据
//     XOR     1B校验，把Cmd和Payload每个字节依次异或得到；传错一个字节就对不上
//
// 订阅档位不靠位图，直接存在每个监控项的tag字段里（见MENU_ITEM）：
//   tag=0 不订阅；tag=1 低速区，8相轮扫约80ms；tag>=2 高速区，每10ms。
// tag在菜单表里写好，连接后天然就是默认订阅；上位机用Subscribe改档位，
// 写tag=0即退订，不需要单独的退订命令。

class USB_Comm
{
private:
    enum Cmd_Post
    {
        // 目录项 Payload:
        Post_Tunable_Directory, // 有内容命令 [CMD 1B][index索引1B][数据类型1B][中文名称长度1B][中文名称][当前数值?B取决于type 可选][XOR]  不发送type为other与str的项，color类型3B，bool与on_off类型1B
        Post_Monitor_Directory, // 有内容命令 格式同上
        Monitor_Post,           // 有内容命令 [CMD 1B][index索引1B][当前数值?B取决于type][XOR]，高速低速项长得一样，分区只影响发送节奏
        Ping = 0xFF,
    };
    enum Cmd_Get
    {
        Request_Tunable, // 无Payload，接收后发送可调参数目录(遍历tunable_items数组)
        Request_Monitor, // 无Payload，接收后发送监控目录，并开始按各项tag推送
        // Payload 2B: [index 1B][tag 1B]，把该项订阅档位改成tag
        // （0退订/1低速/2高速，也可写3/4/5恢复x/y/yaw语义档）；
        // index=0xFF时对全部项生效，tag=0即一键全部退订
        Subscribe,
        // 其他命令不扩展
        Pong = 0xFF,
    };

    static constexpr uint8_t FRAME_HEAD = 0xA5;
    static constexpr uint8_t MAX_PAYLOAD = 128; // 单帧Payload上限；整帧最长131B(A5+Cmd+128+XOR)，
                                                // 小于USB单包512B，一帧不会被USB拆开

    // 接收状态机的四个阶段。
    // 串口给上来的是一串连续字节，一次读取可能只拿到半帧，也可能两帧粘在一起，
    // 所以必须逐字节推进状态，而不能假设读一次就是一条完整消息。
    enum RxState
    {
        WAIT_HEAD, // 正在等帧头0xA5，来别的字节一律丢弃（包括乱码和上一帧的残尾）
        RX_CMD,    // 已收到帧头，下一个字节是命令字
        RX_DATA,   // 正在按命令字规定的长度收Payload
        RX_XOR,    // Payload收够了，等最后一个校验字节，对上了就执行命令
    };
    RxState rx_state = WAIT_HEAD;    // 当前处在接收的哪个阶段
    uint8_t rx_cmd;                  // 本帧收到的命令字，执行命令时用它判断要做什么
    uint8_t rx_payload[MAX_PAYLOAD]; // 本帧已收到的Payload
    uint8_t rx_len;                  // Payload已经收了几个字节
    uint8_t rx_expect;               // 本帧Payload总共该收几个字节（由命令字查表得到）
    uint8_t rx_xor;                  // 从命令字开始逐字节累计的异或值，最后和收到的校验字节比

    // 两张参数表，构造时由外部传入，指向LCD_menu.cpp里的数组。
    // 调参表只读；监控表的tag会被订阅命令改写，所以不能是const。
    const MENU_ITEM *tunable_items;
    uint8_t tunable_count;
    MENU_ITEM *monitor_items;
    uint8_t monitor_count;

    uint8_t slow_phase; // 低速项轮转号0~7，每次tx_tick推进一格，8次一循环
    // 是否正在周期推送。只有上位机主动要过监控目录后才置true开始推送，
    // 避免没人看的时候也在串口上白白发数据。
    bool monitor_active;
    int usb_fd = -1;                   // /dev/serial1的POSIX描述符，init里打开，收发共用
    uint8_t tx_frame[3 + MAX_PAYLOAD]; // 发送组帧缓冲，拼好后一次性write给USB
    // 收命令的任务和10ms推送任务都会发帧，必须用锁把"一整批帧"的发送包起来，
    // 否则目录帧的字节流可能和监控推送帧的字节互相穿插，两边都解析出废帧。
    vex::mutex tx_lock;

    // 查一个下发命令的Payload应该有几字节。
    // 返回0xFF表示不认识的命令——此时无法知道校验位在哪，只能放弃这帧重新找帧头。
    static uint8_t cmd_payload_len(uint8_t cmd);
    // 一个类型的值在帧里占几个字节：1/2/4/8，color固定3B；str和other返回0。
    static uint8_t value_size(VALUE_TYPE type);
    // 把一个参数项的当前值按协议写进out指向的缓冲，返回写了几个字节。
    // color拆成红绿蓝三个字节，bool/on_off写成0/1，其余类型按内存原样拷贝。
    uint8_t append_value(uint8_t *out, const MENU_ITEM &item);

    // Subscribe(index=0xFF)时把全部可发项的tag统一设为指定档位（语义标记会被覆盖）
    void set_all_tag(uint8_t tag);

    // payload已直接拼在tx_frame[2]起时调用：补帧头、命令、校验后整帧发出，len是payload字节数
    void send_frame(uint8_t cmd, uint8_t len);
    void send_directory(const MENU_ITEM *items, uint8_t count, uint8_t cmd); // 把一张表逐项发成目录帧
    void handle_command();                                                   // 一帧接收完整且校验通过后，按命令字执行
    void monitor_tick();                                                     // 高速项直发+低速项按轮转号发，然后推进轮转

public:
    // tunable/tunable_len：可调参数表及其项数；monitor/monitor_len：监控参数表及其项数。
    // 两张表必须是全局存活的数组（构造期间只保存指针，不拷贝内容）。
    USB_Comm(const MENU_ITEM *tunable, uint8_t tunable_len,
             MENU_ITEM *monitor, uint8_t monitor_len);

    void init(); // 打开/dev/serial1原始FIFO（上位机侧须先禁用VEX的User Terminal）
    // 接收任务：阻塞在read()上等字节（O_NONBLOCK实测无效），字节一到就喂拼帧状态机，
    // 收齐完整帧立即执行。函数内部死循环，必须由独立vex任务调用，不要在主周期里轮询。
    void rx_task();
    // 发送节拍：每10ms调用一次，按各项tag推送监控值（高速直发、低速轮转）。
    void tx_tick();
};

#endif
