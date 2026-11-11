/**
 * VEX V5 User 口二进制协议（与下位机 communication.cpp 严格一一对应）
 *
 * 帧格式: A5 | Cmd | Payload... | XOR
 *   XOR = Cmd 起逐字节异或（不含帧头）
 * 多字节数值一律小端（V5 ARM 与 PC x86 同为小端）
 */

import { loadPrefs, MAX_PAYLOAD_CAP } from './persist'

/** 帧头与帧长上限的唯一真源是 prefs；协议层直接读，不缓存任何镜像 */
/** 主控 → 上位机 */
export enum CmdPost {
  TunableDirectory = 0,
  MonitorDirectory = 1,
  MonitorValue = 2,
  /** 调参回显：[index][实际生效值]，收到 SetTunable 后下一拍回 */
  TunableEcho = 3,
  /** 动作命令目录：[命令字][tag][名长][名][参数量][字段描述...]，一条命令一帧 */
  CMDDirectory = 4,
  /** 通用响应帧：[payloadlen][原话cmd][原话payload][status][原因串]，对下行命令的回执 */
  Response = 5,
  /** 按键目录：[index][name_len][name]，一项一帧（name_len 1~16） */
  KeyDirectory = 6,
  /** 心跳回应：收到 Ping 后下一拍回，无 payload，整帧 A5 FF FF */
  Pong = 0xff,
}

/** 合法上行命令字全集：从枚举自动派生，新增命令字不用手改白名单 */
const CMD_POST_VALUES: ReadonlySet<number> = new Set<number>(
  Object.values(CmdPost).filter((v) => typeof v === 'number') as number[],
)

/** 上位机 → 主控 */
export const enum CmdGet {
  RequestTunable = 0,
  RequestMonitor = 1,
  Subscribe = 2,
  /** 调参写入：[index][value×8B 定长]，类型查调参表，不足 8B 高位补零 */
  SetTunable = 3,
  /** 请求动作命令目录（无 payload），主控分批回 CMDDirectory */
  RequestCommand = 4,
  /**
   * 按键事件（下行）：[index][state]。state 是"接下来的期望电平"（1=按下 0=抬起），
   * 按下/松开回调各对应一次边沿；同一电平重复下发不产生新边沿 → 幂等，重发安全。
   * 固件处理完必须回 Response（与动作命令同等待遇），否则后端按 0.1s 超时重发。
   * ⚠ 数值 5 与上行 CmdPost.Response 相同但方向相反：
   *   5 上行 = 通用回执（主控→上位机），5 下行 = 按键事件（上位机→主控），语义无关。
   */
  KeyEvent = 5,
  /** 请求按键目录，payload 同 requestDir：[len][index...]，len=0 全量；与上行 KeyDirectory 同号 */
  RequestKeyDir = 6,
  /** 0x07 起留空，扩展时再安排 */
  /** 心跳：每 80ms 发一帧，无 payload，整帧 A5 FF FF */
  Ping = 0xff,
}

/** 值类型，编号必须与 my_main.h 的 VALUE_TYPE 一致 */
export const enum ValueType {
  UInt8 = 0,
  UInt16 = 1,
  UInt32 = 2,
  UInt64 = 3,
  Int8 = 4,
  Int16 = 5,
  Int32 = 6,
  Int64 = 7,
  Float = 8,
  Double = 9,
  Str = 10,
  Bool = 11,
  OnOff = 12,
  Color = 13,
  Other = 14,
}

/** 监控 tag 单字节位域，与 my_main.h 的 MENU_TAG_* 常量一一对应 */
export const MONITOR_TAG_SUB = 0x80 // bit7：是否推送
export const MONITOR_TAG_FAST = 0x40 // bit6：高速区（每拍）还是低速区（8相轮转）
export const MONITOR_TAG_GETTER = 0x20 // bit5：值为下位机 float() 函数取数（只读，不占用 RAM 指针）
export const MONITOR_TAG_KIND_MASK = 0x07 // bit2~0：语义种类

/** 语义种类（tag & KIND_MASK）：位置量由场地图按 kind 取数喂图，订阅档位与普通量一样可自由切换 */
export const enum MonitorKind {
  None = 0,
  PosX = 1,
  PosY = 2,
  Yaw = 3,
}

export function tagSubscribed(tag: number): boolean {
  return (tag & MONITOR_TAG_SUB) !== 0
}
export function tagFast(tag: number): boolean {
  return (tag & MONITOR_TAG_FAST) !== 0
}
export function tagKind(tag: number): MonitorKind {
  return (tag & MONITOR_TAG_KIND_MASK) as MonitorKind
}

