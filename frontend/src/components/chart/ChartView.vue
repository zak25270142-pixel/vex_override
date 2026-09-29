<template>
  <!-- 页面2：4 通道曲线。每通道任选一个已订阅监听量；分图 2×2 ⇄ 融合叠加比对 -->
  <div ref="rootEl" class="chart-view">
    <div class="chart-view__toolbar">
      <span class="chart-view__title">曲线监视</span>

      <!-- 4 个通道的监听量选择（只列已订阅的数值量） -->
      <div class="pickers">
        <label v-for="(ch, i) in CHANNELS" :key="ch.id" class="picker">
          <i class="picker__dot" :style="{ background: ch.color, boxShadow: `0 0 6px ${ch.color}` }" />
          <select :value="channelSel[i] ?? ''" @change="onPick(i, ($event.target as HTMLSelectElement).value)">
            <option value="">未选择</option>
            <option v-for="it in plottableItems" :key="it.index" :value="it.index">
              {{ it.name }}
            </option>
          </select>
        </label>
      </div>

      <div class="chart-view__switch">
        <button :class="{ 'chart-view__btn--on': !merged }" @click="merged = false">分图</button>
        <button :class="{ 'chart-view__btn--on': merged }" @click="merged = true">融合</button>
      </div>
    </div>

    <div class="chart-view__body">
      <div v-show="merged" ref="mergedEl" class="chart-view__merged" />
      <div v-show="!merged" class="chart-view__grid">
        <div
          v-for="(ch, i) in CHANNELS"
          :key="ch.id"
          :ref="(el) => setSepEl(el, i)"
          class="chart-view__cell"
        />
      </div>
      <!-- 无任何绑定 / 已订阅量时盖一层提示 -->
      <div v-if="boundCount === 0" class="chart-view__hint">
        <template v-if="!connected">尚未连接 V5，请在标题栏连接（或勾选“演示”）</template>
        <template v-else-if="plottableItems.length === 0">
          还没有可绘制的监听量，请先在「参数表-实参表」订阅低速/高速档位
        </template>
        <template v-else>请在上方为至少一个通道选择监听量</template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, type ComponentPublicInstance } from 'vue'
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
  channelSel,
  connected,
  getSeries,
  merged,
  monitorMap,
  plottableItems,
  plotWindowS,
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

const rootEl = ref<HTMLDivElement | null>(null)
const mergedEl = ref<HTMLDivElement | null>(null)
let mergedChart: ECharts | null = null
const sepCharts: ECharts[] = []
const sepEls: HTMLDivElement[] = []
let timer: ReturnType<typeof setInterval> | null = null
let resizeObs: ResizeObserver | null = null

const boundCount = computed(() => channelSel.filter((idx) => idx !== null).length)

function setSepEl(el: Element | ComponentPublicInstance | null, i: number) {
  if (el instanceof HTMLDivElement) sepEls[i] = el
}

function baseOption(extra: echarts.EChartsCoreOption): echarts.EChartsCoreOption {
  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 56, right: 18, top: 36, bottom: 32 },
    xAxis: {
      type: 'value',
      name: '时间(秒)',
      nameTextStyle: { color: chartTheme.axis },
      axisLabel: { color: chartTheme.axis },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value', // 各通道量纲不同，量程交给 ECharts 自动
      scale: true,
      axisLabel: { color: chartTheme.axis },
      axisLine: { lineStyle: { color: chartTheme.axis } },
      splitLine: { lineStyle: { color: chartTheme.grid } },
    },
    ...extra,
  }
}

/** 取一个通道当前要画的 [t, v] 点对；未绑定或暂无缓冲返回空 */
function pointsOf(i: number): [number, number][] {
  const idx = channelSel[i]
  if (idx === null) return []
  const buf = getSeries(idx)
  if (!buf || buf.t.length < 2) return []
  const t0 = Math.max(0, buf.t[buf.t.length - 1]! - plotWindowS.value)
  const out: [number, number][] = []
  for (let k = 0; k < buf.t.length; k++) {
    if (buf.t[k]! >= t0) out.push([buf.t[k]!, buf.v[k]!])
  }
  return out
}

function nameOf(i: number): string {
  const idx = channelSel[i]
  return idx === null || !monitorMap[idx] ? CHANNELS[i]!.name : monitorMap[idx]!.name
}

