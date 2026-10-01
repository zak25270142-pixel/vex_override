/** Web Serial API 传输实现（Chrome/Edge，navigator.serial），收发均为原始字节 */

import type { SerialTransport, TransportCallbacks } from './transport'
import type { PortIdentity } from './persist'

export class WebSerialTransport implements SerialTransport {
  readonly kind = 'webserial' as const
  private port: SerialPort | null = null
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  private writer: WritableStreamDefaultWriter<Uint8Array> | null = null
  private keepReading = false
  private readTask: Promise<void> | null = null
  /** 上次成功使用的端口身份（VID/PID），用于多设备时优先选中同一台 V5 */
  private identity: PortIdentity | null = null
  /** 已授权设备中存在多个同型端口，无法区分 */
  private ambiguous = false

  constructor(private cb: TransportCallbacks) {}

  isPortReady(): boolean {
    return this.port !== null
  }

  /** 是否有多个同 VID/PID 的已授权设备（供上层提示用户手动选口） */
  get hasAmbiguousPort(): boolean {
    return this.ambiguous
  }

  /** 注入持久化记住的端口身份，需在 preselect() 之前调用 */
  setIdentity(id: PortIdentity | null): void {
    this.identity = id
  }

  get portIdentity(): PortIdentity | null {
    return this.identity
  }

  /** 页面加载时自动预选浏览器记住的已授权设备，实现刷新后一键回连 */
  async preselect(): Promise<boolean> {
    if (!('serial' in navigator)) return false
    const ports = await navigator.serial.getPorts()
    if (ports.length === 0) return false
    this.port = this.pickFrom(ports)
    this.rememberIdentity()
    return this.port !== null
  }

  /**
   * 重连前重新解析端口对象：设备重插后 Chrome 通常复用同一 SerialPort 但不保证，
   * 且断电期间 getPorts() 可能返回空数组，此时沿用旧对象继续尝试。
   * 静默循环里绝不能调 requestPort()（需要用户手势）。
   */
  async reacquire(): Promise<boolean> {
    if (!('serial' in navigator)) return this.port !== null
    let ports: SerialPort[] = []
    try {
      ports = await navigator.serial.getPorts()
    } catch {
      /* 查询失败：沿用旧对象 */
    }
    if (ports.length > 0) {
      const picked = this.pickFrom(ports)
      if (picked) this.port = picked
    }
    this.rememberIdentity()
    return this.port !== null
  }

  /** 优先选中与记住的身份一致的端口，否则退回第一个 */
  private pickFrom(ports: SerialPort[]): SerialPort | null {
    if (ports.length === 0) return null
    const id = this.identity
    if (id) {
      const hit = ports.filter((p) => {
        const info = p.getInfo()
        return info.usbVendorId === id.vid && (info.usbProductId ?? 0) === id.pid
      })
      if (hit.length > 0) {
        this.ambiguous = hit.length > 1
        return hit[0]!
      }
    }
    this.ambiguous = ports.length > 1
    return ports[0]!
  }

  private rememberIdentity(): void {
    const info = this.port?.getInfo()
    if (info && info.usbVendorId !== undefined) {
      this.identity = { vid: info.usbVendorId, pid: info.usbProductId ?? 0 }
    }
  }

  /** 必须在点击事件中调用 */
  async pickPort(): Promise<void> {
    if (!('serial' in navigator)) {
      throw new Error('当前浏览器不支持 Web Serial，请使用 Chrome 或 Edge')
    }
    this.port = await navigator.serial.requestPort()
    this.ambiguous = false
    this.rememberIdentity()
  }

  get portLabel(): string {
    if (!this.port) return '未选择串口'
    const info = this.port.getInfo()
    if (info.usbVendorId !== undefined) {
      const vid = info.usbVendorId.toString(16).padStart(4, '0')
      const pid = (info.usbProductId ?? 0).toString(16).padStart(4, '0')
      return `USB 串口 VID:${vid} PID:${pid}`
    }
    return 'USB 串口设备'
  }

  async connect(baudRate: number): Promise<void> {
    if (!this.port) throw new Error('请先选择串口')
    if (this.keepReading) return

    // 先清掉上一次可能残留的读写状态，否则二次 open 会抛 already open / Failed to open
    await this.closeStreams()

    this.cb.onStatus('connecting', `正在打开 ${this.portLabel} …`)

    try {
      // V5 User 口是 USB CDC 虚拟串口，baudRate 不决定实际速率，传值仅满足 API 要求
      await this.port.open({
        baudRate,
        dataBits: 8,
        parity: 'none',
        stopBits: 1,
        flowControl: 'none',
      })
    } catch (e) {
      this.cb.onStatus('error', `串口打开失败: ${(e as Error).message}`)
      throw e
    }

    this.port.addEventListener('disconnect', this.handleUnexpectedDisconnect)
    // 拉高 DTR/RTS：CDC 设备通常不依赖，但部分 USB 转串方案要它就绪才肯收发，
    // 对 V5 User 口无副作用，统一置位更保险。
    try {
      await this.port.setSignals({ dataTerminalReady: true, requestToSend: true })
    } catch {
      /* 个别平台不支持 setSignals，忽略即可 */
    }
    this.keepReading = true
    this.readTask = this.readLoop()
    this.cb.onStatus('connected', `已连接 ${this.portLabel}`)
  }

  async disconnect(): Promise<void> {
    if (!this.port) return
    await this.closeStreams()
  }

  /**
   * 关闭读写流并释放端口。每步独立 try/catch：物理拔线后旧对象上的调用可能抛异常，
   * 但不能中断清理链，否则残留的读写锁会让下次 open() 失败。
   */
  private async closeStreams(): Promise<void> {
    this.keepReading = false
    this.port?.removeEventListener('disconnect', this.handleUnexpectedDisconnect)
    try {
      await this.reader?.cancel()
    } catch {
      /* 关闭路径上的取消异常可忽略 */
    }
    if (this.readTask) {
      try {
        await this.readTask
      } catch {
        /* 读循环退出异常忽略 */
      }
      this.readTask = null
    }
    try {
      this.reader?.releaseLock()
    } catch {
      /* 锁已释放或流已失效 */
    }
    this.reader = null
    try {
      await this.writer?.close()
    } catch {
      /* 写端关闭异常忽略 */
    }
    try {
      this.writer?.releaseLock()
    } catch {
      /* 锁已释放或流已失效 */
    }
    this.writer = null
    try {
      if (this.port && (this.port.readable || this.port.writable)) await this.port.close()
    } catch {
      /* 物理断开后 close 可能抛错，忽略 */
    }
  }

  async send(data: Uint8Array): Promise<void> {
    if (!this.port?.writable || !this.keepReading) throw new Error('串口未连接')
    if (!this.writer) this.writer = this.port.writable.getWriter()
    await this.writer.write(data)
  }

  /** 通信中物理拔线 / 设备掉电 */
  private handleUnexpectedDisconnect = (): void => {
    this.keepReading = false
    this.cb.onStatus('error', '串口设备已断开（可能被拔出或 V5 断电）')
  }

  private async readLoop(): Promise<void> {
    const port = this.port
    if (!port?.readable) return
    try {
      this.reader = port.readable.getReader()
      while (this.keepReading) {
        const { value, done } = await this.reader.read()
        if (done) break
        if (value && value.length > 0) this.cb.onData(value)
      }
    } catch (e) {
      if (this.keepReading) {
        this.cb.onStatus('error', `串口读取异常: ${(e as Error).message}`)
      }
    }
  }
}
