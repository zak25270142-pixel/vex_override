/**
 * VEX V5 User 口二进制协议（与下位机 communication.cpp 严格一一对应）
 *
 * 帧格式: A5 | Cmd | Payload... | XOR
 *   XOR = Cmd 起逐字节异或（不含帧头）
 * 多字节数值一律小端（V5 ARM 与 PC x86 同为小端）
 */

export const FRAME_HEAD = 0xa5

/** 主控 → 上位机 */
export const enum CmdPost {
  TunableDirectory = 0,
  MonitorDirectory = 1,
  MonitorValue = 2,
  /** 调参回显：[index][实际生效值]，收到 SetTunable 后下一拍回 */
  TunableEcho = 3,
  /** 动作命令目录：[命令字][名长][名][参数量][字段描述...]，一条命令一帧 */
  CMDDirectory = 4,
  /** 心跳回应：收到 Ping 后下一拍回，无 payload，整帧 A5 FF FF */
  Pong = 0xff,
}

/** 上位机 → 主控 */
export const enum CmdGet {
  RequestTunable = 0,
  RequestMonitor = 1,
  Subscribe = 2,
  /** 调参写入：[index][value×8B 定长]，类型查调参表，不足 8B 高位补零 */
  SetTunable = 3,
  /** 请求动作命令目录（无 payload），主控分批回 CMDDirectory */
  RequestCommand = 4,
  /** 心跳：每 1s 发一帧，无 payload，整帧 A5 FF FF */
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

/** 语义种类（tag & KIND_MASK），位置量由前端自动订阅并喂给场地图 */
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

/** 单帧 payload 上限（uint8 长度的天然最大值；整帧 258B < USB 单包 512B） */
export const MAX_PAYLOAD = 255

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

/** 一个值：数值类为 number，color 为 #RRGGBB 字符串 */
export type CellValue = number | string

/** 目录项（一帧一项）；tag 为位域字节，用 tagSubscribed/tagFast/tagKind 解读；unit 为空串表示无单位 */
export interface DirItem {
  index: number
  type: ValueType
  tag: number
  name: string
  unit: string
  value: CellValue
}

/** 动作命令的一个输入字段（类型用于渲染控件和编码，name 是字段中文标签） */
export interface CmdField {
  type: ValueType
  name: string
}

/** 动作命令目录项：index 就是下发时用的命令字（0x80~0x9F） */
export interface CmdDirItem {
  index: number
  name: string
  fields: CmdField[]
}

export type RxEvent =
  | { kind: 'directory'; table: 'tunable' | 'monitor'; item: DirItem }
  | { kind: 'value'; index: number; value: CellValue }
  | { kind: 'echo'; index: number; value: CellValue }
  | { kind: 'cmdDirectory'; item: CmdDirItem }
  | { kind: 'pong' }

/** 按类型从小端字节流解码值；调用方保证剩余字节足够 */
function decodeValue(d: DataView, off: number, type: ValueType, le = true): CellValue {
  switch (type) {
    case ValueType.UInt8:
      return d.getUint8(off)
    case ValueType.Int8:
      return d.getInt8(off)
    case ValueType.UInt16:
      return d.getUint16(off, le)
    case ValueType.Int16:
      return d.getInt16(off, le)
    case ValueType.UInt32:
      return d.getUint32(off, le)
    case ValueType.Int32:
      return d.getInt32(off, le)
    case ValueType.Float:
      return d.getFloat32(off, le)
    case ValueType.Double:
      return d.getFloat64(off, le)
    case ValueType.UInt64:
      // 超过2^53会丢精度，监控场景可接受
      return Number(d.getBigUint64(off, le))
    case ValueType.Int64:
      return Number(d.getBigInt64(off, le))
    case ValueType.Bool:
    case ValueType.OnOff:
      return d.getUint8(off)
    case ValueType.Color: {
      const r = d.getUint8(off)
      const g = d.getUint8(off + 1)
      const b = getUint8OrZero(d, off + 2)
      return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')
    }
    default:
      return 0
  }
}

function getUint8OrZero(d: DataView, off: number): number {
  return off < d.byteLength ? d.getUint8(off) : 0
}

/**
 * 按类型把值编码成真实宽度的小端字节（动作命令字段用，紧凑排列无填充）。
 * str/other 宽度为 0，返回空数组；bool 收 0/1；color 收 '#RRGGBB'。
 */
export function encodeValue(type: ValueType, value: CellValue): Uint8Array {
  const size = valueSize(type)
  if (size === 0) return new Uint8Array(0)
  return encodeValue8(type, value).subarray(0, size)
}

/**
 * 把值编码成定长 8B 小端区域（SetTunable 的 value 段），与下位机 write_value 对应。
 * 不足 8B 的类型高位天然为 0；bool 收 0/1；color 收 '#RRGGBB'。
 */
function encodeValue8(type: ValueType, value: CellValue): Uint8Array {
  const buf = new ArrayBuffer(8)
  const d = new DataView(buf)
  const n = typeof value === 'number' ? value : 0
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
    case ValueType.Color: {
      const m = /^#?([0-9a-f]{6})$/i.exec(String(value))
      if (m) {
        const rgb = parseInt(m[1]!, 16)
        d.setUint8(0, (rgb >> 16) & 0xff)
        d.setUint8(1, (rgb >> 8) & 0xff)
        d.setUint8(2, rgb & 0xff)
      }
      break
    }
    default:
      break // str/other 不允许调参，编码全 0
  }
  return new Uint8Array(buf)
}

