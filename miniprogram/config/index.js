// 全局常量与默认配置
module.exports = {
  // 后端默认地址：PC 移动热点网关固定 192.168.137.1（Windows 移动热点默认网段）
  DEFAULT_HOST: '192.168.137.1',
  DEFAULT_PORT: '8000',

  TICK_MS: 50, // UI 抽帧周期：20fps（与 frontend globle.ts 同思路，手机瓶颈在 setData）
  PING_MS: 80, // 心跳：协议规定 80ms 一发，主控 300ms 收不到会停推并收车
  PONG_TIMEOUT_MS: 600, // 本端在线判定余量（协议 300ms + 200ms 网络余量）
  TUN_TIMEOUT_MS: 2000, // 调参回显等待上限（与 frontend 一致）
  TOAST_MS: 3000, // toast 自动消失

  RECONNECT_FAST_MS: 1500, // 断线快重试
  RECONNECT_SLOW_MS: 7000, // 长退避
  RECONNECT_FAST_TIMES: 5,

  STORAGE_KEY: 'vex_mp_settings',
}