function refresh() {
  const bound = channelSel.map((idx, i) => ({ i, idx, data: pointsOf(i) }))

  sepCharts.forEach((chart, i) => {
    chart.setOption({
      title: { text: `${CHANNELS[i]!.name} · ${nameOf(i)}` },
      series: [{ data: bound[i]!.data }],
    })
  })

  const active = bound.filter((b) => b.idx !== null)
  mergedChart?.setOption({
    legend: { data: active.map((b) => nameOf(b.i)) },
    series: CHANNELS.map((_ch, i) => {
      const b = active.find((x) => x.i === i)
      return {
        name: nameOf(i),
        data: b ? b.data : [],
        // 未绑定通道在融合图里不显示
        lineStyle: { opacity: b ? 1 : 0 },
        itemStyle: { opacity: b ? 1 : 0 },
      }
    }),
  })
}

/** 选择项变化时立刻刷一帧，不必等下一个 50ms 节拍 */
function onPick(i: number, raw: string) {
  channelSel[i] = raw === '' ? null : Number(raw)
  refresh()
}

onMounted(() => {
  const styles = getComputedStyle(document.documentElement)
  chartTheme.axis = styles.getPropertyValue('--text-muted').trim()
  chartTheme.title = styles.getPropertyValue('--text-bright').trim()
  chartTheme.legend = styles.getPropertyValue('--text-secondary').trim()
  chartTheme.grid = styles.getPropertyValue('--chart-grid-line').trim()
  chartTheme.channels = CHANNELS.map((ch) => resolveCssColor(ch.color, styles))

  // 隐藏容器尺寸为 0，给兜底尺寸避免 ECharts 警告，显示后由 ResizeObserver 纠正
  const initChart = (el: HTMLDivElement): ECharts =>
    el.clientWidth > 0 && el.clientHeight > 0
      ? echarts.init(el)
      : echarts.init(el, undefined, { width: 640, height: 300 })

  if (mergedEl.value) {
    mergedChart = initChart(mergedEl.value)
    mergedChart.setOption(
      baseOption({
        title: {
          text: '融合通道数据比对',
          left: 10,
          top: 6,
          textStyle: { color: chartTheme.title, fontSize: 13 },
        },
        legend: { top: 6, right: 12, textStyle: { color: chartTheme.legend } },
        grid: { left: 56, right: 18, top: 56, bottom: 32 },
        series: CHANNELS.map((ch, i) => ({
          name: ch.name,
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 2, color: chartTheme.channels[i] },
          itemStyle: { color: chartTheme.channels[i] },
        })),
      }),
    )
  }

  sepEls.forEach((el, i) => {
    const ch = CHANNELS[i]!
    const chart = initChart(el)
    chart.setOption(
      baseOption({
        title: {
          text: ch.name,
          left: 10,
          top: 6,
          textStyle: { color: chartTheme.channels[i], fontSize: 12 },
        },
        series: [
          {
            type: 'line',
            showSymbol: false,
            lineStyle: { width: 2, color: chartTheme.channels[i] },
            itemStyle: { color: chartTheme.channels[i] },
          },
        ],
      }),
    )
    sepCharts.push(chart)
  })

  timer = setInterval(refresh, PLOT_REFRESH_MS)
  const resizeAll = () => {
    if (mergedEl.value && mergedEl.value.clientWidth > 0) mergedChart?.resize()
    sepCharts.forEach((c, i) => {
      if (sepEls[i]!.clientWidth > 0) c.resize()
    })
  }
  resizeObs = new ResizeObserver(resizeAll)
  if (rootEl.value) resizeObs.observe(rootEl.value)
  if (mergedEl.value) resizeObs.observe(mergedEl.value)
  sepEls.forEach((el) => resizeObs!.observe(el))
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  resizeObs?.disconnect()
  mergedChart?.dispose()
  sepCharts.forEach((c) => c.dispose())
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

.pickers {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.picker {
  display: flex;
  align-items: center;
  gap: 6px;
}

.picker__dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}

.picker select {
  height: 28px;
  max-width: 150px;
  font-size: 12.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
}

.picker select:focus {
  border-color: var(--accent);
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

.chart-view__merged {
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