/* ===== 组帧（下发） ===== */

export function buildFrame(cmd: CmdGet, payload: number[] = []): Uint8Array {
  const frame = new Uint8Array(payload.length + 3)
  frame[0] = FRAME_HEAD
  frame[1] = cmd
  let xor = cmd
  payload.forEach((b, i) => {
    frame[2 + i] = b
    xor ^= b
  })
  frame[frame.length - 1] = xor
  return frame
}

export function requestTunable(): Uint8Array {
  return buildFrame(CmdGet.RequestTunable)
}
export function requestMonitor(): Uint8Array {
  return buildFrame(CmdGet.RequestMonitor)
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
/** 心跳帧 A5 FF FF，1s 一发；主控回同形帧 Pong */
export function ping(): Uint8Array {
  return buildFrame(CmdGet.Ping)
}
/** 调参写入帧：[index][value 8B]，主控回 TunableEcho 确认真实生效值 */
export function setTunable(index: number, type: ValueType, value: CellValue): Uint8Array {
  return buildFrame(CmdGet.SetTunable, [index, ...encodeValue8(type, value)])
}
/** 请求动作命令目录（主控分批回 CMDDirectory） */
export function requestCommand(): Uint8Array {
  return buildFrame(CmdGet.RequestCommand)
}
/**
 * 动作命令下发帧：Cmd 就是命令字本身（0x80~0x9F，不是 CmdGet 段），
 * payload 按 fields 顺序把各字段真实宽度的小端字节紧凑拼接（无 ACK）。
 */
export function commandSend(item: CmdDirItem, values: CellValue[]): Uint8Array {
  const payload: number[] = []
  item.fields.forEach((f, i) => {
    payload.push(...encodeValue(f.type, values[i] ?? 0))
  })
  return buildFrame(item.index as CmdGet, payload)
}

/* ===== 解帧（接收状态机，与下位机 refresh 里的状态机同构） =====
   注意：上行帧全是变长帧——
   目录帧 = 4B定长头 + 名字(nameLen) + 单位长1B + 单位(unitLen) + 值(宽度由type决定)
   监控帧 = 1B index + 值(宽度查目录登记表)
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
 * 试算动作命令目录帧的 payload 总长：
 *   [命令字][名长][名][参数量]([类型][字段名长][字段名])×N
 * 当前已收字节不足以走查完全部字段时返回 -1（继续等），走查完返回总长。
 */
function cmdDirLen(p: Uint8Array, len: number): number {
  if (len < 2) return -1
  let off = 2 + p[1]! // 越过 命令字 + 名长 + 名字
  if (len < off + 1) return -1
  const fieldNum = p[off]!
  off++
  for (let i = 0; i < fieldNum; i++) {
    if (len < off + 2) return -1 // 类型+字段名长还没收齐
    off += 2 + p[off + 1]!
  }
  return off
}

export class FrameParser {
  private state = RxState.WaitHead
  private cmd = 0
  /** -1 表示长度尚未能确定（目录帧要收够4B头、监控帧要收到index） */
  private expect = -1
  private len = 0
  private xor = 0
  private payload = new Uint8Array(MAX_PAYLOAD)
  /** 监控项 index→类型，收到监控目录后由外部登记，解 MonitorValue 帧时要用 */
  private monitorType = new Uint8Array(256)
  /** 调参项 index→类型，收到调参目录后登记，解 TunableEcho 帧时用（两张表 index 独立） */
  private tunableType = new Uint8Array(256)

  /** 链路诊断计数（故障定位用，UI 直接读） */
  readonly stats = {
    bytes: 0, // 累计收到的原始字节数
    frames: 0, // 校验通过、成功成帧的数量
    badChecksum: 0, // 校验失败被丢弃的帧数
    unknownCmd: 0, // 帧头后出现未知命令字（可能是波特率/协议版本不对）
    overflow: 0, // payload 超过 MAX_PAYLOAD 被放弃
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

  setTunableType(index: number, type: ValueType): void {
    this.tunableType[index] = type
  }

  resetMonitorTypes(): void {
    this.monitorType.fill(0)
  }

  resetTunableTypes(): void {
    this.tunableType.fill(0)
  }

  /** 喂入任意长度的字节块（半包/粘包都在此消化），完整帧通过回调吐出 */
  feed(chunk: Uint8Array, onEvent: (e: RxEvent) => void): void {
    this.stats.bytes += chunk.length
    for (const b of chunk) {
      switch (this.state) {
        case RxState.WaitHead:
          if (b === FRAME_HEAD) this.state = RxState.WaitCmd
          break

        case RxState.WaitCmd:
          // 业务帧：两类目录、监控值、调参回显（均变长）；Pong 无 payload
          if (b === CmdPost.Pong) {
            this.cmd = b
            this.len = 0
            this.xor = b
            this.state = RxState.WaitXor
          } else if (
            b === CmdPost.TunableDirectory ||
            b === CmdPost.MonitorDirectory ||
            b === CmdPost.MonitorValue ||
            b === CmdPost.TunableEcho ||
            b === CmdPost.CMDDirectory
          ) {
            this.cmd = b
            this.expect = -1
            this.len = 0
            this.xor = b
            this.state = RxState.WaitData
          } else {
            // 其余命令字不可能来自下位机：协议不匹配（常见于固件还是旧版）
            this.stats.unknownCmd++
            this.state = RxState.WaitHead
          }
          break

        case RxState.WaitData:
          if (this.len >= MAX_PAYLOAD) {
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
            if (this.cmd === CmdPost.MonitorValue || this.cmd === CmdPost.TunableEcho) {
              // 两种帧外形相同：[index][值]，只是类型登记表不同
              const types = this.cmd === CmdPost.MonitorValue ? this.monitorType : this.tunableType
              if (this.len >= 1) this.expect = 1 + valueSize(types[this.payload[0]!]!)
            } else if (this.cmd === CmdPost.CMDDirectory) {
              // 命令目录：字段数量嵌在帧里，要沿 [类型+名长+名] 逐步走查才知道总长
              const total = cmdDirLen(this.payload, this.len)
              if (total >= 0) this.expect = total
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
        onEvent({
          kind: 'directory',
          table: this.cmd === CmdPost.TunableDirectory ? 'tunable' : 'monitor',
          item: { index, type, tag, name, unit, value },
        })
        break
      }
      case CmdPost.MonitorValue: {
        // [index][值]，类型查目录登记表
        if (this.len < 1) return
        const index = p[0]!
        const type = this.monitorType[index] as ValueType
        if (valueSize(type) === 0) return
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + 1), 0, type)
        onEvent({ kind: 'value', index, value })
        break
      }
      case CmdPost.TunableEcho: {
        // [index][实际生效值]，类型查调参表登记表
        if (this.len < 1) return
        const index = p[0]!
        const type = this.tunableType[index] as ValueType
        if (valueSize(type) === 0) return
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + 1), 0, type)
        onEvent({ kind: 'echo', index, value })
        break
      }
      case CmdPost.CMDDirectory: {
        // [命令字][名长][名][参数量]([类型][字段名长][字段名])×N
        if (this.len < 2) return
        const index = p[0]!
        const nameLen = p[1]!
        if (this.len < 2 + nameLen + 1) return
        const decoder = new TextDecoder('utf-8')
        const name = decoder.decode(p.subarray(2, 2 + nameLen))
        let off = 2 + nameLen
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
        onEvent({ kind: 'cmdDirectory', item: { index, name, fields } })
        break
      }
      case CmdPost.Pong:
        // 主控的心跳回应，无 payload；是否用来刷新在线状态由 store 决定
        onEvent({ kind: 'pong' })
        break
      default:
        break
    }
  }
}
