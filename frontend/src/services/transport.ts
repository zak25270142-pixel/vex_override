/** 串口传输层统一接口：业务层只依赖它，不直接触碰 navigator.serial，便于切换演示源 */

export type ConnState = 'idle' | 'connecting' | 'connected' | 'error'

export interface TransportCallbacks {
  /** 连接状态变化（含人类可读的说明文本） */
  onStatus: (state: ConnState, message: string) => void
  /** 收到原始字节块（可能含半帧/粘包，由协议层 FrameParser 消化） */
  onData: (data: Uint8Array) => void
}

export interface SerialTransport {
  readonly kind: 'webserial' | 'mock'
  /** 是否已具备可连接的端口（WebSerial 表示已授权，Mock 恒为 true） */
  isPortReady(): boolean
  /** 用户手势触发的选口动作（WebSerial 弹浏览器原生选择框） */
  pickPort(): Promise<void>
  /** 当前端口展示名 */
  readonly portLabel: string
  /** 打开端口并启动接收循环；CDC 虚拟串口的波特率仅为占位，不决定实际速率 */
  connect(baudRate: number): Promise<void>
  /** 关闭端口、释放读写锁 */
  disconnect(): Promise<void>
  /** 发送一个已封好的二进制帧 */
  send(data: Uint8Array): Promise<void>
}
