#include "communication.h"
#include <string.h>
#include <fcntl.h>  // open/O_RDWR
#include <unistd.h> // read/write：POSIX层直连raw FIFO，绕开stdio的CRLF加工

// 查下发命令的Payload固定长度。
// 接收状态机收到命令字后要靠这个数字知道"后面再收几个字节是Payload、
// 再下一个字节就是校验"。不认识的命令返回0xFF，调用方据此放弃整帧。
uint8_t USB_Comm::cmd_payload_len(uint8_t cmd)
{
    switch (cmd)
    {
    case Request_Tunable: // 目录请求都只是"我要"一个信号，不带数据
    case Request_Monitor:
    case Request_Command:
    case Ping:
        return 0;
    case Subscribe:
        return 2; // [index][tag]，tag 为位域；bit7=0 即退订
    case Set_Tunable:
        return 9; // [index][value×8B]，定长，实际宽度由下位机按表里类型取
    }

    // 动作命令：payload长度=各字段类型宽度之和（紧凑排列，无填充）
    const CMD_ITEM *item = find_cmd(cmd);
    if (item == nullptr)
        return 0xFF; // 既不是内建命令表里也没有：无法定位校验位，整帧放弃
    uint8_t len = 0;
    for (uint8_t i = 0; i < item->input_num; i++)
        len += value_size(item->data_type[i]);
    return len;
}

// 命令字必须整段落进0x80~0x9F（不能只看低5位，否则0x00也会误命中），
// 再在外部传入的命令表里线性找同index项。表最多32条，每帧只查一次，遍历成本可忽略。
const CMD_ITEM *USB_Comm::find_cmd(uint8_t cmd) const
{
    if (cmd < CMD_BASE || cmd >= CMD_BASE + CMD_SLOT_MAX)
        return nullptr;
    for (uint8_t i = 0; i < cmd_count; i++)
        if (cmd_items[i].index == cmd)
            return &cmd_items[i];
    return nullptr;
}

// 其余成员都在头文件声明处用类内初始化给了初值，构造函数只剩三张表的指针/项数
// 必须由外部传入，没别的活可干。
USB_Comm::USB_Comm(const MENU_ITEM *tunable, uint8_t tunable_len,
                   MENU_ITEM *monitor, uint8_t monitor_len,
                   const CMD_ITEM *cmds, uint8_t cmd_len)
    : tunable_items(tunable), tunable_count(tunable_len),
      monitor_items(monitor), monitor_count(monitor_len),
      cmd_items(cmds), cmd_count(cmd_len)
{
}

void USB_Comm::init()
{
    // 打开User Port原始FIFO：一个描述符同时收发。
    // 实测O_NONBLOCK在V5 newlib上不生效，read是阻塞式的——rx_task本就是专职
    // 接收的独立任务，挂起等数据不占CPU，正好合适，所以这里也不指望非阻塞。
    usb_fd = open("/dev/serial1", O_RDWR);
}

// 一个类型在帧里占几个字节。宽度必须和枚举类型的真实内存宽度严格一致，
// 发送时按这个宽度从变量地址开始读，给错宽度会读到相邻内存的垃圾数据。
uint8_t USB_Comm::value_size(VALUE_TYPE type)
{
    switch (type)
    {
    case type_uint8_t:
    case type_int8_t:
    case type_bool:
    case type_on_off:
        return 1;
    case type_uint16_t:
    case type_int16_t:
        return 2;
    case type_uint32_t:
    case type_int32_t:
    case type_float:
        return 4;
    case type_uint64_t:
    case type_int64_t:
    case type_double:
        return 8;
    case type_color:
        return 3; // 颜色只传RGB三个分量
    default:
        return 0; // str、other不可发
    }
}

