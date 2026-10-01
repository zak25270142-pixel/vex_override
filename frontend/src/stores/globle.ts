/**
 * 全局数据（模块级 ref/reactive 风格）。
 * 串口字节流 → FrameParser 解帧 → 写本文件的数据；组件只读这里自行视图化。
 */
import { computed, reactive, ref, watch } from 'vue'
import { serialClient } from '@/services/serialClient'
import { loadPrefs, savePrefs } from '@/services/persist'
import {
  FrameParser,
  MONITOR_TAG_FAST,
  MONITOR_TAG_SUB,
  MonitorKind,
  ValueType,
  requestMonitor,
  requestTunable,
  subscribe,
  setTunable,
  requestCommand,
  commandSend,
  ping,
  tagFast,
  tagKind,
  tagSubscribed,
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
/** 演示模式（持久化：下次打开沿用上次的选择） */
export const demoMode = ref(loadPrefs().demo)
export const connState = ref(serialClient.state)
export const connected = computed(() => connState.value === 'connected')
export const statusText = ref(
  serialSupported.value ? '未连接' : '浏览器不支持 Web Serial，请使用 Chrome / Edge',
)
/** User 口是 USB CDC，波特率无实际意义，固定 115200 仅为满足 API */
export const baud = 115200
/** 顶栏"自动连接"开关（持久化）：页面加载与断线后自动重连 */
export const autoConnect = ref(loadPrefs().autoConnect)

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
/** 已发出 SetTunable、尚未收到回显确认的调参项 index（右侧面板显示"等待确认"） */
export const pendingTunable = reactive(new Set<number>())
/** 参数表单击选中的调参项 index（右侧面板据此显示编辑卡片）；null=未选中，右侧保持任务下发 */
export const selectedTunableIndex = ref<number | null>(null)
export const selectedTunableItem = computed<DirItem | null>(() =>
  selectedTunableIndex.value === null ? null : (tunableMap[selectedTunableIndex.value] ?? null),
)
/** index→超时计时器：超时还没收到 TunableEcho 就判失败，避免一直转圈 */
const setTimers = new Map<number, number>()
const SET_ACK_TIMEOUT_MS = 2000
/** 监控量最新值，由 50ms 定时刷新从高频原始缓冲搬运（避免 100Hz 直接触发响应式） */
export const monitorLatest = reactive<Record<number, CellValue>>({})

/* ===== 场地位姿（x/y 单位米，yaw 单位度、顺时针正、0°朝 X+） ===== */
export const pose = reactive({ x: 0, y: 0, yaw: 0, valid: false })

/* ===== 曲线：4 个通道，每通道至多 4 条追踪曲线 ===== */
/** 每通道追踪槽上限（槽位颜色取 --channel-1~4） */
export const TRACE_MAX = 4
/** traces[ch]：该通道当前追踪的监控量 index，按槽位顺序排列 */
export const channelTraces = reactive<number[][]>(CHANNELS.map(() => []))
/** 当前在右栏配置 / 大图查看的通道，null=未选中 */
export const focusCh = ref<number | null>(null)
/** 显示模式：false=2×2分图，true=选中通道大图（大图占满宽，配置卡默认收起为浮层） */
export const bigView = ref(false)
/** 大图模式下配置浮层是否展开（切通道自动收起） */
export const scopeDrawer = ref(false)

/** 每通道独立的坐标轴配置（x 时间窗口、y 自动/手动量程） */
export interface ChannelAxisCfg {
  windowS: number
  yAuto: boolean
  yMin: number
  yMax: number
}
export const channelCfg = reactive<ChannelAxisCfg[]>(
  CHANNELS.map(() => ({ windowS: PLOT_WINDOW_S, yAuto: true, yMin: 0, yMax: 1 })),
)

/** 改动某通道已有槽位的追踪项：raw='' 移除该槽，否则换成新 index（同通道禁重复） */
export function pickTrace(ch: number, slot: number, raw: string): void {
  const list = channelTraces[ch]!
  if (raw === '') {
    list.splice(slot, 1)
    return
  }
  const index = Number(raw)
  if (list.some((v, k) => k !== slot && v === index)) return
  if (slot < list.length) list[slot] = index
  else if (list.length < TRACE_MAX) list.push(index)
}

/** 末尾空槽添加追踪项（raw='' 是占位"未选择"，忽略） */
export function addTrace(ch: number, raw: string): void {
  if (raw === '') return
  pickTrace(ch, channelTraces[ch]!.length, raw)
}

/* ===== 持久化：订阅意向 + 曲线配置 ===== */
/**
 * 用户期望的订阅档位（按 monitor index，含退订）。
 * 订阅态本身寄生在 monitorMap[index].tag 的位域里，而每次 fetchMonitor 都会清表重建，
 * 所以必须单独留一份意向，重连后据此补发订阅帧。
 */
const desiredSubs = new Map<number, { sub: boolean; fast: boolean }>()
/** 本次连接已补发过订阅的 index，防止重复拉目录时重发 */
const restoredSubs = new Set<number>()

/** 目录项到达后，按持久化意向补发订阅（逐项触发，天然与目录分批同节奏） */
function restoreSubscription(item: DirItem): void {
  const want = desiredSubs.get(item.index)
  if (!want || restoredSubs.has(item.index)) return
  restoredSubs.add(item.index)
  // 与主控当前状态一致就无需发帧（多为"本来就没订阅过"的项）
  if (tagSubscribed(item.tag) === want.sub && tagFast(item.tag) === want.fast) return
  void setSubscription(item.index, want.sub, want.fast)
}

/** 应用启动时把持久化配置写回响应式状态；须在任何组件挂载前调用 */
export function restorePrefs(): void {
  const prefs = loadPrefs()
  for (const [k, v] of Object.entries(prefs.subs)) desiredSubs.set(Number(k), v)
  prefs.chart.traces.forEach((list, ch) => {
    if (ch < channelTraces.length) channelTraces[ch] = list.slice(0, TRACE_MAX)
  })
  prefs.chart.cfg.forEach((c, ch) => {
    const target = channelCfg[ch]
    if (target) Object.assign(target, c)
  })
  // 写在最后：watch(focusCh) 会收起浮层，避免刚恢复的展开态被重置
  focusCh.value = prefs.chart.focusCh
  bigView.value = prefs.chart.bigView
}

/** 曲线配置改动即落盘（savePrefs 内部已防抖） */
watch(
  [channelTraces, channelCfg, focusCh, bigView],
  () => {
    savePrefs({
      chart: {
        traces: channelTraces.map((l) => l.slice()),
        cfg: channelCfg.map((c) => ({ ...c })),
        focusCh: focusCh.value,
        bigView: bigView.value,
      },
    })
  },
  { deep: true },
)

/* ===== 订阅档位展示文案（tag 为位域：先看语义种类，再按订阅位拼档位） ===== */
export function tagLabel(tag: number): string {
  const kind = tagKind(tag)
  if (kind === MonitorKind.PosX) return '位置X'
  if (kind === MonitorKind.PosY) return '位置Y'
  if (kind === MonitorKind.Yaw) return '航向'
  if (!tagSubscribed(tag)) return '未订阅'
  return tagFast(tag) ? '高速' : '低速'
}

/** x/y/yaw 三个语义标记量：连接时即自动订阅，不允许在界面上改档位 */
export function isPoseTag(tag: number): boolean {
  const kind = tagKind(tag)
  return kind === MonitorKind.PosX || kind === MonitorKind.PosY || kind === MonitorKind.Yaw
}

/** 可绑到曲线通道的监听量：数值类型且当前已订阅（x/y/yaw 也算） */
export const plottableItems = computed<DirItem[]>(() =>
  sortedMonitor(monitorMap).filter((it) => tagSubscribed(it.tag) && valueSize(it.type) > 0),
)

function sortedIndices(map: Record<number, DirItem>): number[] {
  return Object.keys(map)
    .map(Number)
    .sort((a, b) => a - b)
}

function sortedMonitor(map: Record<number, DirItem>): DirItem[] {
  // 已订阅优先（高速排前），同档按 index；未订阅按 index 跟在后面
  return sortedIndices(map)
    .map((i) => map[i]!)
    .sort((a, b) => {
      const sa = tagSubscribed(a.tag) ? 1 : 0
      const sb = tagSubscribed(b.tag) ? 1 : 0
      if (sa !== sb) return sb - sa
      if (sa === 1) {
        const fa = tagFast(a.tag) ? 1 : 0
        const fb = tagFast(b.tag) ? 1 : 0
        if (fa !== fb) return fb - fa
      }
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
      // 目录帧能到达即证明主控程序已就绪，此时补发订阅最稳妥
      restoreSubscription(item)
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
    // t 存绝对接收时刻（秒），显示时由图表换算成"距现在"，实现示波器式右对齐时间轴
    let buf = seriesBuf.get(index)
    if (!buf) {
      buf = { t: [], v: [] }
      seriesBuf.set(index, buf)
    }
    buf.t.push(performance.now() / 1000)
    buf.v.push(e.value)
    if (buf.t.length > MAX_POINTS) {
      buf.t.splice(0, buf.t.length - MAX_POINTS)
      buf.v.splice(0, buf.v.length - MAX_POINTS)
    }
  }

  // x/y/yaw 按语义种类识别（不依赖名字）
  if (item && typeof e.value === 'number') {
    const kind = tagKind(item.tag)
    if (kind === MonitorKind.PosX) rawPose.x = e.value
    else if (kind === MonitorKind.PosY) rawPose.y = e.value
    else if (kind === MonitorKind.Yaw) rawPose.yaw = e.value
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
  const item3 = Object.values(monitorMap).find((it) => tagKind(it.tag) === MonitorKind.PosX)
  const item4 = Object.values(monitorMap).find((it) => tagKind(it.tag) === MonitorKind.PosY)
  const item5 = Object.values(monitorMap).find((it) => tagKind(it.tag) === MonitorKind.Yaw)
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
    // 新一轮连接：允许按持久化意向重新补发订阅（connected 必定转发，不受重试去重影响）
    restoredSubs.clear()
    // 连上自动拉三张表，标题栏按钮也可随时手动刷新
    void fetchTunable()
    void fetchMonitor()
    void fetchCommands()
    startPing() // 同时启动1s心跳；主控2.56s收不到Ping会自动停推监控值
  } else {
    stopPing() // 断开/连接中/出错都停心跳，避免向已关闭的端口写
    clearPendingTunable() // 待确认的写参也全部作废
    selectedTunableIndex.value = null
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
  savePrefs({ demo: on })
}

/** 顶栏"自动连接"开关：落盘并联动串口客户端的重连开关 */
export function setAutoConnect(on: boolean): void {
  autoConnect.value = on
  savePrefs({ autoConnect: on })
  serialClient.setAutoConnect(on)
  pushDiag('EVT', on ? '已开启自动连接' : '已关闭自动连接')
  // 开启时立即发起一次连接，避免开关打开却停在断开状态
  if (on && !connected.value && !demoMode.value) void serialClient.autoConnect(baud)
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
  selectedTunableIndex.value = null // 表都没了，右侧编辑卡片一并关闭
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

/* ===== 动作：订阅 / 退订（同一条 Subscribe，sub=false 即退订） ===== */
export async function setSubscription(index: number, sub: boolean, fast: boolean): Promise<void> {
  if (!connected.value) return
  const label = sub ? (fast ? '高速' : '低速') : '未订阅'
  const ok = await sendFrame(subscribe(index, sub, fast), `订阅 #${index}→${label}`)
  if (!ok) return
  // 记录订阅意向（含退订），重连后按 index 自动恢复
  desiredSubs.set(index, { sub, fast: sub && fast })
  savePrefs({ subs: Object.fromEntries(desiredSubs) })
  // 等主控目录/推送侧自然印证即可；发送成功才本地先改，避免失败时界面说谎。
  // 本地只覆盖 SUB/FAST 两位，GETTER/KIND 等本机位与下位机 merge 语义保持一致
  if (monitorMap[index]) {
    const old = monitorMap[index]!.tag
    monitorMap[index]!.tag =
      (old & ~(MONITOR_TAG_SUB | MONITOR_TAG_FAST)) |
      (sub ? MONITOR_TAG_SUB : 0) |
      (fast ? MONITOR_TAG_FAST : 0)
  }
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
