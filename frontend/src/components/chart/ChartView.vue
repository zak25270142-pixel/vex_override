<template>
  <!-- 页面2：4 通道曲线。点击分图选中通道（右侧配置追踪项与坐标轴），大图模式只放大当前选中通道 -->
  <div class="chart-view">
    <div class="chart-view__toolbar">
      <span class="chart-view__title">曲线监视</span>
      <span v-if="focusCh !== null" class="chart-view__focus">
        当前：通道{{ focusCh + 1 }}（{{ channelTraces[focusCh]!.length }}/4 条曲线）
      </span>
      <span class="chart-view__tip">点击分图选中通道，在右侧添加追踪曲线（每通道最多 4 条）</span>

      <!-- 大图模式曲线占满宽，配置改为浮层，按需呼出 -->
      <button
        v-if="bigView"
        class="chart-view__drawer"
        :disabled="focusCh === null"
        @click="scopeDrawer = !scopeDrawer"
      >
        通道设置
      </button>

      <div class="chart-view__switch">
        <button :class="{ 'chart-view__btn--on': !bigView }" @click="bigView = false">分图</button>
        <button :class="{ 'chart-view__btn--on': bigView }" @click="bigView = true">大图</button>
      </div>
    </div>

    <div class="chart-view__body">
      <div v-show="bigView" ref="bigEl" class="chart-view__big" />
      <div v-show="!bigView" class="chart-view__grid">
        <div
          v-for="(ch, i) in CHANNELS"
          :key="ch.id"
          :ref="(el) => setSepEl(el, i)"
          class="chart-view__cell"
          :class="{ 'is-focus': focusCh === i }"
          @click="focusCh = i"
        />
      </div>
      <!-- 不同状态下的引导提示（不挡点击） -->
      <div v-if="hint" class="chart-view__hint">{{ hint }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, type ComponentPublicInstance } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ECharts } from 'echarts/core'
import { CHANNELS, PLOT_REFRESH_MS } from '@/config/workspace'
import {
  bigView,
  channelCfg,
  channelTraces,
  connected,
  focusCh,
  getSeries,
  monitorMap,
  plottableItems,
  scopeDrawer,
} from '@/stores/globle'

echarts.use([LineChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer])

/** ECharts 在 canvas 中，不能直接消费 CSS 变量，挂载时从 :root 解析为实际色值 */
const chartTheme = {
  axis: '',
  title: '',
  legend: '',
  grid: '',
  channels: [] as string[],
}

function resolveCssColor(raw: string, styles: CSSStyleDeclaration): string {
  const m = raw.match(/^var\((--[\w-]+)\)$/)
  return m ? styles.getPropertyValue(m[1]!).trim() : raw
}

const bigEl = ref<HTMLDivElement | null>(null)
let bigChart: ECharts | null = null
/** 分图实例延迟创建：挂载时容器在 v-show 下尺寸为 0，等首次有真实尺寸再 init */
const sepCharts: (ECharts | null)[] = [null, null, null, null]
const sepEls: HTMLDivElement[] = []
let timer: ReturnType<typeof setInterval> | null = null

const SLOTS = [0, 1, 2, 3]

/** 盖层提示：连接 / 订阅 / 选中 / 加曲线 的分步引导 */
const hint = computed(() => {
  if (!connected.value) return '尚未连接 V5，请在标题栏连接（或勾选“演示”）'
  if (plottableItems.value.length === 0)
    return '还没有可绘制的监听量，请先在「参数表-实参表」订阅低速/高速档位'
  if (bigView.value && focusCh.value === null) return '请先在分图中点击一个通道，再查看大图'
  if (focusCh.value === null) return '点击任一分图选中通道，然后在右侧面板添加追踪曲线'
  if (channelTraces[focusCh.value]!.length === 0)
    return '该通道还没有追踪曲线，请在右侧面板添加（每通道最多 4 条）'
  return ''
})

function setSepEl(el: Element | ComponentPublicInstance | null, i: number) {
  if (el instanceof HTMLDivElement) sepEls[i] = el
}

