/**
 * 演示传输：无硬件时模拟下位机行为，走与真机完全相同的二进制帧路径。
 * 收到目录请求后回目录帧，随后按 tag 位域节奏推送监控值（x/y/yaw 10ms，其余 80ms）。
 */

import {
  FRAME_HEAD,
  CmdGet,
  CmdPost,
  MONITOR_TAG_FAST,
  MONITOR_TAG_SUB,
  MonitorKind,
  ValueType,
  CmdSpec,
  CMD_TAG_ARGC_SHIFT,
  CMD_TAG_RANGE,
  tagFast,
  tagSubscribed,
} from './protocol'
import type { SerialTransport, TransportCallbacks } from './transport'

interface MockItem {
  type: ValueType
  tag: number
  name: string
  unit: string
  read: () => number
}

// tag 位域与真机一致：位姿量 = SUB|FAST|KIND_*（语义由本机钉死，上位机订阅命令改不动）
const POS_X = MONITOR_TAG_SUB | MONITOR_TAG_FAST | MonitorKind.PosX
const POS_Y = MONITOR_TAG_SUB | MONITOR_TAG_FAST | MonitorKind.PosY
const YAW = MONITOR_TAG_SUB | MONITOR_TAG_FAST | MonitorKind.Yaw
const SLOW = MONITOR_TAG_SUB
const NONE = 0

const monitorItems: MockItem[] = [
  { type: ValueType.Float, tag: POS_X, name: '全局坐标X', unit: 'm', read: () => 1.2 + Math.sin(performance.now() / 1000) * 0.8 },
  { type: ValueType.Float, tag: POS_Y, name: '全局坐标Y', unit: 'm', read: () => 0.6 + Math.cos(performance.now() / 1400) * 0.5 },
  { type: ValueType.Float, tag: YAW, name: '航向角', unit: 'deg', read: () => (performance.now() / 20) % 360 - 180 },
  { type: ValueType.Float, tag: SLOW, name: '角度', unit: 'deg', read: () => 114.514 + Math.sin(performance.now() / 800) * 20 },
  { type: ValueType.UInt32, tag: SLOW, name: '幸运数2', unit: '', read: () => 1919810 },
  { type: ValueType.Bool, tag: SLOW, name: 'LED PC13', unit: '', read: () => (Math.floor(performance.now() / 700) % 2) },
  { type: ValueType.Int64, tag: NONE, name: '这是一个64位整数', unit: '', read: () => -1145141909810114514 },
]

// 调参项是可写的：当前值单独放一份状态，SetTunable 时改写并回显
const tunableState = [114514, 990, 114.514]
const tunableItems: MockItem[] = [
  { type: ValueType.UInt32, tag: NONE, name: '幸运数', unit: '', read: () => tunableState[0]! },
  { type: ValueType.UInt16, tag: NONE, name: 'LED5 PWM', unit: '', read: () => tunableState[1]! },
  { type: ValueType.Float, tag: NONE, name: '角度', unit: 'deg', read: () => tunableState[2]! },
]

/** 按类型从小端字节解码（SetTunable 的 8B value 段），与 protocol.encodeValue8 对应 */
function decodeValueLE(bytes: Uint8Array, type: ValueType): number {
  const d = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  switch (type) {
    case ValueType.UInt8:
    case ValueType.Bool:
    case ValueType.OnOff:
      return d.getUint8(0)
    case ValueType.Int8:
      return d.getInt8(0)
    case ValueType.UInt16:
      return d.getUint16(0, true)
    case ValueType.Int16:
      return d.getInt16(0, true)
    case ValueType.UInt32:
      return d.getUint32(0, true)
    case ValueType.Int32:
      return d.getInt32(0, true)
    case ValueType.Float:
      return d.getFloat32(0, true)
    case ValueType.Double:
      return d.getFloat64(0, true)
    case ValueType.UInt64:
      return Number(d.getBigUint64(0, true))
    case ValueType.Int64:
      return Number(d.getBigInt64(0, true))
    default:
      return 0
  }
}

