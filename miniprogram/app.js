// VEX V5 数据显示上位机（小程序端）
// 协议第四端：与 include/communication.h、backend/app/protocol.py、
// frontend/src/services/protocol.ts 逐字节一致；协议改动必须四端同步。
//
// 链路：本小程序 --ws://PC局域网IP:端口/ws--> backend(FastAPI) --串口/USB--> V5 主控
// （未来 backend 可改走 TCP/UDP 连 Hi3861，小程序侧无感知）

const store = require('./store/index.js')

App({
  onLaunch() {
    store.init()
  },
  onShow() {
    // 切后台 WS 会被系统中断（fail interrupted），回前台按意愿重连
    store.onAppShow()
  },
})
