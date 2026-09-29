/** 串口客户端单例：在 Web Serial 与演示传输之间切换，业务组件只与本对象打交道 */

import type { ConnState, SerialTransport, TransportCallbacks } from './transport'
import { WebSerialTransport } from './webSerialTransport'
import { MockTransport } from './mockTransport'

class SerialClient {
  /** 由 store 注册：状态变化回调 */
  onStatus: TransportCallbacks['onStatus'] | null = null
  /** 由 store 注册：收到原始字节回调 */
  onData: TransportCallbacks['onData'] | null = null

  state: ConnState = 'idle'
  private transport: SerialTransport | null = null
  private demo = false

  /** 当前环境是否支持 Web Serial */
  get supported(): boolean {
    return typeof navigator !== 'undefined' && 'serial' in navigator
  }

  get isDemo(): boolean {
    return this.demo
  }

  get connected(): boolean {
    return this.state === 'connected'
  }

  get portLabel(): string {
    return this.transport?.portLabel ?? '未选择串口'
  }

  /** 当前传输是否已有可直接连接的端口 */
  isPortReady(): boolean {
    return this.transport?.isPortReady() ?? false
  }

  private readonly cb: TransportCallbacks = {
    onStatus: (state, message) => {
      this.state = state
      this.onStatus?.(state, message)
    },
    onData: (data) => this.onData?.(data),
  }

  /** 应用启动：预选已授权设备 */
  async init(): Promise<void> {
    if (!this.supported) return
    const transport = new WebSerialTransport(this.cb)
    this.transport = transport
    if (await transport.preselect()) {
      this.cb.onStatus('idle', `检测到已授权设备：${transport.portLabel}，可直接连接`)
    }
  }

  /** 用户手势触发选口 */
  async pickPort(): Promise<void> {
    if (this.demo) return
    if (!this.supported) throw new Error('当前浏览器不支持 Web Serial，请使用 Chrome 或 Edge')
    if (this.transport?.kind !== 'webserial') {
      this.transport = new WebSerialTransport(this.cb)
    }
    await this.transport.pickPort()
    this.cb.onStatus('idle', `已选择 ${this.transport.portLabel}`)
  }

  async connect(baudRate: number): Promise<void> {
    if (!this.transport) throw new Error('请先选择串口')
    await this.transport.connect(baudRate)
  }

  async disconnect(): Promise<void> {
    await this.transport?.disconnect()
    this.cb.onStatus('idle', '已断开连接')
  }

  async send(data: Uint8Array): Promise<void> {
    await this.transport?.send(data)
  }

  /** 切换演示模式；若已连接先断开，避免串口占用 */
  async setDemoMode(on: boolean): Promise<void> {
    if (this.demo === on) return
    if (this.connected) await this.disconnect()
    this.demo = on
    this.transport = on ? new MockTransport(this.cb) : new WebSerialTransport(this.cb)
    if (!on && this.supported && this.transport instanceof WebSerialTransport) {
      await this.transport.preselect()
    }
    this.cb.onStatus('idle', on ? '演示模式（未连接）' : '未连接')
  }
}

export const serialClient = new SerialClient()
