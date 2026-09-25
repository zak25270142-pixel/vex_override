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
// 帧格式（字节流里怎么分出一条完整消息）：
//   A5 | Cmd | Payload... | XOR
//     A5      固定帧头，每一帧都以它开头，收信方靠它在乱码的字节流里找到帧的起点
//     Cmd     1B命令字，说明这帧是干什么的（见下面两个枚举）
//     Payload 0~255B数据，有些命令没有数据
//     XOR     1B校验，把Cmd和Payload每个字节依次异或得到；传错一个字节就对不上
//
// 订阅档位不靠位图，直接存在每个监控项的tag字段里（见MENU_ITEM）：
//   tag=0 不订阅；tag=1 低速区，8相轮扫约80ms；tag>=2 高速区，每10ms。
// tag在菜单表里写好，连接后天然就是默认订阅；上位机用Subscribe改档位，
// 写tag=0即退订，不需要单独的退订命令。
//
// 心跳：上位机每1s发Ping(A5 FF FF)，主控下一拍回Pong(同为A5 FF FF)；
// 主控每256拍(2.56s)巡视一次，周期内没收到Ping就停推监控值，Ping恢复即续传。

// 动作命令处理函数：在rx线程上下文同步执行，规矩和handle_command一样——
// 只做快速赋值/置标志，不许write串口、不许Delay或任何会让出CPU的调用；
// 是否拒绝忙时下发等行为细节由各函数自己决定（比如先查is_busy()）。
// p指向payload首字节（各输入字段紧凑排列），长度由命令表按类型累加可知。
typedef void (*CmdFunc)(const uint8_t *p);

// 一条动作命令的登记表项，全部为const静态描述，各子系统在自己文件里写成
// const数组，构造USB_Comm时整张表传入。payload长度不存：收帧时按data_type
// 逐字段value_size累加即可，避免表项与真实字段两处不一致。
struct CMD_ITEM
{
    CmdFunc func;                  // 处理函数指针
    uint8_t index;                 // 命令字，范围0x80~0x9F
    const VALUE_TYPE *data_type;   // 输入字段类型表（无参时可为nullptr）
    const char *const *field_name; // 输入字段中文名表（无参时可为nullptr）
    uint8_t input_num;             // 输入字段数量，上限6
    const char *name;              // 命令中文名，目录帧与诊断用
};

class USB_Comm
{
private:
    enum Cmd_Post
    {
        // 目录项 Payload:
        Post_Tunable_Directory, // 有内容命令 [CMD 1B][index索引1B][数据类型1B][中文名称长度1B][中文名称][当前数值?B取决于type 可选][XOR]
        Post_Monitor_Directory, // 有内容命令 格式同上
        Monitor_Post,           // 有内容命令 [CMD 1B][index索引1B][当前数值?B取决于type][XOR]
        Tunable_Echo,           // 有内容命令 [CMD 1B][index索引1B][实际生效值?B取决于type][XOR]
        Post_CMD_Directory,     // 动作命令目录帧：[命令字1B][名长][名][参数量][每参:类型+字段名长+字段名]，一命令一帧
        Pong = 0xFF,
    };
    enum Cmd_Get
    {
        Request_Tunable, // 无Payload，接收后发送可调参数目录(遍历tunable_items数组)
        Request_Monitor, // 无Payload，接收后发送监控目录，并开始按各项tag推送

        // Payload 2B: [index 1B][tag 1B]，把该项订阅档位改成tag
        // （0退订/1低速/2高速，也可写3/4/5恢复x/y/yaw语义档）；
        // index=0xFF时对全部项生效，tag=0即一键全部退订
        Subscribe,

        // Payload固定9B: [index 1B][value 8B]，按调参表里该项的真实类型只取前若干字节，
        // 不足8B的类型高位补零。改完后主控回一帧Tunable_Echo确认真实生效值。
        Set_Tunable,
        Request_Command, // 无Payload，接收后分批回传动作命令目录（遍历已注册命令槽）
        Ping = 0xFF,
    };
    enum RxState
    {
        WAIT_HEAD, // 等帧头0xA5
        RX_CMD,    // 等待命令字
        RX_DATA,   // 正在按命令字规定的长度收Payload
        RX_XOR,    // 等校验字节，无误后执行命令
    };

    static constexpr uint8_t FRAME_HEAD = 0xA5; // 固定帧头
    // Payload上限取uint8能表达的最大值：命令帧多参数时余量充足。
    // 整帧最长258B(A5+Cmd+255+XOR)，仍小于USB单包512B，一帧不会被USB拆开。
    static constexpr uint16_t MAX_PAYLOAD = 255;
    static constexpr uint8_t SLOW_PHASES = 8; // 低速项轮转相数：单项刷新周期=8×10ms=80ms
    static constexpr uint8_t DIR_BATCH = 8;   // 调参/监控目录分批：每个10ms拍最多连续发8项
    // ---- 动作命令表 ----
    static constexpr uint8_t CMD_BASE = 0x80;    // 用户命令段起点，0x80~0x9F共32个合法命令字
    static constexpr uint8_t CMD_SLOT_MAX = 32;  // 合法命令字个数，仅用于收帧时的范围校验
    static constexpr uint8_t CMD_DIR_BATCH = 4;  // 命令目录每拍最多发4条
    static constexpr uint8_t CMD_NAME_MAX = 24;  // 命令名截断长度（字节）
    static constexpr uint8_t CMD_FIELD_MAX = 10; // 单个字段名截断长度（字节）
    static constexpr uint8_t CMD_INPUT_MAX = 6;  // 单命令参数个数上限