/** 动作命令 tag 单字节位域，与 communication.h 的 CMD_TAG_* 常量一一对应 */
export const CMD_TAG_RANGE = 0x80 // bit7：是否带取值范围（预留）
export const CMD_TAG_ARGC_MASK = 0x70 // bit6~4：参数个数 0~7
export const CMD_TAG_ARGC_SHIFT = 4
export const CMD_TAG_SPEC_MASK = 0x0f // bit3~0：特化命令
/** 特化命令类型（tag & SPEC_MASK）：0 普通，1 停车，其余暂未定义 */
export const enum CmdSpec {
  Normal = 0,
  Stop = 1,
}
export function cmdSpec(tag: number): CmdSpec {
  return (tag & CMD_TAG_SPEC_MASK) as CmdSpec
}


/** User 口是 USB CDC，波特率无实际意义，固定值仅为满足 Web Serial API */
export const BAUD_CDC = 115200

/** 各类型在帧里占的字节数；str/other 不上报，返回 0 */
export function valueSize(type: ValueType): number {
  switch (type) {
    case ValueType.UInt8:
    case ValueType.Int8:
    case ValueType.Bool:
    case ValueType.OnOff:
      return 1
    case ValueType.UInt16:
    case ValueType.Int16:
      return 2
    case ValueType.UInt32:
    case ValueType.Int32:
    case ValueType.Float:
      return 4
    case ValueType.UInt64:
    case ValueType.Int64:
    case ValueType.Double:
      return 8
    case ValueType.Color:
      return 3
    default:
      return 0
  }
}

/** 类型中文名（表头/调试用） */
export function typeName(type: ValueType): string {
  const names: Record<number, string> = {
    [ValueType.UInt8]: 'uint8',
    [ValueType.UInt16]: 'uint16',
    [ValueType.UInt32]: 'uint32',
    [ValueType.UInt64]: 'uint64',
    [ValueType.Int8]: 'int8',
    [ValueType.Int16]: 'int16',
    [ValueType.Int32]: 'int32',
    [ValueType.Int64]: 'int64',
    [ValueType.Float]: 'float',
    [ValueType.Double]: 'double',
    [ValueType.Bool]: 'bool',
    [ValueType.OnOff]: 'on_off',
    [ValueType.Color]: 'color',
  }
  return names[type] ?? 'unknown'
}

/**
 * 目录项（一帧一项）；tag 为位域字节，用 tagSubscribed/tagFast/tagKind 解读；unit 为空串表示无单位。
 *
 * value 一律 any，不设类型别名：下位机 ValueType 会扩展（含复合类型），前端手写的封闭
 * 集合改漏了不报错、只会变成假文档；真正的类型约束在 encodeValue8 / formatValue 的
 * 运行时 typeof 判断。
 */
export interface DirItem {
  index: number
  type: ValueType
  tag: number
  name: string
  unit: string
  value: any
}

/**
 * 动作命令的一个输入字段（类型用于渲染控件和编码，name 是字段中文标签）。
 * min/max：目录 tag bit7=1 时下发的取值范围，**按本字段 type 解码、与字段同型**
 * （整型字段为整数，float/double 为小数），未下发时为 undefined。
 */
export interface CmdField {
  type: ValueType
  name: string
  min?: number
  max?: number
}

/**
 * 动作命令目录项：index 就是下发时用的命令字（0x80~0x9F）；
 * tag 为位域字节，用 cmdSpec/cmdArgc/cmdHasRange 解读特化类型与参数个数
 */
export interface CmdDirItem {
  index: number
  tag: number
  name: string
  fields: CmdField[]
}

/* 解帧事件：每个事件一个 class；kind 是只读字面量属性，靠它做联合类型收窄。
   与后端 protocol.py 的 RxEvent 子类一一对应，消费方用 e.kind 判别即可。 */
export class DirectoryEvent {
  readonly kind = 'directory'
  constructor(
    readonly table: 'tunable' | 'monitor',
    readonly item: DirItem,
  ) {}
}

export class ValueEvent {
  readonly kind = 'value'
  constructor(
    readonly index: number,
    readonly value: any,
    /** 下位机采样时刻：毫秒时钟低16位，65.536s 回绕，同一拍发出的帧相同 */
    readonly tMs: number,
  ) {}
}

export class EchoEvent {
  readonly kind = 'echo'
  constructor(
    readonly index: number,
    readonly value: any,
  ) {}
}

export class CmdDirectoryEvent {
  readonly kind = 'cmdDirectory'
  constructor(readonly item: CmdDirItem) {}
}

/** 一条按键目录项：index 是按键表下标，name 是上位机显示名（UTF-8） */
export interface KeyDirItem {
  index: number
  name: string
}

export class KeyDirectoryEvent {
  readonly kind = 'keyDirectory'
  constructor(readonly item: KeyDirItem) {}
}

export class ResponseEvent {
  readonly kind = 'response'
  constructor(
    /** 回显的原话命令字（据此找到是哪条下行帧的回执） */
    readonly echoCmd: number,
    /** 回显的原话 payload */
    readonly echoPayload: Uint8Array,
    /** status 字节（位域见上方 RESP_* 常量） */
    readonly status: number,
    /** 尾字节：串模式=UTF-8 原因串；OK 模式=二进制附加信息（如目录项数） */
    readonly reason: Uint8Array,
  ) {}
}

