/**
 * 本地持久化层：只负责 localStorage 读写与字段校验，不含业务语义。
 * 组件/store 通过 loadPrefs() 读取快照、savePrefs() 局部覆盖（内部合并 + 防抖写盘）。
 */
import { CHANNELS, PLOT_WINDOW_S } from '@/config/workspace'

const KEY = 'v5.prefs.v1'
const WRITE_DEBOUNCE_MS = 300

/** x 轴窗口取值范围，与 ScopeConfig 的校验保持一致 */
const WINDOW_MIN = 0.5
const WINDOW_MAX = 60

export interface PortIdentity {
  vid: number
  pid: number
}

export interface SubEntry {
  sub: boolean
  fast: boolean
}

export interface ChannelCfg {
  windowS: number
  yAuto: boolean
  yMin: number
  yMax: number
}

export interface ChartPrefs {
  /** 4 通道，每通道为追踪项的 monitor index 列表 */
  traces: number[][]
  cfg: ChannelCfg[]
  focusCh: number | null
  bigView: boolean
}

export interface Prefs {
  autoConnect: boolean
  demo: boolean
  port: PortIdentity | null
  /** monitor index → 订阅档位（含退订，用于对抗主控默认订阅位） */
  subs: Record<number, SubEntry>
  chart: ChartPrefs
}

function defaultChart(): ChartPrefs {
  return {
    traces: CHANNELS.map(() => []),
    cfg: CHANNELS.map(() => ({ windowS: PLOT_WINDOW_S, yAuto: true, yMin: 0, yMax: 1 })),
    focusCh: null,
    bigView: false,
  }
}

export function defaultPrefs(): Prefs {
  return { autoConnect: true, demo: false, port: null, subs: {}, chart: defaultChart() }
}

/* ===== 字段校验（storage 是系统边界，脏数据一律回落到默认值） ===== */

function asBool(v: unknown, fallback: boolean): boolean {
  return typeof v === 'boolean' ? v : fallback
}

function validIdx(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0
}

function readPort(raw: unknown): PortIdentity | null {
  const p = raw as PortIdentity | null
  if (!p || typeof p.vid !== 'number' || typeof p.pid !== 'number') return null
  return { vid: p.vid, pid: p.pid }
}

function readSubs(raw: unknown): Record<number, SubEntry> {
  const out: Record<number, SubEntry> = {}
  if (!raw || typeof raw !== 'object') return out
  for (const [k, v] of Object.entries(raw as Record<string, SubEntry>)) {
    if (!validIdx(Number(k)) || !v || typeof v !== 'object') continue
    if (typeof v.sub !== 'boolean' || typeof v.fast !== 'boolean') continue
    out[Number(k)] = { sub: v.sub, fast: v.fast }
  }
  return out
}

function readCfg(raw: unknown): ChannelCfg[] {
  const fallback = defaultChart().cfg
  if (!Array.isArray(raw) || raw.length !== CHANNELS.length) return fallback
  return fallback.map((def, i) => {
    const c = raw[i] as ChannelCfg
    if (!c || typeof c !== 'object') return def
    const windowS =
      typeof c.windowS === 'number' && c.windowS >= WINDOW_MIN && c.windowS <= WINDOW_MAX
        ? c.windowS
        : def.windowS
    const yAuto = asBool(c.yAuto, def.yAuto)
    const minOk = typeof c.yMin === 'number'
    const maxOk = typeof c.yMax === 'number'
    const rangeOk = minOk && maxOk && c.yMin < c.yMax
    return {
      windowS,
      yAuto,
      yMin: rangeOk ? c.yMin : def.yMin,
      yMax: rangeOk ? c.yMax : def.yMax,
    }
  })
}

function readTraces(raw: unknown): number[][] {
  const fallback = defaultChart().traces
  if (!Array.isArray(raw) || raw.length !== CHANNELS.length) return fallback
  return fallback.map((_def, i) => {
    const list = raw[i]
    if (!Array.isArray(list)) return []
    const out: number[] = []
    for (const v of list) if (validIdx(v) && !out.includes(v)) out.push(v) // 同通道禁重复
    return out
  })
}

function readChart(raw: unknown): ChartPrefs {
  const def = defaultChart()
  const c = raw as Partial<ChartPrefs> | undefined
  if (!c || typeof c !== 'object') return def
  const focus = validIdx(c.focusCh) && c.focusCh < CHANNELS.length ? c.focusCh : null
  return {
    traces: readTraces(c.traces),
    cfg: readCfg(c.cfg),
    focusCh: focus,
    bigView: asBool(c.bigView, def.bigView),
  }
}

function readPrefs(): Prefs {
  const def = defaultPrefs()
  let raw: Partial<Prefs> | null = null
  try {
    const text = localStorage.getItem(KEY)
    raw = text ? (JSON.parse(text) as Partial<Prefs>) : null
  } catch {
    /* 无 localStorage（隐私模式）或内容损坏：用默认值，仅内存生效 */
  }
  if (!raw) return def
  return {
    autoConnect: asBool(raw.autoConnect, def.autoConnect),
    demo: asBool(raw.demo, def.demo),
    port: readPort(raw.port),
    subs: readSubs(raw.subs),
    chart: readChart(raw.chart),
  }
}

let cache: Prefs | null = null
let writeTimer: number | null = null

/** 读取持久化快照（首次调用读盘并校验，之后返回同一份内存对象） */
export function loadPrefs(): Prefs {
  if (!cache) cache = readPrefs()
  return cache
}

function flush(): void {
  writeTimer = null
  try {
    localStorage.setItem(KEY, JSON.stringify(cache))
  } catch {
    /* 隐私模式/配额不足：静默放弃持久化，不影响运行 */
  }
}

/** 局部覆盖并防抖写盘 */
export function savePrefs(patch: Partial<Prefs>): void {
  Object.assign(loadPrefs(), patch)
  if (writeTimer !== null) return
  writeTimer = window.setTimeout(flush, WRITE_DEBOUNCE_MS)
}