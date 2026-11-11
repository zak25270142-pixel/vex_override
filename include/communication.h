#ifndef __COMMUNICATION_H__
#define __COMMUNICATION_H__

#include "my_main.h"

typedef uint8_t(*CmdFunc)(const uint8_t *p);
// 一条动作命令的登记表项
struct CMD_ITEM
{
    CmdFunc func;                  // 处理函数指针
    const VALUE_TYPE *data_type;   // 输入字段类型表（无参时可为nullptr）
    const char *const *field_name; // 输入字段中文名表（无参时可为nullptr）
    uint8_t field_count;           // 输入字段个数，<8
    const char *name;              // 命令中文名，目录帧与诊断用
    uint8_t tag;                   // tag位域：bit6~4参数个数 bit3~0特化命令（bit7恒0，发送时自动置位）
    const void *ranges;            // 取值范围：按字段顺序紧凑排列的{min1,max1,min2,max2,...}

    // tag 单字节位域（风格与 my_main.h 的 MENU_TAG_* 一致）：
    //   bit7 RANGE / bit6~4 ARGC 参数个数（=field_count，0~7）/ bit3~0 SPEC 特化命令（0普通 1停车）
    static constexpr uint8_t if_range = 0x80u; // bit7：本帧是否附带取值范围
    static constexpr uint8_t spc_mask = 0x0Fu;
    enum Spec : uint8_t
    {
        normal = 0, // 普通命令
        stop,       // 停车命令
    };

    constexpr CMD_ITEM(CmdFunc f, const VALUE_TYPE *type, const char *const *fld_nm,
                       uint8_t count, const char *nm, Spec spc = normal, const void *rgs = nullptr)
        : func(f), data_type(type), field_name(fld_nm), field_count(count), name(nm),
        tag(((count & 0x07u) << 4) | (spc & spc_mask)), ranges(rgs)
    {
    }
};

class USB_Comm
{
public:
    // ---------- 通用响应帧（Response）的 status 字节 ----------
    // 帧: A5 | Response | L | 原话cmd | status | 附加段 | 原话payload | XOR
    //   L 覆盖本字节之后到校验前的全部字节；status 固定在 p[2]，一眼可得。
    //   附加段长度由 status 自己给出，不用反推：
    //     0x00              OK 无附加段（传完通知 / 通用 OK）
    //     0x80（OK+info）/ 内定err类  [len][data...]，len=0 即不带内容
    //     串类              裸串 data...，长度 = status & 0x3F（低 6 位就是串长）
    //   原话payload 只做 ACK 匹配；长度为 0 = 匹配该 cmd 全部。
    // 分区（范围分类，位运算同构）：
    //   0x00      OK，无 info（目录传完通知 / 通用 OK）
    //   0x80      OK + info（整表请求收到即回，info = 1B 表内总项数）
    //   0x01~0x3F 需补发+串类      0x40~0x7F 需补发+内定err类
    //   0x81~0xBF 明确拒绝+串类    0xC0~0xFF 明确拒绝+内定err类
    //   OK 族 = (status & 0x7F) == 0；其余 &0x80 判类别、&0x40 判串/err。
    //   低 6 位两类都有实义：串类 = 串长(1~63)，内定err类 = err号(0~63)。
    // 0x42 / 0xC4 的 info 是 1B 目录补发池空余，供上位机规划等待重发。
    enum RespStatus : uint8_t
    {
        ok = 0x00,                    // OK（无 info 段）
        ok_info = 0x80,               // OK + info（整表请求收到即回，带表项数）
        retry = 0x40,                 // 需补发·通用
        xor_error,                    // 需补发·校验失败
        retry_pool_full = 0x42,       // 需补发·目录补发池空间不足（info=1B 当前空余）
        reject = 0xC0,                // 拒绝·通用
        index_error,                  // 拒绝·下标超表
        busy,                         // 拒绝·忙
        cmd_unknown,                  // 拒绝·命令字未注册（重发无意义）
        reject_in_flight = 0xC4,      // 拒绝·同表全量/增量在途（info=1B 当前空余）
    };

private:
    enum Cmd_Post
    {
        Post_Tunable_Directory, // [index 1B][数据类型 1B][tag 1B][名称长度 1B][名称][单位长度 1B][单位][当前值?B取决于type]
        Post_Monitor_Directory, // 监控目录；其中 tag 为位域字节（见 my_main.h）
        Monitor_Post,      // 监控值帧 Payload: [payloadlen 1B][time 2B]([index 1B][值?B取决于type])×N
        Tunable_Echo,      // 有内容命令 [type 1B][index索引1B][实际生效值?B][XOR]，type前缀自带宽度免查表
        Post_CMD_Directory,// 动作命令目录帧：[命令字1B][tag 1B][名长][名][参数量][每参:类型+字段名长+字段名][bit7=1时:每参min+max，各按该参类型定宽]，一命令一帧
        Response,// 通用响应帧  [L][原话cmd][status][附加段][原话payload]
        Pong = 0xFF,
    };
    enum Cmd_Get
    {
        Request_Tunable, // Payload:[len 1B][index len B]：len=0全量；非0=只补发这些下标（单段最多 REQ_BUF_CAP 个，受池剩余空间约束）
        Request_Monitor, // 同上；接收后发送监控目录，并开始按各项tag推送
        Subscribe,       // [index 1B][tag 1B]
        Set_Tunable,     // 固定9B: [index 1B][value 8B]
        Request_Command, // 同Request_Tunable的[len][index]格式；接收后分批回传动作命令目录（遍历已注册命令槽）
        Ping = 0xFF,
    };
    enum RxState
    {
        wait_head, // 等帧头0xA5
        wait_cmd,    // 等待命令字
        wait_len,    // 等长度前缀字节（仅目录请求：Cmd后先收1B index个数，再收这么多个字节）
        wait_data,   // 正在按命令字规定的长度收Payload
        wait_xor,    // 等校验字节，无误后执行命令
    };

