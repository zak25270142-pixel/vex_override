/** 串口客户端单例：在 Web Serial 与演示传输之间切换，业务组件只与本对象打交道 */

import type { ConnState, SerialTransport, TransportCallbacks } from './transport'
import { WebSerialTransport } from './webSerialTransport'
import { MockTransport } from './mockTransport'
import { loadPrefs, savePrefs } from './persist'

/** 断线自动重连的间隔：太密会与主控枚举竞争，太疏体感迟钝 */
const RETRY_MS = 1500

/** 缺少用户手势导致的打开失败：静默重试没有意义，需请用户点一次「连接」 */
function isGestureError(e: unknown): boolean {
  return e instanceof DOMException && (e.name === 'SecurityError' || e.name === 'NotAllowedError')
}

class SerialClient {
  /** 由 store 注册：状态变化回调 */
  onStatus: TransportCallbacks['onStatus'] | null = null
  /** 由 store 注册：收到原始字节回调 */
  onData: TransportCallbacks['onData'] | null = null

  state: ConnState = 'idle'
  private transport: SerialTransport | null = null
  private demo = false
  /** 用户是否希望保持连接（手动连接或自动连接过，断开则清零） */
  private wantConnected = false
  /** 自动重连开关（顶栏"自动连接"勾选项）；关闭后不再重试 */
  private autoConnectEnabled = false
  /** 重试进行中，防止并发 open 同一个端口 */
  private retrying = false
  private retryTimer: number | null = null
  /** 本轮重试是否已提示过，避免刷屏 */
  private retryHinted = false
  /** 已遇到缺手势错误，暂停重试等待用户手动点一次 */
  private gestureBlocked = false
  /** 上次转发的状态文案，用于抑制重试期间的重复刷屏 */
  private lastForwarded = ''

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
      if (state === 'connected') this.retryHinted = false
      // 只对"重试过程中"反复出现的 connecting/error 去重，避免每 1.5s 刷屏；
      // 重试循环之外的（如意外断开）必须每次都转发，否则界面会一直停留在"已连接"
      if (this.retrying && (state === 'connecting' || state === 'error')) {
        const key = `${state}|${message}`
        if (key === this.lastForwarded) return
        this.lastForwarded = key
      }
      this.onStatus?.(state, message)
    },
    onData: (data) => this.onData?.(data),
  }

  /** 应用启动：预选已授权设备（优先持久化记住的那台） */
  async init(): Promise<void> {
    if (!this.supported) return
    this.autoConnectEnabled = loadPrefs().autoConnect
    const transport = new WebSerialTransport(this.cb)
    this.transport = transport
    transport.setIdentity(loadPrefs().port)
    if (await transport.preselect()) {
      const tip = transport.hasAmbiguousPort
        ? '（检测到多台同型设备，可点「选口」指定）'
        : '，可直接连接'
      this.cb.onStatus('idle', `检测到已授权设备：${transport.portLabel}${tip}`)
    }
  }

  /** 顶栏"自动连接"开关联动：关闭时立即停止重试 */
  setAutoConnect(on: boolean): void {
    this.autoConnectEnabled = on
    if (!on) {
      this.wantConnected = false
      this.clearRetry()
    }
  }

  /**
   * 页面加载时的自动连接：仅当存在已授权的端口、且不处于演示模式时尝试。
   * open() 不需要用户手势（只有 requestPort() 需要），故加载即连可行。
   */
  async autoConnect(baudRate: number): Promise<void> {
    if (this.demo) return
    if (!this.isPortReady()) {
      this.cb.onStatus('idle', '未检测到已授权串口，请点「选口」选择设备')
      return
    }
    this.wantConnected = true
    this.gestureBlocked = false
    await this.attempt(baudRate)
  }

  /** 用户手势触发选口 */
  async pickPort(): Promise<void> {
    if (this.demo) return
    if (!this.supported) throw new Error('当前浏览器不支持 Web Serial，请使用 Chrome 或 Edge')
    let t: WebSerialTransport
    if (this.transport instanceof WebSerialTransport) {
      t = this.transport
    } else {
      t = new WebSerialTransport(this.cb)
      this.transport = t
    }
    await t.pickPort()
    // 记住这台设备，下次启动优先选中
    if (t.portIdentity) savePrefs({ port: t.portIdentity })
    this.cb.onStatus('idle', `已选择 ${t.portLabel}`)
  }

  async connect(baudRate: number): Promise<void> {
    if (!this.transport) throw new Error('请先选择串口')
    this.wantConnected = true
    this.gestureBlocked = false
    this.retryHinted = false
    await this.attempt(baudRate)
  }

  async disconnect(): Promise<void> {
    this.wantConnected = false
    this.gestureBlocked = false
    this.clearRetry()
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
    this.wantConnected = false
    this.gestureBlocked = false
    this.clearRetry()
    this.demo = on
    this.transport = on ? new MockTransport(this.cb) : new WebSerialTransport(this.cb)
    if (!on && this.transport instanceof WebSerialTransport) {
      this.transport.setIdentity(loadPrefs().port)
      await this.transport.preselect()
    }
    this.cb.onStatus('idle', on ? '演示模式（未连接）' : '未连接')
  }

  /**
   * 一次连接尝试。失败不抛出：错误已由传输层上报，这里只决定是否继续重试。
   * 重试用递归 setTimeout 而非 setInterval——open() 可能挂起，interval 会并发开同一端口。
   */
  private async attempt(baudRate: number): Promise<void> {
    if (this.retrying) return
    this.retrying = true
    let gestureBlocked = false
    try {
      const t = this.transport
      if (t instanceof WebSerialTransport) {
        await t.reacquire()
        await t.connect(baudRate)
      } else if (t) {
        await t.connect(baudRate)
      }
    } catch (e) {
      gestureBlocked = isGestureError(e)
    } finally {
      this.retrying = false
      if (gestureBlocked) {
        this.gestureBlocked = true
        this.clearRetry()
        this.cb.onStatus('idle', '设备已就绪，请点击「连接」')
      } else if (this.wantConnected && !this.connected && !this.demo) {
        this.schedule(baudRate)
      }
    }
  }

  private schedule(baudRate: number): void {
    if (
      !this.wantConnected ||
      !this.autoConnectEnabled ||
      this.demo ||
      this.gestureBlocked ||
      this.retryTimer !== null
    ) {
      return
    }
    if (!this.retryHinted) {
      this.retryHinted = true
      this.cb.onStatus('connecting', '设备未连接或已断开，正在自动重连…')
    }
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null
      void this.attempt(baudRate)
    }, RETRY_MS)
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }
}

export const serialClient = new SerialClient()