/** 工作区配置：通道元数据与业务常量集中在此（模板不硬编码业务数据） */

export interface ChannelMeta {
  id: string
  name: string
  color: string
}

/* ===== 曲线通道（固定4路，每路任选一个已订阅监听量） =====
   color 使用 CSS 变量；ECharts 挂载时解析为实际色值 */
export const CHANNELS: ChannelMeta[] = [
  { id: 'ch1', name: '通道1', color: 'var(--channel-1)' },
  { id: 'ch2', name: '通道2', color: 'var(--channel-2)' },
  { id: 'ch3', name: '通道3', color: 'var(--channel-3)' },
  { id: 'ch4', name: '通道4', color: 'var(--channel-4)' },
]

/* ===== 场地：VEX 标准场 6×6 格、每格 60cm，边长 360cm ===== */
export const FIELD_SIZE = 6
export const CELL_CM = 60
export const FIELD_CM = FIELD_SIZE * CELL_CM // 360

/* ===== 曲线缓冲与刷新 ===== */
export const TRACE_BUF_CAP = 18000 // 每条追踪曲线的环形缓冲容量（高速100Hz下约3分钟），图表显示与导出共用
export const PLOT_REFRESH_MS = 50 // 图表 20fps 批量刷新，避免逐点重绘
export const PLOT_WINDOW_S = 10 // X 轴默认滑动窗口

/** 主视图分页：参数表 / 曲线 / 场地图 / 视觉（预留） */
export type WorkView = 'params' | 'chart' | 'map' | 'vision'
