/**
 * 数据中枢（单例）：协议解析 → 目录表/实时值/订阅意图 → 20fps 抽帧发布
 *
 * 设计照搬 frontend/src/stores/globle.ts 的核心思路，按小程序环境落地：
 *   - WS onMessage 直喂 FrameParser，事件只写非响应式 JS 对象（100Hz 也不触发渲染）
 *   - 单一 50ms 定时器（20fps）驱动：UI 订阅者拉取视图、心跳超时巡查、toast 过期
 *   - 订阅意图本端只做乐观更新（V5 合并 SUB/FAST，无 tag 回显），与前端同策略
 *
 * 多客户端隔离在后端：订阅意图按 conn_id 记，小程序与 PC 浏览器可同时在线。
 */

const cfg = require('../config/index.js')
const proto = require('../utils/protocol.js')
const { WsClient, ConnState } = require('../services/wsClient.js')

const store = {
  /* ---------- 连接 ---------- */
  connState: ConnState.IDLE,
  connMsg: '',
  host: cfg.DEFAULT_HOST,
  port: cfg.DEFAULT_PORT,
  autoConnect: false,
  lastPongAt: 0,
  backendStatus: '', // 后端 op=status 的 message 原文

  /* ---------- 目录表 ---------- */
  tunableDir: [], // [{index,type,tag,name,unit,value}]
  monitorDir: [], // 同上；tag 含 SUB/FAST/GETTER/KIND 位域
  cmdDir: [], // [{index,tag,name,fields:[{type,name,min,max}]}]

  /* ---------- 实时值（非响应式，抽帧时才进视图） ---------- */
  latest: {}, // index → value（原始类型）
  latestAt: {}, // index → Date.now() 到达时刻

  /* ---------- 调参待确认 ---------- */
  pendingTun: {}, // index → {value, since}

  /* ---------- toast ---------- */
  toasts: [],

  /* ---------- 内部 ---------- */
  parser: null,
  client: null,
  subs: new Set(),
  tickTimer: null,
  pingTimer: null,
  inited: false,
}

/* ================= 初始化与生命周期 ================= */

store.init = function () {
  if (store.inited) return
  store.inited = true

  // 恢复持久化设置
  try {
    const s = wx.getStorageSync(cfg.STORAGE_KEY)
    if (s) {
      if (s.host) store.host = s.host
      if (s.port) store.port = s.port
      store.autoConnect = !!s.autoConnect
    }
  } catch (e) {}

  store.parser = new proto.FrameParser()

  store.client = new WsClient({
    getUrl: () => 'ws://' + store.host + ':' + store.port + '/ws',
    getHttpBase: () => 'http://' + store.host + ':' + store.port,
    onStateChange: (state, msg) => {
      store.connState = state
      store.connMsg = msg
      if (state === ConnState.CONNECTED) {
        store.lastPongAt = Date.now()
        store._saveSettings({ lastOk: true })
        // 连上即拉三张表（后端有目录缓存，重复请求不打车）
        store._send(proto.requestTunable())
        store._send(proto.requestMonitor())
        store._send(proto.requestCommand())
      }
    },
    onStatusJson: (obj) => {
      store.backendStatus = obj.message || ''
    },
    onAlertJson: (obj) => {
      // AlertLevel（2026-10-25 后端新增枚举，str 序列化）：
      //   reject=固件明确拒绝（重发无意义）/ retry=帧没成但已自动重发 /
      //   giveup=重试预算耗尽或无法结算，命令状态不明——按 error 级红 toast
      store._toast(obj.level === 'giveup' ? 'error' : 'warn', obj.text || '后端提示')
    },
    onBinary: (bytes) => {
      store.parser.feed(bytes, store._onEvent)
    },
  })

  // 20fps 抽帧总 tick：驱动 UI 订阅者 + 心跳巡查 + toast 过期
  store.tickTimer = setInterval(store._tick, cfg.TICK_MS)
  // 80ms 心跳（与 frontend 一致；后端自己也发，Ping 幂等无害）
  store.pingTimer = setInterval(() => {
    if (store.client.isOpen()) {
      store._send(proto.ping())
    }
  }, cfg.PING_MS)

  // 上次连成功过才自动连，避免每次开小程序都朝不存在的地址撞
  try {
    const s = wx.getStorageSync(cfg.STORAGE_KEY)
    if (s && s.lastOk) store.connect()
  } catch (e) {}
}

store.onAppShow = function () {
  // 切后台 WS 会被系统中断；回前台按意愿重连
  if (store.inited && store.autoConnect && !store.client.isOpen()) {
    store.connect()
  }
}

/* ================= 连接操作 ================= */

store.connect = function () {
  store._saveSettings()
  store.client.connect()
}

store.disconnect = function () {
  store._saveSettings({ lastOk: false })
  store.client.disconnect()
  store._resetData()
}

store.saveSettings = function (host, port, autoConnect) {
  store.host = host
  store.port = port
  store.autoConnect = autoConnect
  store._saveSettings()
}

