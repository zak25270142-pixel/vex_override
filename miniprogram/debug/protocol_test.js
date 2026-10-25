/**
 * 协议层自测：node debug/protocol_test.js（不需要微信环境，纯 CommonJS）
 *
 * 为什么值得单测：本文件是 protocol.ts 的小程序移植版，帧长试算/状态机/
 * 值编解码任何一处偏移都会表现为"值对不上"而不是报错——这正是协议注释里
 * 反复警告的静默错乱。改 utils/protocol.js 后必须重跑本测试。
 *
 * 对齐 backend/debug/test_protocol.py 的角色：那边验 Python 端，这边验 JS 端。
 */
'use strict'

const proto = require('../utils/protocol.js')

let passed = 0
let failed = 0
const failures = []

function check(name, cond, detail) {
  if (cond) {
    passed++
  } else {
    failed++
    failures.push(name + (detail ? ' —— ' + detail : ''))
  }
}

function eq(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  check(name, a === e, '期望 ' + e + ' 实际 ' + a)
}

function utf8(s) {
  return Array.from(Buffer.from(s, 'utf8'))
}

/** 收集一帧事件 */
function feedFrames(parser, chunks) {
  const events = []
  for (const c of chunks) {
    parser.feed(c instanceof Uint8Array ? c : new Uint8Array(c), (e) => events.push(e))
  }
  return events
}

/* ================= 1. 组帧（下行） ================= */

{
  const f = proto.ping()
  eq('ping 帧 = A5 FF FF', Array.from(f), [0xa5, 0xff, 0xff])

  // 目录请求新格式（2026-10-25 协议变更）：[len 1B][index...]，len=0 全量
  const rt = proto.requestTunable()
  eq('requestTunable 全量帧', Array.from(rt), [0xa5, 0x00, 0x00, 0x00])
  const rm = proto.requestMonitor([3, 7])
  eq('requestMonitor 补发帧', Array.from(rm), [0xa5, 0x01, 0x02, 0x03, 0x07, 0x01 ^ 0x02 ^ 0x03 ^ 0x07])
  const rc = proto.requestCommand()
  eq('requestCommand 全量帧', Array.from(rc), [0xa5, 0x04, 0x00, 0x04])

  const s = proto.subscribe(5, true, true)
  eq('subscribe 帧', Array.from(s), [0xa5, 0x02, 5, 0xc0, 0x02 ^ 5 ^ 0xc0])

  const st = proto.setTunable(3, proto.ValueType.Float, 1.5)
  check('setTunable 帧长 12', st.length === 12, 'len=' + st.length)
  let stXor = st[1]
  for (let i = 2; i < st.length - 1; i++) stXor ^= st[i]
  check('setTunable XOR 对', stXor === st[st.length - 1])
  const dv = new DataView(st.buffer, st.byteOffset)
  check('setTunable float 值', dv.getFloat32(3, true) === 1.5)

  const cmd = { index: 0x84, tag: (2 << 4) | 0, name: '设置电机电压', fields: [
    { type: proto.ValueType.UInt8, name: '编号' },
    { type: proto.ValueType.Float, name: '电压' },
  ] }
  const cf = proto.commandSend(cmd, [2, 3.5])
  check('commandSend 帧长 8', cf.length === 8, 'len=' + cf.length)
  eq('commandSend payload', Array.from(cf.subarray(2, cf.length - 1)),
    [2].concat(Array.from(new Uint8Array(new Float32Array([3.5]).buffer))))
}

/* ================= 2. 目录帧（上行） ================= */

{
  const name = utf8('左0转速')
  const unit = utf8('rpm')
  const payload = [7, proto.ValueType.Float, 0x80].concat(name.length, name, unit.length, unit)
  // 值：float 3B? 不，float 是 4B
  const valBuf = new Uint8Array(new Float32Array([123.5]).buffer)
  const full = payload.concat(Array.from(valBuf))
  const frame = proto.buildFrame(proto.CmdPost.MonitorDirectory, full)

  const p = new proto.FrameParser()
  const ev = feedFrames(p, [frame])
  check('目录帧 1 事件', ev.length === 1, 'events=' + ev.length)
  check('目录帧 kind', ev[0] && ev[0].kind === 'directory')
  eq('目录帧 table', ev[0] && ev[0].table, 'monitor')
  eq('目录帧 name', ev[0].item && ev[0].item.name, '左0转速')
  eq('目录帧 unit', ev[0].item && ev[0].item.unit, 'rpm')
  eq('目录帧 index/type/tag', ev[0].item && [ev[0].item.index, ev[0].item.type, ev[0].item.tag], [7, 8, 0x80])
  check('目录帧 value', ev[0].item && ev[0].item.value === 123.5, 'value=' + (ev[0].item && ev[0].item.value))
}