export class PongEvent {
  readonly kind = 'pong'
}

export type RxEvent =
  | DirectoryEvent
  | ValueEvent
  | EchoEvent
  | CmdDirectoryEvent
  | KeyDirectoryEvent
  | ResponseEvent
  | PongEvent

/* ===== Response 帧（与 backend protocol.py 一一对应）=====
   帧: A5 | Response | L | 原话cmd | status | 附加段 | 原话payload | XOR
   status 固定在 p[2]。附加段长度由 status 自己给出：
     0x00                   OK 无附加段（传完通知 / 通用 OK）
     0x80（OK+info）/ 内定err类       [len][data...]，len=0 即不带内容
     串类                   裸串 data...，长度 = status & 0x3F（低 6 位就是串长）
   原话payload 在帧尾、只做 ACK 匹配，长度由 L 反推；长度 0 = 匹配该 cmd 全部。
   status 按范围分四类 + 两个 OK 特殊点（位运算同构）：
     0x00      OK 无 info / 0x80      OK + info（整表请求收到即回，info=1B 表项数）
     0x01~0x3F 需补发+串类 / 0x40~0x7F 需补发+内定err类
     0x81~0xBF 明确拒绝+串类 / 0xC0~0xFF 明确拒绝+内定err类
   OK 族 = (status & 0x7F) === 0；低 6 位两类都有实义：串类=串长，内定err类=err号。 */
export const RESP_OK = 0x00
export const RESP_OK_INFO = 0x80
export const RESP_OK_FAMILY_MASK = 0x7f
export const RESP_CLASS_MASK = 0x80
export const RESP_MODE_MASK = 0x40
export const RESP_PAYLOAD_MASK = 0x3f

/** 内定err号（status = 0x40|n 需补发 / 0xC0|n 明确拒绝），与 firmware/backend 同名同值 */
export const RESP_REJECT_INDEX_RANGE = 0xc0 | 1 // 下标超表

/** 内定err中文名（status 全字节为键；与 backend _RESP_ERR_NAMES_ZH、固件 communication.h 同步）。
    拒绝类顺位：Generic=0 / IndexRange=1 / Busy=2 / UnknownCmd=3 / InFlight=4 */
export const RESP_ERR_NAMES_ZH: Record<number, string> = {
  0x40: '通用重试',
  0x41: '校验失败',
  0x42: '目录补发池满',
  0xc0: '通用拒绝',
  0xc1: '下标超表',
  0xc2: '忙',
  0xc3: '命令字未注册',
  0xc4: '同表目录在途',
}

/** 解 Response 帧 payload：[L][原话cmd][status][附加段][原话payload]。
    status 固定在 p[2]，附加段长度由 status 自己给出：
      0x00                 无
      0x80（OK+info）/ 内定err类     [len][data...]（返回 data，不含长度字节）
      串类                 裸串（长度 = status & 0x3F，返回整段）
    原话payload 在帧尾、长度由 L 反推；长度 0 =「无回显 = 匹配该 cmd 全部」。
    不看命令字、不读原话payload 内容，整帧自描述。与后端 decode_response 同构。 */
export function decodeResponse(
  p: Uint8Array,
): { echoCmd: number; echoPayload: Uint8Array; status: number; reason: Uint8Array } | null {
  if (p.length < 3) return null
  if (p[0]! < 2 || p[0]! !== p.length - 1) return null
  const echoCmd = p[1]!
  const status = p[2]!
  let off = 3
  let info: Uint8Array<ArrayBufferLike> = new Uint8Array(0)
  if (status !== RESP_OK) {
    if (status === RESP_OK_INFO || (status & RESP_MODE_MASK) !== 0) {
      if (off >= p.length) return null // 缺 len
      const n = p[off]!
      off += 1 + n
      if (off > p.length) return null // len 越出帧外
      info = p.subarray(4, off)
    } else {
      off += status & RESP_PAYLOAD_MASK // 串类：低 6 位就是串长，不带长度前缀
      if (off > p.length) return null
      info = p.subarray(3, off)
    }
  }
  return {
    echoCmd,
    echoPayload: p.subarray(off),
    status,
    reason: info,
  }
}

/** 按类型从小端字节流解码值（协议多字节数值一律小端）；调用方保证剩余字节足够 */
function decodeValue(d: DataView, off: number, type: ValueType): any {
  switch (type) {
    case ValueType.UInt8:
      return d.getUint8(off)
    case ValueType.Int8:
      return d.getInt8(off)
    case ValueType.UInt16:
      return d.getUint16(off, true)
    case ValueType.Int16:
      return d.getInt16(off, true)
    case ValueType.UInt32:
      return d.getUint32(off, true)
    case ValueType.Int32:
      return d.getInt32(off, true)
    case ValueType.Float:
      return d.getFloat32(off, true)
    case ValueType.Double:
      return d.getFloat64(off, true)
    case ValueType.UInt64:
      // 超过2^53会丢精度，监控场景可接受
      return Number(d.getBigUint64(off, true))
    case ValueType.Int64:
      return Number(d.getBigInt64(off, true))
    case ValueType.Bool:
    case ValueType.OnOff:
      return d.getUint8(off)
    case ValueType.Color: {
      const r = d.getUint8(off)
      const g = d.getUint8(off + 1)
      const b = d.getUint8(off + 2)
      return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')
    }
    default:
      return 0
  }
}