// 把参数当前值序列化到发送缓冲。
// 参数out要由调用方保证至少有value_size(type)字节的空间。
// 返回实际写入的字节数，调用方据此推进缓冲位置。
// GETTER：data_ptr 为 MenuFloatGetter，只走 float 小端（监控电机遥测约定）。
uint8_t USB_Comm::append_value(uint8_t *out, const MENU_ITEM &item)
{
    if (item.is_getter())
    {
        MenuFloatGetter get = reinterpret_cast<MenuFloatGetter>(item.data_ptr);
        float v = get ? get() : 0.0f;
        memcpy(out, &v, 4);
        return 4;
    }

    switch (item.data_type)
    {
    case type_color:
    {
        // 菜单里颜色按0xRRGGBB存在一个32位整数里（见vex::color），
        // 拆成红、绿、蓝三个字节，按人看十六进制颜色值的顺序发出
        uint32_t rgb = *(const uint32_t *)item.data_ptr;
        out[0] = (rgb >> 16) & 0xFF; // R 高字节
        out[1] = (rgb >> 8) & 0xFF;  // G 中字节
        out[2] = rgb & 0xFF;         // B 低字节
        return 3;
    }
    case type_bool:
    case type_on_off:
        // bool显式转成0/1
        out[0] = *(const bool *)item.data_ptr ? 1 : 0;
        return 1;
    default:
    {
        // 直接把变量内存里的字节拷进帧里。依赖通信双方都是小端
        uint8_t n = value_size(item.data_type);
        if (n > 0)
            memcpy(out, item.data_ptr, n);
        return n;
    }
    }
}

// 把帧里的值写回参数变量，是append_value的逆过程。
// in指向帧payload中value的起点（定长8B区域），实际只按类型读需要的字节数。
void USB_Comm::write_value(const uint8_t *in, const MENU_ITEM &item)
{
    switch (item.data_type)
    {
    case type_color:
    {
        // 三字节RGB合回菜单使用的0xRRGGBB整数（与append_value拆解顺序严格对应）
        uint32_t rgb = ((uint32_t)in[0] << 16) | ((uint32_t)in[1] << 8) | in[2];
        *(uint32_t *)item.data_ptr = rgb;
        break;
    }
    case type_bool:
    case type_on_off:
        // 只认0/非0，避免把任意整数值直接写进bool
        *(bool *)item.data_ptr = (in[0] != 0);
        break;
    default:
    {
        // 数值类型：按真实宽度原样拷进变量，高位补的零自然落在被忽略区域
        uint8_t n = value_size(item.data_type);
        if (n > 0)
            memcpy(item.data_ptr, in, n);
        break;
    }
    }
}

// 封帧发出：调用方已把payload直接拼在成员缓冲tx_frame[2]开始的位置，
// 这里只补帧头、命令字和校验，再整帧write到/dev/serial1（USB User Port）。
void USB_Comm::send_frame(uint8_t cmd, uint8_t len)
{
    tx_frame[0] = FRAME_HEAD;
    tx_frame[1] = cmd;
    uint8_t xor_sum = cmd; // 校验从命令字开始算，帧头A5不参与
    for (uint8_t i = 0; i < len; i++)
        xor_sum ^= tx_frame[2 + i]; // 每来一个数据字节就异或进去
    tx_frame[2 + len] = xor_sum;    // payload后面紧跟校验
    if (usb_fd >= 0)
        write(usb_fd, tx_frame, len + 3); // /dev/serial1原始通道直出，无CRLF翻译
}