/* ================= 3. 监控值帧 ================= */

{
  const p = new proto.FrameParser()
  p.setMonitorType(7, proto.ValueType.Float)
  p.setMonitorType(8, proto.ValueType.Int16)

  const body = [0x34, 0x56] // time 低16位
    .concat([7], Array.from(new Uint8Array(new Float32Array([-1.25]).buffer)))
    .concat([8, 0x10, 0x27]) // int16 = 0x2710 小端 → 10000
  const frame = proto.buildFrame(proto.CmdPost.MonitorValue, [body.length].concat(body))

  const ev = feedFrames(p, [frame])
  check('监控帧 2 事件', ev.length === 2, 'events=' + ev.length)
  check('监控帧 value kind', ev[0] && ev[0].kind === 'value')
  eq('监控帧 tMs', ev[0] && ev[0].tMs, 0x5634)
  check('监控帧 float 值', ev[0] && ev[0].value === -1.25)
  eq('监控帧 index', ev[0] && ev[0].index, 7)
  check('监控帧 int16 值', ev[1] && ev[1].value === 10000)

  // 未登记 index：丢剩余，已解的照常发
  const bad = [2].concat([99, 0x01]) // index 99 没登记
  const ev2 = feedFrames(p, [proto.buildFrame(proto.CmdPost.MonitorValue, bad)])
  check('未登记 index 无事件', ev2.length === 0, 'events=' + ev2.length)
}

/* ================= 4. 调参回显帧 ================= */

{
  const p = new proto.FrameParser()
  const frame = proto.buildFrame(proto.CmdPost.TunableEcho,
    [proto.ValueType.Float, 3].concat(Array.from(new Uint8Array(new Float32Array([2.5]).buffer))))
  const ev = feedFrames(p, [frame])
  check('回显帧 1 事件', ev.length === 1 && ev[0].kind === 'echo')
  eq('回显帧 index', ev[0].index, 3)
  check('回显帧 value', ev[0].value === 2.5)
}

/* ================= 5. 命令目录帧（带 min/max） ================= */

{
  const name = utf8('设置电机电压')
  const fields = [
    { type: proto.ValueType.UInt8, fname: '编号' },
    { type: proto.ValueType.Float, fname: '电压' },
  ]
  const tag = proto.CMD_TAG_RANGE | (2 << 4) | proto.CmdSpec.Normal
  let content = [0x84, tag, name.length].concat(name, fields.length)
  for (const f of fields) {
    const fn = utf8(f.fname)
    content = content.concat([f.type, fn.length], fn)
  }
  // min/max：uint8 {0,7}、float {0,12}
  content = content.concat([0, 7])
  const fmin = new Uint8Array(new Float32Array([0]).buffer)
  const fmax = new Uint8Array(new Float32Array([12]).buffer)
  content = content.concat(Array.from(fmin), Array.from(fmax))
  const lenBytes = [content.length & 0xff, (content.length >> 8) & 0xff]
  const frame = proto.buildFrame(proto.CmdPost.CMDDirectory, lenBytes.concat(content))

  const p = new proto.FrameParser()
  const ev = feedFrames(p, [frame])
  check('命令目录 1 事件', ev.length === 1 && ev[0].kind === 'cmdDirectory', 'events=' + ev.length)
  eq('命令目录 name', ev[0].item.name, '设置电机电压')
  eq('命令目录 index', ev[0].item.index, 0x84)
  check('命令目录 argc', proto.cmdArgc(ev[0].item.tag) === 2)
  check('命令目录 spec', proto.cmdSpec(ev[0].item.tag) === proto.CmdSpec.Normal)
  eq('命令目录字段数', ev[0].item.fields.length, 2)
  eq('命令目录字段名', ev[0].item.fields.map((f) => f.name), ['编号', '电压'])
  eq('命令目录 min/max', [ev[0].item.fields[0].min, ev[0].item.fields[0].max, ev[0].item.fields[1].min, ev[0].item.fields[1].max], [0, 7, 0, 12])
}

/* ================= 6. Response 帧 ================= */