/** 小端写入一个数值，返回写入字节数 */
function appendValue(p: number[], type: ValueType, v: number): number {
  const pushN = (bytes: number, base: bigint, signed = false) => {
    let u = BigInt(Math.trunc(v))
    if (signed && u < 0n) u = base + u
    for (let i = 0; i < bytes; i++) p.push(Number((u >> BigInt(8 * i)) & 0xffn))
  }
  switch (type) {
    case ValueType.UInt8:
    case ValueType.Bool:
    case ValueType.OnOff:
      p.push(v & 0xff)
      return 1
    case ValueType.Int8:
      p.push(v & 0xff)
      return 1
    case ValueType.UInt16:
    case ValueType.Int16:
      pushN(2, 1n << 16n)
      return 2
    case ValueType.UInt32:
    case ValueType.Int32:
      pushN(4, 1n << 32n)
      return 4
    case ValueType.UInt64:
    case ValueType.Int64:
      pushN(8, 1n << 64n)
      return 8
    case ValueType.Float: {
      const buf = new ArrayBuffer(4)
      new DataView(buf).setFloat32(0, v, true)
      p.push(...new Uint8Array(buf))
      return 4
    }
    case ValueType.Double: {
      const buf = new ArrayBuffer(8)
      new DataView(buf).setFloat64(0, v, true)
      p.push(...new Uint8Array(buf))
      return 8
    }
    default:
      return 0
  }
}

/** 演示用动作命令目录项（字段定义与固件 robot_and_control.cpp 的登记表一致） */
interface MockCmd {
  index: number
  tag: number
  name: string
  fields: { type: ValueType; name: string }[]
  /** tag bit7=1 时下发的取值范围数组[min1,max1,...]，与字段顺序配对 */
  ranges?: number[]
}

const mockCommands: MockCmd[] = [
  { index: 0x80, tag: (0 << CMD_TAG_ARGC_SHIFT) | CmdSpec.Stop, name: '停止运动', fields: [] },
  { index: 0x81, tag: (1 << CMD_TAG_ARGC_SHIFT) | CMD_TAG_RANGE, name: '原地转向', fields: [{ type: ValueType.Float, name: '角度(度)' }], ranges: [-360, 360] },
  { index: 0x82, tag: (1 << CMD_TAG_ARGC_SHIFT) | CMD_TAG_RANGE, name: '直行', fields: [{ type: ValueType.Float, name: '距离(米)' }], ranges: [-10, 10] },
  { index: 0x83, tag: (3 << CMD_TAG_ARGC_SHIFT) | CMD_TAG_RANGE, name: '局部移动', fields: [
    { type: ValueType.Float, name: 'x前(米)' },
    { type: ValueType.Float, name: 'y右(米)' },
    { type: ValueType.Float, name: '航向(度)' },
  ], ranges: [-10, -10, -10, 10, -360, 360] },
]

/** 拼动作命令目录帧：[命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×参数量] */
function cmdDirectoryFrame(c: MockCmd): Uint8Array {
  const enc = new TextEncoder()
  const payload: number[] = [c.index, c.tag]
  const nameBytes = [...enc.encode(c.name)]
  payload.push(nameBytes.length, ...nameBytes, c.fields.length)
  for (const f of c.fields) {
    const fb = [...enc.encode(f.name)]
    payload.push(f.type, fb.length, ...fb)
  }
  // tag bit7=1：字段后跟每字段一对min/max（float小端各4B）
  if (c.tag & CMD_TAG_RANGE)
    for (const v of c.ranges ?? []) appendValue(payload, ValueType.Float, v)
  return frame(CmdPost.CMDDirectory, payload)
}

/** 通用封帧（响应帧的命令字不是 CmdGet，所以不能直接用 protocol.buildFrame） */
function frame(cmd: number, payload: number[]): Uint8Array {
  const out = new Uint8Array(payload.length + 3)
  out[0] = FRAME_HEAD
  out[1] = cmd
  let xor = cmd
  payload.forEach((b, i) => {
    out[2 + i] = b
    xor ^= b
  })
  out[out.length - 1] = xor
  return out
}

/** 拼调参/监控目录帧：[index][type][tag][名长][名][单位长][单位][值]，与真机 send_dir_batch 同构 */
function directoryFrames(cmd: number, items: MockItem[]): Uint8Array[] {
  return items.map((it, i) => {
    const enc = new TextEncoder()
    const nameBytes = [...enc.encode(it.name)]
    const unitBytes = [...enc.encode(it.unit)]
    const payload = [i, it.type, it.tag, nameBytes.length, ...nameBytes, unitBytes.length, ...unitBytes]
    appendValue(payload, it.type, it.read())
    return frame(cmd, payload)
  })
}

