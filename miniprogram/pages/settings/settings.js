/* 设置页逻辑 */
const store = require('../../store/index.js')
const cfg = require('../../config/index.js')

Page({
  data: {
    host: '',
    port: '',
    autoConnect: false,
    conn: { dot: 'idle', label: '未连接', msg: '' },
    stats: { frames: 0, bytes: 0, badChecksum: 0, unknownCmd: 0, overflow: 0 },
    testMsg: '',
  },

  onLoad() {
    this._unsub = store.subscribe(() => this._refresh())
    this._cj = ''
    this._sj = ''
    this._refresh()
  },

  onShow() {
    this._refresh()
  },

  onUnload() {
    if (this._unsub) this._unsub()
  },

  _refresh() {
    const patch = {}
    if (store.host !== this.data.host) patch.host = store.host
    if (store.port !== this.data.port) patch.port = store.port
    if (store.autoConnect !== this.data.autoConnect) patch.autoConnect = store.autoConnect
    const c = store.getConnView()
    const cj = JSON.stringify(c)
    if (cj !== this._cj) {
      patch.conn = c
      this._cj = cj
    }
    const s = store.getStats()
    const sj = JSON.stringify(s)
    if (sj !== this._sj) {
      patch.stats = s
      this._sj = sj
    }
    if (Object.keys(patch).length > 0) this.setData(patch)
  },

  onHostInput(e) {
    this.setData({ host: e.detail.value.trim() })
  },

  onPortInput(e) {
    this.setData({ port: e.detail.value.trim() })
  },

  onAutoSwitch(e) {
    const v = e.detail.value
    store.saveSettings(this.data.host, this.data.port, v)
    this.setData({ autoConnect: v })
  },

  onSaveConnect() {
    const host = this.data.host.trim()
    const port = this.data.port.trim()
    if (!host) {
      wx.showToast({ title: 'IP 不能为空', icon: 'none' })
      return
    }
    if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
      wx.showToast({ title: '端口需为 1~65535', icon: 'none' })
      return
    }
    this.setData({ testMsg: '' })
    store.saveSettings(host, port, this.data.autoConnect)
    store.reconnect()
  },

  onDisconnect() {
    store.disconnect()
  },

  /** GET /api/status：不经 WS 直接探后端活着没、串口连上没、目录到没 */
  onTestBackend() {
    const host = this.data.host.trim() || store.host
    const port = this.data.port.trim() || store.port
    wx.showLoading({ title: '探测中', mask: false })
    wx.request({
      url: 'http://' + host + ':' + port + '/api/status',
      method: 'GET',
      timeout: 3000,
      success: (res) => {
        wx.hideLoading()
        const d = res.data || {}
        if (!d || typeof d !== 'object') {
          this.setData({ testMsg: '返回异常：' + JSON.stringify(d) })
          return
        }
        const lines = [
          'HTTP 通：OK',
          '串口状态：' + (d.connected ? '已连接' : '未连接（' + (d.message || d.status || '?') + '）'),
          '目录项：监控 ' + (d.monitor_count || 0) + ' / 调参 ' + (d.tunable_count || 0) +
            ' / 命令 ' + (d.cmd_count || 0),
          '本机串口：' + ((d.ports || []).length + ' 个'),
        ]
        this.setData({ testMsg: lines.join('\n') })
      },
      fail: (err) => {
        wx.hideLoading()
        this.setData({
          testMsg: 'HTTP 不通（' + (err.errMsg || '请求失败') + '）。检查 IP/端口、PC 防火墙、uvicorn 是否 0.0.0.0 启动。',
        })
      },
    })
  },
})
