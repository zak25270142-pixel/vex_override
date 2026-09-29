/**
 * 全局数据（模块级 ref/reactive 风格）。
 * 串口字节流 → FrameParser 解帧 → 写本文件的数据；组件只读这里自行视图化。
 */
import { computed, reactive, ref } from 'vue'
import { serialClient } from '@/services/serialClient'
import {
  FrameParser,
  MonitorTag,
  ValueType,
  requestMonitor,
  requestTunable,
  subscribe,
  setTunable,
  requestCommand,
  commandSend,
  ping,
  typeName,
  valueSize,
  type CellValue,
  type DirItem,
  type CmdDirItem,
  type RxEvent,
} from '@/services/protocol'
import { CHANNELS, MAX_POINTS, PLOT_WINDOW_S } from '@/config/workspace'
import type { WorkView } from '@/config/workspace'

/* ===== 连接 ===== */
export const serialSupported = ref(serialClient.supported)
export const demoMode = ref(false)
export const connState = ref(serialClient.state)
export const connected = computed(() => connState.value === 'connected')
export const statusText = ref(
  serialSupported.value ? '未连接' : '浏览器不支持 Web Serial，请使用 Chrome / Edge',
)
/** User 口是 USB CDC，波特率无实际意义，固定 115200 仅为满足 API */
export const baud = 115200

/* ===== 分页 / 参数表切换 ===== */
export const view = ref<WorkView>('params')
/** 调参表 = 下位机 tunable 目录；实参表 = monitor 目录（实时监视量） */
export const paramTable = ref<'tunable' | 'monitor'>('monitor')
export const keyword = ref('')

/* ===== 通信诊断（让"点了没反应"能定位到具体环节） ===== */
export interface DiagLog {
  t: string
  dir: 'TX' | 'RX' | 'EVT' | 'ERR'
  text: string
}
export const commStats = reactive({
  txFrames: 0, // 已发出的请求帧
  txBytes: 0,
  rxChunks: 0, // 串口到达的数据块次数
  rxBytes: 0,
  frames: 0, // 成功解析的帧
  badChecksum: 0,
  unknownCmd: 0,
  overflow: 0,
  dirFrames: 0, // 其中目录帧条数
  valueFrames: 0, // 其中监控值帧条数
})
export const commLog = ref<DiagLog[]>([])
export const diagOpen = ref(false)
const MAX_DIAG_LOGS = 120
let lastRxLogAt = 0

function toHex(data: Uint8Array, max = 24): string {
  const n = Math.min(data.length, max)
  const s = Array.from(data.subarray(0, n), (b) => b.toString(16).padStart(2, '0')).join(' ')
  return data.length > n ? `${s} …(+${data.length - n}B)` : s
}