/** 换了地址等情况重连：旧 socket 关干净再开新的（小程序同时只允许一个 socket） */
store.reconnect = function () {
  const c = store.client
  if (c.isOpen() || c.wsConnecting) {
    c.disconnect()
    setTimeout(function () {
      store.connect()
    }, 400)
  } else {
    store.connect()
  }
}

store._saveSettings = function (extra) {
  const s = {
    host: store.host,
    port: store.port,
    autoConnect: store.autoConnect,
    lastOk: false,
  }
  try {
    const old = wx.getStorageSync(cfg.STORAGE_KEY)
    if (old && old.lastOk) s.lastOk = true
  } catch (e) {}
  if (extra) Object.assign(s, extra)
  try {
    wx.setStorageSync(cfg.STORAGE_KEY, s)
  } catch (e) {}
}

store._resetData = function () {
  store.tunableDir = []
  store.monitorDir = []
  store.cmdDir = []
  store.latest = {}
  store.latestAt = {}
  store.pendingTun = {}
  store.parser.resetMonitorTypes()
  store.parser.resetCmdEchoLens()
}

store._send = function (bytes) {
  try {
    store.client.send(bytes)
  } catch (e) {
    store._toast('warn', '未连接，发送失败')
  }
}

/* ================= 协议事件 → 数据 ================= */

store._onEvent = function (e) {
  switch (e.kind) {
    case 'directory': {
      const list = e.table === 'tunable' ? store.tunableDir : store.monitorDir
      const i = list.findIndex((it) => it.index === e.item.index)
      if (i >= 0) list[i] = e.item
      else list.push(e.item)
      if (e.table === 'monitor') {
        store.parser.setMonitorType(e.item.index, e.item.type)
      }
      break
    }
    case 'value': {
      store.latest[e.index] = e.value
      store.latestAt[e.index] = Date.now()
      break
    }
    case 'echo': {
      // 调参回显：清待确认 + 用真实生效值更新目录项
      delete store.pendingTun[e.index]
      const it = store.tunableDir.find((x) => x.index === e.index)
      if (it) it.value = e.value
      break
    }
    case 'cmdDirectory': {
      const i = store.cmdDir.findIndex((it) => it.index === e.item.index)
      if (i >= 0) store.cmdDir[i] = e.item
      else store.cmdDir.push(e.item)
      // 登记回显长度（Response 帧定位 status 用）：各字段真实宽度累加
      let echoLen = 0
      for (let k = 0; k < e.item.fields.length; k++) {
        echoLen += proto.valueSize(e.item.fields[k].type)
      }
      store.parser.setCmdEchoLen(e.item.index, echoLen)
      break
    }
    case 'response': {
      if (!proto.respIsOk(e.status)) {
        store._toast('warn', proto.respReasonText(e.status, e.reason))
        // 调参被拒：清待确认，让行内"等待确认…"结束
        if (e.echoCmd === proto.CmdGet.SetTunable && e.echoPayload.length >= 1) {
          delete store.pendingTun[e.echoPayload[0]]
        }
      }
      break
    }
    case 'pong': {
      store.lastPongAt = Date.now()
      break
    }
    default:
      break
  }
}

/* ================= 20fps 抽帧 ================= */

store._tick = function () {
  const now = Date.now()

  // 心跳巡查：协议 300ms 停推线 + 200ms 网络余量（后端另有自己的 300ms 巡视）
  if (store.connState === ConnState.CONNECTED && now - store.lastPongAt > cfg.PONG_TIMEOUT_MS) {
    store.connState = ConnState.ERROR
    store.connMsg = '心跳超时（' + Math.round((now - store.lastPongAt) / 100) / 10 + 's 无 Pong）'
  }

  // 调参回显超时
  const pendKeys = Object.keys(store.pendingTun)
  for (let i = 0; i < pendKeys.length; i++) {
    const k = pendKeys[i]
    const p = store.pendingTun[k]
    if (now - p.since > cfg.TUN_TIMEOUT_MS) {
      delete store.pendingTun[k]
      store._toast('warn', '调参无回显（主控未确认，已超时）')
    }
  }

  // toast 过期
  if (store.toasts.length > 0) {
    store.toasts = store.toasts.filter((t) => now - t.at < cfg.TOAST_MS)
  }

  // 通知 UI 订阅者（页面自行拉取视图）
  store.subs.forEach((fn) => {
    try {
      fn()
    } catch (e) {}
  })
}

store.subscribe = function (fn) {
  store.subs.add(fn)
  return function () {
    store.subs.delete(fn)
  }
}

/* ================= 视图构建（页面 setData 用） ================= */

function fmtValue(v, type) {
  if (type === proto.ValueType.Float || type === proto.ValueType.Double) {
    return proto.formatFloat32(v)
  }
  return String(v)
}

function tagClass(tag) {
  if (!proto.tagSubscribed(tag)) return 'off'
  return proto.tagFast(tag) ? 'fast' : 'slow'
}