{
  const p = new proto.FrameParser()
  // 对 SetTunable（echoLen=9：[index][8B 定长值]）的 OK 响应
  const f32 = new Uint8Array(new Float32Array([1]).buffer)
  const echoPayload = [3].concat(Array.from(f32), [0, 0, 0, 0])
  const body = [proto.CmdGet.SetTunable].concat(echoPayload, [0x00])
  const frame = proto.buildFrame(proto.CmdPost.Response, [body.length].concat(body))
  const ev = feedFrames(p, [frame])
  check('响应帧 1 事件', ev.length === 1 && ev[0].kind === 'response', 'events=' + ev.length)
  eq('响应帧 echoCmd', ev[0] && ev[0].echoCmd, proto.CmdGet.SetTunable)
  check('响应帧 OK', ev[0] && proto.respIsOk(ev[0].status))
  eq('响应帧 echoPayload 长', ev[0] && ev[0].echoPayload.length, 9)

  // 忙拒绝 + 内定err（status = 0xC0|3）。动作命令的回显长度来自命令目录登记——
  // 未登记时按 0 猜解会定位错 status，这正是 frontend 收到命令目录即登记的原因
  p.setCmdEchoLen(0x84, 2)
  const body2 = [0x84, 0x02, 0x05, 0xc3]
  const frame2 = proto.buildFrame(proto.CmdPost.Response, [body2.length].concat(body2))
  const ev2 = feedFrames(p, [frame2])
  check('拒绝帧 1 事件', ev2.length === 1, 'events=' + ev2.length)
  check('拒绝帧 isReject', ev2[0] && proto.respIsReject(ev2[0].status))
  eq('拒绝帧人话', ev2[0] && proto.respReasonText(ev2[0].status, ''), '忙')

  // 串模式：需补发 + 原因串（status 低6位 = 串字节数；8 个汉字 = 24B → 0x18）
  const reason = utf8('校验失败，请重发')
  const body3 = [proto.CmdGet.Subscribe, 0x01, 0xc0, 0x18].concat(reason)
  const frame3 = proto.buildFrame(proto.CmdPost.Response, [body3.length].concat(body3))
  const ev3 = feedFrames(p, [frame3])
  check('串模式 1 事件', ev3.length === 1, 'events=' + ev3.length)
  eq('串模式尾字节长', ev3[0] && ev3[0].reason.length, 24)
  eq('串模式转人话', ev3[0] && proto.respReasonText(ev3[0].status, ev3[0].reason), '校验失败，请重发')
  check('串模式 isRetry', ev3[0] && proto.respIsRetry(ev3[0].status))
}

/* ================= 6b. OK 尾字节（二进制附加信息，2026-10-25 协议变更） ================= */

{
  // 目录请求的 OK 响应：尾字节 = 表内总项数（长度隐式，帧长剩余即 info）
  const p = new proto.FrameParser()
  // echo = RequestMonitor 的全量请求 payload [len=0]（BUILTIN_ECHO_LEN=1）
  const body = [proto.CmdGet.RequestMonitor, 0x00, 0x00, 0x0c] // [echoCmd][echo 1B][status=OK][info=12]
  const frame = proto.buildFrame(proto.CmdPost.Response, [body.length].concat(body))
  const ev = feedFrames(p, [frame])
  check('OK+info 1 事件', ev.length === 1, 'events=' + ev.length)
  check('OK+info isOk', ev[0] && proto.respIsOk(ev[0].status))
  eq('OK+info 回显长', ev[0] && ev[0].echoPayload.length, 1)
  eq('OK+info 项数', ev[0] && Array.from(ev[0].reason), [12])
  eq('OK+info 人话', ev[0] && proto.respReasonText(ev[0].status, ev[0].reason), 'OK')

  // 普通 OK（无尾字节）：帧长剩余为 0
  const body2 = [proto.CmdGet.Subscribe, 0x01, 0xc0, 0x00]
  const frame2 = proto.buildFrame(proto.CmdPost.Response, [body2.length].concat(body2))
  const ev2 = feedFrames(p, [frame2])
  check('裸 OK 1 事件', ev2.length === 1)
  eq('裸 OK 尾字节空', ev2[0] && ev2[0].reason.length, 0)

  // respStrLen：OK 为 -1（隐式长度哨兵），其余不变
  eq('respStrLen OK', proto.respStrLen(proto.RESP_OK), -1)
  eq('respStrLen 串模式', proto.respStrLen(0x18), 24)
  eq('respStrLen 内定err', proto.respStrLen(0xc3), 0)
}

/* ================= 7. Pong / 坏帧 / 半包粘包 ================= */