function pushDiag(dir: DiagLog['dir'], text: string): void {
  const d = new Date()
  const t = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(
    d.getSeconds(),
  ).padStart(2, '0')}.${String(d.getMilliseconds()).padStart(3, '0')}`
  commLog.value.unshift({ t, dir, text })

  // RX 原始字节是高频日志，单独限 60 条；
  // TX/EVT/ERR 是关键事件，不许被高频 RX 挤出窗口
  let rxCount = 0
  for (let i = 0; i < commLog.value.length; i++) {
    if (commLog.value[i]!.dir !== 'RX') continue
    rxCount++
    if (rxCount > 60) {
      commLog.value.splice(i, 1)
      i--
    }
  }
  if (commLog.value.length > MAX_DIAG_LOGS) {
    // 超总量时也只从最老的 RX 条目开始淘汰
    for (let i = commLog.value.length - 1; i >= 0 && commLog.value.length > MAX_DIAG_LOGS; i--) {
      if (commLog.value[i]!.dir === 'RX') commLog.value.splice(i, 1)
    }
    // 极端情况下全是关键事件才硬截
    if (commLog.value.length > MAX_DIAG_LOGS) commLog.value.length = MAX_DIAG_LOGS
  }
}

export function clearDiag(): void {
  commLog.value = []
  commStats.txFrames = 0
  commStats.txBytes = 0
  commStats.rxChunks = 0
  commStats.rxBytes = 0
  commStats.frames = 0
  commStats.badChecksum = 0
  commStats.unknownCmd = 0
  commStats.overflow = 0
  commStats.dirFrames = 0
  commStats.valueFrames = 0
  parser.clearStats()
}

/**
 * 统一的发帧出口：记日志、捕获异常上屏（原来 send 抛错无人接，表现就是"没反应"）。
 * quiet 用于 1s 一次的心跳：成功不刷诊断日志（否则日志全是心跳），但计数照加；
 * 失败无论是否 quiet 都上屏，断线必须看得见。
 */
async function sendFrame(frame: Uint8Array, label: string, quiet = false): Promise<boolean> {
  try {
    await serialClient.send(frame)
    commStats.txFrames++
    commStats.txBytes += frame.length
    if (!quiet) pushDiag('TX', `${label}：${toHex(frame)}`)
    return true
  } catch (e) {
    const msg = `${label}发送失败：${(e as Error).message}`
    statusText.value = msg
    pushDiag('ERR', msg)
    return false
  }
}

/* ===== 心跳：连上后每 1s 发 Ping，主控回 Pong；主控 2.56s 收不到会停推监控值 ===== */
let pingTimer: number | null = null

function stopPing(): void {
  if (pingTimer !== null) {
    clearInterval(pingTimer)
    pingTimer = null
  }
}

function startPing(): void {
  stopPing()
  // 立刻先发一个：主控看门狗第一个巡视周期（2.56s）内必须见到 Ping，
  // 干等到首个 1s 间隔虽然也够，但立即发能让"刚连上"的链路最快被认可
  void sendFrame(ping(), '心跳Ping', true)
  pingTimer = window.setInterval(() => {
    void sendFrame(ping(), '心跳Ping', true)
  }, 1000)
}

/* ===== 两张参数目录（key=下位机数组下标 index） ===== */
export const tunableMap = reactive<Record<number, DirItem>>({})
export const monitorMap = reactive<Record<number, DirItem>>({})
/** 动作命令目录（key=命令字 0x80~0x9F），右侧下发表单按它渲染 */
export const commandMap = reactive<Record<number, CmdDirItem>>({})
/** 已发出 SetTunable、尚未收到回显确认的调参项 index（行内显示"等待确认"） */
export const pendingTunable = reactive(new Set<number>())
/** index→超时计时器：超时还没收到 TunableEcho 就判失败，避免一直转圈 */
const setTimers = new Map<number, number>()
const SET_ACK_TIMEOUT_MS = 2000
/** 监控量最新值，由 50ms 定时刷新从高频原始缓冲搬运（避免 100Hz 直接触发响应式） */
export const monitorLatest = reactive<Record<number, CellValue>>({})

/* ===== 场地位姿（x/y 单位米，yaw 单位度、顺时针正、0°朝 X+） ===== */
export const pose = reactive({ x: 0, y: 0, yaw: 0, valid: false })

/* ===== 曲线：4 个通道各自绑定一个监控量 index，null=未绑定 ===== */
export const channelSel = reactive<(number | null)[]>(CHANNELS.map(() => null))
export const merged = ref(false)
export const plotWindowS = ref(PLOT_WINDOW_S)

/* ===== 订阅档位展示文案 ===== */
export function tagLabel(tag: MonitorTag): string {
  switch (tag) {
    case MonitorTag.Slow:
      return '低速'
    case MonitorTag.Fast:
      return '高速'
    case MonitorTag.PosX:
      return '位置X'
    case MonitorTag.PosY:
      return '位置Y'
    case MonitorTag.Yaw:
      return '航向'
    default:
      return '未订阅'
  }
}

/** x/y/yaw 三个语义标记量：连接时即自动订阅，不允许在界面上改档位 */
export function isPoseTag(tag: MonitorTag): boolean {
  return tag === MonitorTag.PosX || tag === MonitorTag.PosY || tag === MonitorTag.Yaw
}

/** 可绑到曲线通道的监听量：数值类型且当前已订阅（x/y/yaw 也算） */
export const plottableItems = computed<DirItem[]>(() =>
  sortedMonitor(monitorMap).filter((it) => it.tag !== MonitorTag.None && valueSize(it.type) > 0),
)

function sortedIndices(map: Record<number, DirItem>): number[] {
  return Object.keys(map)
    .map(Number)
    .sort((a, b) => a - b)
}

function sortedMonitor(map: Record<number, DirItem>): DirItem[] {
  // 已订阅优先（档位高的排前），同档按 index；未订阅按 index 跟在后面
  return sortedIndices(map)
    .map((i) => map[i]!)
    .sort((a, b) => {
      const sa = a.tag === MonitorTag.None ? 0 : 1
      const sb = b.tag === MonitorTag.None ? 0 : 1
      if (sa !== sb) return sb - sa
      if (sa === 1 && a.tag !== b.tag) return b.tag - a.tag
      return a.index - b.index
    })
}

/** 参数表页面实际渲染的行（带搜索过滤） */
export const visibleRows = computed<DirItem[]>(() => {
  const map = paramTable.value === 'monitor' ? monitorMap : tunableMap
  const list =
    paramTable.value === 'monitor' ? sortedMonitor(map) : sortedIndices(map).map((i) => map[i]!)
  const kw = keyword.value.trim().toLowerCase()
  if (kw === '') return list
  return list.filter(
    (it) =>
      it.name.toLowerCase().includes(kw) ||
      String(it.index).includes(kw) ||
      typeName(it.type).includes(kw),
  )
})

/* ===== 高频数据的非响应式缓冲（串口回调里写，定时/组件按需读） ===== */
interface SeriesBuf {
  t: number[]
  v: number[]
}
const seriesBuf = new Map<number, SeriesBuf>()
const rawLatest: Record<number, CellValue> = {}
const rawPose = { x: 0, y: 0, yaw: 0 }
let tOrigin = 0

/** 图表 20fps 拉取曲线数据 */
export function getSeries(index: number): SeriesBuf | undefined {
  return seriesBuf.get(index)
}

/* ===== 协议解析 ===== */
const parser = new FrameParser()

function clearMonitorData(): void {
  for (const k of Object.keys(monitorMap)) delete monitorMap[Number(k)]
  for (const k of Object.keys(monitorLatest)) delete monitorLatest[Number(k)]
  seriesBuf.clear()
  parser.resetMonitorTypes()
  tOrigin = 0
  pose.valid = false
}

/** 清空调参待确认状态（重取目录 / 断开时用） */
function clearPendingTunable(): void {
  pendingTunable.clear()
  for (const t of setTimers.values()) clearTimeout(t)
  setTimers.clear()
}

function handleEvent(e: RxEvent): void {
  if (e.kind === 'pong') {
    // 心跳回应：帧已计入 parser 的 frames 计数，业务上无需动作；
    // 主控看门狗靠它自己收到的 Ping 工作，不依赖前端处理 Pong
    return
  }
  if (e.kind === 'directory') {
    commStats.dirFrames++
    const item = e.item
    if (e.table === 'tunable') {
      tunableMap[item.index] = item
      // 登记类型，收 TunableEcho 解帧时按此确定 value 宽度
      parser.setTunableType(item.index, item.type)
    } else {
      parser.setMonitorType(item.index, item.type)
      monitorMap[item.index] = item
      monitorLatest[item.index] = item.value
    }
    pushDiag(
      'EVT',
      `${e.table === 'tunable' ? '调参' : '实参'}目录 #${item.index} ${item.name}（${typeName(
        item.type,
      )}，${tagLabel(item.tag)}）`,
    )
    return
  }
  if (e.kind === 'echo') {
    // 调参回显：值是主控从内存重读的真实生效值，直接覆盖目录行
    const item = tunableMap[e.index]
    if (item) item.value = e.value
    const old = setTimers.get(e.index)
    if (old !== undefined) {
      clearTimeout(old)
      setTimers.delete(e.index)
    }
    pendingTunable.delete(e.index)
    pushDiag('EVT', `调参 #${e.index} 已确认：${item ? formatValue(item, e.value) : e.value}`)
    return
  }
  if (e.kind === 'cmdDirectory') {
    // 动作命令目录：一条命令一帧，按命令字存表，右侧面板据此渲染表单
    commandMap[e.item.index] = e.item
    return
  }

  commStats.valueFrames++
  // MonitorValue：index→值
  const index = e.index
  const item = monitorMap[index]
  rawLatest[index] = e.value

  if (typeof e.value === 'number') {
    // 曲线缓冲（所有数值量都记，是否展示由通道绑定决定）
    if (tOrigin === 0) tOrigin = performance.now()
    let buf = seriesBuf.get(index)
    if (!buf) {
      buf = { t: [], v: [] }
      seriesBuf.set(index, buf)
    }
    buf.t.push((performance.now() - tOrigin) / 1000)
    buf.v.push(e.value)
    if (buf.t.length > MAX_POINTS) {
      buf.t.splice(0, buf.t.length - MAX_POINTS)
      buf.v.splice(0, buf.v.length - MAX_POINTS)
    }
  }

  // x/y/yaw 按语义 tag 识别（不依赖名字）
  if (item && typeof e.value === 'number') {
    if (item.tag === MonitorTag.PosX) rawPose.x = e.value
    else if (item.tag === MonitorTag.PosY) rawPose.y = e.value
    else if (item.tag === MonitorTag.Yaw) rawPose.yaw = e.value
  }
}

