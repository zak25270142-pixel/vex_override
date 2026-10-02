/** 串口客户端单例：在 Web Serial 与演示传输之间切换，业务组件只与本对象打交道 */

import type { ConnState, PortProbe, SerialTransport, TransportCallbacks } from './transport'
import { ConnectError, WebSerialTransport } from './webSerialTransport'
import { MockTransport } from './mockTransport'
import { loadPrefs, savePrefs } from './persist'
import { FRAME_HEAD, ping } from './protocol'

/** 口能打开但收不到 Pong 时的重试间隔：设备在、只是用户程序还没起来，尽快接住 */
const RETRY_MS = 1500

/**
 * 一个口都打不开（设备不在 / 被 VEXcode 或别的标签页占用）时的退避间隔：
 * 此时频繁敲门只会抢口，休息几秒给 VS Code 烧录、主控启动留窗口
 */
const RETRY_BUSY_MS = 7000

/** 单口探测时长：有线 USB，主控收到 Ping 最迟下一拍（10ms）就回 Pong，200ms 足够 */
const PROBE_TIMEOUT_MS = 200

/**
 * 识别 User 口的探测：发心跳 Ping，收到完整 Pong（A5 FF FF）才算这个口对。
 * 下载口跑 VEXos 自有协议，不会回这三个字节，借此把两个同 VID/PID 的口区分开。
 */
const userPortProbe: PortProbe = {
  frame: ping(),
  ack: (bytes) => {
    for (let i = 0; i + 2 < bytes.length; i++) {
      if (bytes[i] === FRAME_HEAD && bytes[i + 1] === 0xff && bytes[i + 2] === 0xff) return true
    }
    return false
  },
  timeoutMs: PROBE_TIMEOUT_MS,
}

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
  /** 最近一次连接使用的波特率，掉线/设备重新接入事件触发重试时沿用 */
  private lastBaud = 115200

  constructor() {
    // 设备断电恢复 / 重新插好 / 烧录完重启后，系统重新枚举完成才会发 connect 事件：
    // 这一下比盲轮询更贴时机（Windows 重新枚举本身有 1~3s 延迟），收到立刻试连一次
    if (this.supported) {
      navigator.serial.addEventListener('connect', this.handleDeviceBack)
    }
  }

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
      if (state === 'connected') {
        this.retryHinted = false
        // 连接过程中可能已挂着重试定时器，连上了立刻撤掉，避免成功后空转一拍
        this.clearRetry()
      }
      // 只对"重试过程中"反复出现的 connecting/error 去重，避免每 1.5s 刷屏；
      // 重试循环之外的（如意外断开）必须每次都转发，否则界面会一直停留在"已连接"
      if (this.retrying && (state === 'connecting' || state === 'error')) {
        const key = `${state}|${message}`
        if (key === this.lastForwarded) return
        this.lastForwarded = key
      }
      this.onStatus?.(state, message)
      // 已连接后中途掉线时 attempt() 早已结束，它的 finally 管不到这里，
      // 必须在掉线这一刻补挂重试循环，否则"断线自动重连"只会在初次连接失败时生效
      if (
        state === 'error' &&
        this.wantConnected &&
        !this.demo &&
        this.autoConnectEnabled &&
        !this.gestureBlocked
      ) {
        this.schedule(this.lastBaud)
      }
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
    this.lastBaud = baudRate
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
    this.lastBaud = baudRate
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

  /** 切换演示模式；若已连接先断开，避免串口占用。
      同模式但传输实例尚未安装（如启动时按持久化直接恢复演示）也会补装 */
  async setDemoMode(on: boolean): Promise<void> {
    if (this.demo === on && this.transport?.kind === (on ? 'mock' : 'webserial')) return
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
    let busy = false
    try {
      const t = this.transport
      if (t instanceof WebSerialTransport) {
        await t.reacquire()
        // 带 Ping 探测建链：下载口/User 口同 VID/PID，只在回 Pong 的口上落定
        await t.connect(baudRate, userPortProbe)
      } else if (t) {
        await t.connect(baudRate)
      }
    } catch (e) {
      gestureBlocked = isGestureError(e)
      // 一个口都打不开才长退避；能打开但没应答说明车在、程序没起，保持快重试
      busy = e instanceof ConnectError && e.reason === 'busy'
    } finally {
      this.retrying = false
      if (gestureBlocked) {
        this.gestureBlocked = true
        this.clearRetry()
        this.cb.onStatus('idle', '设备已就绪，请点击「连接」')
      } else if (this.wantConnected && !this.connected && !this.demo) {
        // 传输层的 error 回调可能已按默认间隔挂了一次重试，这里按失败原因换成正确节奏
        this.clearRetry()
        this.schedule(
          baudRate,
          busy ? RETRY_BUSY_MS : RETRY_MS,
          // 只在进入长退避时提示一次，说明接下来不是卡死而是在给烧录/启动让窗口
          busy ? `设备未接入或串口被占用，${RETRY_BUSY_MS / 1000}s 后再试（给烧录/启动留窗口）…` : undefined,
        )
      }
    }
  }

  /**
   * 浏览器通知设备已重新接入（connect 事件）。用户本意是要连着的，就立刻试一次，
   * 不等 1.5s 轮询节拍；自动连接开关只管轮询，"设备刚回来"本身就值得立即响应。
   * 手动断开（wantConnected=false）或正在尝试中则不插手。
   */
  private handleDeviceBack = (): void => {
    if (!this.wantConnected || this.connected || this.demo || this.retrying) return
    this.clearRetry()
    this.gestureBlocked = false
    void this.attempt(this.lastBaud)
  }

  /**
   * 挂下一次重试。delayMs 按失败原因分两档：设备不在/口被占用用长退避（让出烧录窗口），
   * 其余快重试；hint 非空时替换默认的"正在自动重连"提示。
   */
  private schedule(baudRate: number, delayMs = RETRY_MS, hint?: string): void {
    if (
      !this.wantConnected ||
      !this.autoConnectEnabled ||
      this.demo ||
      this.gestureBlocked ||
      this.retryTimer !== null
    ) {
      return
    }
    if (hint) {
      this.retryHinted = true
      this.cb.onStatus('connecting', hint)
    } else if (!this.retryHinted) {
      this.retryHinted = true
      this.cb.onStatus('connecting', '设备未连接或已断开，正在自动重连…')
    }
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null
      void this.attempt(baudRate)
    }, delayMs)
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }
}

/**
 * 单例挂到 globalThis 上复用：Vite HMR 热更新会重新执行本模块，若直接 new，
 * 旧实例仍持有打开的串口和读循环，新实例 open() 必然 already open，
 * 表现为"改了代码/刷新后怎么都连不上，非要重启 dev"。整页硬刷新时 globalThis 一并重置。
 */
const globalScope = globalThis as unknown as { __v5SerialClient?: SerialClient }
export const serialClient: SerialClient =
  globalScope.__v5SerialClient ?? (globalScope.__v5SerialClient = new SerialClient())