// 发目录的一批：只发原数组下标 [batch*DIR_BATCH, +DIR_BATCH) 这连续几项，一项一帧。
// 目录由tx_tick分多个10ms拍发完，避免一次性write几百项时长时间占用发送任务。
// 参数cmd决定这些帧标成"可调目录"还是"监控目录"，复用同一套拼帧逻辑。
// 直接在成员tx_frame里拼帧，不再用临时payload数组中转，省一次栈空间和拷贝：
//   [A5][Cmd]由send_frame补，
//   [index][类型][tag][名字长度][名字][单位长度][单位][当前值]在这里写
void USB_Comm::send_dir_batch(const MENU_ITEM *items, uint8_t count, uint8_t cmd, uint8_t batch)
{
    uint8_t start = batch * DIR_BATCH;
    if (start >= count)
        return; // 批次超界（正常不会发生，倒计时保证batch取0~总批数-1），容错直接返回
    // 本批实际发几项：剩余不足一批时发完为止。用"剩余数"限幅而不用start+8，
    // 因为255项满载最后一批start=248，start+8=256会撑爆uint8回0
    uint8_t n = count - start;
    if (n > DIR_BATCH)
        n = DIR_BATCH;

    for (uint8_t k = 0; k < n; k++)
    {
        uint8_t i = start + k; // start+k最大255，不会溢出
        const MENU_ITEM &item = items[i];

        if (item.data_type == type_str || item.data_type == type_other)
            continue;

        uint8_t vlen = value_size(item.data_type);
        uint8_t name_len = (uint8_t)strlen(item.Chinese_name);

        if (name_len > 30)
            name_len = 30; // 名称基本在30字节以下

        // 单位可能没填（nullptr或空串），统一发成长度0的单位段
        uint8_t unit_len = item.unit != nullptr ? (uint8_t)strlen(item.unit) : 0;
        if (unit_len > UNIT_MAX)
            unit_len = UNIT_MAX;

        tx_frame[2] = i;                       // 用原数组下标当编号
        tx_frame[3] = (uint8_t)item.data_type; // 上位机据此知道后面的值占几字节
        tx_frame[4] = (uint8_t)item.tag;       // 位域：SUB/FAST/GETTER/KIND
        tx_frame[5] = name_len;                // 名字字节数，上位机读完名字正好对齐到单位段
        memcpy(&tx_frame[6], item.Chinese_name, name_len);
        tx_frame[6 + name_len] = unit_len; // 单位字节数，0表示无单位
        if (unit_len > 0)
            memcpy(&tx_frame[7 + name_len], item.unit, unit_len);
        append_value(&tx_frame[7 + name_len + unit_len], item);
        send_frame(cmd, 5 + name_len + unit_len + vlen);
    }
}

// 发动作命令目录的一批：直接取外部表下标 [batch*CMD_DIR_BATCH, +CMD_DIR_BATCH)
// 这连续几条，一条拼一帧。
// 帧payload: [命令字][命令名长][命令名][参数量]([字段类型][字段名长][字段名])×N
void USB_Comm::send_cmd_dir_batch(uint8_t batch)
{
    uint8_t start = batch * CMD_DIR_BATCH;
    if (start >= cmd_count)
        return; // 批次超界（正常不会发生，倒计时保证batch取0~总批数-1），容错直接返回
    uint8_t n = cmd_count - start;
    if (n > CMD_DIR_BATCH)
        n = CMD_DIR_BATCH; // 最后一批不足4条时发完为止

    for (uint8_t k = 0; k < n; k++)
    {
        const CMD_ITEM *item = &cmd_items[start + k];

        uint8_t pos = 2; // payload从tx_frame[2]起拼，最后pos-2就是payload总长
        tx_frame[pos++] = item->index;

        uint8_t name_len = (uint8_t)strlen(item->name);
        if (name_len > CMD_NAME_MAX)
            name_len = CMD_NAME_MAX;
        tx_frame[pos++] = name_len;
        memcpy(&tx_frame[pos], item->name, name_len);
        pos += name_len;

        tx_frame[pos++] = item->input_num;
        for (uint8_t f = 0; f < item->input_num; f++)
        {
            tx_frame[pos++] = (uint8_t)item->data_type[f];
            uint8_t flen = (uint8_t)strlen(item->field_name[f]);
            if (flen > CMD_FIELD_MAX)
                flen = CMD_FIELD_MAX;
            tx_frame[pos++] = flen;
            memcpy(&tx_frame[pos], item->field_name[f], flen);
            pos += flen;
        }
        send_frame(Post_CMD_Directory, pos - 2);
    }
}

