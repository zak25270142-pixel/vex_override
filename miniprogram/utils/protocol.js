/**
 * VEX V5 User 口二进制协议（小程序端移植版）
 *
 * 真源链：include/communication.h ↔ backend/app/protocol.py ↔
 *         frontend/src/services/protocol.ts ↔ 本文件（第四端）
 * 任何一端改了编号或位域而其他端没跟上，链路会静默错乱（表现为"值对不上"
 * 而不是"报错"）——改这里必须四端一起改。
 *
 * 帧格式: A5 | Cmd | Payload... | XOR
 *   XOR = Cmd 起逐字节异或（不含帧头）
 * 多字节数值一律小端（V5 ARM 与手机小端一致）
 *
 * 与 TS 版的差异（小程序环境约束，协议行为零变化）：
 *   - const enum → Object.freeze 常量对象
 *   - TextDecoder → 手写 decodeUtf8（小程序不保证有 TextDecoder）
 *   - 64 位整数读取带 BigInt 缺失时的降级路径
 */

const FRAME_HEAD = 0xa5

/** 主控 → 上位机 */
const CmdPost = Object.freeze({
  TunableDirectory: 0,
  MonitorDirectory: 1,
  MonitorValue: 2,
  /** 调参回显：[type][index][实际生效值]，收到 SetTunable 后下一拍回 */
  TunableEcho: 3,
  /** 动作命令目录：[内容长度2B][命令字][tag][名长][名][参数量][字段描述...]，一条命令一帧 */
  CMDDirectory: 4,
  /** 通用响应帧：[payloadlen][原话cmd][原话payload][status][原因串] */
  Response: 5,
  /** 心跳回应：收到 Ping 后下一拍回，无 payload，整帧 A5 FF FF */
  Pong: 0xff,
})

/** 上位机 → 主控 */
const CmdGet = Object.freeze({
  RequestTunable: 0,
  RequestMonitor: 1,
  Subscribe: 2,
  /** 调参写入：[index][value×8B 定长] */
  SetTunable: 3,
  RequestCommand: 4,
  /** 心跳：每 80ms 发一帧，无 payload */
  Ping: 0xff,
})

/** 值类型，编号必须与 my_main.h 的 VALUE_TYPE 一致 */
const ValueType = Object.freeze({
  UInt8: 0,
  UInt16: 1,
  UInt32: 2,
  UInt64: 3,
  Int8: 4,
  Int16: 5,
  Int32: 6,
  Int64: 7,
  Float: 8,
  Double: 9,
  Str: 10,
  Bool: 11,
  OnOff: 12,
  Color: 13,
  Other: 14,
})

/** 监控 tag 单字节位域 */
const MONITOR_TAG_SUB = 0x80 // bit7：是否推送
const MONITOR_TAG_FAST = 0x40 // bit6：高速区（每拍）还是低速区（8 相轮转）
const MONITOR_TAG_GETTER = 0x20 // bit5：下位机 float() 函数取数（只读）
const MONITOR_TAG_KIND_MASK = 0x07 // bit2~0：语义种类

const MonitorKind = Object.freeze({ None: 0, PosX: 1, PosY: 2, Yaw: 3 })

/** 动作命令 tag 单字节位域 */
const CMD_TAG_RANGE = 0x80 // bit7：是否带取值范围
const CMD_TAG_ARGC_MASK = 0x70 // bit6~4：参数个数 0~7
const CMD_TAG_ARGC_SHIFT = 4
const CMD_TAG_SPEC_MASK = 0x0f // bit3~0：特化命令

const CmdSpec = Object.freeze({ Normal: 0, Stop: 1 })

/** 动作命令字段：0x80~0x9F */
const CMD_ACTION_MIN = 0x80
const CMD_ACTION_MAX = 0x9f

/** 单帧 payload 上限（整帧 448B < USB 单包 512B） */
const MAX_PAYLOAD = 445

function tagSubscribed(tag) { return (tag & MONITOR_TAG_SUB) !== 0 }
function tagFast(tag) { return (tag & MONITOR_TAG_FAST) !== 0 }
function tagKind(tag) { return tag & MONITOR_TAG_KIND_MASK }
function cmdSpec(tag) { return tag & CMD_TAG_SPEC_MASK }
function cmdArgc(tag) { return (tag & CMD_TAG_ARGC_MASK) >> CMD_TAG_ARGC_SHIFT }
function cmdHasRange(tag) { return (tag & CMD_TAG_RANGE) !== 0 }

