/**
 * WebSocket 传输：对接 Python backend
 * 与 WebSerialTransport / MockTransport 实现同一契约，serialClient 可直接切换。
 *
 * 地址来源（不再写死 127.0.0.1——手机端打开时它指手机自己，永远连不上）：
 *   1. prefs.backendUrl 有值 → 用它（设置页手填，应对"前端与 backend 不同机"）
 *   2. 否则按页面来源推导：前端从哪台机器加载，就连那台机器的 backend，端口固定 8000
 * 地址在每次 connect 时才读取，所以改完设置点「连接」即生效，不必重建传输实例。
 *
 * 数据约定：
 *   - 后端 → 浏览器：ArrayBuffer 原样协议帧（进 FrameParser，store 零改动）
 *                   文本 JSON：连接状态 {"op":"status",...} + 固件回执异常
 *                   {"op":"alert",...}（后端待确认表的拒绝/放弃结论，见 transport.FwAlert）
 *                   + 脚本推荐的曲线配置 {"op":"chart_suggest",...}（见 transport.ChartSuggestion）
 *   - 浏览器 → 后端：ArrayBuffer 原样协议帧
 *
 * OPEN 之前发出的帧进入待发队列，连接一建立按序 flush，避免「连接中点击/自动拉表」丢命令。
 */

import type { ConnState, PortProbe, SerialTransport, TransportCallbacks } from './transport'
import { loadPrefs } from './persist'

const CONNECT_TIMEOUT_MS = 5000

/** 按页面来源推导：http 页面 → ws://，https 页面 → wss://。
 *  https 页面拿到的是 wss://，而局域网 backend 通常没配 TLS，那种场景要在设置页
 *  手动填 ws://<局域网IP>:8000/ws（能否放行见 openWs 的说明）。 */
export function defaultWsUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${location.hostname}:8000/ws`
}

/** 当前生效的后端 WS 地址：持久化覆盖值优先，否则推导 */
export function currentWsUrl(): string {
  return loadPrefs().backendUrl ?? defaultWsUrl()
}

/** 手填简写 → 完整 URL：允许只填 host[:port]，首尾自动补 `ws://` 与 `/ws`，
 *  端口缺省 8000。已带 `ws(s)://` 的按原样保留——wss 只能显式写出来。 */