// 只合并上位机的订阅/速率位，保留本机 GETTER 与 KIND（及预留位）
static uint8_t merge_subscribe_tag(uint8_t old_tag, uint8_t incoming)
{
    const uint8_t host_bits = MENU_TAG_SUB | MENU_TAG_FAST;
    return static_cast<uint8_t>((incoming & host_bits) | (old_tag & static_cast<uint8_t>(~host_bits)));
}

// 接收状态机收齐一帧、且校验通过后调用。
// 此时rx_cmd是命令字，rx_payload前rx_len字节是数据。
void USB_Comm::handle_command()
{
    switch (rx_cmd)
    {
    case Request_Tunable:
        // 上位机要可调参数目录。不在接收任务里直接发（一次性发几百项会阻塞read），
        // 只置"还剩几批"的倒计时，真正的发送由后续tx_tick每拍一批完成。
        dir_tun = (tunable_count + DIR_BATCH - 1) / DIR_BATCH;
        break;
    case Request_Monitor:
        // 上位机要监控目录：同样置倒计时分批发送，随后打开推送开关。
        // 推送是按tx_tick节拍走的，下一拍自然就开始发，不用特殊触发。
        dir_mon = (monitor_count + DIR_BATCH - 1) / DIR_BATCH;
        monitor_active = true;
        break;
    case Request_Command:
        // 上位机要动作命令目录：按外部表条数分批，每拍CMD_DIR_BATCH条
        if (cmd_count > 0)
            dir_cmd = (cmd_count + CMD_DIR_BATCH - 1) / CMD_DIR_BATCH;
        break;
    case Subscribe:
    {
        // Payload: [index][tag]
        // tag 为位域（见 my_main.h）。上位机主要改 bit7(SUB)/bit6(FAST)；
        // 下位机 merge 时保留 bit5(GETTER) 与 bit2~0(KIND) 及预留位。
        // 旧习惯整字节 0 表示退订 → bit7=0，仍然成立。
        uint8_t index = rx_payload[0];
        uint8_t tag = rx_payload[1];
        if (index == 0xFF)
        {
            for (uint8_t i = 0; i < monitor_count; i++)
            {
                VALUE_TYPE t = monitor_items[i].data_type;
                if (t != type_str && t != type_other)
                    monitor_items[i].tag = merge_subscribe_tag(monitor_items[i].tag, tag);
            }
        }
        else if (index < monitor_count)
            monitor_items[index].tag = merge_subscribe_tag(monitor_items[index].tag, tag);
        break;
    }
    case Set_Tunable:
    {
        // Payload: [index][value×8B]。写参发生在机器人静止时、目标是对齐标量，
        // 所以rx线程直接写即可，无需转交主线程。安全只靠下面三道校验保证。
        uint8_t index = rx_payload[0];
        if (index >= tunable_count)
            break; // 下标超表，静默丢弃（上位机拿不到回显会自己超时判失败）
        const MENU_ITEM &item = tunable_items[index];
        // 类型以下位机自己的表为准：str/other不可写，也防止上位机发错宽度写穿相邻内存
        if (item.data_type == type_str || item.data_type == type_other)
            break;
        write_value(&rx_payload[1], item);
        // 不直接回帧（rx不能write），登记回显槽，tx下一拍从内存重读真实值回发
        echo_index = index;
        // 置脏标志就算通知完，谁关心谁自己来取（如LCD菜单刷新界面）
        tunable_dirty = true;
        break;
    }
    case Ping:
        // 上位机心跳（每1s）：标记本周期见过心跳并立刻认可链路（掉线后再收到Ping
        // 可即时恢复推送，不必等巡视点）；Pong不在这里发——rx任务不能写串口，
        // 只置pending标志，由下一拍tx_tick统一发，延迟最多10ms。
        ping_seen = true;
        link_ok = true;
        pong_pending = true;
        break;

        // 内建命令未实现的分支；动作命令不靠switch，在下面统一分发
    }

    // 动作命令分发：内建switch处理完后，凡是命令表里有的命令字都在这里直接执行。
    // payload长度已由cmd_payload_len按表收齐，handler按自己的字段表解读即可。
    const CMD_ITEM *action = find_cmd(rx_cmd);
    if (action != nullptr)
        action->func(rx_payload);
}