    static constexpr uint8_t FRAME_HEAD = 0xA5;      // 固定帧头
    static constexpr uint16_t MAX_PAYLOAD = 445;     // Payload上限445B：整帧最长448B
    static constexpr uint16_t CMD_WAIT_LEN = 0xFFFE; //等待长度的哨兵值

    static constexpr uint8_t REQ_BUF_CAP = 48;  // 三张表共用的增量下标池，只放下标：三个 tx_ptr 天然切段，48 个正好装满；与 config.req_list_max 同值，足够 4~5 张表
    static constexpr uint8_t SLOW_PHASES = 8;   // 低速项轮转相数：单项刷新周期=8×10ms=80ms
    static constexpr uint8_t MON_PACK_MAX = 16; // 监控值帧最多并几项：满额先发一帧（最宽8B值也只有150B，payloadlen 1B 装得下）
    static constexpr uint8_t DIR_BATCH = 8;     // 调参/监控目录分批：每个10ms拍最多连续发8项
    static constexpr uint8_t UNIT_MAX = 8;      // 目录帧单位名截断长度（字节），如 m/s、deg/s
    static constexpr uint8_t DIR_NAME_MAX = 30; // 目录帧名称截断长度（字节）
    // ---- 动作命令表 ----
    static constexpr uint8_t CMD_BASE = 0x80;    // 用户命令段起点，0x80~0x9F共32个合法命令字
    static constexpr uint8_t CMD_DIR_BATCH = 4;  // 命令目录每拍最多发4条
    static constexpr uint8_t CMD_NAME_MAX = 32;  // 命令名截断长度（字节）
    static constexpr uint8_t CMD_FIELD_MAX = 24; // 单个字段名截断长度（字节）

    RxState rx_state = wait_head;    // 当前处在接收的哪个阶段
    uint8_t rx_cmd = 0;              // 本帧收到的命令字，执行命令时用它判断要做什么
    uint8_t rx_payload[MAX_PAYLOAD]; // 本帧已收到的Payload
    uint16_t rx_len = 0;             // Payload已经收了几个字节（上限445超出uint8，用16位）
    uint16_t rx_expect = 0;          // 本帧Payload总共该收几个字节（由命令字查表得到）
    uint8_t rx_xor = 0;              // 从命令字开始逐字节累计的异或值，最后和收到的校验字节比

    // 三张表
    const MENU_ITEM *tunable_items;
    uint8_t tunable_count;
    MENU_ITEM *monitor_items;
    uint8_t monitor_count;
    const CMD_ITEM *cmd_items;
    uint8_t cmd_count;

    uint16_t tx_stamp = 0; // 本拍时间戳：get_time_ms()低16位，tx_tick开头取一次，同拍的监控帧共用
    uint8_t tick = 0;    // 节拍计数，每拍+1，uint8自然溢出回0，只用于低速项按 tick%8 轮转。

    // 全量请求：整张表还剩几项要发，每拍发一批、扣到0结束
    uint8_t tun_need_tx = 0;
    uint8_t mon_need_tx = 0;
    uint8_t cmd_need_tx = 0;

    // 增量（补发）请求：池里只放下标，三段天然由三个 tx_ptr 切出
    uint8_t tun_tx_ptr = 0;
    uint8_t mon_tx_ptr = 0;
    uint8_t cmd_tx_ptr = 0;
    uint8_t req_idx_buf[REQ_BUF_CAP] = {};

    bool monitor_active = false; // 是否正在周期推送

    // 心跳看门狗（上位机每80ms发Ping）：
    //   link_ok      链路是否被认可；false时monitor_tick停推监控值（目录仍可正常发）
    //                初值必须 false：掉线回调只在"上周期认可、本周期没见Ping"的边沿触发，
    //                初值给 true 会让上电后第一个巡视周期（300ms）必然误触发一次——
    //                现在被手动档挡住看不出来，自动任务流一开机不在手动档就会被误收一次。
    //   pong_pending 收到Ping后置true，由tx_tick下一拍发Pong（写串口只能在tx任务）
    bool ping_seen = false; // 本个300ms巡视周期(LINK_PATROL_TICKS拍)内收到过Ping，收到即置true
    bool link_ok = false;
    bool pong_pending = false;