export function normalizeBackendUrl(input: string): string {
  const s = input.trim()
  if (s === '') return ''
  if (/^wss?:\/\//i.test(s)) return s
  const host = s.replace(/\/+$/, '') // 容忍手滑多敲的尾斜杠
  return `ws://${host.includes(':') ? host : `${host}:8000`}/ws`
}

/** 完整 URL → 输入框里的简写。仅标准形态（ws://host:port/ws）才简化成 host:port，
 *  其余（wss、自定义路径）原样返回，免得把用户显式写的东西悄悄改掉 */
export function shortBackendUrl(url: string): string {
  const m = /^ws:\/\/([^/?#]+)\/ws\/?$/i.exec(url)
  return m ? m[1] : url
}

/** WS 地址 → HTTP base（同 host，ws→http / wss→https），供 /api/* 使用 */
function apiBaseOf(wsUrl: string): string {
  try {
    const u = new URL(wsUrl)
    return `${u.protocol === 'wss:' ? 'https:' : 'http:'}//${u.host}`
  } catch {
    return ''
  }
}

/** 建 WS。https 页面发 ws:// 属 mixed content，Chrome 154+ 可以靠
 *  `targetAddressSpace:'local'` 经 Local Network Access 授权放行；老浏览器不接受
 *  该 options（个别实现会抛），故退回无参构造，失败信息由 onerror 正常上报。 */
function openWs(url: string): WebSocket {
  if (location.protocol === 'https:' && url.startsWith('ws://')) {
    try {
      return new WebSocket(url, { targetAddressSpace: 'local' } as unknown as string)
    } catch {
      /* 退回下面的无参构造 */
    }
  }
  return new WebSocket(url)
}

export class WsTransport implements SerialTransport {
  readonly kind = 'ws' as const
  private ws: WebSocket | null = null
  /** CONNECTING 阶段积压的帧，onopen 时按序发出 */
  private pending: Uint8Array[] = []

  constructor(private cb: TransportCallbacks) {}

  isPortReady(): boolean {
    // 后端模式无需选口授权，随时可连
    return true
  }

  async pickPort(): Promise<void> {
    // 后端固定地址，无口可选
  }

  get portLabel(): string {
    return currentWsUrl()
  }

  /** 让 Backend 去连下位机；串口探测阻塞交给后端重连线程，前端不等结果 */
  private async askBackendConnect(wsUrl: string): Promise<void> {
    try {
      await fetch(`${apiBaseOf(wsUrl)}/api/connect`, { method: 'POST' })
    } catch {
      // 后端还没起来/网络闪断时静默，WS 建链失败会走正常 error 上报
    }
  }

  async connect(_baudRate: number, _probe?: PortProbe): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return
    const url = currentWsUrl()
    this.cb.onStatus('connecting', `连接后端 ${url} …`)
    await this.askBackendConnect(url)
    await new Promise<void>((resolve, reject) => {
      const ws = openWs(url)
      ws.binaryType = 'arraybuffer'
      let settled = false
      const timer = window.setTimeout(() => {
        if (settled) return
        settled = true
        ws.onopen = ws.onerror = null
        ws.close()
        reject(new Error(`连接后端超时：${url}（确认 uvicorn 已启动、手机与电脑同网段）`))
      }, CONNECT_TIMEOUT_MS)

      ws.onopen = () => {
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        this.ws = ws
        // 连接建立前积压的请求（自动拉目录等）一次性按序补发
        for (const f of this.pending) ws.send(this.toBuffer(f))
        this.pending = []
        this.cb.onStatus('connected', `已连接后端 ${url}`)
        resolve()
      }
      ws.onerror = () => {
        // 具体失败原因浏览器不暴露；onclose 随后还会触发断开状态
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        reject(new Error(`后端连接错误：${url}（uvicorn 未启动或地址不对）`))
      }
      ws.onclose = () => {
        this.ws = null
        this.cb.onStatus('error', '后端已断开')
      }
      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const obj = JSON.parse(ev.data)
            if (obj.op === 'status') {
              // 状态映射：只有串口 connected 才整体算 connected；
              // connecting/reconnecting/disconnected 都是「链路还没好」，前端亮连接中
              const state: ConnState =
                obj.state === 'connected'
                  ? 'connected'
                  : obj.state === 'connecting' || obj.state === 'reconnecting'
                    ? 'connecting'
                    : obj.state === 'disconnected'
                      ? 'connecting' // 后端正在重连，不亮红灯
                      : obj.state === 'idle'
                        ? 'connecting' // 串口 idle 表示 WS 活着但车没接上
                        : 'error'
              this.cb.onStatus(state, obj.message ?? '')
            } else if (obj.op === 'alert') {
              // 固件回执异常（补发预算耗尽/无法结算等）：Response 帧本身走二进制
              // 透传、store 自己解析，这里是后端独有的决策结论，弹 toast 提示
              this.cb.onAlert?.({
                level: obj.level,
                cmd: obj.cmd,
                name: obj.name,
                text: obj.text,
              })
            } else if (obj.op === 'chart_suggest') {
              // Python 脚本推荐的曲线配置：交给 store 弹建议条（桌面端左下角）
              this.cb.onChartSuggest?.({
                id: obj.id,
                groups: obj.groups ?? [],
                source: obj.source ?? '',
                note: obj.note ?? '',
              })
            }
          } catch {
            /* 非 JSON 文本忽略 */
          }
        } else if (ev.data instanceof ArrayBuffer) {
          this.cb.onData(new Uint8Array(ev.data))
        }
      }
    })
  }

  async disconnect(): Promise<void> {
    // 主动断开抑制 onclose 上报 error，避免触发自动重连
    const ws = this.ws
    if (ws) {
      ws.onclose = null
      ws.close()
    }
    this.ws = null
    this.pending = []
    this.cb.onStatus('idle', '已断开后端')
  }

  async send(data: Uint8Array): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(this.toBuffer(data))
    } else if (this.ws?.readyState === WebSocket.CONNECTING) {
      // 连接建立中：排队等 onopen flush，不静默丢弃
      this.pending.push(data)
    } else {
      throw new Error('后端未连接')
    }
  }

  /**
   * WS.send 要求 BufferSource 必须落在 ArrayBuffer 上（TS 5.7 起 Uint8Array
   * 默认是 ArrayBufferLike，可能含 SharedArrayBuffer）：拷一份独立 ArrayBuffer
   */
  private toBuffer(data: Uint8Array): ArrayBuffer {
    const buf = new Uint8Array(data.length)
    buf.set(data)
    return buf.buffer
  }
}
