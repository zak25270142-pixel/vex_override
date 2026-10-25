/* 数据表页：监控表 + 调参表
 *
 * 渲染策略（手机性能约束）：
 *   - store 20fps 抽帧回调里做 JSON diff，无变化不 setData（断线/静止时零开销）
 *   - 监控值 100Hz 进非响应缓冲，这里最多 20fps 上屏
 */
const store = require('../../store/index.js')
const proto = require('../../utils/protocol.js')

Page({
  data: {
    tab: 'monitor', // monitor | tunable
    keyword: '',
    monitorRows: [],
    tunableRows: [],
    conn: { dot: 'idle', label: '未连接', msg: '', demo: false },
    toasts: [],
    stopReady: false,
    edit: {
      show: false,
      index: -1,
      name: '',
      unit: '',
      type: 0,
      typeText: '',
      kind: 'num', // num | bool | color
      input: '',
      boolValue: false,
    },
  },

  onLoad() {
    this._unsub = store.subscribe(() => this._refresh())
    this._mj = ''
    this._tj = ''
    this._cj = ''
    this._tsj = ''
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
    const m = store.getMonitorView(this.data.keyword)
    const mj = JSON.stringify(m)
    if (mj !== this._mj) {
      patch.monitorRows = m
      this._mj = mj
    }
    const t = store.getTunableView(this.data.keyword)
    const tj = JSON.stringify(t)
    if (tj !== this._tj) {
      patch.tunableRows = t
      this._tj = tj
    }
    const c = store.getConnView()
    const cj = JSON.stringify(c)
    if (cj !== this._cj) {
      patch.conn = c
      this._cj = cj
    }
    const ts = store.getToasts()
    const tsj = JSON.stringify(ts)
    if (tsj !== this._tsj) {
      patch.toasts = ts
      this._tsj = tsj
    }
    const sr = store.hasStopCmd()
    if (sr !== this.data.stopReady) patch.stopReady = sr
    if (Object.keys(patch).length > 0) this.setData(patch)
  },

  /* ---------- 分段 / 搜索 ---------- */

  onSwitchTab(e) {
    const t = e.currentTarget.dataset.t
    if (t !== this.data.tab) this.setData({ tab: t })
  },

  onKeyword(e) {
    this.setData({ keyword: e.detail.value })
  },

  /* ---------- 监控行：订阅档位 ---------- */

  onMonitorTap(e) {
    const idx = Number(e.currentTarget.dataset.index)
    const it = store.monitorDir.find((x) => x.index === idx)
    if (!it) return
    // str/other 无值宽：固件不推送也不接受订阅意图变更，登记为只读
    if (proto.valueSize(it.type) === 0) {
      wx.showToast({ title: '只读项，不可订阅', icon: 'none' })
      return
    }
    const sub = proto.tagSubscribed(it.tag)
    const fast = proto.tagFast(it.tag)
    let items
    if (!sub) items = ['订阅（高速）', '订阅（低速）']
    else if (!fast) items = ['升为高速', '退订']
    else items = ['降为低速', '退订']
    wx.showActionSheet({
      itemList: items,
      success: (res) => {
        const tap = res.tapIndex
        if (!sub) {
          store.setSubscription(idx, true, tap === 0)
        } else if (tap === 0) {
          store.setSubscription(idx, true, !fast)
        } else {
          store.setSubscription(idx, false, false)
        }
      },
    })
  },

  /* ---------- 调参行：编辑弹层 ---------- */

  onTunableTap(e) {
    const idx = Number(e.currentTarget.dataset.index)
    const it = store.tunableDir.find((x) => x.index === idx)
    if (!it) return
    // str/other 不可写入（固件同款拒绝），不弹编辑框
    if (proto.valueSize(it.type) === 0) {
      wx.showToast({ title: '只读项，不可编辑', icon: 'none' })
      return
    }
    const kind =
      it.type === proto.ValueType.Bool || it.type === proto.ValueType.OnOff
        ? 'bool'
        : it.type === proto.ValueType.Color
          ? 'color'
          : 'num'
    this.setData({
      edit: {
        show: true,
        index: it.index,
        name: it.name,
        unit: it.unit || '',
        type: it.type,
        typeText: proto.typeName(it.type),
        kind,
        input: kind === 'color' ? String(it.value) : proto.formatFloat32(Number(it.value)),
        boolValue: Number(it.value) !== 0,
      },
    })
  },

  onEditInput(e) {
    this.setData({ 'edit.input': e.detail.value })
  },

  onEditSwitch(e) {
    this.setData({ 'edit.boolValue': e.detail.value })
  },

  onCloseEdit() {
    this.setData({ 'edit.show': false })
  },

  onSubmitEdit() {
    const ed = this.data.edit
    let value
    if (ed.kind === 'bool') {
      value = ed.boolValue ? 1 : 0
    } else if (ed.kind === 'color') {
      value = ed.input.trim()
      if (!/^#?[0-9a-fA-F]{6}$/.test(value)) {
        wx.showToast({ title: '颜色需 6 位十六进制', icon: 'none' })
        return
      }
    } else {
      value = Number(ed.input)
      if (!Number.isFinite(value)) {
        wx.showToast({ title: '不是合法数值', icon: 'none' })
        return
      }
    }
    store.setTunable(ed.index, value)
    this.setData({ 'edit.show': false })
  },

  /* ---------- 常驻停车 ---------- */

  onStop() {
    // 急停：无确认、无二次点击，与 frontend 常驻停车条一致
    store.emergencyStop()
  },

  noop() {},
})