    static constexpr uint8_t LINK_PATROL_TICKS = 30; // 掉线巡视周期：30拍(300ms)没见Ping即断链

    // 调参回显槽：rx线程改完值写入该项index，tx下一拍从data_ptr重读真实值发Tunable_Echo后清0xFF。
    // 单字节槽、人工调参节奏低，一槽足够；0xFF表示无待回显
    uint8_t echo_index = 0xFF;

    // ---------- Response 待发队列 ----------
    // rx线程只入队（禁止写串口），tx_tick末尾统一发；Pong仍最优先（看门狗不能等）。
    // 满则丢最新：丢的只是一个ACK，上位机超时重发即自愈。
    static constexpr uint8_t RESP_ECHO_MAX = 64;  // 原话payload上限（目录请求最长 1+48=49B）
    static constexpr uint8_t RESP_INFO_MAX = 64;  // 附加段上限（[len][data] 或裸串）
    static constexpr uint8_t RESP_QUEUE_DEPTH = 8;
    struct RespSlot
    {
        uint8_t cmd;                       // 回显的命令字（原话cmd）
        uint8_t status;                    // 状态字节，固定在 p[2]
        uint8_t raw_len;                   // 原话payload实际字节数（0=无回显，匹配该cmd全部）
        uint8_t info_len;                  // 附加段实际字节数（串类=串长，其余=1+len；ok 时为 0）
        uint8_t raw[RESP_ECHO_MAX];        // 原话payload，只做 ACK 匹配
        uint8_t info[RESP_INFO_MAX];       // [len][data...] 或裸串
    };
    RespSlot resp_q[RESP_QUEUE_DEPTH];
    uint8_t resp_head = 0; // 环形队列头（待发）；头=尾即空
    uint8_t resp_tail = 0; // 环形队列尾（下一个空位）

    int usb_fd = -1;                   // /dev/serial1的POSIX描述符，init里打开，收发共用
    uint8_t tx_frame[3 + MAX_PAYLOAD]; // 发送帧缓冲

    // 内建命令返回固定长度（static，无表可查）；动作命令长度查注册表，
    // 所以整体不是static——rx_task收到命令字时调用它决定再收几字节。
    // 目录请求返回CMD_WAIT_LEN（长度前缀模式）；0xFFFF表示不认识的命令：445超出uint8，未知哨兵不能用0xFF了。
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

    // 发目录的一批共n项，一项一帧：idx=nullptr时按表内下标[off,off+n)连续取（全量）；
    // 非空时按idx[off..off+n)寻址（补发）。str/other照发（值宽0，见Cmd_Post枚举处说明）。
    void send_dir_batch(const MENU_ITEM *items, uint8_t count, uint8_t cmd, const uint8_t *idx,
                        uint8_t off, uint8_t n);

    // 发动作命令目录的一批共n条：idx=nullptr按注册序[off,off+n)，非空按idx寻址（补发），一条一帧
    void send_cmd_dir_batch(const uint8_t *idx, uint8_t off, uint8_t n);

    // 入队一帧通用响应：raw=原话payload（只做匹配），info=附加信息内容（不含长度前缀）。
    // 0x00 忽略 info；0x80（OK+info）/ 内定err类自动加 1B 长度；串类写裸串并把低 6 位改成串长。
    void enqueue_response(uint8_t cmd, uint8_t status, const uint8_t *raw, uint16_t raw_len,
                          const uint8_t *info, uint8_t info_len);

    // 目录请求专用：现场重建原话payload [len][idx...]（rx_payload 不含 len 前缀），
    // 再按 status 入队。整表成功回 ok_info+表项数，末批/空表回 ok+无回显的"传完通知"。
    void enqueue_dir_response(uint8_t cmd, uint8_t status, const uint8_t *info, uint8_t info_len);

    // 池当前可放的下标数 = REQ_BUF_CAP - max(三个 tx_ptr)，随回执 info 带给上位机。
    uint8_t req_buf_free() const;
    // 作废一张表的增量段：后续段 memmove 前移、同步减它们的 ptr，池保持紧密排布。
    void drop_increment(uint8_t &tx_ptr);
    // 校验一次目录请求：len=0全量；非0=增量列表（下标 < count，不超 REQ_BUF_CAP 个）。
    bool req_list_ok(const uint8_t *p, uint8_t len, uint8_t count);
    // 登记一次目录请求：返回 RespStatus(ok=成功)。全量会打断并作废本表增量段；
    // 全量在途或本表增量在途时拒绝增量，池放不下时回"需补发"。池里只存下标。
    uint8_t req_register(uint8_t len, const uint8_t *p, uint8_t count,
                         uint8_t &need_tx, uint8_t &tx_ptr);

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