    RxState rx_state = WAIT_HEAD;    // 当前处在接收的哪个阶段
    uint8_t rx_cmd = 0;              // 本帧收到的命令字，执行命令时用它判断要做什么
    uint8_t rx_payload[MAX_PAYLOAD]; // 本帧已收到的Payload
    uint8_t rx_len = 0;              // Payload已经收了几个字节
    uint8_t rx_expect = 0;           // 本帧Payload总共该收几个字节（由命令字查表得到）
    uint8_t rx_xor = 0;              // 从命令字开始逐字节累计的异或值，最后和收到的校验字节比

    // 三张表都在构造时由外部传入，指向各子系统写好的数组，本类只借用不拥有。
    // 调参表只读；监控表的tag会被订阅命令改写，所以不能是const；命令表全const。
    const MENU_ITEM *tunable_items;
    uint8_t tunable_count;
    MENU_ITEM *monitor_items;
    uint8_t monitor_count;
    const CMD_ITEM *cmd_items;
    uint8_t cmd_count;

    uint8_t tick = 0;    // 节拍计数，每拍+1，uint8自然溢出回0，只用于低速项按 tick%8 轮转。
    uint8_t dir_tun = 0; // 目录发送倒计时：收到目录请求时置"还剩几批"，每发一批-1，到0结束。
    uint8_t dir_mon = 0; // 三张表各一个；同一拍内只发一个表的一批（tunable优先），保证目录不并行。
    uint8_t dir_cmd = 0;

    bool monitor_active = false; // 是否正在周期推送

    // 心跳看门狗（上位机每1s发Ping）：
    //   ping_seen    本个2.56s巡视周期(256拍)内收到过Ping，收到即置true
    //   link_ok      链路是否被认可；false时monitor_tick停推监控值（目录仍可正常发）
    //   pong_pending 收到Ping后置true，由tx_tick下一拍发Pong（写串口只能在tx任务）
    bool ping_seen = false;
    bool link_ok = true;
    bool pong_pending = false;

    // 调参回显槽：rx线程改完值写入该项index，tx下一拍从data_ptr重读真实值发Tunable_Echo后清0xFF。
    // 单字节槽、人工调参节奏低，一槽足够；0xFF表示无待回显
    uint8_t echo_index = 0xFF;

    int usb_fd = -1;                   // /dev/serial1的POSIX描述符，init里打开，收发共用
    uint8_t tx_frame[3 + MAX_PAYLOAD]; // 发送帧缓冲

    // 内建命令返回固定长度（static，无表可查）；动作命令长度查注册表，
    // 所以整体不是static——rx_task收到命令字时调用它决定再收几字节。
    uint8_t cmd_payload_len(uint8_t cmd);
    static uint8_t value_size(VALUE_TYPE type);

    // 命令字落在0x80~0x9F且外部表里存在同index项时返回表项，否则nullptr
    const CMD_ITEM *find_cmd(uint8_t cmd) const;

    // 把一个参数项的当前值按协议写进out指向的缓冲，返回写了几个字节。
    uint8_t append_value(uint8_t *out, const MENU_ITEM &item);

    // append_value的逆操作：把帧里的value字节按项的真实类型写回data_ptr。
    void write_value(const uint8_t *in, const MENU_ITEM &item);

    // payload已直接拼在tx_frame[2]起时调用：补帧头、命令、校验后整帧发出，len是payload字节数
    void send_frame(uint8_t cmd, uint8_t len);

    // 发目录的第batch批（下标连续的DIR_BATCH项），一项一帧，str/other跳过
    void send_dir_batch(const MENU_ITEM *items, uint8_t count, uint8_t cmd, uint8_t batch);

    // 发动作命令目录的第batch批：扫非空槽按注册序取连续CMD_DIR_BATCH条，一条一帧
    void send_cmd_dir_batch(uint8_t batch);

    void handle_command(); // 一帧接收完整且校验通过后，按命令字只置请求标志，真正的发送留给tx_tick
    void monitor_tick();   // 高速项直发+低速项按tick轮转相发（本拍不推进tick）

public:
    USB_Comm(const MENU_ITEM *tunable, uint8_t tunable_len,
             MENU_ITEM *monitor, uint8_t monitor_len,
             const CMD_ITEM *cmds, uint8_t cmd_len);

    void init(); // 打开/dev/serial1原始FIFO

    // 上位机改过调参的脏标志：Set_Tunable写入成功后由rx置true，本类只置不读不清。
    bool tunable_dirty = false;

    // 接收任务：阻塞在read()上等字节（O_NONBLOCK实测无效），字节一到就喂拼帧状态机，
    // 收齐完整帧立即执行。函数内部死循环，必须由独立vex任务调用，不要在主周期里轮询。
    void rx_task();

    // 发送节拍：每10ms调用一次。先发目录倒计时当前批（每拍≤DIR_BATCH项），
    // 再按各项tag推送监控值（高速直发、低速轮转），最后推进tick。
    void tx_tick();
};

#endif
