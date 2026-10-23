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
//     Payload 0~445B数据，有些命令没有数据
//     XOR     1B校验，把Cmd和Payload每个字节依次异或得到；传错一个字节就对不上
//   带长度前缀的帧（Cmd后紧跟1B payloadlen，收信方收1B就能定帧长=1+payloadlen）：
//     监控值帧 Monitor_Post、通用响应帧 Response，见下面枚举处说明。
//
// 订阅档位存在每个监控项的 tag 字段里（见 MENU_ITEM / my_main.h 位域说明）：
//   bit7 SUB：是否推送；bit6 FAST：高速/低速；bit5 GETTER：函数指针取数；
//   bit2~0：语义种类（x/y/yaw）。目录帧仍带完整 tag 字节，上位机解析位域。
//   连接后表内预置的 SUB/FAST/KIND 即默认订阅；Subscribe 改档时下位机只合并
//   上位机下发的 SUB+FAST，保留本机 GETTER 与 KIND（见 handle_command）。
//
// 心跳：上位机每80ms发Ping(A5 FF FF)，主控下一拍回Pong(同为A5 FF FF)；
// 主控每30拍(300ms)巡视一次，周期内没收到Ping就停推监控值并触发掉线收车，Ping恢复即续传。

// 动作命令处理函数：在rx线程上下文同步执行，只做快速赋值/置标志
// 是否拒绝忙时下发等行为细节由各函数自己决定（比如先查is_busy()）。
// 返回值：CMD_RET_OK=已处理；CMD_RET_BUSY=忙时拒绝——handle_command 据此回
// Response(明确拒绝·忙)，让上位机看得见"命令被拒"（原来是静默return，上位机零反馈）。
// p指向payload首字节（各输入字段紧凑排列），长度由命令表按类型累加可知。
typedef uint8_t (*CmdFunc)(const uint8_t *p);

// CmdFunc 返回值：OK=已处理；BUSY=忙时拒绝；REJECT=其他拒绝（如参数越界）。
// 非OK由handle_command映射为对应的Response拒绝状态，把拒绝摆给上位机
static constexpr uint8_t CMD_RET_OK = 0;
static constexpr uint8_t CMD_RET_BUSY = 1;
static constexpr uint8_t CMD_RET_REJECT = 2;

// 动作命令 tag：单字节位域（风格与 my_main.h 的 MENU_TAG_* 一致）
//   bit7 (0x80) CMD_TAG_RANGE  是否带取值范围：发送目录时按ranges指针是否为空自动置位，表项里不用填
//   bit6~4      CMD_TAG_ARGC_* 参数个数0~7（上限7，参数个数的唯一来源）
//   bit3~0      CMD_TAG_SPEC_* 特化命令：0普通 1停车，其余暂未定义
static constexpr uint8_t CMD_TAG_RANGE = 0x80u;
static constexpr uint8_t CMD_TAG_ARGC_MASK = 0x70u;
static constexpr uint8_t CMD_TAG_ARGC_SHIFT = 4;
static constexpr uint8_t CMD_TAG_SPEC_MASK = 0x0Fu;
static constexpr uint8_t CMD_SPEC_NORMAL = 0u;
static constexpr uint8_t CMD_SPEC_STOP = 1u;

// 拼一条命令的tag：argc=参数个数(0~7)，spec=特化类型。
// bit7不用手填：发送目录时按ranges是否为空自动置位，表项不会两处不一致。
static inline uint8_t cmd_tag_make(uint8_t argc, uint8_t spec)
{
    return static_cast<uint8_t>(((argc & 0x07u) << CMD_TAG_ARGC_SHIFT) |
                                (spec & CMD_TAG_SPEC_MASK));
}

// 读一条命令tag里的参数个数（bit6~4）
static inline uint8_t cmd_tag_argc(uint8_t tag)
{
    return static_cast<uint8_t>((tag & CMD_TAG_ARGC_MASK) >> CMD_TAG_ARGC_SHIFT);
}