/* float32 往返比较用的复用缓冲：JS 数字都是 double，1.2f 进来就是 1.2000000476837158，
   判"短写法是否无损"要靠重新编码回 float32 比对比特，而不是直接比 double */
const f32Buf = new DataView(new ArrayBuffer(4))
function f32Bits(v: number): number {
  f32Buf.setFloat32(0, v, true)
  return f32Buf.getUint32(0, true)
}

/**
 * 把解出的 float32 值转成最短十进制串：逐位增加有效数字（1~9），
 * 第一个「parseFloat 后再编码为 float32 与原值逐位相同」的写法即采用。
 * 这样 1.2f 显示成 "1.2" 而不是 "1.2000000476837158"，且再下发/记录仍是同一个 float32。
 */
export function formatFloat32(v: number): string {
  if (!Number.isFinite(v)) return String(v)
  for (let p = 1; p <= 9; p++) {
    const s = v.toPrecision(p)
    if (f32Bits(Number(s)) === f32Bits(v)) return String(Number(s))
  }
  return String(v)
}

/**
 * 按类型把值编码成真实宽度的小端字节（动作命令字段用，紧凑排列无填充）。
 * str/other 宽度为 0，返回空数组；bool 收 0/1；color 收 '#RRGGBB'。
 */
export function encodeValue(type: ValueType, value: any): Uint8Array {
  const size = valueSize(type)
  if (size === 0) return new Uint8Array(0)
  return encodeValue8(type, value).subarray(0, size)
}

/**
 * 把值编码成定长 8B 小端区域（SetTunable 的 value 段），与下位机 write_value 对应。
 * 不足 8B 的类型高位天然为 0；bool 收 0/1（仅开关字段）；color 收 '#RRGGBB'。
 * 类型不符直接抛错，绝不静默编 0——与 backend protocol.py 的 encode_value8 一一对应。
 */
function encodeValue8(type: ValueType, value: any): Uint8Array {
  const buf = new ArrayBuffer(8)
  const d = new DataView(buf)
  // 类型纪律：这是"值→字节"的最后一站，value 必须已经是目录字段声明类型的对应物
  // ——数值字段 number、Color 字段 '#RRGGBB' 字符串。类型不符直接抛错：
  // 静默编 0 会把笔误伪装成"生效且值为 0"，是最难排查的一类故障。
  // 第一道校验在 UI 输入处（面板转 Number / 颜色串）。
  if (type === ValueType.Color) {
    if (typeof value !== 'string') {
      throw new TypeError(`Color 字段需要 '#RRGGBB' 字符串，收到 ${typeof value}: ${value}`)
    }
    const m = /^#?([0-9a-f]{6})$/i.exec(value)
    if (!m) throw new Error(`Color 需要 6 位十六进制（如 '#ff0000'），收到 ${value}`)
    const rgb = parseInt(m[1]!, 16)
    d.setUint8(0, (rgb >> 16) & 0xff)
    d.setUint8(1, (rgb >> 8) & 0xff)
    d.setUint8(2, rgb & 0xff)
    return new Uint8Array(buf)
  }
  // 其余均为数值型（含 Bool/OnOff）：bool 仅开关字段放行。
  // 旧代码 `typeof value === 'number' ? value : 0` 会把 true 编成 0（Python 侧编 1），
  // 两边对布尔输入行为不一致，顺手修正
  if (typeof value === 'boolean') {
    if (type !== ValueType.Bool && type !== ValueType.OnOff) {
      throw new TypeError(`数值字段收到布尔值 ${value}`)
    }
  } else if (typeof value !== 'number') {
    throw new TypeError(`数值字段需要 number，收到 ${typeof value}: ${value}`)
  }
  const n = Number(value)
  switch (type) {
    case ValueType.UInt8:
      d.setUint8(0, n)
      break
    case ValueType.Int8:
      d.setInt8(0, n)
      break
    case ValueType.UInt16:
      d.setUint16(0, n, true)
      break
    case ValueType.Int16:
      d.setInt16(0, n, true)
      break
    case ValueType.UInt32:
      d.setUint32(0, n >>> 0, true)
      break
    case ValueType.Int32:
      d.setInt32(0, n, true)
      break
    case ValueType.Float:
      d.setFloat32(0, n, true)
      break
    case ValueType.Double:
      d.setFloat64(0, n, true)
      break
    case ValueType.UInt64:
      d.setBigUint64(0, BigInt(Math.trunc(n)), true)
      break
    case ValueType.Int64:
      d.setBigInt64(0, BigInt(Math.trunc(n)), true)
      break
    case ValueType.Bool:
    case ValueType.OnOff:
      d.setUint8(0, n ? 1 : 0)
      break
    default:
      break // str/other 不允许调参，编码全 0
  }
  return new Uint8Array(buf)
}

