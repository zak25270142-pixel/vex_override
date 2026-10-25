/**
 * WS 传输层：小程序 ↔ backend FastAPI /ws
 *
 * 契约与 frontend/src/services/wsTransport.ts 完全一致：
 *   - 后端 → 小程序：ArrayBuffer 原样协议帧（喂 FrameParser）
 *                    文本 JSON：{"op":"status",...} / {"op":"alert",...}
 *   - 小程序 → 后端：ArrayBuffer 原样协议帧
 *
 * 局域网 IP 免域名校验（官方文档）：wx.connectSocket 允许 ws://手机同网段IP:端口，
 * 不需要配置 socket 合法域名、不要求 wss。手机与 PC 在同一热点网段即通。
 *
 * 与 TS 版的差异（小程序 API）：
 *   - new WebSocket → wx.connectSocket + wx.onSocket* 系列回调
 *   - fetch → wx.request（POST /api/connect 让后端去连下位机）
 *   - OPEN 前积压的帧在 onOpen 按序 flush（与 TS 版同策略）
 */

const cfg = require('../config/index.js')

// 连接状态：idle 未连接 / connecting 连接中（含重连等待）/ connected 已连接 / error 异常
const ConnState = Object.freeze({
  IDLE: 'idle',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  ERROR: 'error',
})

class WsClient {
  /**
   * @param handlers {
   *   onStateChange(state, msg),   // 本端连接状态变化
   *   onStatusJson(obj),           // 后端 op=status 带外消息
   *   onAlertJson(obj),            // 后端 op=alert 带外消息
   *   onBinary(bytes),             // 协议帧
   *   getUrl()                     // 当前应连接的 ws:// 地址（每次重连时重取，允许热改）
   * }
   */
  constructor(handlers) {
    this.h = handlers
    this.wsOpen = false
    this.wsConnecting = false
    this.pending = [] // CONNECTING 阶段积压的帧，onOpen 时按序发出
    this.wantConnected = false
    this.retryCount = 0
    this.retryTimer = null
    this.closedByUser = false
    this._bound = false
  }

  /** 注册全局 socket 回调（小程序 onSocket* 是全局单例，只绑一次） */
  _bind() {
    if (this._bound) return
    this._bound = true

    wx.onSocketOpen(() => {
      this.wsOpen = true
      this.wsConnecting = false
      this.retryCount = 0
      this._clearRetry()
      // 连接建立前积压的帧一次性按序补发
      for (let i = 0; i < this.pending.length; i++) {
        this._rawSend(this.pending[i])
      }
      this.pending = []
      this.h.onStateChange(ConnState.CONNECTED, '后端已连接')
    })

    wx.onSocketMessage((res) => {
      if (typeof res.data === 'string') {
        try {
          const obj = JSON.parse(res.data)
          if (obj && obj.op === 'status') {
            this.h.onStatusJson(obj)
          } else if (obj && obj.op === 'alert') {
            this.h.onAlertJson(obj)
          }
        } catch (e) {
          /* 非 JSON 文本忽略 */
        }
      } else if (res.data instanceof ArrayBuffer) {
        this.h.onBinary(new Uint8Array(res.data))
      }
    })

    wx.onSocketClose(() => {
      this.wsOpen = false
      this.wsConnecting = false
      if (this.closedByUser) {
        this.closedByUser = false
        this.h.onStateChange(ConnState.IDLE, '已断开')
        return
      }
      this.h.onStateChange(ConnState.CONNECTING, '后端连接中断，重连中…')
      this._scheduleRetry()
    })

    wx.onSocketError((res) => {
      // 具体原因不暴露；onSocketClose 随后一定到，重连在那里排
      this.wsOpen = false
      this.wsConnecting = false
      if (!this.wantConnected) {
        this.h.onStateChange(ConnState.ERROR, (res && res.errMsg) || '后端连接失败')
      }
    })
  }

  /** 连接（或按新地址重连）；已打开则直接返回 */
  connect() {
    this.wantConnected = true
    if (this.wsOpen || this.wsConnecting) return
    this._bind()
    const url = this.h.getUrl()
    this.wsConnecting = true
    this.h.onStateChange(ConnState.CONNECTING, '连接后端 ' + url + ' …')
    // 先让后端去连下位机（串口探测阻塞在后端重连线程里，这里不等结果）
    this._askBackendConnect()
    // 超时在 app.json networktimeout.connectSocket 配（10s）
    wx.connectSocket({ url })
  }

  /** 用户主动断开：抑制 onClose 的重连上报 */
  disconnect() {
    this.wantConnected = false
    this._clearRetry()
    this.closedByUser = true
    if (this.wsOpen || this.wsConnecting) {
      wx.closeSocket()
    }
    this.wsOpen = false
    this.wsConnecting = false
    this.pending = []
    this.h.onStateChange(ConnState.IDLE, '已主动断开')
  }

  /** 发一帧；未打开时：连接中入队积压，否则抛错 */
  send(bytes) {
    if (this.wsOpen) {
      this._rawSend(bytes)
    } else if (this.wsConnecting) {
      this.pending.push(bytes)
    } else {
      throw new Error('后端未连接')
    }
  }

  isOpen() {
    return this.wsOpen
  }

  _rawSend(bytes) {
    // 拷一份独立 ArrayBuffer，避免视图偏移/共享缓冲带来的边界问题
    const buf = new Uint8Array(bytes.length)
    buf.set(bytes)
    wx.sendSocketMessage({ data: buf.buffer })
  }

  _askBackendConnect() {
    const httpBase = this.h.getHttpBase()
    wx.request({
      url: httpBase + '/api/connect',
      method: 'POST',
      timeout: 3000,
      fail: () => {
        // 后端还没起来/网络闪断时静默，WS 建链失败会走正常 error 上报
      },
    })
  }

  _scheduleRetry() {
    if (!this.wantConnected) return
    this._clearRetry()
    const delay =
      this.retryCount < cfg.RECONNECT_FAST_TIMES ? cfg.RECONNECT_FAST_MS : cfg.RECONNECT_SLOW_MS
    this.retryCount++
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      if (!this.wantConnected) return
      this.connect()
    }, delay)
  }

  _clearRetry() {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }
}

module.exports = { WsClient, ConnState }