// ---------- 通用响应帧（Response）的 status 字节 ----------
// 协议文档三端同步。位域：
//   bit7  类别  0=需补发（重发可能成功）          1=明确拒绝（重发无意义，提示用户）
//   bit6  模式  0=原因串（低6位=串长1~63）         1=内定err（低6位=err号0~63，不带串）
//   bit5~0 载荷 串长或内定err号，按bit6解释
// 分区（每个字节值都有定义，无洞无越界）：
//   0x00      OK，一切正常
//   0x01~0x3F 需补发+原因串（长度=低6位，UTF-8中文）
//   0x40~0x7F 需补发+内定err（号=低6位）
//   0x80      明确拒绝，无原因串（裸拒绝）
//   0x81~0xBF 明确拒绝+原因串（长度=低6位）
//   0xC0~0xFF 明确拒绝+内定err（号=低6位）
// 内定err表按类别各自编号（协议文档定义，三端同步，小而稳；字符串是长尾兜底，
// 可带运行时值如"下标37超出监控表"，任何版本上位机都能显示）：
//   需补发类(0x40+n): 0=通用重试 1=校验失败 2=命令字未注册 3~63预留
//   拒绝类(0xC0+n):   0=通用拒绝 1=下标超表 2=类型不可写 3=忙   4~63预留
static constexpr uint8_t RESP_OK = 0x00u;
static constexpr uint8_t RESP_CLASS_MASK = 0x80u; // bit7 类别
static constexpr uint8_t RESP_MODE_MASK = 0x40u;  // bit6 模式
static constexpr uint8_t RESP_PAYLOAD_MASK = 0x3Fu;
// 常用组合值（内定err）
static constexpr uint8_t RESP_RETRY_GENERIC = 0x40u | 0u;       // 需补发·通用
static constexpr uint8_t RESP_RETRY_XOR = 0x40u | 1u;           // 需补发·校验失败
static constexpr uint8_t RESP_RETRY_UNKNOWN_CMD = 0x40u | 2u;   // 需补发·命令字未注册
static constexpr uint8_t RESP_REJECT_GENERIC = 0xC0u | 0u;      // 拒绝·通用
static constexpr uint8_t RESP_REJECT_INDEX_RANGE = 0xC0u | 1u;  // 拒绝·下标超表
static constexpr uint8_t RESP_REJECT_TYPE_UNWRITABLE = 0xC0u | 2u; // 拒绝·类型不可写
static constexpr uint8_t RESP_REJECT_BUSY = 0xC0u | 3u;         // 拒绝·忙

// 一条动作命令的登记表项，全部为const静态描述，各子系统在自己文件里写成
// const数组，构造USB_Comm时整张表传入。payload长度不存：收帧时按data_type
// 逐字段value_size累加即可；参数个数也不单存，tag的bit6~4就是（上限7）。
struct CMD_ITEM
{
    CmdFunc func;                  // 处理函数指针
    const VALUE_TYPE *data_type;   // 输入字段类型表（无参时可为nullptr）
    const char *const *field_name; // 输入字段中文名表（无参时可为nullptr）
    const char *name;              // 命令中文名，目录帧与诊断用
    uint8_t tag;                   // tag位域：bit6~4参数个数 bit3~0特化命令（bit7恒0，发送时自动置位）
    // 取值范围：按字段顺序紧凑排列的{min1,max1,min2,max2,...}，每个值占 value_size(data_type[f])
    // 字节、小端——min/max 的类型与宽度必须和它那个字段的 VALUE_TYPE 对齐，不能一律当 float。
    // 用 void* 正是因为各字段类型可以不同（例如 uint8_t 编号 + float 电压）；
    // 定义端用同型数组（字段同型）或 __attribute__((packed)) 结构体（字段异型）给出这段字节流。
    // 非空时目录帧自动在字段描述后追加全部 min/max 并置 tag bit7；str/other 宽度为 0，无范围。
    const void *ranges;
};