/* ===== 组帧（下发） ===== */

/** cmd 收 CmdGet | number：动作命令字(0x80~0x9F)不在 CmdGet 小枚举里（commandSend
    传 item.index），演示源的 Response 帧又是上行命令字——这里只写一个字节，
    按段判定的活儿不归它管，故不收窄成 CmdGet 逼各处 cast */
export function buildFrame(cmd: CmdGet | number, payload: number[] = []): Uint8Array {
  const frame = new Uint8Array(payload.length + 3)
  frame[0] = loadPrefs().frameHead
  frame[1] = cmd
  let xor = cmd
  payload.forEach((b, i) => {
    frame[2 + i] = b
    xor ^= b
  })
  frame[frame.length - 1] = xor
  return frame
}

/* ===== 上位机内部段 0xE0~0xEF：后端自己造的帧，不下发主控 =====
   作为独立 WS 二进制消息发送，不混进下位机原始字节流；
   前端 onmessage 按 Cmd 先截下来，不进 FrameParser。 */
export const CMD_UI_STATUS = 0xe0
export const CMD_UI_ALERT = 0xe1
export const CMD_UI_CHART_SUGGEST = 0xe2

/** 与后端 protocol.py 同名同值；对应 config.json 的 max_groups/max_items（桌面端 4 通道 × 4 条）。 */
export const UI_SUGGEST_MAX_GROUPS = 4
export const UI_SUGGEST_MAX_ITEMS = 4

export type UiFrame =
  | { kind: 'uiStatus'; state: number }
  | { kind: 'uiAlert'; cmd: number }
  | { kind: 'uiSuggest'; groups: number[][]; source: string; note: string }

/** 解一帧上位机内部段；校验 XOR，坏帧或非本段返回 null。文案一律由前端造。 */
export function decodeUiFrame(d: Uint8Array): UiFrame | null {
  if (d.length < 5 || d[0] !== loadPrefs().frameHead || (d[1]! & 0xf0) !== 0xe0) return null
  let xor = 0
  for (let i = 1; i < d.length - 1; i++) xor ^= d[i]!
  if (xor !== d[d.length - 1]) return null
  const p = d.subarray(2, d.length - 1)
  const dec = new TextDecoder('utf-8')
  if (d[1] === CMD_UI_STATUS && p.length >= 2) return { kind: 'uiStatus', state: p[0]! }
  if (d[1] === CMD_UI_ALERT && p.length >= 2) return { kind: 'uiAlert', cmd: p[0]! }
  if (d[1] === CMD_UI_CHART_SUGGEST) {
    const groups: number[][] = []
    let off = 0
    for (let ch = 0; ch < UI_SUGGEST_MAX_GROUPS; ch++) {
      const n = p[off++] ?? 0
      if (n > UI_SUGGEST_MAX_ITEMS || off + n > p.length) return null
      groups.push(Array.from(p.subarray(off, off + n)))
      off += n
    }
    const L = p[off++] ?? 0
    if (off + L > p.length) return null
    const ex = p.subarray(off, off + L)
    let source = '', note = ''
    if (ex.length >= 1) {
      const sl = ex[0]!
      source = dec.decode(ex.subarray(1, 1 + sl))
      const nl = ex[1 + sl] ?? 0
      note = dec.decode(ex.subarray(2 + sl, 2 + sl + nl))
    }
    return { kind: 'uiSuggest', groups, source, note }
  }
  return null
}

/** 目录请求帧：[len 1B][index...]；len=0 全量，非0=只补发这些下标。
    补发由后端算缺失项后发起（不清已收目录）；直连串口模式同理。 */
function requestDir(cmd: number, indices?: number[]): Uint8Array {
  const idx = indices ?? []
  return buildFrame(cmd, [idx.length & 0xff, ...idx.map((i) => i & 0xff)])
}

/** 请求调参目录（indices 省略/空=全量） */
export function requestTunable(indices?: number[]): Uint8Array {
  return requestDir(CmdGet.RequestTunable, indices)
}
/** 请求监控目录（indices 语义同 requestTunable） */
export function requestMonitor(indices?: number[]): Uint8Array {
  return requestDir(CmdGet.RequestMonitor, indices)
}
/**
 * 改订阅档位；index=0xff 对全部生效。
 * 只发上位机负责的两个位（SUB/FAST），下位机合并时保留 GETTER/KIND 等本机位；
 * sub=false 即退订，与订阅是同一条命令，无需单独的退订命令。
 */
