<template>
  <!-- 曲线页：单通道 × 5 条轨迹
       布局：画布锁死上半屏（50%），下半屏 5 个槽位（选量 + 实时数值），各自独立滚动。
       时间窗口固定 10s —— 手机屏窄不给选择器，少一处误触。
       数据复用桌面端的 channelTraces[0]（与桌面「通道1」同一份状态，不另开一套），
       槽位上限见 globle.ts 的 TRACE_MAX。 -->
  <div class="m-chart">
    <div ref="chartEl" class="m-chart__canvas" />

    <div class="m-chart__cfg">
      <div v-if="!items.length" class="m-note">
        先到「数据 · 实参」订阅至少一个监控量，曲线才会出点。
      </div>

      <div v-for="(idx, slot) in slots" :key="slot" class="m-trace">
        <i class="m-trace__dot" :style="{ background: colors[slot] }" />
        <select
          class="m-trace__sel"
          :value="idx === undefined ? '' : String(idx)"
          @change="onPick(slot, $event)"
        >
          <option value="">未选择</option>
          <option v-for="it in items" :key="it.index" :value="String(it.index)">{{ it.name }}</option>
        </select>
        <span class="m-trace__val">{{ valueText(idx) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ECharts } from 'echarts/core'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { PLOT_REFRESH_MS, TRACE_BUF_CAP } from '@/config/workspace'
import {
  TRACE_MAX,
  channelTraces,
  formatValueCompact,
  getLatestT,
  getSeries,
  monitorLatest,
  monitorMap,
  pickTrace,
  plottableItems,
} from '@/stores/globle'

echarts.use([LineChart, GridComponent, CanvasRenderer])

/** 手机曲线页固定用「通道1」——与桌面共享同一份追踪状态 */
const CH = 0
/** 时间窗口固定 10s（不提供选择） */
const WINDOW_S = 10

const items = plottableItems
const traces = computed(() => channelTraces[CH] ?? [])
/** 恒定 TRACE_MAX 行：未配置的槽位是 undefined，渲染成空行便于补选 */
const slots = computed<(number | undefined)[]>(() =>
  Array.from({ length: TRACE_MAX }, (_, i) => traces.value[i]),
)

/** 槽位配色取 --channel-1..5，与桌面曲线槽位同色（手机页底部行即图例） */
const colors = ref<string[]>([])
function readColors(): void {
  const styles = getComputedStyle(document.documentElement)
  colors.value = Array.from(
    { length: TRACE_MAX },
    (_, i) => styles.getPropertyValue(`--channel-${i + 1}`).trim() || '#4fc3f7',
  )
}

const chartEl = ref<HTMLDivElement | null>(null)
let chart: ECharts | null = null
let timer: number | null = null

function cssColor(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** 取单条追踪曲线窗口内点；t 换算成"距最新样本秒数"（最新≈0）
    ⚠️ buf.t 与 getLatestT() 同为【秒】（globle.ts 的单位契约），别再改成毫秒 */
function tracePoints(index: number, windowS: number): [number, number][] {
  const buf = getSeries(index)
  if (!buf || buf.len < 2) return []
  const now = getLatestT()
  const t0 = now - windowS
  const out: [number, number][] = []
  const start = (buf.head - buf.len + TRACE_BUF_CAP) % TRACE_BUF_CAP
  for (let k = 0; k < buf.len; k++) {
    const pos = (start + k) % TRACE_BUF_CAP
    if (buf.t[pos]! >= t0) out.push([buf.t[pos]! - now, buf.v[pos]!])
  }
  return out
}

function initChart(): void {
  if (!chartEl.value) return
  const axis = cssColor('--text-muted') || '#8a8f9c'
  const grid = cssColor('--chart-grid-line') || '#23262f'
  chart = echarts.init(chartEl.value)
  chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    grid: { left: 48, right: 12, top: 14, bottom: 22 },
    xAxis: {
      type: 'value',
      name: '秒',
      nameTextStyle: { color: axis },
      axisLabel: { color: axis },
      axisLine: { lineStyle: { color: axis } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: axis },
      axisLine: { lineStyle: { color: axis } },
      splitLine: { lineStyle: { color: grid } },
    },
    // 固定 5 条 series：空槽 data 为空、不画图例（底部行就是图例）
    series: Array.from({ length: TRACE_MAX }, (_, slot) => ({
      type: 'line' as const,
      showSymbol: false,
      lineStyle: { width: 2, color: colors.value[slot] ?? '#4fc3f7' },
      itemStyle: { color: colors.value[slot] ?? '#4fc3f7' },
      data: [] as [number, number][],
    })),
  })
}

function refresh(): void {
  if (!chart || !chartEl.value) return
  // 尺寸对齐：任务面板/键盘/转屏都会改容器尺寸，变了才 resize（避免每拍重排）
  const el = chartEl.value
  if (el.clientWidth > 0 && el.clientHeight > 0) {
    if (chart.getWidth() !== el.clientWidth || chart.getHeight() !== el.clientHeight) chart.resize()
  }
  chart.setOption({
    xAxis: { min: -WINDOW_S, max: 0 },
    series: slots.value.map((idx) => ({
      data: idx === undefined ? [] : tracePoints(idx, WINDOW_S),
    })),
  })
}

function onPick(slot: number, e: Event): void {
  const raw = (e.target as HTMLSelectElement).value
  pickTrace(CH, slot, raw) // '' = 移除该槽
  refresh()
}

function valueText(idx: number | undefined): string {
  if (idx === undefined) return '—'
  const item = monitorMap[idx]
  const v = monitorLatest[idx]
  if (v === undefined) return '—'
  return item ? formatValueCompact(item, v) : String(v)
}

// 目录到达后默认给槽位0选第一个可绘图量（其余留空由用户补）
watch(
  items,
  (list) => {
    if (list.length > 0 && traces.value.length === 0) pickTrace(CH, 0, String(list[0]!.index))
  },
  { immediate: true },
)

onMounted(() => {
  readColors()
  initChart()
  timer = window.setInterval(refresh, PLOT_REFRESH_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  chart?.dispose()
})
</script>

<style scoped>
.m-chart {
  display: flex;
  flex-direction: column;
  height: 100%;
}
/* 画布固定上半屏：flex-basis 50% + flex-grow/shrink 0，任务面板弹起也不被挤变形 */
.m-chart__canvas {
  flex: 0 0 50%;
  min-height: 0;
  margin: 8px 12px;
  border: 1px solid var(--border-panel, #2a2e39);
  border-radius: 12px;
  background: var(--chart-surface, #0c1118);
}
.m-chart__cfg {
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 0 12px 12px;
}
.m-chart__cfg .m-note {
  padding: 4px 2px 10px;
}
/* 槽位行：色块(=曲线颜色) + 选量 + 实时数值 */
.m-trace {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.m-trace__dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 3px;
}
.m-trace__sel {
  flex: 1;
  min-width: 0;
  height: 38px;
  padding: 0 8px;
  font-size: 14px;
  font-family: inherit;
  color: var(--text-primary);
  background: #12141a;
  border: 1px solid var(--border-input, #2a2e39);
  border-radius: 10px;
  outline: none;
  box-sizing: border-box;
}
.m-trace__val {
  flex: none;
  width: 88px;
  text-align: right;
  font-size: 14px;
  font-family: var(--font-mono, monospace);
  color: var(--text-bright, #e8eaf0);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