// 分区推送，每次tx_tick调用一次：
//   位域：无 SUB 不发；有 FAST 每拍发；仅 SUB 则按 index%8 低速轮转。
// 用index%8取模代替位图轮转，不用任何额外订阅存储。
// 本函数只按当前tick的值发，不推进tick——推进由tx_tick末尾统一做。
void USB_Comm::monitor_tick()
{
    // monitor_active：上位机要过监控目录；link_ok：心跳看门狗认可链路。
    // 掉线（2.56s没见Ping）时只停监控值推送，订阅档位和表里数据都保留，Ping恢复即续传。
    if (!monitor_active || !link_ok)
        return;

    // 发一个监控项的完整链路：直接在tx_frame里写[index][当前值]，再封帧发出。
    // 不带类型——上位机从目录里已知index→类型对应。
    auto post_item = [&](uint8_t index)
    {
        const MENU_ITEM &item = monitor_items[index];
        if (item.data_type == type_str || item.data_type == type_other)
            return;
        tx_frame[2] = index;
        uint8_t vlen = append_value(&tx_frame[3], item); // 值最多8B，写在index后面
        send_frame(Monitor_Post, 1 + vlen);
    };

    uint8_t phase = tick & (SLOW_PHASES - 1); // SLOW_PHASES是2的幂，用掩码代替取模
    for (uint8_t i = 0; i < monitor_count; i++)
    {
        const MENU_ITEM &item = monitor_items[i];
        if (!item.is_subscribed())
            continue;
        if (item.is_fast())
            post_item(i); // 高速项每轮直发
        else if ((i & (SLOW_PHASES - 1)) == phase)
            post_item(i); // 低速项轮到自己所在的相才发
    }
}

// 10ms发送节拍：先回心跳Pong和调参回显，再发目录倒计时的当前批，然后推送监控值，
// tick走到255时做一次心跳巡视，最后推进tick。
// 这是全程序唯一write串口的地方（接收任务只置标志），所以各帧天然不会互相穿插。
void USB_Comm::tx_tick()
{
    // Pong最高优先：3字节小帧，收到Ping后最迟下一拍就回，上位机拿它算往返/在线
    if (pong_pending)
    {
        send_frame(Pong, 0);
        pong_pending = false;
    }

    // 调参回显：从data_ptr重读内存里的真实值（而非回响收到值），
    // 这样上位机看到的是裁剪/换算后实际生效的结果。先取index再清槽。
    if (echo_index != 0xFF)
    {
        uint8_t index = echo_index;
        echo_index = 0xFF;
        const MENU_ITEM &item = tunable_items[index];
        tx_frame[2] = index;
        uint8_t vlen = append_value(&tx_frame[3], item);
        send_frame(Tunable_Echo, 1 + vlen);
    }

    // 有待发目录时这一拍先发一批。两个倒计时同时非零（理论上几乎不会，
    // 两类目录请求是人工低频操作）则tunable优先，下一拍再发monitor，串行不并行。
    // 当前批次号 = 总批数 - 剩余批数：倒计时从总批数减到0，批次号正好从0走到总批数-1。
    if (dir_tun > 0)
    {
        uint8_t total = (tunable_count + DIR_BATCH - 1) / DIR_BATCH;
        send_dir_batch(tunable_items, tunable_count, Post_Tunable_Directory, total - dir_tun);
        dir_tun--;
    }
    else if (dir_mon > 0)
    {
        uint8_t total = (monitor_count + DIR_BATCH - 1) / DIR_BATCH;
        send_dir_batch(monitor_items, monitor_count, Post_Monitor_Directory, total - dir_mon);
        dir_mon--;
    }
    else if (dir_cmd > 0)
    {
        // 命令目录排最后，批次号算法同上
        uint8_t total = (cmd_count + CMD_DIR_BATCH - 1) / CMD_DIR_BATCH;
        send_cmd_dir_batch(total - dir_cmd);
        dir_cmd--;
    }

    monitor_tick();

    // 心跳巡视：每256拍（256×10ms=2.56s，上位机1s一个Ping，周期内必见2~3个）
    // 在tick即将从255溢出回0前检查一次：本周期见过Ping就保持/恢复认可，没见过则断链停推。
    if (tick == 255)
    {
        // 掉线只在"上周期认可、本周期没见Ping"的边沿通知一次：
        // 直接调注入的回调（本线程=主循环10ms，回调内只做快速收车，不阻塞）
        if (link_ok && !ping_seen && on_link_lost != nullptr)
            on_link_lost();
        link_ok = ping_seen;
        ping_seen = false; // 清零，开始统计下一个周期
    }
    tick++; // uint8自然溢出回0，掩码轮转无缝衔接，无需回绕判断
}

