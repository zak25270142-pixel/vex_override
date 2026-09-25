#include "communication.h"
#include "timer.h" // Delay，收数轮询空转时让出CPU
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
    case Pong:
        return 0;
    case Subscribe:
        return 2; // [index][tag]，档位由第二字节给出，tag=0即退订
    default:
        return 0xFF; // 未知命令，无法定位校验位
    }
}

USB_Comm::USB_Comm(const MENU_ITEM *tunable, uint8_t tunable_len,
                   MENU_ITEM *monitor, uint8_t monitor_len)
    : tunable_items(tunable), tunable_count(tunable_len),
      monitor_items(monitor), monitor_count(monitor_len)
{

    tick = 0;
    dir_tun = 0; // 没有待发目录
    dir_mon = 0;
    monitor_active = false; // 默认不推送，等上位机显式请求监控目录后才开始
    rx_len = 0;
    rx_expect = 0;
    rx_xor = 0;
    rx_cmd = 0;
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
uint8_t USB_Comm::append_value(uint8_t *out, const MENU_ITEM &item)
{
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
        // bool在C++里宽度不保证是1（虽然V5上就是1），显式转成0/1发，避免歧义
        out[0] = *(const bool *)item.data_ptr ? 1 : 0;
        return 1;
    default:
    {
        // 数值类型：直接把变量内存里的字节拷进帧里。
        // 依赖通信双方都是小端，这一点在头文件协议说明里已写明。
        uint8_t n = value_size(item.data_type);
        if (n > 0)
            memcpy(out, item.data_ptr, n);
        return n;
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
//   [A5][Cmd]由send_frame补，[index][类型][tag][名字长度][名字][当前值]在这里写
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

        tx_frame[2] = i;                       // 用原数组下标当编号
        tx_frame[3] = (uint8_t)item.data_type; // 上位机据此知道后面的值占几字节
        tx_frame[4] = (uint8_t)item.tag;       // 监控目录据此给出默认订阅档位和语义标记
        tx_frame[5] = name_len;                // 名字字节数，上位机读完名字正好对齐到值
        memcpy(&tx_frame[6], item.Chinese_name, name_len);
        append_value(&tx_frame[6 + name_len], item);
        send_frame(cmd, 4 + name_len + vlen);
    }
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
    case Subscribe:
    {
        // Payload: [index][tag]，tag取值0~5：
        //   0=退订（停推），1低速，2高速，3/4/5=x/y/yaw语义档
        // 退订和订阅是同一套写法（往tag里写0），所以不再单设退订命令。
        uint8_t index = rx_payload[0];
        uint8_t tag = rx_payload[1];
        if (tag > monitor_tag_yaw)
            break; // 超出已定义档位范围，忽略
        if (index == 0xFF)
            for (uint8_t i = 0; i < monitor_count; i++)
            {
                VALUE_TYPE t = monitor_items[i].data_type;
                if (t != type_str && t != type_other)
                    monitor_items[i].tag = (MONITOR_TAG)tag;
            }
        else if (index < monitor_count)
            monitor_items[index].tag = (MONITOR_TAG)tag;
        break;
    }
    case Pong:
        break; // 上位机的心跳回应，收到即说明连接正常，暂时不需要额外动作
    default:
        break; // 能走到这里的命令长度已知但没实现，先忽略，方便以后扩展
    }
}

// 分区推送，每次tx_tick调用一次：
//   tag>=2（高速/x/y/yaw语义量）：每轮都发，约100Hz
//   tag==1（低速）：按index%8分到8个轮转相，每轮只发本相的项，单项约80ms
//   tag==0：不发
// 用index%8取模代替位图轮转，不用任何额外订阅存储。
// 本函数只按当前tick的值发，不推进tick——推进由tx_tick末尾统一做。
void USB_Comm::monitor_tick()
{
    if (!monitor_active)
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
        uint8_t tag = (uint8_t)monitor_items[i].tag;
        if (tag >= monitor_tag_fast)
            post_item(i); // 高速项每轮直发
        else if (tag == monitor_tag_slow && (i & (SLOW_PHASES - 1)) == phase)
            post_item(i); // 低速项轮到自己所在的相才发
    }
}

// 10ms发送节拍：先发目录倒计时的当前批，再推送监控值，最后推进tick。
// 这是全程序唯一write串口的地方（接收任务只置标志），所以各帧天然不会互相穿插。
void USB_Comm::tx_tick()
{
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

    monitor_tick();
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
            Delay(10); // 通道没打开，空等（正常不会发生，init在任务启动前完成）
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
            // 无论校验对错，一帧到此结束，先回等帧头状态准备下一帧
            rx_state = WAIT_HEAD;
            // 校验原理：rx_xor是Cmd和全部Payload的异或，再异或收到的校验字节，
            // 传输无误时结果为0。同时检查长度没溢出缓冲才执行，双重保险。
            if ((rx_xor ^ ch) == 0 && rx_len <= MAX_PAYLOAD)
                handle_command();
            // 校验失败就静默丢弃这帧，不回错误、不重发——上位机会靠目录和
            // 下一周期的推送自然拿到新数据，简单可靠。
            break;
        }
    }
}
