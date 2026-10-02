/** Web Serial API 传输实现（Chrome/Edge，navigator.serial），收发均为原始字节 */

import type { PortProbe, SerialTransport, TransportCallbacks } from './transport'
import type { PortIdentity } from './persist'

/**
 * 物理掉线后，个别 Chrome 版本上 reader.cancel() / writer.close() 可能迟迟不结束，
 * 给清理链每一步配超时兜底（超时时间单位：毫秒），绝不能让清理把下次 open() 永久卡死
 */
function bailAfter(p: Promise<unknown> | null | undefined, ms: number): Promise<void> {
  if (!p) return Promise.resolve()
  return Promise.race([
    p.then(
      () => undefined,
      () => undefined,
    ),
    new Promise<void>((resolve) => window.setTimeout(resolve, ms)),
  ])
}

/** 连接失败原因：决定重试节奏。
 *  busy=一个口都打不开（设备没插/被占用），该长退避；noanswer=口能打开但不回 Pong，
 *  说明设备在、用户程序还没跑起来，适合快速重试 */
export class ConnectError extends Error {
  constructor(
    readonly reason: 'busy' | 'noanswer',
    message: string,
  ) {
    super(message)
    this.name = 'ConnectError'
  }
}

export class WebSerialTransport implements SerialTransport {
  readonly kind = 'webserial' as const
  private port: SerialPort | null = null
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  private writer: WritableStreamDefaultWriter<Uint8Array> | null = null
  private keepReading = false
  private readTask: Promise<void> | null = null
  /** 正在进行的清理任务：多个入口（手动断开/掉线/重连）并发调用时共用一次清理 */
  private closeTask: Promise<void> | null = null
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
   * 重连前重新解析端口对象。这里只保证"手里有对象"，不决定用哪个口——
   * 同 VID/PID 的口有下载口/User 口两个，最终选谁由 connect() 的 Ping 探测说了算。
   * 已在枚举里的对象保持不动（上次探测命中的口优先复用，重连最快）；
   * 旧对象已随设备重插消失时，仅在两手空空时预选一个，其余交给探测逐个试。
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
    if (ports.length === 0) return this.port !== null
    if (this.port && ports.includes(this.port)) {
      this.rememberIdentity()
      return true
    }
    if (!this.port) {
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

  async connect(baudRate: number, probe?: PortProbe): Promise<void> {
    if (this.keepReading) return

    // 先清掉上一次可能残留的读写状态，否则二次 open 会抛 already open / Failed to open
    await this.closeStreams()

    const candidates = probe ? await this.listCandidates() : this.port ? [this.port] : []
    if (candidates.length === 0) {
      const msg = '未检测到已授权串口，请点「选口」选择设备'
      this.cb.onStatus('error', msg)
      throw new ConnectError('busy', msg)
    }
    this.cb.onStatus(
      'connecting',
      probe && candidates.length > 1
        ? `正在识别 V5 User 口（${candidates.length} 个候选）…`
        : `正在打开 ${this.labelOf(candidates[0]!)} …`,
    )

    // V5 User 口是 USB CDC 虚拟串口，baudRate 不决定实际速率，传值仅满足 API 要求
    const openOptions: SerialOptions = {
      baudRate,
      dataBits: 8,
      parity: 'none',
      stopBits: 1,
      flowControl: 'none',
    }

    let openedAny = false
    let lastError: unknown = null
    for (const cand of candidates) {
      this.port = cand
      try {
        await cand.open(openOptions)
      } catch (e) {
        // 常见于口被 VEXcode/另一个标签页占着：关掉本候选继续试下一个
        lastError = e
        await this.closeStreams()
        continue
      }
      openedAny = true
      if (probe) {
        let ok = false
        try {
          ok = await this.probeAnswers(cand, probe)
        } catch {
          ok = false
        }
        // 无论是否命中，探测用过的 reader/writer 都已随 cancel/close 报废（Web Serial 的
        // 流一次 open 只有一套，关了不能再生），所以先把口彻底关掉：
        //  · 不命中（下载口/程序没起）：立即释放，绝不占着影响烧录；
        //  · 命中：关掉再原地重新 open，拿到一套全新的流交给正式收发。
        await this.closeStreams()
        if (!ok) continue
        try {
          await cand.open(openOptions)
        } catch (e) {
          lastError = e
          await this.closeStreams()
          continue
        }
      }

      cand.addEventListener('disconnect', this.handleUnexpectedDisconnect)
      // 拉高 DTR/RTS：CDC 设备通常不依赖，但部分 USB 转串方案要它就绪才肯收发，
      // 对 V5 User 口无副作用，统一置位更保险。
      try {
        await cand.setSignals({ dataTerminalReady: true, requestToSend: true })
      } catch {
        /* 个别平台不支持 setSignals，忽略即可 */
      }
      this.rememberIdentity()
      this.keepReading = true
      this.readTask = this.readLoop()
      this.cb.onStatus('connected', `已连接 ${this.labelOf(cand)}`)
      return
    }

    // 兜底：正常路径下每个试过的口都已在循环里关闭，这里再收一次尾，
    // 保证"谁都没确认成 User 口"时绝不可能留下一个开着的口占住设备
    await this.closeStreams()

    // 能打开但谁都不回 Pong：多半是只授权了下载口；一个都打不开：被占用或系统未就绪
    const noAnswerMsg =
      '已打开串口但收不到主控应答：可能选到了下载口（请点「选口」改选 User 口），或主控程序还没运行'
    const busyMsg = `串口打开失败: ${(lastError as Error)?.message ?? '未知错误'}`
    const reason: 'busy' | 'noanswer' = probe && openedAny ? 'noanswer' : 'busy'
    const msg = reason === 'noanswer' ? noAnswerMsg : busyMsg
    this.cb.onStatus('error', msg)
    throw new ConnectError(reason, msg)
  }

  /** 某个端口对象的展示名（探测期间 this.port 会逐个换，不能用 portLabel 取值） */
  private labelOf(p: SerialPort): string {
    const info = p.getInfo()
    if (info.usbVendorId !== undefined) {
      const vid = info.usbVendorId.toString(16).padStart(4, '0')
      const pid = (info.usbProductId ?? 0).toString(16).padStart(4, '0')
      return `USB 串口 VID:${vid} PID:${pid}`
    }
    return 'USB 串口设备'
  }

  /**
   * 列出本次要试探的候选口：已授权设备里与记住身份同 VID/PID 的全部口
   * （V5 的下载口和 User 口同 VID/PID，会一起进来），上次用过的口排最前优先试。
   * 没有身份记录或一个都匹配不上时，退回全部已授权口。
   */
  private async listCandidates(): Promise<SerialPort[]> {
    let ports: SerialPort[] = []
    try {
      ports = await navigator.serial.getPorts()
    } catch {
      /* 查询失败：用手里现有对象 */
    }
    const id = this.identity
    const matched = id
      ? ports.filter((p) => {
          const info = p.getInfo()
          return info.usbVendorId === id.vid && (info.usbProductId ?? 0) === id.pid
        })
      : ports
    const list = matched.length > 0 ? matched : ports
    const cur = this.port
    return cur ? [cur, ...list.filter((p) => p !== cur)] : list
  }

  /**
   * 在已打开的候选口上发探测帧等应答。这里拿到的 reader/writer 退出时必然随
   * cancel/close 报废（Web Serial 一次 open 只给一套流），所以无论是否命中，
   * 调用方随后都必须先 port.close()：未命中直接释放，命中则重新 open 再正式建链。
   */
  private async probeAnswers(port: SerialPort, probe: PortProbe): Promise<boolean> {
    if (!port.readable || !port.writable) return false
    const reader = port.readable.getReader()
    const writer = port.writable.getWriter()
    const acc: number[] = []
    const writeProbe = async (): Promise<void> => {
      try {
        await writer.write(probe.frame)
      } catch {
        /* 口已失效：当成无应答处理 */
      }
    }
    await writeProbe()
    // 主控 10ms 一拍即回 Pong，窗口内再补发一次只是兜底某些 CDC 实现的首批丢失
    const rePing = window.setInterval(() => void writeProbe(), 100)
    type ReadResult = Awaited<ReturnType<typeof reader.read>>
    let readP: Promise<ReadResult> = reader.read()
    let timer = 0
    const timeout = new Promise<null>((resolve) => {
      timer = window.setTimeout(() => resolve(null), probe.timeoutMs)
    })
    let hit = false
    try {
      for (;;) {
        const fromRead = readP.then<ReadResult | null>((r) => r)
        const result = await Promise.race([fromRead, timeout])
        if (result === null) break
        if (result.done) {
          readP = reader.read()
          continue
        }
        if (result.value) {
          for (const b of result.value) acc.push(b)
          if (acc.length > 4096) acc.splice(0, acc.length - 4096)
          if (probe.ack(new Uint8Array(acc))) {
            hit = true
            break
          }
        }
        readP = reader.read()
      }
    } catch {
      hit = false
    } finally {
      window.clearInterval(rePing)
      window.clearTimeout(timer)
      // 先停写，再 cancel 让挂起的 read() 收尾，最后两把锁一起释放
      try {
        await writer.close()
      } catch {
        /* 忽略 */
      }
      try {
        writer.releaseLock()
      } catch {
        /* 忽略 */
      }
      try {
        await reader.cancel()
      } catch {
        /* 忽略 */
      }
      try {
        await readP
      } catch {
        /* 取消后的收尾异常忽略 */
      }
      try {
        reader.releaseLock()
      } catch {
        /* 忽略 */
      }
    }
    return hit
  }

  async disconnect(): Promise<void> {
    if (!this.port) return
    await this.closeStreams()
  }

  /**
   * 关闭读写流并释放端口。并发调用共用同一次清理（掉线事件与重连可能同时发生），
   * 清理对象在入口处固化为快照：清理进行期间 this.port 可能已被 reacquire 换成新口，
   * 绝不能回头把新口的流关掉。
   */
  private closeStreams(): Promise<void> {
    if (!this.closeTask) {
      const port = this.port
      this.closeTask = this.doCloseStreams(port).finally(() => {
        this.closeTask = null
      })
    }
    return this.closeTask
  }

  /**
   * 实际清理链。每步独立 try/catch：物理拔线后旧对象上的调用可能抛异常，
   * 但不能中断清理链，否则残留的读写锁会让下次 open() 失败；
   * 可能挂住不返回的步骤一律加超时，不能让整条链卡死后续重连。
   */
  private async doCloseStreams(port: SerialPort | null): Promise<void> {
    this.keepReading = false
    port?.removeEventListener('disconnect', this.handleUnexpectedDisconnect)
    await bailAfter(this.reader?.cancel(), 800)
    if (this.readTask) {
      await bailAfter(this.readTask, 800)
      this.readTask = null
    }
    try {
      this.reader?.releaseLock()
    } catch {
      /* 锁已释放或流已失效（cancel 超时未完成时也会抛，下一轮重试会再收拾） */
    }
    this.reader = null
    await bailAfter(this.writer?.close(), 800)
    try {
      this.writer?.releaseLock()
    } catch {
      /* 锁已释放或流已失效 */
    }
    this.writer = null
    try {
      if (port && (port.readable || port.writable)) await port.close()
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
    // 必须把 reader/writer/port 全套释放：不清的话设备重启后旧对象仍占着锁，
    // 新口 open() 会一直 Failed to open / already open。异步清理即可，重连前还会再等它一次
    void this.closeStreams()
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
