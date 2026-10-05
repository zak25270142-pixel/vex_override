<template>
  <!-- 实参详情：选中监控量后占满标签内容区 100% 高度，显示 5s 曲线 + 实时值 + 订阅挡位。
       未选中时不渲染（由父组件 v-if 控制） -->
  <section class="mon-detail">
    <header class="mon-detail__head">
      <span class="mon-detail__name">{{ item!.name }}</span>
      <span class="mon-detail__idx">#{{ item!.index }}</span>
      <button class="mon-detail__back" @click="selectMonitor(null)">‹ 返回</button>
    </header>

    <div class="mon-detail__body">
      <!-- 实时值 -->
      <div class="live">
        <span class="live__num">{{ formatValue(item!, liveValue) }}</span>
        <i v-if="item!.unit" class="live__unit">{{ item!.unit }}</i>
      </div>

      <!-- 5s 曲线 -->
      <div class="chart">
        <canvas ref="canvasEl" class="chart__canvas" />
        <span v-if="!subscribed" class="chart__empty">先订阅后再看波形</span>
      </div>

      <!-- 订阅挡位（位姿 X/Y/Yaw 同样可订阅/退订） -->
      <div class="sub">
        <template v-if="!subscribed">
          <span class="sub-state sub-state--off"><i />未订阅</span>
          <button class="sub-btn sub-btn--slow" @click="onSub(false)">订阅低速</button>
          <button class="sub-btn sub-btn--fast" @click="onSub(true)">订阅高速</button>
        </template>
        <template v-else-if="fast">
          <span class="sub-state sub-state--fast"><i />高速</span>
          <button class="sub-btn sub-btn--off" @click="onUnsub">退订</button>
          <button class="sub-btn sub-btn--slow" @click="onSub(false)">降级低速</button>
        </template>
        <template v-else>
          <span class="sub-state sub-state--slow"><i />低速</span>
          <button class="sub-btn sub-btn--off" @click="onUnsub">退订</button>
          <button class="sub-btn sub-btn--fast" @click="onSub(true)">升级高速</button>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { tagFast, tagSubscribed, type CellValue } from '@/services/protocol'
import {
  formatValue,
  getMiniSeries,
  monitorLatest,
  selectedMonitorItem,
  selectMonitor,
  setSubscription,
} from '@/stores/globle'

const item = selectedMonitorItem

const liveValue = computed<CellValue>(() => {
  const it = item.value
  if (!it) return ''
  return monitorLatest[it.index] ?? it.value
})

const subscribed = computed(() => (item.value ? tagSubscribed(item.value.tag) : false))
const fast = computed(() => (item.value ? tagFast(item.value.tag) : false))

/* ===== 5s 曲线：纯 canvas 折线 ===== */
const canvasEl = ref<HTMLCanvasElement | null>(null)
let raf = 0

/* ===== 纵轴量程：数据居中、上下各留 50% 余量；
   余量被吃到只剩 10% 边带（或数据缩得太小）时才一次性重标定，平时不动轴 ===== */
let axisMin = NaN
let axisMax = NaN

/** 主题色只取一次，避免每帧读 computedStyle */
let themeColors: { accent: string; muted: string } | null = null
function getThemeColors() {
  if (!themeColors) {
    const cs = getComputedStyle(document.documentElement)
    themeColors = {
      accent: cs.getPropertyValue('--accent').trim() || '#4fe0ff',
      muted: cs.getPropertyValue('--text-muted').trim() || '#5f91ad',
    }
  }
  return themeColors
}

/** 以数据中点为心、上下各留一个数据跨度，数据只占中间一半高度 */
function resetAxis(dataMin: number, dataMax: number): void {
  const span = Math.max(dataMax - dataMin, 1e-9)
  const mid = (dataMin + dataMax) / 2
  axisMin = mid - span
  axisMax = mid + span
}

/** 只有数据贴到上下 10% 边带、或缩到量程 1/4 以下时，才整体重标定一次 */
function updateAxis(dataMin: number, dataMax: number): void {
  if (!Number.isFinite(axisMin) || !Number.isFinite(axisMax)) {
    resetAxis(dataMin, dataMax)
    return
  }
  const span = axisMax - axisMin
  const band = span * 0.1
  const usedUp = dataMin < axisMin + band || dataMax > axisMax - band
  const tooSmall = dataMax - dataMin < span * 0.25
  if (usedUp || tooSmall) resetAxis(dataMin, dataMax)
}

/** 刻度文字按量程选小数位，避免出现一长串小数 */
function fmtTick(v: number): string {
  const span = axisMax - axisMin
  const digits = span >= 100 ? 0 : span >= 10 ? 1 : span >= 1 ? 2 : 3
  return v.toFixed(digits)
}