{
  const p = new proto.FrameParser()
  const ev = feedFrames(p, [proto.buildFrame(proto.CmdPost.Pong, [])])
  check('Pong 1 事件', ev.length === 1 && ev[0].kind === 'pong')

  // 校验错：破坏值字节（长度已定 → 走到等校验 → XOR 不符）
  // 注意不能破坏 type 字节：那会让帧在定长阶段就被判非法而重同步，走不到校验
  const good = proto.buildFrame(proto.CmdPost.TunableEcho,
    [proto.ValueType.UInt8, 1, 42])
  const bad = good.slice()
  bad[4] = bad[4] ^ 0xff
  const ev2 = feedFrames(p, [bad])
  check('坏校验无事件', ev2.length === 0)
  check('badChecksum+1', p.stats.badChecksum === 1, 'count=' + p.stats.badChecksum)

  // 未知命令字
  const ev3 = feedFrames(p, [new Uint8Array([0xa5, 0x77, 0x00, 0x77])])
  check('未知命令字无事件', ev3.length === 0)
  check('unknownCmd+1', p.stats.unknownCmd === 1, 'count=' + p.stats.unknownCmd)

  // 半包 + 粘包：一帧拆两段 + 两帧合一
  const f1 = proto.buildFrame(proto.CmdPost.TunableEcho, [proto.ValueType.UInt8, 1, 42])
  const f2 = proto.buildFrame(proto.CmdPost.TunableEcho, [proto.ValueType.UInt8, 2, 43])
  const half = Math.floor(f1.length / 2)
  const ev4 = feedFrames(p, [f1.subarray(0, half), f1.subarray(half), new Uint8Array([0x00, 0xff]), f2])
  check('半包+粘包 2 事件', ev4.length === 2, 'events=' + ev4.length)
  eq('半包+粘包 index', ev4.map((e) => e.index), [1, 2])

  // 乱码前缀后重同步（前缀不含 A5：单个 0xA5 会吃掉真帧的帧头，那是预期行为）
  const ev5 = feedFrames(p, [new Uint8Array([0x12, 0x34]), f1])
  check('乱码后重同步', ev5.length === 1, 'events=' + ev5.length)
}

/* ================= 8. 值编解码 / formatFloat32 ================= */

{
  const cases = [
    [proto.ValueType.UInt8, 200],
    [proto.ValueType.Int8, -100],
    [proto.ValueType.UInt16, 60000],
    [proto.ValueType.Int16, -30000],
    [proto.ValueType.UInt32, 4000000000],
    [proto.ValueType.Int32, -2000000000],
    [proto.ValueType.Float, 3.14159],
    [proto.ValueType.Double, -2.718281828],
    [proto.ValueType.Bool, 1],
    [proto.ValueType.OnOff, 0],
    [proto.ValueType.Color, '#ff8800'],
  ]
  for (const [t, v] of cases) {
    const enc = proto.encodeValue8(t, v)
    const dec = proto.decodeValue(new DataView(enc.buffer, enc.byteOffset), 0, t)
    // float32 无法精确表示多数小数，与 fround 后的值比（协议本来就是 32 位）
    const expect = t === proto.ValueType.Float ? Math.fround(v) : v
    check('往返 ' + t + ' ' + v, dec === expect, '得到 ' + dec)
  }
  check('encodeValue 紧凑宽度', proto.encodeValue(proto.ValueType.UInt8, 5).length === 1)
  check('encodeValue float 4B', proto.encodeValue(proto.ValueType.Float, 5).length === 4)

  // 8B 定长（SetTunable 用）
  const e8 = proto.encodeValue8(proto.ValueType.UInt8, 5)
  check('encodeValue8 定长 8', e8.length === 8)
  check('encodeValue8 高位补零', Array.from(e8).every((b, i) => (i === 0 ? b === 5 : b === 0)))

  eq('formatFloat32 0.3', proto.formatFloat32(0.3), '0.3')
  eq('formatFloat32 1.2', proto.formatFloat32(1.2), '1.2')
  eq('formatFloat32 0', proto.formatFloat32(0), '0')
  eq('formatFloat32 -12.5', proto.formatFloat32(-12.5), '-12.5')
  check('formatFloat32 无损', proto.f32Bits === undefined || (() => {
    const v = 0.1
    return proto.formatFloat32(v) === '0.1'
  })())

  // 类型纪律：数值字段收字符串必须抛错，不静默编 0
  let threw = false
  try {
    proto.encodeValue8(proto.ValueType.Float, '6.0')
  } catch (e) {
    threw = true
  }
  check('类型不符抛错', threw)
}

/* ================= 9. valueSize 与 TS/Python 一致 ================= */

{
  eq('valueSize uint8', proto.valueSize(proto.ValueType.UInt8), 1)
  eq('valueSize float', proto.valueSize(proto.ValueType.Float), 4)
  eq('valueSize double', proto.valueSize(proto.ValueType.Double), 8)
  eq('valueSize color', proto.valueSize(proto.ValueType.Color), 3)
  eq('valueSize str', proto.valueSize(proto.ValueType.Str), 0)
  eq('命令字段', [proto.CMD_ACTION_MIN, proto.CMD_ACTION_MAX], [0x80, 0x9f])
  eq('监控位域', [proto.MONITOR_TAG_SUB, proto.MONITOR_TAG_FAST], [0x80, 0x40])
}

/* ================= 汇总 ================= */

console.log('---- 结果 ----')
console.log('通过 ' + passed + ' / ' + (passed + failed))
if (failures.length > 0) {
  console.log('失败 ' + failed + ' 项：')
  failures.forEach((f) => console.log('  ✗ ' + f))
  process.exit(1)
} else {
  console.log('全部通过')
}