export function subscribe(index: number, sub: boolean, fast: boolean): Uint8Array {
  const tag = (sub ? MONITOR_TAG_SUB : 0) | (fast ? MONITOR_TAG_FAST : 0)
  return buildFrame(CmdGet.Subscribe, [index, tag])
}
/** 心跳帧 A5 FF FF，80ms 一发；主控回同形帧 Pong */
export function ping(): Uint8Array {
  return buildFrame(CmdGet.Ping)
}
/** 调参写入帧：[index][value 8B]，主控回 TunableEcho 确认真实生效值 */
export function setTunable(index: number, type: ValueType, value: any): Uint8Array {
  return buildFrame(CmdGet.SetTunable, [index, ...encodeValue8(type, value)])
}
/** 请求动作命令目录（主控分批回 CMDDirectory；indices 语义同 requestTunable） */
export function requestCommand(indices?: number[]): Uint8Array {
  return requestDir(CmdGet.RequestCommand, indices)
}
/** 请求按键目录（indices 语义同 requestTunable） */
export function requestKeyDir(indices?: number[]): Uint8Array {
  return requestDir(CmdGet.RequestKeyDir, indices)
}
/**
 * 动作命令下发帧：Cmd 就是命令字本身（0x80~0x9F，不是 CmdGet 段），
 * payload 按 fields 顺序把各字段真实宽度的小端字节紧凑拼接（无填充）。
 * 固件回 Response 帧确认（OK/忙拒绝），补发逻辑在后端待确认表；
 * 相对量命令（原地转向/直行/局部移动）重复执行会累计，重发窗口见协议注释。
 */
export function commandSend(item: CmdDirItem, values: any[]): Uint8Array {
  const payload: number[] = []
  item.fields.forEach((f, i) => {
    payload.push(...encodeValue(f.type, values[i] ?? 0))
  })
  return buildFrame(item.index, payload)
}

/* ===== 解帧（接收状态机，与下位机 refresh 里的状态机同构） =====
   注意：上行帧全是变长帧——
   目录帧 = 4B定长头 + 名字(nameLen) + 单位长1B + 单位(unitLen) + 值(宽度由type决定)
   监控帧 = 1B payloadlen + 2B time + ([index][值])×N
            payloadlen 已给总长；帧内既无项数也无值宽，靠目录登记表推进偏移走到末尾
   调参回显 = 1B index + 值(宽度查调参登记表)
   长度要边收边算，不能像下行命令那样查固定长度表。 */

const enum RxState {
  WaitHead,
  WaitCmd,
  WaitData,
  WaitXor,
}

/**
 * 试算调参/监控目录帧的 payload 总长：
 *   [index][type][tag][名长][名][单位长][单位][值]
 * 单位长字节还没收到时返回 -1（继续等），收齐后算出总长。
 */
function dirLen(p: Uint8Array, len: number): number {
  if (len < 4) return -1
  const nameLen = p[3]!
  const unitLenAt = 4 + nameLen // 单位长字节的位置
  if (len < unitLenAt + 1) return -1
  return 5 + nameLen + p[unitLenAt]! + valueSize(p[1]!)
}

/**
 * 动作命令目录帧的 payload 总长：
 *   [内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[tag bit7=1时:(min,max)×N]
 * 头 2B 是"后续内容长度"（小端 uint16），收够 2B 即可定长，不必遍历字段走查。
 * 由下位机给长度是因为：min/max 按字段类型定宽后，长度要按各字段 valueSize 累加，
 * 上位机再实现一遍这套走查既啰嗦又容易与下位机不一致。
 */
function cmdDirLen(p: Uint8Array, len: number): number {
  if (len < 2) return -1 // 长度前缀还没收齐
  return 2 + (p[0]! | (p[1]! << 8))
}

/**
 * 按键目录帧的 payload 总长：[index][name_len][name]，name_len 1~16。
 * 收够 2B 即可定长；name_len 非法返回 -2（调用方回找帧头），还不够返回 -1。
 */
function keyDirLen(p: Uint8Array, len: number): number {
  if (len < 2) return -1
  const nameLen = p[1]!
  if (nameLen === 0 || nameLen > 16) return -2
  return 2 + nameLen
}

export class FrameParser {
  private state = RxState.WaitHead
  private cmd = 0
  /** -1 表示长度尚未能确定（目录帧要收够4B头、监控帧要收到index） */
  private expect = -1
  private len = 0
  private xor = 0
  private payload = new Uint8Array(MAX_PAYLOAD_CAP)
  /**
   * 监控项 index→类型，收到监控目录后由外部登记（setMonitorType），
   * 解 MonitorValue 帧时要用。初值 0xFF 不是合法 ValueType，即"目录里
   * 没登记过"——解到这种 index 就丢该帧剩余，与 0(UInt8) 区分开。
   */
  private monitorType = new Uint8Array(256).fill(0xFF)