class USB_Comm
{
private:
    enum Cmd_Post
    {
        // 目录项 Payload:
        // [index 1B][数据类型 1B][tag 1B][名称长度 1B][名称][单位长度 1B][单位][当前值?B取决于type]
        Post_Tunable_Directory, // 调参目录（tag 位调参表固定为0）
        Post_Monitor_Directory, // 监控目录；其中 tag 为位域字节（见 my_main.h）
        // 监控值帧 Payload: [payloadlen 1B][time 2B]([index 1B][值?B取决于type])×N
        //   payloadlen 本字段之后到校验前的总字节数；收信方收1B就能定帧长(=1+payloadlen)
        //   项数N不单独给：上位机按目录里的类型表逐项推进偏移，走到payload末尾为止
        //   值宽也不单独给：同上，由该项在目录里登记的类型决定
        //   time 下位机毫秒时钟低16位，小端，65.536s自然回绕；同一帧的项采样时刻相同
        Monitor_Post, // 监控值推送：一帧可含多项，最多 MON_PACK_MAX 项
        Tunable_Echo,           // 有内容命令 [type 1B][index索引1B][实际生效值?B][XOR]，type前缀自带宽度免查表
        Post_CMD_Directory,     // 动作命令目录帧：[命令字1B][tag 1B][名长][名][参数量][每参:类型+字段名长+字段名][bit7=1时:每参min+max，各按该参类型定宽]，一命令一帧
        // 通用响应帧（对下行命令的回执，让上位机知道"收到没、处理对没、要不要补发"）：
        //   Payload: [payloadlen 1B][原话cmd 1B][原话payload ?B][status 1B][原因串 ?B]
        //     payloadlen  本字段之后到校验前的总字节数（=1+原话payload长+1+原因串长），
        //                 收1B即定帧长，同监控值帧；最小2，取0/1为流错位
        //     原话cmd     回显收到的命令字；校验失败时该字节本身可能已损坏，仅作线索
        //     原话payload 回显收到的payload，截断到RESP_ECHO_MAX（下行帧实际最大56B，不可达）
        //     status      状态字节，位域与内定err表见文件上方 Response 说明
        //     原因串      UTF-8中文，≤63B，仅status串模式有
        //   覆盖：三个目录请求在burst末批当拍回OK（表为空收到即回）；Subscribe；
        //         动作命令回OK/忙。Ping有Pong、Set_Tunable成功有Tunable_Echo（显式回应），
        //         但Set_Tunable被拒时无回显兜底，补一帧拒绝响应
        Response,
        Pong = 0xFF,
    };
    enum Cmd_Get
    {
        Request_Tunable, // 无Payload，接收后发送可调参数目录(遍历tunable_items数组)
        Request_Monitor, // 无Payload，接收后发送监控目录，并开始按各项tag推送

        // Payload 2B: [index 1B][tag 1B]
        // tag 位域：上位机主要写 bit7(SUB)/bit6(FAST)；bit5 与 bit2~0 由下位机保留。
        // 旧习惯 tag 字节为 0 表示退订，对应 bit7=0，仍然成立。
        // index=0xFF 时对全部监控项套用同一套 SUB+FAST。
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
    // Payload上限445B：整帧最长448B(A5+Cmd+445+XOR)，仍小于USB单包512B，一帧不会被USB拆开。
    static constexpr uint16_t MAX_PAYLOAD = 445;
    static constexpr uint8_t SLOW_PHASES = 8; // 低速项轮转相数：单项刷新周期=8×10ms=80ms
    static constexpr uint8_t MON_PACK_MAX = 16; // 监控值帧最多并几项：满额先发一帧（最宽8B值也只有150B，payloadlen 1B 装得下）
    static constexpr uint8_t DIR_BATCH = 8;   // 调参/监控目录分批：每个10ms拍最多连续发8项
    static constexpr uint8_t UNIT_MAX = 8;    // 目录帧单位名截断长度（字节），如 m/s、deg/s
    static constexpr uint8_t DIR_NAME_MAX = 30; // 目录帧名称截断长度（字节）
    // ---- 动作命令表 ----
    static constexpr uint8_t CMD_BASE = 0x80;    // 用户命令段起点，0x80~0x9F共32个合法命令字
    static constexpr uint8_t CMD_DIR_BATCH = 4;  // 命令目录每拍最多发4条
    static constexpr uint8_t CMD_NAME_MAX = 32;  // 命令名截断长度（字节）
    static constexpr uint8_t CMD_FIELD_MAX = 24; // 单个字段名截断长度（字节）