/** 50ms 把高频缓冲搬运到响应式状态（表格数值、场地位姿以 20fps 刷新足够），
    同时搬运解析器计数（高频数据不逐帧触发响应式） */
setInterval(() => {
  for (const k of Object.keys(rawLatest)) {
    const index = Number(k)
    const v = rawLatest[index] as CellValue
    monitorLatest[index] = v
    if (monitorMap[index]) monitorMap[index]!.value = v
    delete rawLatest[index]
  }
  commStats.rxBytes = parser.stats.bytes
  commStats.frames = parser.stats.frames
  commStats.badChecksum = parser.stats.badChecksum
  commStats.unknownCmd = parser.stats.unknownCmd
  commStats.overflow = parser.stats.overflow
  const item3 = Object.values(monitorMap).find((it) => it.tag === MonitorTag.PosX)
  const item4 = Object.values(monitorMap).find((it) => it.tag === MonitorTag.PosY)
  const item5 = Object.values(monitorMap).find((it) => it.tag === MonitorTag.Yaw)
  if (item3 || item4 || item5) {
    pose.x = rawPose.x
    pose.y = rawPose.y
    pose.yaw = rawPose.yaw
    pose.valid = true
  }
}, 50)

/* ===== 串口回调挂载 ===== */
serialClient.onStatus = (state, message) => {
  connState.value = state
  statusText.value = message
  pushDiag('EVT', `连接状态：${state}（${message}）`)
  if (state === 'connected') {
    // 连上自动拉三张表，标题栏按钮也可随时手动刷新
    void fetchTunable()
    void fetchMonitor()
    void fetchCommands()
    startPing() // 同时启动1s心跳；主控2.56s收不到Ping会自动停推监控值
  } else {
    stopPing() // 断开/连接中/出错都停心跳，避免向已关闭的端口写
    clearPendingTunable() // 待确认的写参也全部作废
  }
}
serialClient.onData = (data) => {
  commStats.rxChunks++
  // 原始字节日志限速：高速推送约100Hz，全记会刷屏，但计数一个不漏
  const now = performance.now()
  if (now - lastRxLogAt > 200) {
    lastRxLogAt = now
    pushDiag('RX', `原始字节 ${data.length}B：${toHex(data)}`)
  }
  parser.feed(data, handleEvent)
}