  /** 链路诊断计数（故障定位用，UI 直接读） */
  readonly stats = {
    bytes: 0, // 累计收到的原始字节数
    frames: 0, // 校验通过、成功成帧的数量
    badChecksum: 0, // 校验失败被丢弃的帧数
    unknownCmd: 0, // 帧头后出现未知命令字（可能是波特率/协议版本不对）
    overflow: 0, // payload 超过 prefs.maxPayload 被放弃
  }

  clearStats(): void {
    this.stats.bytes = 0
    this.stats.frames = 0
    this.stats.badChecksum = 0
    this.stats.unknownCmd = 0
    this.stats.overflow = 0
  }

  setMonitorType(index: number, type: ValueType): void {
    this.monitorType[index] = type
  }

  resetMonitorTypes(): void {
    this.monitorType.fill(0xFF)
  }

  /** 喂入任意长度的字节块（半包/粘包都在此消化），完整帧通过回调吐出 */
  feed(chunk: Uint8Array, onEvent: (e: RxEvent) => void): void {
    this.stats.bytes += chunk.length
    for (const b of chunk) {
      switch (this.state) {
        case RxState.WaitHead:
          if (b === loadPrefs().frameHead) this.state = RxState.WaitCmd
          break

        case RxState.WaitCmd:
          // CmdPost 枚举就是合法上行命令字全集；不在里面的字节立刻重找帧头，
          // 不能当"有 payload 的帧"收（会一路吞到 prefs.maxPayload，吃掉后面几帧）。
          if (!CMD_POST_VALUES.has(b)) {
            // 其余命令字不可能来自下位机：协议不匹配（常见于固件还是旧版）
            this.stats.unknownCmd++
            this.state = RxState.WaitHead
          } else if (b === CmdPost.Pong) {
            this.cmd = b
            this.len = 0
            this.xor = b
            this.state = RxState.WaitXor
          } else {
            this.cmd = b
            this.expect = -1
            this.len = 0
            this.xor = b
            this.state = RxState.WaitData
          }
          break

        case RxState.WaitData:
          if (this.len >= loadPrefs().maxPayload) {
            // 长度异常，放弃这帧
            this.stats.overflow++
            this.state = RxState.WaitHead
            break
          }
          this.payload[this.len] = b
          this.len++
          this.xor ^= b

          // 收到足够的前置字节后才能算出本帧总 payload 长度
          if (this.expect < 0) {
            if (this.cmd === CmdPost.MonitorValue) {
              // 监控值帧自带 1B 长度前缀，不必先拿到目录；len 只算它后面那些字节，+1 是 len 自身。
              // payloadlen==0 非法（固件 monitor_tick 仅在 pack_num>0 时 flush、每帧必含 2B
              // 时间戳），只可能是流错位——直接回找帧头，不猜长度
              if (this.payload[0]! === 0) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 1 + this.payload[0]!
              }
            } else if (this.cmd === CmdPost.TunableEcho) {
              // 调参回显：[type][index][value]，type 前缀自带宽度，解帧不查表
              // （与后端 protocol.py 同步）。width=0 的 type 不合法：流错位，重找帧头
              const sz = valueSize(this.payload[0]! as ValueType)
              if (sz === 0) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 2 + sz
              }
            } else if (this.cmd === CmdPost.KeyDirectory) {
              // 按键目录：[index][name_len][name]，收够 2B 即定长；必须在 dirLen 兜底之前判定
              const total = keyDirLen(this.payload, this.len)
              if (total === -2) {
                this.state = RxState.WaitHead
              } else if (total >= 0) {
                this.expect = total
              }
            } else if (this.cmd === CmdPost.CMDDirectory) {
              // 命令目录：payload 头 2B 就是内容长度，收够 2B 即定长
              const total = cmdDirLen(this.payload, this.len)
              if (total >= 0) this.expect = total
            } else if (this.cmd === CmdPost.Response) {
              // 响应帧 [payloadlen][原话cmd][原话payload][status][原因串]：
              // payloadlen 最小 2（原话cmd+status），0/1 只可能是流错位，重找 A5
              if (this.payload[0]! < 2) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 1 + this.payload[0]!
              }
            } else {
              // 目录帧：沿 名长→名→单位长 走查才能算出总长
              const total = dirLen(this.payload, this.len)
              if (total >= 0) this.expect = total
            }
          }
          if (this.expect > 0 && this.len >= this.expect) this.state = RxState.WaitXor
          break