    RxState rx_state = WAIT_HEAD;    // 当前处在接收的哪个阶段
    uint8_t rx_cmd = 0;              // 本帧收到的命令字，执行命令时用它判断要做什么
    uint8_t rx_payload[MAX_PAYLOAD]; // 本帧已收到的Payload
    uint16_t rx_len = 0;             // Payload已经收了几个字节（上限445超出uint8，用16位）
    uint16_t rx_expect = 0;          // 本帧Payload总共该收几个字节（由命令字查表得到）
    uint8_t rx_xor = 0;              // 从命令字开始逐字节累计的异或值，最后和收到的校验字节比

    // 三张表都在构造时由外部传入，指向各子系统写好的数组，本类只借用不拥有。
    // 调参表只读；监控表的tag会被订阅命令改写，所以不能是const；命令表全const。
    const MENU_ITEM *tunable_items;
    uint8_t tunable_count;
    MENU_ITEM *monitor_items;
    uint8_t monitor_count;
    const CMD_ITEM *cmd_items;
    uint8_t cmd_count;

    uint16_t tx_stamp = 0; // 本拍时间戳：get_time_ms()低16位，tx_tick开头取一次，同拍的监控帧共用
    uint8_t tick = 0;    // 节拍计数，每拍+1，uint8自然溢出回0，只用于低速项按 tick%8 轮转。
    uint8_t dir_tun = 0; // 目录发送倒计时：收到目录请求时置"还剩几批"，每发一批-1，到0结束。
    uint8_t dir_mon = 0; // 三张表各一个；同一拍内只发一个表的一批（tunable优先），保证目录不并行。
    uint8_t dir_cmd = 0;

    bool monitor_active = false; // 是否正在周期推送

    // 心跳看门狗（上位机每80ms发Ping）：
    //   ping_seen    本个300ms巡视周期(LINK_PATROL_TICKS拍)内收到过Ping，收到即置true
    //   link_ok      链路是否被认可；false时monitor_tick停推监控值（目录仍可正常发）
    //   pong_pending 收到Ping后置true，由tx_tick下一拍发Pong（写串口只能在tx任务）
    bool ping_seen = false;
    bool link_ok = true;
    bool pong_pending = false;

    static constexpr uint8_t LINK_PATROL_TICKS = 30; // 掉线巡视周期：30拍(300ms)没见Ping即断链

    // 调参回显槽：rx线程改完值写入该项index，tx下一拍从data_ptr重读真实值发Tunable_Echo后清0xFF。
    // 单字节槽、人工调参节奏低，一槽足够；0xFF表示无待回显
    uint8_t echo_index = 0xFF;

    // ---------- Response 待发队列 ----------
    // rx线程只入队（禁止写串口），tx_tick末尾统一发；Pong仍最优先（看门狗不能等）。
    // 槽位装变长内容：回显payload截断到RESP_ECHO_MAX（下行帧实际最大56B=7参×8B，
    // 截断不可达，只为让协议完整）；原因串截断到RESP_STR_MAX。
    // 满则丢最新：丢的只是一个ACK，上位机超时重发即自愈。
    static constexpr uint8_t RESP_ECHO_MAX = 64;  // 回显payload截断上限
    static constexpr uint8_t RESP_STR_MAX = 63;   // 原因串截断上限（协议上限）
    static constexpr uint8_t RESP_QUEUE_DEPTH = 8;
    struct RespSlot
    {
        uint8_t cmd;                 // 回显的命令字
        uint8_t status;              // 状态字节
        uint8_t echo_len;            // 回显payload实际字节数
        uint8_t str_len;             // 原因串实际字节数
        uint8_t echo[RESP_ECHO_MAX]; // 回显的payload
        char str[RESP_STR_MAX];      // 原因串（UTF-8，按字节数取用，不当C串）
    };
    RespSlot resp_q[RESP_QUEUE_DEPTH];
    uint8_t resp_head = 0; // 环形队列头（待发）；头=尾即空
    uint8_t resp_tail = 0; // 环形队列尾（下一个空位）