/* ===== 动作：连接 / 演示 ===== */
export async function pickPort(): Promise<void> {
  try {
    await serialClient.pickPort()
    pushDiag('EVT', `已选择端口：${serialClient.portLabel}`)
  } catch (e) {
    statusText.value = `选择串口失败：${(e as Error).message}`
    pushDiag('ERR', `选择串口失败：${(e as Error).message}`)
  }
}

export async function toggleConnect(): Promise<void> {
  if (connected.value) {
    await serialClient.disconnect()
    return
  }
  try {
    if (!demoMode.value && !serialClient.isPortReady()) await serialClient.pickPort()
    await serialClient.connect(baud)
  } catch (e) {
    statusText.value = `连接失败：${(e as Error).message}`
    pushDiag('ERR', `连接失败：${(e as Error).message}`)
  }
}

export async function setDemoMode(on: boolean): Promise<void> {
  await serialClient.setDemoMode(on)
  demoMode.value = on
  connState.value = serialClient.state
}

/* ===== 动作：获取数据表 ===== */
export async function fetchTunable(): Promise<void> {
  if (!connected.value) {
    statusText.value = '尚未连接，无法请求调参表'
    return
  }
  for (const k of Object.keys(tunableMap)) delete tunableMap[Number(k)]
  parser.resetTunableTypes()
  clearPendingTunable()
  // 先上屏"请求已发出"，再走统一发帧出口（失败也会被捕获上屏）
  statusText.value = '已发送调参表请求，等待主控回传…'
  const ok = await sendFrame(requestTunable(), '请求调参表')
  if (ok) paramTable.value = 'tunable'
}