        case RxState.WaitXor:
          this.state = RxState.WaitHead
          if ((this.xor ^ b) === 0) {
            this.stats.frames++
            this.dispatch(onEvent)
          } else {
            this.stats.badChecksum++
          }
          break
      }
    }
  }

  private dispatch(onEvent: (e: RxEvent) => void): void {
    const p = this.payload
    switch (this.cmd) {
      case CmdPost.TunableDirectory:
      case CmdPost.MonitorDirectory: {
        // [index][type][tag][名字长度][名字UTF-8][单位长度][单位UTF-8][值]
        if (this.len < 4) return
        const index = p[0]!
        const type = p[1]!
        const tag = p[2]!
        const nameLen = p[3]!
        const unitLenAt = 4 + nameLen
        if (this.len < unitLenAt + 1) return
        const unitLen = p[unitLenAt]!
        const valueAt = 5 + nameLen + unitLen
        if (this.len < valueAt) return
        const decoder = new TextDecoder('utf-8')
        const name = decoder.decode(p.subarray(4, unitLenAt))
        const unit = decoder.decode(p.subarray(unitLenAt + 1, valueAt))
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + valueAt), 0, type)
        onEvent(
          new DirectoryEvent(
            this.cmd === CmdPost.TunableDirectory ? 'tunable' : 'monitor',
            { index, type, tag, name, unit, value },
          ),
        )
        break
      }
      case CmdPost.MonitorValue: {
        // [payloadlen][time 2B]([index][值])×N：没有项数字段，类型查目录登记表；
        // 遇没登记过的 index（monitorType 里是 0xFF 哨兵）丢剩余，已解出的照常发
        if (this.len < 3) return
        const tMs = p[1]! | (p[2]! << 8)
        let off = 3
        while (off < this.len) {
          const type = this.monitorType[p[off]!] as ValueType
          const sz = valueSize(type)
          if (sz === 0) break // 目录里没登记过这项：丢剩余
          const value = decodeValue(new DataView(p.buffer, p.byteOffset + off + 1), 0, type)
          onEvent(new ValueEvent(p[off]!, value, tMs))
          off += 1 + sz
        }
        break
      }
      case CmdPost.TunableEcho: {
        // [type][index][实际生效值]：type 前缀自带宽度，不查表
        if (this.len < 2) return
        const type = p[0]! as ValueType
        if (valueSize(type) === 0) return
        const index = p[1]!
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + 2), 0, type)
        onEvent(new EchoEvent(index, value))
        break
      }
      case CmdPost.KeyDirectory: {
        // [index][name_len][name]
        if (this.len < 2) return
        const index = p[0]!
        const nameLen = p[1]!
        if (nameLen === 0 || nameLen > 16 || this.len < 2 + nameLen) return
        const name = new TextDecoder('utf-8').decode(p.subarray(2, 2 + nameLen))
        onEvent(new KeyDirectoryEvent({ index, name }))
        break
      }
      case CmdPost.CMDDirectory: {
        // [内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×N]
        if (this.len < 5) return
        let off = 2 // 跳过 payload 头的 2B 内容长度
        const index = p[off]!
        const tag = p[off + 1]!
        const nameLen = p[off + 2]!
        const nameAt = off + 3
        off = nameAt + nameLen
        if (this.len < off + 1) return
        const decoder = new TextDecoder('utf-8')
        const name = decoder.decode(p.subarray(nameAt, off))
        const fieldNum = p[off]!
        off++
        const fields: CmdField[] = []
        for (let i = 0; i < fieldNum; i++) {
          if (off + 2 > this.len) return
          const type = p[off]! as ValueType
          const flen = p[off + 1]!
          off += 2
          if (off + flen > this.len) return
          fields.push({ type, name: decoder.decode(p.subarray(off, off + flen)) })
          off += flen
        }
        // tag bit7=1：每字段一对 min/max，与该字段同类型、同宽度（valueSize），按字段顺序排列
        if (tag & CMD_TAG_RANGE) {
          for (const f of fields) {
            const sz = valueSize(f.type)
            if (sz === 0 || off + 2 * sz > this.len) break // str/other 无范围；或帧被截断
            const d = new DataView(p.buffer, p.byteOffset + off)
            const mn = decodeValue(d, 0, f.type)
            const mx = decodeValue(d, sz, f.type)
            if (typeof mn === 'number') f.min = mn
            if (typeof mx === 'number') f.max = mx
            off += 2 * sz
          }
        }
        onEvent(new CmdDirectoryEvent({ index, tag, name, fields }))
        break
      }
      case CmdPost.Response: {
        // [payloadlen][原话cmd][原话payload][status][尾字节]。
        // 长度与 status 定位全由帧自带 L 决定，不依赖目录登记的回显长度。
        if (this.len < 3) return
        // decodeResponse 与后端 decode_response 同构：长度完全由帧自带的 L 给出，
        // 只需传"本帧 payload 的紧致切片"。this.payload 是 MAX_PAYLOAD_CAP 满缓冲，
        // 整个传进去 L 会与长度不符——Response 帧会被全部静默丢弃。
        const decoded = decodeResponse(p.subarray(0, this.len))
        if (decoded === null) return
        onEvent(
          new ResponseEvent(decoded.echoCmd, decoded.echoPayload, decoded.status, decoded.reason),
        )
        break
      }
      case CmdPost.Pong:
        // 主控的心跳回应，无 payload；是否用来刷新在线状态由 store 决定
        onEvent(new PongEvent())
        break
      default:
        break
    }
  }
}