/** 各类型在帧里占的字节数；str/other 不上报，返回 0 */
function valueSize(type) {
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
function typeName(type) {
  const names = {
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
  return names[type] || 'unknown'
}

/** 手写 UTF-8 解码（小程序不保证有 TextDecoder）；名字串 ≤30B，不涉及流式 */
function decodeUtf8(bytes, start, end) {
  let out = ''
  for (let i = start; i < end; i++) {
    const b = bytes[i]
    if (b < 0x80) {
      out += String.fromCharCode(b)
    } else if (b < 0xe0) {
      out += String.fromCharCode(((b & 0x1f) << 6) | (bytes[++i] & 0x3f))
    } else if (b < 0xf0) {
      out += String.fromCharCode(
        ((b & 0x0f) << 12) | ((bytes[++i] & 0x3f) << 6) | (bytes[++i] & 0x3f),
      )
    } else {
      const c =
        (((b & 0x07) << 18) |
          ((bytes[++i] & 0x3f) << 12) |
          ((bytes[++i] & 0x3f) << 6) |
          (bytes[++i] & 0x3f)) -
        0x10000
      out += String.fromCharCode(0xd800 + (c >> 10), 0xdc00 + (c & 0x3ff))
    }
  }
  return out
}

/* ===== Response 帧 status 字节（与 backend protocol.py 一一对应）=====
   bit7 类别：0=需补发（重发可能成功） 1=明确拒绝（重发无意义）
   bit6 模式：0=原因串（低6位=串长1~63B） 1=内定err（低6位=err号，不带串）
   分区：0x00 OK / 0x01~0x3F 补发+串 / 0x40~0x7F 补发+内定err /
        0x80 拒绝无串 / 0x81~0xBF 拒绝+串 / 0xC0~0xFF 拒绝+内定err
   0x00 的尾字节是二进制附加信息（长度隐式=帧长剩余）：目录请求=表内总项数1B，
   由后端对照补发；小程序只是观察者，见 respStrLen(-1) 与 decodeResponse */
const RESP_OK = 0x00
const RESP_CLASS_MASK = 0x80
const RESP_MODE_MASK = 0x40
const RESP_PAYLOAD_MASK = 0x3f

/** 内定err常用组合值（与 firmware/backend 同名同值；演示源构造拒绝帧用） */
const RESP_RETRY_CHECK = 0x40 | 1 // 校验失败
const RESP_RETRY_UNKNOWN_CMD = 0x40 | 2 // 命令字未注册
const RESP_REJECT_INDEX_RANGE = 0xc0 | 1 // 下标超表
const RESP_REJECT_TYPE_UNWRITABLE = 0xc0 | 2 // 类型不可写
const RESP_REJECT_BUSY = 0xc0 | 3 // 忙时拒绝

const RESP_ERR_RETRY_NAMES = ['通用重试', '校验失败', '命令字未注册']
const RESP_ERR_REJECT_NAMES = ['通用拒绝', '下标超表', '类型不可写', '忙']

function respIsOk(status) { return status === RESP_OK }
function respIsRetry(status) { return status !== RESP_OK && (status & RESP_CLASS_MASK) === 0 }
function respIsReject(status) { return (status & RESP_CLASS_MASK) !== 0 }
function respStrLen(status) {
  // OK 的尾字节长度隐式：0x00 低6位恒0装不下，长度由帧长反推（见 decodeResponse）
  if (status === RESP_OK) return -1
  return status & RESP_MODE_MASK ? 0 : status & RESP_PAYLOAD_MASK
}

/** status+尾字节 → 一句人话（toast 用）。尾字节：串模式=UTF-8 原因串；
    OK 模式=二进制 info（这里只显示 OK，info 由后端按回显 cmd 解释） */
function respReasonText(status, reason) {
  if (respIsOk(status)) return 'OK'
  if (status & RESP_MODE_MASK) {
    const code = status & RESP_PAYLOAD_MASK
    const names = respIsRetry(status) ? RESP_ERR_RETRY_NAMES : RESP_ERR_REJECT_NAMES
    return names[code] || `未知内定err#${code}`
  }
  return reason && reason.length ? decodeUtf8(reason, 0, reason.length) : '(空)'
}

/** 内置下行命令的 payload 定长（Response 回显长度兜底；动作命令查命令目录）。
    目录请求是 [len][index...] 变长：兜底按全量请求算 1B（补发帧的回显长度
    由待确认表/登记表给出，不走这里） */
const BUILTIN_ECHO_LEN = {
  [CmdGet.RequestTunable]: 1,
  [CmdGet.RequestMonitor]: 1,
  [CmdGet.RequestCommand]: 1,
  [CmdGet.Subscribe]: 2,
  [CmdGet.SetTunable]: 9,
  [CmdGet.Ping]: 0,
}

/**
 * 解 Response 帧 payload：[payloadlen][原话cmd][原话payload][status][尾字节]。
 * echoLen=回显payload应有字节数（从命令目录/内置定长得知；
 * 同命令字长度恒定是不变量）。
 * 尾字节按 status 模式解释：串模式=UTF-8 原因串（长度=低6位，须与帧长吻合）；
 * OK 模式=二进制 info（长度隐式=帧长剩余，目录请求时是 1B 表内总项数）。
 * 一致性校验不过返回 null（流错位/截断），与后端 decode_response 同构——
 * 小程序只是观察者，补发与重试在后端做。
 */
function decodeResponse(p, echoLen) {
  if (p.length < 3) return null
  if (p[0] < 2 || p[0] !== p.length - 1) return null
  const echoCmd = p[1]
  const statusAt = 2 + echoLen
  if (statusAt >= p.length) return null
  const status = p[statusAt]
  if (respIsOk(status)) {
    // OK+info：尾字节长度隐式（帧长剩余即附加信息），不再做第二重校验——
    // 帧总长已由 payloadlen 在上层钉过
    return {
      echoCmd,
      echoPayload: p.subarray(2, statusAt),
      status,
      reason: p.subarray(statusAt + 1),
    }
  }
  const strLen = respStrLen(status)
  if (statusAt + 1 + strLen !== p.length) return null
  return { echoCmd, echoPayload: p.subarray(2, statusAt), status, reason: p.subarray(statusAt + 1) }
}

/* ===== 值编解码 ===== */

function readBigU64(d, off) {
  if (typeof BigInt !== 'undefined') return Number(d.getBigUint64(off, true))
  const lo = d.getUint32(off, true)
  const hi = d.getUint32(off + 4, true)
  return hi * 0x100000000 + lo
}

function readBigI64(d, off) {
  if (typeof BigInt !== 'undefined') return Number(d.getBigInt64(off, true))
  const lo = d.getUint32(off, true)
  const hi = d.getInt32(off + 4, true)
  return hi * 0x100000000 + lo
}

/** 按类型从小端字节流解码值；调用方保证剩余字节足够 */
function decodeValue(d, off, type) {
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
      // 超过 2^53 会丢精度，监控场景可接受（与 TS 版同注）
      return readBigU64(d, off)
    case ValueType.Int64:
      return readBigI64(d, off)
    case ValueType.Bool:
    case ValueType.OnOff:
      // 直接用整字节当布尔：下位机写 0/1，不判 !=0 是为了与 TS 行为一致
      return d.getUint8(off)
    case ValueType.Color: {
      const r = d.getUint8(off)
      const g = d.getUint8(off + 1)
      const b = d.getUint8(off + 2)
      return (
        '#' +
        [r, g, b]
          .map(function (v) {
            return v.toString(16).padStart(2, '0')
          })
          .join('')
      )
    }
    default:
      return 0
  }
}

/* float32 往返比较用的复用缓冲：JS 数字都是 double，1.2f 进来就是
   1.2000000476837158，判"短写法是否无损"要靠重新编码回 float32 比对比特 */
const f32Buf = new DataView(new ArrayBuffer(4))
function f32Bits(v) {
  f32Buf.setFloat32(0, v, true)
  return f32Buf.getUint32(0, true)
}

/** 把 float32 值转成最短十进制串（1.2f 显示 "1.2" 而不是 1.2000000476837158） */
function formatFloat32(v) {
  if (!Number.isFinite(v)) return String(v)
  for (let p = 1; p <= 9; p++) {
    const s = v.toPrecision(p)
    if (f32Bits(Number(s)) === f32Bits(v)) return String(Number(s))
  }
  return String(v)
}

/** 按类型把值编码成真实宽度的小端字节（动作命令字段用，紧凑无填充） */
function encodeValue(type, value) {
  const size = valueSize(type)
  if (size === 0) return new Uint8Array(0)
  return encodeValue8(type, value).subarray(0, size)
}

/**
 * 把值编码成定长 8B 小端区域（SetTunable 的 value 段），与下位机 write_value 对应。
 * 类型不符直接抛错，绝不静默编 0——与 backend protocol.py 的 encode_value8 一一对应。
 */
function encodeValue8(type, value) {
  const buf = new ArrayBuffer(8)
  const d = new DataView(buf)
  if (type === ValueType.Color) {
    if (typeof value !== 'string') {
      throw new TypeError(`Color 字段需要 '#RRGGBB' 字符串，收到 ${typeof value}: ${value}`)
    }
    const m = /^#?([0-9a-f]{6})$/i.exec(value)
    if (!m) throw new Error(`Color 需要 6 位十六进制（如 '#ff0000'），收到 ${value}`)
    const rgb = parseInt(m[1], 16)
    d.setUint8(0, (rgb >> 16) & 0xff)
    d.setUint8(1, (rgb >> 8) & 0xff)
    d.setUint8(2, rgb & 0xff)
    return new Uint8Array(buf)
  }
  // 其余均为数值型（含 Bool/OnOff）：bool 仅开关字段放行
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
      if (typeof BigInt !== 'undefined') {
        d.setBigUint64(0, BigInt(Math.trunc(n)), true)
      } else {
        const lo = Math.trunc(n) >>> 0
        const hi = Math.floor(Math.trunc(n) / 0x100000000)
        d.setUint32(0, lo, true)
        d.setUint32(4, hi, true)
      }
      break
    case ValueType.Int64:
      if (typeof BigInt !== 'undefined') {
        d.setBigInt64(0, BigInt(Math.trunc(n)), true)
      } else {
        const lo = Math.trunc(n) >>> 0
        const hi = Math.floor(Math.trunc(n) / 0x100000000)
        d.setUint32(0, lo, true)
        d.setUint32(4, hi, true)
      }
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

function buildFrame(cmd, payload) {
  payload = payload || []
  const frame = new Uint8Array(payload.length + 3)
  frame[0] = FRAME_HEAD
  frame[1] = cmd
  let xor = cmd
  for (let i = 0; i < payload.length; i++) {
    frame[2 + i] = payload[i]
    xor ^= payload[i]
  }
  frame[frame.length - 1] = xor
  return frame
}

/** 目录请求帧：[len 1B][index...]；len=0 全量，非0=只补发这些下标。
    补发由后端算缺失项后发起（不清已收目录）；小程序作为观察者只用全量，
    接口留 indices 参数是为与 protocol.ts 的 requestDir 保持同形。 */
function requestDir(cmd, indices) {
  const idx = indices || []
  const payload = [idx.length & 0xff]
  for (let i = 0; i < idx.length; i++) payload.push(idx[i] & 0xff)
  return buildFrame(cmd, payload)
}

/** 请求调参目录（indices 省略/空=全量） */
function requestTunable(indices) { return requestDir(CmdGet.RequestTunable, indices) }
/** 请求监控目录（indices 语义同 requestTunable） */
function requestMonitor(indices) { return requestDir(CmdGet.RequestMonitor, indices) }
/** 请求动作命令目录（主控分批回 CMDDirectory；indices 语义同 requestTunable） */
function requestCommand(indices) { return requestDir(CmdGet.RequestCommand, indices) }
/**
 * 改订阅档位；index=0xff 对全部生效。
 * 只发上位机负责的两个位（SUB/FAST），下位机合并时保留 GETTER/KIND 等本机位。
 */
function subscribe(index, sub, fast) {
  const tag = (sub ? MONITOR_TAG_SUB : 0) | (fast ? MONITOR_TAG_FAST : 0)
  return buildFrame(CmdGet.Subscribe, [index, tag])
}

/** 心跳帧 A5 FF FF，80ms 一发；主控回同形帧 Pong */
function ping() { return buildFrame(CmdGet.Ping) }

/** 调参写入帧：[index][value 8B]，主控回 TunableEcho 确认真实生效值 */
function setTunable(index, type, value) {
  return buildFrame(CmdGet.SetTunable, [index].concat(Array.prototype.slice.call(encodeValue8(type, value))))
}

/**
 * 动作命令下发帧：Cmd 就是命令字本身（0x80~0x9F，不是 CmdGet 段），
 * payload 按 fields 顺序把各字段真实宽度的小端字节紧凑拼接（无填充）。
 */
function commandSend(item, values) {
  const payload = []
  item.fields.forEach(function (f, i) {
    const v = values[i] !== undefined ? values[i] : 0
    payload.push.apply(payload, Array.prototype.slice.call(encodeValue(f.type, v)))
  })
  return buildFrame(item.index, payload)
}

/* ===== 解帧（接收状态机，与下位机 rx 状态机同构） =====
   上行帧全是变长帧：
   目录帧 = 4B定长头 + 名字(nameLen) + 单位长1B + 单位(unitLen) + 值(宽度由type决定)
   监控帧 = 1B payloadlen + 2B time + ([index][值])×N
   调参回显 = 1B type + 1B index + 值(宽度由type决定) */

const RxState = Object.freeze({ WaitHead: 0, WaitCmd: 1, WaitData: 2, WaitXor: 3 })

/** 试算调参/监控目录帧的 payload 总长；单位长字节还没收到时返回 -1 */
function dirLen(p, len) {
  if (len < 4) return -1
  const nameLen = p[3]
  const unitLenAt = 4 + nameLen
  if (len < unitLenAt + 1) return -1
  return 5 + nameLen + p[unitLenAt] + valueSize(p[1])
}

/** 动作命令目录帧：payload 头 2B 就是"后续内容长度"（小端 uint16） */
function cmdDirLen(p, len) {
  if (len < 2) return -1
  return 2 + (p[0] | (p[1] << 8))
}

/* 解帧事件（与 TS 版 RxEvent 一一对应，消费方用 e.kind 判别） */
class DirectoryEvent {
  constructor(table, item) {
    this.kind = 'directory'
    this.table = table // 'tunable' | 'monitor'
    this.item = item // {index,type,tag,name,unit,value}
  }
}
class ValueEvent {
  constructor(index, value, tMs) {
    this.kind = 'value'
    this.index = index
    this.value = value
    this.tMs = tMs // 下位机毫秒时钟低16位，65.536s 回绕
  }
}
class EchoEvent {
  constructor(index, value) {
    this.kind = 'echo'
    this.index = index
    this.value = value
  }
}
class CmdDirectoryEvent {
  constructor(item) {
    this.kind = 'cmdDirectory'
    this.item = item // {index,tag,name,fields:[{type,name,min,max}]}
  }
}
class ResponseEvent {
  constructor(echoCmd, echoPayload, status, reason) {
    this.kind = 'response'
    this.echoCmd = echoCmd
    this.echoPayload = echoPayload
    this.status = status
    /** 尾字节：串模式=UTF-8 原因串；OK 模式=二进制附加信息（如目录项数）。
        用 respReasonText 转人话（串模式才解码，OK 只显示 OK） */
    this.reason = reason
  }
}
class PongEvent {
  constructor() {
    this.kind = 'pong'
  }
}

class FrameParser {
  constructor() {
    this.state = RxState.WaitHead
    this.cmd = 0
    /** -1 表示长度尚未能确定 */
    this.expect = -1
    this.len = 0
    this.xor = 0
    this.payload = new Uint8Array(MAX_PAYLOAD)
    /** 监控项 index→类型，收到监控目录后由外部登记（setMonitorType）。
        初值 0xFF 不是合法 ValueType，即"目录里没登记过"，解到这种 index 丢该帧剩余 */
    this.monitorType = new Uint8Array(256).fill(0xff)
    /** 动作命令字 → 回显 payload 应有字节数（命令目录字段累加），
        收到命令目录后由外部登记（setCmdEchoLen） */
    this.cmdEchoLen = new Uint8Array(256).fill(0xff)
    /** 链路诊断计数（设置页展示） */
    this.stats = {
      bytes: 0,
      frames: 0,
      badChecksum: 0,
      unknownCmd: 0,
      overflow: 0,
    }
  }

  clearStats() {
    this.stats.bytes = 0
    this.stats.frames = 0
    this.stats.badChecksum = 0
    this.stats.unknownCmd = 0
    this.stats.overflow = 0
  }

  setMonitorType(index, type) {
    this.monitorType[index] = type
  }

  resetMonitorTypes() {
    this.monitorType.fill(0xff)
  }

  setCmdEchoLen(index, len) {
    this.cmdEchoLen[index] = len
  }

  resetCmdEchoLens() {
    this.cmdEchoLen.fill(0xff)
  }

  /** Response 回显 payload 应有长度：命令目录登记值 → 内置命令定长 → 0（猜解） */
  expectedEchoLen(cmd) {
    const registered = this.cmdEchoLen[cmd]
    if (registered !== 0xff) return registered
    const builtin = BUILTIN_ECHO_LEN[cmd]
    return builtin === undefined ? 0 : builtin
  }

  /** 喂入任意长度的字节块（半包/粘包都在此消化），完整帧通过回调吐出 */
  feed(chunk, onEvent) {
    this.stats.bytes += chunk.length
    for (let ci = 0; ci < chunk.length; ci++) {
      const b = chunk[ci]
      switch (this.state) {
        case RxState.WaitHead:
          if (b === FRAME_HEAD) this.state = RxState.WaitCmd
          break

        case RxState.WaitCmd:
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
            b === CmdPost.CMDDirectory ||
            b === CmdPost.Response
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
            this.stats.overflow++
            this.state = RxState.WaitHead
            break
          }
          this.payload[this.len] = b
          this.len++
          this.xor ^= b

          if (this.expect < 0) {
            if (this.cmd === CmdPost.MonitorValue) {
              // 监控值帧自带 1B 长度前缀；payloadlen==0 非法（流错位），回找帧头不猜长度
              if (this.payload[0] === 0) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 1 + this.payload[0]
              }
            } else if (this.cmd === CmdPost.TunableEcho) {
              // [type][index][value]：type 前缀自带宽度，解帧不查表
              const sz = valueSize(this.payload[0])
              if (sz === 0) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 2 + sz
              }
            } else if (this.cmd === CmdPost.CMDDirectory) {
              const total = cmdDirLen(this.payload, this.len)
              if (total >= 0) this.expect = total
            } else if (this.cmd === CmdPost.Response) {
              // payloadlen 最小 2（原话cmd+status），0/1 只可能是流错位
              if (this.payload[0] < 2) {
                this.state = RxState.WaitHead
              } else {
                this.expect = 1 + this.payload[0]
              }
            } else {
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

  dispatch(onEvent) {
    const p = this.payload
    switch (this.cmd) {
      case CmdPost.TunableDirectory:
      case CmdPost.MonitorDirectory: {
        // [index][type][tag][名字长度][名字UTF-8][单位长度][单位UTF-8][值]
        if (this.len < 4) return
        const index = p[0]
        const type = p[1]
        const tag = p[2]
        const nameLen = p[3]
        const unitLenAt = 4 + nameLen
        if (this.len < unitLenAt + 1) return
        const unitLen = p[unitLenAt]
        const valueAt = 5 + nameLen + unitLen
        if (this.len < valueAt) return
        const name = decodeUtf8(p, 4, unitLenAt)
        const unit = decodeUtf8(p, unitLenAt + 1, valueAt)
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + valueAt), 0, type)
        onEvent(
          new DirectoryEvent(this.cmd === CmdPost.TunableDirectory ? 'tunable' : 'monitor', {
            index,
            type,
            tag,
            name,
            unit,
            value,
          }),
        )
        break
      }
      case CmdPost.MonitorValue: {
        // [payloadlen][time 2B]([index][值])×N：没有项数字段，类型查目录登记表
        if (this.len < 3) return
        const tMs = p[1] | (p[2] << 8)
        let off = 3
        while (off < this.len) {
          const type = this.monitorType[p[off]]
          const sz = valueSize(type)
          if (sz === 0) break // 目录里没登记过这项：丢剩余
          const value = decodeValue(new DataView(p.buffer, p.byteOffset + off + 1), 0, type)
          onEvent(new ValueEvent(p[off], value, tMs))
          off += 1 + sz
        }
        break
      }
      case CmdPost.TunableEcho: {
        // [type][index][实际生效值]
        if (this.len < 2) return
        const type = p[0]
        if (valueSize(type) === 0) return
        const index = p[1]
        const value = decodeValue(new DataView(p.buffer, p.byteOffset + 2), 0, type)
        onEvent(new EchoEvent(index, value))
        break
      }
      case CmdPost.CMDDirectory: {
        // [内容长度2B][命令字][tag][名长][名][参数量]([类型][字段名长][字段名])×N[bit7=1时:(min,max)×N]
        if (this.len < 5) return
        let off = 2
        const index = p[off]
        const tag = p[off + 1]
        const nameLen = p[off + 2]
        const nameAt = off + 3
        off = nameAt + nameLen
        if (this.len < off + 1) return
        const name = decodeUtf8(p, nameAt, off)
        const fieldNum = p[off]
        off++
        const fields = []
        for (let i = 0; i < fieldNum; i++) {
          if (off + 2 > this.len) return
          const type = p[off]
          const flen = p[off + 1]
          off += 2
          if (off + flen > this.len) return
          fields.push({ type, name: decodeUtf8(p, off, off + flen) })
          off += flen
        }
        // tag bit7=1：每字段一对 min/max，与该字段同类型、同宽度
        if (tag & CMD_TAG_RANGE) {
          for (let i = 0; i < fields.length; i++) {
            const f = fields[i]
            const sz = valueSize(f.type)
            if (sz === 0 || off + 2 * sz > this.len) break
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
        // 尾字节：串模式=UTF-8 原因串；OK 模式=二进制 info（长度隐式，帧长即长度，
        // 目录请求时是 1B 表内总项数）。
        // status 位置依赖回显长度：已登记命令字/内置命令可推；推不出按 0 猜解，
        // 一致性校验不过就静默丢（小程序只是观察者，补发与重试在后端做）
        if (this.len < 3) return
        const echoCmd = p[1]
        // 必须传本帧长度的切片：decodeResponse 内部用 p.length 做一致性校验，
        // 传满缓冲（445B）会永远校验失败。backend protocol.py 传 payload[:len]
        // 是正确行为；frontend protocol.ts:844 传满缓冲是同址 bug（Response 帧
        // 在网页端从未解析成功过），本端按后端行为实现，不照搬。
        const decoded = decodeResponse(p.subarray(0, this.len), this.expectedEchoLen(echoCmd))
        if (decoded === null) return
        onEvent(
          new ResponseEvent(decoded.echoCmd, decoded.echoPayload, decoded.status, decoded.reason),
        )
        break
      }
      case CmdPost.Pong:
        onEvent(new PongEvent())
        break
      default:
        break
    }
  }
}

module.exports = {
  FRAME_HEAD,
  CmdPost,
  CmdGet,
  ValueType,
  MonitorKind,
  CmdSpec,
  MONITOR_TAG_SUB,
  MONITOR_TAG_FAST,
  MONITOR_TAG_GETTER,
  MONITOR_TAG_KIND_MASK,
  CMD_TAG_RANGE,
  CMD_TAG_ARGC_MASK,
  CMD_TAG_ARGC_SHIFT,
  CMD_TAG_SPEC_MASK,
  CMD_ACTION_MIN,
  CMD_ACTION_MAX,
  MAX_PAYLOAD,
  RESP_OK,
  RESP_CLASS_MASK,
  RESP_MODE_MASK,
  RESP_PAYLOAD_MASK,
  RESP_RETRY_CHECK,
  RESP_RETRY_UNKNOWN_CMD,
  RESP_REJECT_INDEX_RANGE,
  RESP_REJECT_TYPE_UNWRITABLE,
  RESP_REJECT_BUSY,
  tagSubscribed,
  tagFast,
  tagKind,
  cmdSpec,
  cmdArgc,
  cmdHasRange,
  valueSize,
  typeName,
  decodeUtf8,
  respIsOk,
  respIsRetry,
  respIsReject,
  respStrLen,
  respReasonText,
  decodeResponse,
  decodeValue,
  formatFloat32,
  encodeValue,
  encodeValue8,
  buildFrame,
  requestTunable,
  requestMonitor,
  requestCommand,
  subscribe,
  ping,
  setTunable,
  commandSend,
  FrameParser,
  DirectoryEvent,
  ValueEvent,
  EchoEvent,
  CmdDirectoryEvent,
  ResponseEvent,
  PongEvent,
}
