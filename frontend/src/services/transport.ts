/** 串口传输层统一接口：业务层只依赖它，不直接触碰 navigator.serial，便于切换演示源 */

export type ConnState = 'idle' | 'connecting' | 'connected' | 'error'

export interface TransportCallbacks {
  /** 连接状态变化（含人类可读的说明文本） */
  onStatus: (state: ConnState, message: string) => void
  /** 收到原始字节块（可能含半帧/粘包，由协议层 FrameParser 消化） */
  onData: (data: Uint8Array) => void
}

/**
 * 连接前的端口探测：打开候选口后发 frame，timeoutMs 内 ack(收到的字节) 为真，
 * 才认定这个口是目标口。V5 的下载口与 User 口 VID/PID 完全相同，Web Serial 又拿不到
 * 接口号/COM 名，只能靠协议应答来区分——回 Pong 的才是跑用户程序的 User 口。
 */
export interface PortProbe {
  /** 探测帧（V5 即心跳 Ping A5 FF FF） */
  frame: Uint8Array
  /** 累计收到的字节里是否出现了预期应答（V5 即 Pong A5 FF FF） */
  ack: (bytes: Uint8Array) => boolean
  /** 单个候选口的最长探测时长 */
 timeoutMs: number
}

export interface SerialTransport {
  readonly kind: 'webserial' | 'mock'
  /** 是否已具备可连接的端口（WebSerial 表示已授权，Mock 恒为 true） */
  isPortReady(): boolean
  /** 用户手势触发的选口动作（WebSerial 弹浏览器原生选择框） */
  pickPort(): Promise<void>
  /** 当前端口展示名 */
  readonly portLabel: string
  /** 打开端口并启动接收循环；CDC 虚拟串口的波特率仅为占位，不决定实际速率。
   *  带 probe 时会逐个打开已授权的同型候选口做协议探测，只在收到应答的口上建链 */
  connect(baudRate: number, probe?: PortProbe): Promise<void>
  /** 关闭端口、释放读写锁 */
  disconnect(): Promise<void>
  /** 发送一个已封好的二进制帧 */
  send(data: Uint8Array): Promise<void>
}