function baseOption(extra: echarts.EChartsCoreOption): echarts.EChartsCoreOption {
  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 54, right: 14, top: 40, bottom: 30 },
    xAxis: {
      type: 'value',
      name: '秒（右0=现在）',
      nameTextStyle: { color: chartTheme.axis },
      axisLabel: { color: chartTheme.axis },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value', // 手动/自动量程由每拍 setOption 覆盖 min/max/scale
      scale: true,
      axisLabel: { color: chartTheme.axis },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      splitLine: { lineStyle: { color: chartTheme.grid } },
    },
    ...extra,
  }
}

/** 取某条追踪曲线当前窗口内的点；t 换算成"距现在的秒数"（最新点≈0） */
function tracePoints(index: number | undefined, windowS: number): [number, number][] {
  if (index === undefined) return []
  const buf = getSeries(index)
  if (!buf || buf.t.length < 2) return []
  const now = performance.now() / 1000
  const t0 = now - windowS
  const out: [number, number][] = []
  for (let k = 0; k < buf.t.length; k++) {
    if (buf.t[k]! >= t0) out.push([buf.t[k]! - now, buf.v[k]!])
  }
  return out
}

function traceName(index: number | undefined): string {
  if (index === undefined) return ''
  return monitorMap[index]?.name ?? `#${index}`
}

/** 把一个通道（4 槽）的数据与坐标轴配置刷到指定图实例 */
function applyChannel(chart: ECharts, ch: number) {
  const cfg = channelCfg[ch]!
  const traces = channelTraces[ch]!
  const names = traces.map((idx) => traceName(idx))
  chart.setOption({
    title: { text: `通道${ch + 1}` },
    legend: { data: names },
    xAxis: { min: -cfg.windowS, max: 0 },
    yAxis: {
      scale: cfg.yAuto,
      min: cfg.yAuto ? null : cfg.yMin,
      max: cfg.yAuto ? null : cfg.yMax,
    },
    series: SLOTS.map((slot) => {
      const idx = traces[slot]
      const active = idx !== undefined
      return {
        name: names[slot] ?? '',
        data: tracePoints(idx, cfg.windowS),
        // 空槽透明不画线，也不在图例中出现（legend.data 只给了有效名）
        lineStyle: { opacity: active ? 1 : 0 },
        itemStyle: { opacity: active ? 1 : 0 },
      }
    }),
  })
}

/** 每个图固定 4 条 series（槽位颜色=通道配色），数据由 applyChannel 覆盖 */
function seriesInit(fontSize: number) {
  return SLOTS.map((slot) => ({
    name: '',
    type: 'line' as const,
    showSymbol: false,
    lineStyle: { width: 2, color: chartTheme.channels[slot], opacity: 0 },
    itemStyle: { color: chartTheme.channels[slot], opacity: 0 },
  }))
}

/** 尺寸对齐兼延迟初始化：v-show 切页/分图大图切换/右栏伸缩都会改变容器尺寸，
    每拍检查——实例未建且容器已有尺寸就建，已建但尺寸不符就 resize，隐藏（尺寸0）跳过 */
function fitCharts(): void {
  sepEls.forEach((el, i) => {
    if (el.clientWidth === 0 || el.clientHeight === 0) return
    if (!sepCharts[i]) {
      const chart = echarts.init(el)
      chart.setOption(
        baseOption({
          title: {
            text: `通道${i + 1}`,
            left: 10,
            top: 6,
            textStyle: { color: chartTheme.title, fontSize: 12 },
          },
          legend: {
            top: 6,
            right: 8,
            itemWidth: 12,
            itemHeight: 8,
            textStyle: { color: chartTheme.legend, fontSize: 10.5 },
          },
          series: seriesInit(10.5),
        }),
      )
      sepCharts[i] = chart
    } else {
      const chart = sepCharts[i]!
      if (chart.getWidth() !== el.clientWidth || chart.getHeight() !== el.clientHeight) {
        chart.resize()
      }
    }
  })

  const el = bigEl.value
  if (el && el.clientWidth > 0 && el.clientHeight > 0) {
    if (!bigChart) {
      bigChart = echarts.init(el)
      bigChart.setOption(
        baseOption({
          title: { text: '', left: 10, top: 6, textStyle: { color: chartTheme.title, fontSize: 13 } },
          legend: { top: 6, right: 12, textStyle: { color: chartTheme.legend, fontSize: 12 } },
          grid: { left: 60, right: 20, top: 48, bottom: 34 },
          series: seriesInit(12),
        }),
      )
    } else if (bigChart.getWidth() !== el.clientWidth || bigChart.getHeight() !== el.clientHeight) {
      bigChart.resize()
    }
  }
}