function tagText(tag) {
  if (!proto.tagSubscribed(tag)) return '未订'
  return proto.tagFast(tag) ? '高速' : '低速'
}

/** 监控表视图行（可按关键字过滤） */
store.getMonitorView = function (keyword) {
  const kw = (keyword || '').trim()
  const now = Date.now()
  const rows = store.monitorDir
    .filter((it) => {
      if (!kw) return true
      return it.name.indexOf(kw) >= 0 || String(it.index) === kw
    })
    .map((it) => {
      const pend = store.pendingTun[it.index]
      let text = '--'
      if (pend) text = '等待确认…'
      else if (store.latest[it.index] !== undefined) text = fmtValue(store.latest[it.index], it.type)
      return {
        index: it.index,
        name: it.name,
        unit: it.unit || '',
        text,
        tagClass: tagClass(it.tag),
        tagText: tagText(it.tag),
        stale: !pend && store.latestAt[it.index] !== undefined && now - store.latestAt[it.index] > 1500,
        _sub: proto.tagSubscribed(it.tag) ? 0 : 1,
      }
    })
  // 已订阅的排前，与 frontend sortedMonitor 一致（_sub 预计算，避免排序里反复查表）
  rows.sort((a, b) => a._sub - b._sub || a.index - b.index)
  for (let i = 0; i < rows.length; i++) delete rows[i]._sub
  return rows
}

/** 调参表视图行 */
store.getTunableView = function (keyword) {
  const kw = (keyword || '').trim()
  return store.tunableDir
    .filter((it) => {
      if (!kw) return true
      return it.name.indexOf(kw) >= 0 || String(it.index) === kw
    })
    .map((it) => {
      const pend = store.pendingTun[it.index]
      let text
      if (pend) text = '等待确认…'
      else if (proto.valueSize(it.type) === 0) text = '—' // str/other 无值宽（2026-10-25 协议：目录照发但不带值）
      else text = fmtValue(it.value, it.type)
      return {
        index: it.index,
        type: it.type,
        name: it.name,
        unit: it.unit || '',
        text,
        pending: !!pend,
        readOnly: proto.valueSize(it.type) === 0, // str/other 不可写入，UI 禁编辑
        typeText: proto.typeName(it.type),
      }
    })
    .sort((a, b) => a.index - b.index)
}

/** 连接状态文案（设置页/数据页顶栏） */
store.getConnView = function () {
  const dot =
    store.connState === ConnState.CONNECTED
      ? 'connected'
      : store.connState === ConnState.CONNECTING
        ? 'connecting'
        : store.connState === ConnState.ERROR
          ? 'error'
          : 'idle'
  const label =
    store.connState === ConnState.CONNECTED
      ? '已连接'
      : store.connState === ConnState.CONNECTING
        ? '连接中'
        : store.connState === ConnState.ERROR
          ? '异常'
          : '未连接'
  let msg = store.connMsg || ''
  if (store.connState === ConnState.CONNECTED && store.backendStatus) msg = store.backendStatus
  return { dot, label, msg }
}

store.getStats = function () {
  return store.parser.stats
}

store.getToasts = function () {
  return store.toasts
}

/* ================= 业务动作 ================= */

/** 订阅/退订/升降速（本端乐观更新 tag；V5 侧合并 SUB/FAST，无回显） */
store.setSubscription = function (index, sub, fast) {
  const it = store.monitorDir.find((x) => x.index === index)
  if (!it) return
  const newTag = (it.tag & ~(proto.MONITOR_TAG_SUB | proto.MONITOR_TAG_FAST)) |
    (sub ? proto.MONITOR_TAG_SUB : 0) |
    (fast ? proto.MONITOR_TAG_FAST : 0)
  it.tag = newTag
  store._send(proto.subscribe(index, sub, fast))
}

/** 写调参项；回显或 2s 超时前显示"等待确认…" */
store.setTunable = function (index, value) {
  const it = store.tunableDir.find((x) => x.index === index)
  if (!it) return
  store.pendingTun[index] = { value, since: Date.now() }
  store._send(proto.setTunable(index, it.type, value))
}

/** 常驻停车：命令目录里 spec=Stop 的那条，无参直发 */
store.emergencyStop = function () {
  const stop = store.cmdDir.find((c) => proto.cmdSpec(c.tag) === proto.CmdSpec.Stop)
  if (!stop) {
    store._toast('warn', '命令目录未就绪或未登记停车命令')
    return false
  }
  store._send(proto.commandSend(stop, []))
  store._toast('error', '已发送停车')
  return true
}

store.hasStopCmd = function () {
  return store.cmdDir.some((c) => proto.cmdSpec(c.tag) === proto.CmdSpec.Stop)
}

/* ================= toast ================= */

store._toast = function (level, text) {
  store.toasts.push({ level, text, at: Date.now() })
  if (store.toasts.length > 5) store.toasts.shift()
}

module.exports = store