function valueFrame(index: number, item: MockItem): Uint8Array {
  const payload: number[] = [index]
  appendValue(payload, item.type, item.read())
  return frame(CmdPost.MonitorValue, payload)
}

export class MockTransport implements SerialTransport {
  readonly kind = 'mock' as const
  private fastTimer: ReturnType<typeof setInterval> | null = null
  private slowTimer: ReturnType<typeof setInterval> | null = null
  private slowPhase = 0
  private monitorOn = false

  constructor(private cb: TransportCallbacks) {}

  isPortReady(): boolean {
    return true
  }

  async pickPort(): Promise<void> {
    /* 演示源无需选口 */
  }

  get portLabel(): string {
    return '演示数据源（模拟 V5）'
  }

  async connect(): Promise<void> {
    this.cb.onStatus('connecting', '正在启动演示数据源 …')
    await new Promise((r) => setTimeout(r, 200))
    this.cb.onStatus('connected', '已连接演示数据源')
  }

  async disconnect(): Promise<void> {
    this.stopTimers()
    this.monitorOn = false
  }

  /** 模拟下位机收到下发命令 */
  async send(data: Uint8Array): Promise<void> {
    if (data.length < 2 || data[0] !== FRAME_HEAD) return
    const cmd = data[1]
    if (cmd === CmdGet.RequestTunable) {
      directoryFrames(CmdPost.TunableDirectory, tunableItems).forEach((f) => this.emit(f))
    } else if (cmd === CmdGet.RequestMonitor) {
      directoryFrames(CmdPost.MonitorDirectory, monitorItems).forEach((f) => this.emit(f))
      this.startTimers()
    } else if (cmd === CmdGet.Subscribe) {
      // [index][tag]，与真机 merge_subscribe_tag 同语义：
      // 只合并上位机的 SUB/FAST 两位，GETTER/KIND 等本机位保留；bit7=0 即退订；index=0xFF 全部生效
      const index = data[2]!
      const incoming = data[3]!
      const hostBits = MONITOR_TAG_SUB | MONITOR_TAG_FAST
      const merge = (old: number) => (incoming & hostBits) | (old & ~hostBits & 0xff)
      if (index === 0xff) monitorItems.forEach((it) => (it.tag = merge(it.tag)))
      else if (monitorItems[index]) monitorItems[index]!.tag = merge(monitorItems[index]!.tag)
    } else if (cmd === CmdGet.SetTunable) {
      // [index][value 8B]：按类型解码写入状态，下一"拍"回 TunableEcho
      const index = data[2]!
      const it = tunableItems[index]
      if (!it) return
      const v = decodeValueLE(data.subarray(3, 11), it.type)
      tunableState[index] = v
      const payload: number[] = [index]
      appendValue(payload, it.type, v)
      this.emit(frame(CmdPost.TunableEcho, payload))
    } else if (cmd === CmdGet.RequestCommand) {
      // 命令目录：一条命令一帧，分两拍发，模拟真机分批轮转
      mockCommands.forEach((c, i) => {
        setTimeout(() => this.cb.onData(cmdDirectoryFrame(c)), i * 10)
      })
    } else if (cmd >= 0x80 && cmd <= 0x9f) {
      // 动作命令下发：演示源不执行真动作，静默吞掉（无ACK协议，真机同理不回帧）
    }
  }

  private emit(f: Uint8Array): void {
    // 异步抛出，模拟串口到达的时序
    setTimeout(() => this.cb.onData(f), 0)
  }

  private startTimers(): void {
    if (this.monitorOn) return
    this.monitorOn = true
    // 高速项 10ms
    this.fastTimer = setInterval(() => {
      monitorItems.forEach((it, i) => {
        if (tagSubscribed(it.tag) && tagFast(it.tag)) this.cb.onData(valueFrame(i, it))
      })
    }, 10)
    // 低速项按 index%8 分相，80ms 一轮（演示源每项都在相0，简化）
    this.slowTimer = setInterval(() => {
      monitorItems.forEach((it, i) => {
        if (tagSubscribed(it.tag) && !tagFast(it.tag) && (i & 7) === this.slowPhase)
          this.cb.onData(valueFrame(i, it))
      })
      this.slowPhase = (this.slowPhase + 1) & 7
    }, 10)
  }

  private stopTimers(): void {
    if (this.fastTimer) clearInterval(this.fastTimer)
    if (this.slowTimer) clearInterval(this.slowTimer)
    this.fastTimer = null
    this.slowTimer = null
  }
}