function refresh() {
  fitCharts()
  sepCharts.forEach((chart, i) => {
    if (chart) applyChannel(chart, i)
  })
  if (focusCh.value !== null && bigChart) {
    applyChannel(bigChart, focusCh.value)
  } else if (bigChart) {
    // 未选通道：大图清空
    bigChart.setOption({
      title: { text: '' },
      legend: { data: [] },
      series: SLOTS.map(() => ({ data: [], lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 } })),
    })
  }
}

/** 选中通道 / 切换模式 / 改配置 / 增删曲线时立即补一帧，不等 50ms 节拍 */
watch([focusCh, bigView], () => refresh())
watch(channelCfg, () => refresh(), { deep: true })
watch(channelTraces, () => refresh(), { deep: true })
// 切换选中通道时收起大图模式下的配置浮层
watch(focusCh, () => {
  scopeDrawer.value = false
})

onMounted(() => {
  // ECharts 在 canvas 中不能消费 CSS 变量，挂载时从 :root 解析一次实际色值
  const styles = getComputedStyle(document.documentElement)
  chartTheme.axis = styles.getPropertyValue('--text-muted').trim()
  chartTheme.title = styles.getPropertyValue('--text-bright').trim()
  chartTheme.legend = styles.getPropertyValue('--text-secondary').trim()
  chartTheme.grid = styles.getPropertyValue('--chart-grid-line').trim()
  chartTheme.channels = CHANNELS.map((ch) => resolveCssColor(ch.color, styles))

  // 图实例由 fitCharts 在容器首次有尺寸时延迟创建；20fps 节拍同时负责尺寸对齐
  timer = setInterval(refresh, PLOT_REFRESH_MS)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  bigChart?.dispose()
  sepCharts.forEach((c) => c?.dispose())
})
</script>

<style scoped>
.chart-view {
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.chart-view__toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 34px;
  padding: 0 4px;
  flex-shrink: 0;
}

.chart-view__title {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  color: var(--text-secondary);
  white-space: nowrap;
}

.chart-view__focus {
  font-size: 12px;
  font-weight: 700;
  color: var(--accent);
  text-shadow: var(--glow-accent);
  white-space: nowrap;
}

.chart-view__tip {
  flex: 1;
  font-size: 11.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 大图模式下呼出配置浮层的按钮 */
.chart-view__drawer {
  flex-shrink: 0;
  height: 26px;
  padding: 0 12px;
  font-size: 12px;
  font-family: inherit;
  font-weight: 700;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  border-radius: var(--radius);
  text-shadow: 0 0 8px rgba(79, 224, 255, 0.35);
  cursor: pointer;
}

.chart-view__drawer:hover:not(:disabled) {
  background: rgba(79, 224, 255, 0.24);
}

.chart-view__drawer:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.chart-view__switch {
  display: flex;
  border: var(--border-subtle);
  border-radius: var(--radius);
  overflow: hidden;
  flex-shrink: 0;
}

.chart-view__switch button {
  padding: 4px 16px;
  font-size: 12.5px;
  color: var(--text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
}

.chart-view__switch button + button {
  border-left: var(--border-subtle);
}

.chart-view__switch button.chart-view__btn--on {
  color: var(--text-on-accent);
  background: var(--accent);
  font-weight: 700;
}

.chart-view__body {
  position: relative;
  flex: 1;
  min-height: 0;
  display: grid;
}

.chart-view__big {
  width: 100%;
  height: 100%;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--chart-surface);
  overflow: hidden;
}

.chart-view__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  gap: 10px;
  width: 100%;
  height: 100%;
  min-height: 0;
}

.chart-view__cell {
  min-width: 0;
  min-height: 0;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--chart-surface);
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.12s, box-shadow 0.12s;
}

.chart-view__cell:hover {
  border-color: var(--accent-border);
}

/* 当前选中通道：青色描边发光 */
.chart-view__cell.is-focus {
  border-color: var(--accent);
  box-shadow: 0 0 12px rgba(79, 224, 255, 0.35), inset 0 0 12px rgba(79, 224, 255, 0.08);
}

.chart-view__hint {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  text-align: center;
  font-size: 15px;
  color: var(--text-muted);
  background: rgba(7, 25, 45, 0.55);
  pointer-events: none;
}
</style>