// 接收入口：独立任务，阻塞在/dev/serial1的read()上等字节，读到一个喂一次状态机。
// 没数据时任务挂起（V5 newlib不支持非阻塞读），不占CPU；数据到达自动唤醒。
void USB_Comm::rx_task()
{
    uint8_t ch;
    while (true)
    {
        if (usb_fd < 0)
        {
            vex::wait(10, vex::msec); // 通道没打开，空等（正常不会发生，init在任务启动前完成）
            continue;
        }
        if (read(usb_fd, &ch, 1) != 1)
            continue; // 理论上阻塞读要么挂起要么返回1，这分支只作容错

        switch (rx_state)
        {
        case WAIT_HEAD:
            // 平时一直在这里丢字节，直到碰到帧头才进入下一状态。
            // 中途的乱码、校验失败帧的残尾都是这样被自然跳过的。
            if (ch == FRAME_HEAD)
                rx_state = RX_CMD;
            break;

        case RX_CMD:
        {
            // 帧头后的第一个字节是命令字，先查它该带几字节Payload
            uint8_t expect = cmd_payload_len(ch);
            if (expect == 0xFF)
            {
                // 未知命令：我们连校验位在哪都不知道，没法继续收，
                // 只能回到等帧头状态。极端情况下Payload里恰好有0xA5也没关系，
                // 下一帧真正的帧头出现时状态机会再次同步上。
                rx_state = WAIT_HEAD;
                break;
            }
            rx_cmd = ch;
            rx_expect = expect;
            rx_len = 0;
            rx_xor = ch; // 校验从命令字起算，不含帧头
            // Payload长度为0的命令直接去等校验字节
            rx_state = (expect > 0) ? RX_DATA : RX_XOR;
            break;
        }

        case RX_DATA:
            // 收数据的同时一边存一边累计校验。
            // rx_len仍照常自增、不因为超长就停，是为了让校验累计的字节数和
            // 发送方实际发的一致（正常命令长度都很小，这里的保护只是防数组越界）。
            if (rx_len < MAX_PAYLOAD)
                rx_payload[rx_len] = ch;
            rx_len++;
            rx_xor ^= ch;
            if (rx_len >= rx_expect)
                rx_state = RX_XOR; // 数据收够了，下一个字节必须是校验
            break;

        case RX_XOR:
            rx_state = WAIT_HEAD;
            // 校验原理：rx_xor是Cmd和全部Payload的异或，再异或收到的校验字节，
            if ((rx_xor ^ ch) == 0 && rx_len <= MAX_PAYLOAD)
                handle_command();
            break;
        }
    }
}