    int usb_fd = -1;                   // /dev/serial1的POSIX描述符，init里打开，收发共用
    uint8_t tx_frame[3 + MAX_PAYLOAD]; // 发送帧缓冲

    // 内建命令返回固定长度（static，无表可查）；动作命令长度查注册表，
    // 所以整体不是static——rx_task收到命令字时调用它决定再收几字节。
    // 返回0xFFFF表示不认识的命令：445超出uint8，未知哨兵不能用0xFF了。
    uint16_t cmd_payload_len(uint8_t cmd);
    static uint8_t value_size(VALUE_TYPE type);

    // 命令字落在0x80~0x9F且外部表里存在同index项时返回表项，否则nullptr
    const CMD_ITEM *find_cmd(uint8_t cmd) const;

    // 把一个参数项的当前值按协议写进out指向的缓冲，返回写了几个字节。
    // GETTER 项：调用 MenuFloatGetter 取 float 再按小端写入。
    uint8_t append_value(uint8_t *out, const MENU_ITEM &item);

    // append_value的逆操作：把帧里的value字节按项的真实类型写回data_ptr。
    // GETTER 项直接返回，不写。
    void write_value(const uint8_t *in, const MENU_ITEM &item);

    // payload已直接拼在tx_frame[2]起时调用：补帧头、命令、校验后整帧发出，len是payload字节数
    void send_frame(uint8_t cmd, uint16_t len);

    // 发目录的第batch批（下标连续的DIR_BATCH项），一项一帧，str/other跳过
    void send_dir_batch(const MENU_ITEM *items, uint8_t count, uint8_t cmd, uint8_t batch);

    // 发动作命令目录的第batch批：扫非空槽按注册序取连续CMD_DIR_BATCH条，一条一帧
    void send_cmd_dir_batch(uint8_t batch);

    // 入队一帧通用响应：echo=回显的payload（可为nullptr），str=原因串（可为nullptr，仅串模式）。
    // echo截断到RESP_ECHO_MAX、str截断到RESP_STR_MAX；队列满丢最新（丢ACK可自愈）。rx线程安全。
    void enqueue_response(uint8_t cmd, uint8_t status, const uint8_t *echo, uint16_t echo_len,
                          const char *str, uint8_t str_len);

    // 把队列里的Response帧全部发出（tx_tick末尾调用；Pong已在拍首发完）
    void send_responses();

    void handle_command(); // 一帧接收完整且校验通过后，按命令字只置请求标志，真正的发送留给tx_tick
    void monitor_tick();   // 按 tag 位域：SUB 才发；FAST 每拍；否则低速轮转（本拍不推进tick）

public:
    USB_Comm(const MENU_ITEM *tunable, uint8_t tunable_len,
             MENU_ITEM *monitor, uint8_t monitor_len,
             const CMD_ITEM *cmds, uint8_t cmd_len);

    void init(); // 打开/dev/serial1原始FIFO

    // 上位机改过调参的脏标志：Set_Tunable写入成功后由rx置true，本类只置不读不清。
    bool tunable_dirty = false;

    // 心跳掉线回调：tx_tick 巡视发现 link_ok 由真变假的当拍同步调用一次，nullptr=不通知。
    // 调用点在主循环10ms线程（tx_tick 由 my_while 调用），回调内禁止阻塞，只做快速收车；
    // 由顶层装配（my_Init）注入。
    void (*on_link_lost)() = nullptr;

    // 接收任务：阻塞在read()上等字节（O_NONBLOCK实测无效），字节一到就喂拼帧状态机，
    // 收齐完整帧立即执行。函数内部死循环，必须由独立vex任务调用，不要在主周期里轮询。
    void rx_task();

    // 发送节拍：每10ms调用一次。先回Pong，再发调参回显，然后发目录倒计时当前批
    // （每拍≤DIR_BATCH项），再按各项tag推送监控值（高速直发、低频轮转），
    // 最后把Response队列发完并推进tick。
    void tx_tick();
};

#endif