function draw(): void {
  const canvas = canvasEl.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const dpr = window.devicePixelRatio || 1
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr
    canvas.height = h * dpr
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  if (!subscribed.value) {
    raf = requestAnimationFrame(draw)
    return
  }

  const { t, v, head, len, cap } = getMiniSeries()
  if (len < 2) {
    raf = requestAnimationFrame(draw)
    return
  }

  const now = performance.now() / 1000
  const WINDOW_S = 5
  const t0 = now - WINDOW_S
  const start = (head - len + cap) % cap

  const pts: [number, number][] = []
  for (let k = 0; k < len; k++) {
    const pos = (start + k) % cap
    if (t[pos]! >= t0) pts.push([t[pos]!, v[pos]!])
  }
  if (pts.length < 2) {
    raf = requestAnimationFrame(draw)
    return
  }

  // 本次窗口内的数据范围 → 量程（余量足够时不动，避免波形"呼吸"）
  let dMin = pts[0]![1]!
  let dMax = dMin
  for (const [, val] of pts) {
    if (val < dMin) dMin = val
    if (val > dMax) dMax = val
  }
  updateAxis(dMin, dMax)

  const { accent, muted } = getThemeColors()
  const xLeft = 42 // 左侧留给 Y 刻度文字
  const xRight = w - 8
  const yTop = 8
  const yBottom = h - 16 // 底部留给时间标注

  // 横向网格
  const ticks = 6
  ctx.strokeStyle = muted
  ctx.globalAlpha = 0.3
  ctx.lineWidth = 1
  for (let i = 0; i <= ticks; i++) {
    const y = yTop + (i / ticks) * (yBottom - yTop)
    ctx.beginPath()
    ctx.moveTo(xLeft, y)
    ctx.lineTo(xRight, y)
    ctx.stroke()
  }

  // Y 刻度文字（顶=最大值）
  ctx.globalAlpha = 1
  ctx.fillStyle = muted
  ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace'
  ctx.textAlign = 'right'
  for (let i = 0; i <= ticks; i++) {
    const y = yTop + (i / ticks) * (yBottom - yTop)
    const val = axisMax - (i / ticks) * (axisMax - axisMin)
    ctx.fillText(fmtTick(val), xLeft - 4, y + 3)
  }

  // 时间轴：左 −5s，右 0（现在）
  ctx.fillText('-5s', xLeft, h - 4)

  // 波形
  ctx.beginPath()
  ctx.strokeStyle = accent
  ctx.lineWidth = 1.5
  for (let i = 0; i < pts.length; i++) {
    const [tv, val] = pts[i]!
    const x = xLeft + ((tv - t0) / WINDOW_S) * (xRight - xLeft)
    const y = yTop + (1 - (val - axisMin) / (axisMax - axisMin)) * (yBottom - yTop)
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  }
  ctx.stroke()
  ctx.textAlign = 'left'

  raf = requestAnimationFrame(draw)
}

onMounted(() => {
  raf = requestAnimationFrame(draw)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
})

watch(
  () => item.value?.index,
  () => {
    // 换了一个量必须重标定量程，否则会沿用上一个量的轴
    axisMin = NaN
    axisMax = NaN
    cancelAnimationFrame(raf)
    raf = requestAnimationFrame(draw)
  },
)

async function onSub(f: boolean): Promise<void> {
  if (!item.value) return
  await setSubscription(item.value.index, true, f)
}

async function onUnsub(): Promise<void> {
  if (!item.value) return
  await setSubscription(item.value.index, false, false)
}
</script>

<style scoped>
.mon-detail {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.mon-detail__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: var(--border-subtle);
  flex-shrink: 0;
}

.mon-detail__name {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-bright);
}

.mon-detail__idx {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}

.mon-detail__back {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
}

.mon-detail__back:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.mon-detail__body {
  flex: 1;
  min-height: 0;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.live {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.live__num {
  font-family: var(--font-mono);
  font-size: 28px;
  font-weight: 700;
  color: var(--text-bright);
  font-variant-numeric: tabular-nums;
}

.live__unit {
  font-style: normal;
  font-size: 14px;
  color: var(--text-muted);
}

.chart {
  position: relative;
  flex: 1;
  min-height: 0;
  border: var(--border-subtle);
  border-radius: var(--radius);
  background: var(--surface-floating);
  overflow: hidden;
}

.chart__canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.chart__empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 13px;
  color: var(--text-muted);
}

.sub {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  flex-shrink: 0;
  padding-top: 2px;
}

.sub-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13.5px;
  font-weight: 700;
}

.sub-state i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}

.sub-state--off {
  color: var(--status-offline);
}
.sub-state--off i {
  background: var(--status-offline);
}
.sub-state--slow {
  color: var(--status-waiting);
  text-shadow: var(--glow-waiting);
}
.sub-state--slow i {
  background: var(--status-waiting);
  box-shadow: var(--glow-waiting);
}
.sub-state--fast {
  color: var(--accent);
  text-shadow: var(--glow-accent);
}
.sub-state--fast i {
  background: var(--accent);
  box-shadow: var(--glow-accent);
}

/* 订阅挡位按钮：做大做亮，一眼能看出是可按的 */
.sub-btn {
  min-width: 104px;
  height: 38px;
  padding: 0 18px;
  font-size: 13.5px;
  font-family: inherit;
  font-weight: 700;
  letter-spacing: 0.04em;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s, box-shadow 0.12s, transform 0.08s;
}

.sub-btn:active {
  transform: translateY(1px);
}

.sub-btn--slow {
  color: var(--status-waiting);
  background: rgba(249, 199, 94, 0.18);
  border: 1px solid rgba(249, 199, 94, 0.65);
  box-shadow: var(--glow-waiting);
}
.sub-btn--slow:hover {
  background: rgba(249, 199, 94, 0.32);
}

.sub-btn--fast {
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  box-shadow: var(--glow-accent);
}
.sub-btn--fast:hover {
  background: rgba(79, 224, 255, 0.28);
}

.sub-btn--off {
  color: var(--status-danger);
  background: var(--danger-soft);
  border: 1px solid var(--danger-border);
}
.sub-btn--off:hover {
  background: rgba(255, 83, 61, 0.28);
  box-shadow: var(--glow-danger, 0 0 12px rgba(255, 83, 61, 0.4));
}
</style>