export async function fetchMonitor(): Promise<void> {
  if (!connected.value) {
    statusText.value = '尚未连接，无法请求实参表'
    return
  }
  clearMonitorData()
  statusText.value = '已发送实参表请求，等待主控回传…'
  const ok = await sendFrame(requestMonitor(), '请求实参表')
  if (ok) paramTable.value = 'monitor'
}

/* ===== 动作：动作命令表 ===== */
export async function fetchCommands(): Promise<void> {
  if (!connected.value) {
    statusText.value = '尚未连接，无法请求命令表'
    return
  }
  for (const k of Object.keys(commandMap)) delete commandMap[Number(k)]
  statusText.value = '已发送命令表请求，等待主控回传…'
  await sendFrame(requestCommand(), '请求命令表')
}

/**
 * 下发动作命令（无 ACK，发完即走；车是否执行可从实参表观察）。
 * values 顺序与 item.fields 对应，由面板保证已填且为有效数。
 */
export async function sendCommand(index: number, values: CellValue[]): Promise<void> {
  if (!connected.value) return
  const item = commandMap[index]
  if (!item) return
  await sendFrame(commandSend(item, values), `下发命令 ${item.name}`)
}

/* ===== 动作：订阅 / 退订（同一条 Subscribe，tag=0 即退订） ===== */
export async function setSubscription(index: number, tag: MonitorTag): Promise<void> {
  if (!connected.value) return
  const ok = await sendFrame(subscribe(index, tag), `订阅 #${index}→${tagLabel(tag)}`)
  // 等主控目录/推送侧自然印证即可；发送成功才本地先改，避免失败时界面说谎
  if (ok && monitorMap[index]) monitorMap[index]!.tag = tag
}

/* ===== 动作：写调参（SetTunable，以 TunableEcho 回显为确认） ===== */
export async function setTunableValue(index: number, value: CellValue): Promise<void> {
  if (!connected.value) return
  const item = tunableMap[index]
  // 类型以下位机目录为准；str/other 无可写宽度，直接拒绝
  if (!item || valueSize(item.type) === 0) return
  const ok = await sendFrame(setTunable(index, item.type, value), `写调参 #${index}`)
  if (!ok) return
  // 行内进入"等待确认"；本地值不提前改，等回显回来才算数（回显可能被裁剪/换算）
  pendingTunable.add(index)
  const old = setTimers.get(index)
  if (old !== undefined) clearTimeout(old)
  setTimers.set(
    index,
    window.setTimeout(() => {
      setTimers.delete(index)
      pendingTunable.delete(index)
      statusText.value = `调参 #${index} 写入超时，未收到主控确认`
      pushDiag('ERR', `调参 #${index} 写入后 2s 未收到回显`)
    }, SET_ACK_TIMEOUT_MS),
  )
}

/** 数值显示格式化：浮点保留3位小数，整数原样 */
export function formatValue(item: DirItem, v: CellValue): string {
  if (typeof v === 'string') return v // color: #RRGGBB
  if (item.type === ValueType.Bool || item.type === ValueType.OnOff) return v ? '开' : '关'
  if (item.type === ValueType.Float || item.type === ValueType.Double) {
    return Number.isInteger(v) ? String(v) : v.toFixed(3)
  }
  return String(v)
}
