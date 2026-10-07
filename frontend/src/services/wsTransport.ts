/**
 * WebSocket 传输：对接本机 Python backend（ws://127.0.0.1:8000/ws）
 * 与 WebSerialTransport / MockTransport 实现同一契约，serialClient 可直接切换。
 *
 * 数据约定：
 *   - 后端 → 浏览器：ArrayBuffer 原样协议帧（进 FrameParser，store 零改动）
 *                   文本 JSON 仅连接状态 {"op":"status",...}
 *   - 浏览器 → 后端：ArrayBuffer 原样协议帧
 *
 * OPEN 之前发出的帧进入待发队列，连接一建立按序 flush，避免「连接中点击/自动拉表」丢命令。
 */

import type { ConnState, PortProbe, SerialTransport, TransportCallbacks } from './transport'

const DEFAULT_URL = 'ws://127.0.0.1:8000/ws'
const CONNECT_TIMEOUT_MS = 5000

export class WsTransport implements SerialTransport {
  readonly kind = 'ws' as const
  private ws: WebSocket | null = null
  /** CONNECTING 阶段积压的帧，onopen 时按序发出 */
  private pending: Uint8Array[] = []

  constructor(
    private cb: TransportCallbacks,
    private url = DEFAULT_URL,
  ) {}

  isPortReady(): boolean {
    // 后端模式无需选口授权，随时可连
    return true
  }

  async pickPort(): Promise<void> {
    // 后端固定地址，无口可选
  }

  get portLabel(): string {
    return this.url
  }

  async connect(_baudRate: number, _probe?: PortProbe): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return
    this.cb.onStatus('connecting', `连接后端 ${this.url} …`)
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(this.url)
      ws.binaryType = 'arraybuffer'
      let settled = false
      const timer = window.setTimeout(() => {
        if (settled) return
        settled = true
        ws.onopen = ws.onerror = null
        ws.close()
        reject(new Error('连接后端超时（确认 uvicorn 已启动）'))
      }, CONNECT_TIMEOUT_MS)

      ws.onopen = () => {
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        this.ws = ws
        // 连接建立前积压的请求（自动拉目录等）一次性按序补发
        for (const f of this.pending) ws.send(this.toBuffer(f))
        this.pending = []
        this.cb.onStatus('connected', `已连接后端 ${this.url}`)
        resolve()
      }
      ws.onerror = () => {
        // 具体失败原因浏览器不暴露；onclose 随后还会触发断开状态
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        reject(new Error('后端连接错误（uvicorn 未启动或地址不对）'))
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
              // 后端串口 idle 表示 WS 活着但车没接上，映射成连接中而不是断开
              const state: ConnState =
                obj.state === 'idle' ? 'connecting' : ((obj.state as ConnState) ?? 'error')
              this.cb.onStatus(state, obj.message ?? '')
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
