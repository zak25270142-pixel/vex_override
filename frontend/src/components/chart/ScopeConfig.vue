<template>
  <!-- 右侧30%通道配置卡：追踪曲线（实时值+单位）、横轴时间窗口、纵轴量程粒度。
       App 用 :key 按通道号强制重建，故本组件内草稿只需按挂载时的通道初始化一次 -->
  <aside class="scope-panel">
    <div class="scope-panel__head">
      <span class="scope-panel__title">通道 {{ ch + 1 }} · 曲线设置</span>
      <button class="back-btn" title="收起配置卡" @click="focusCh = null">× 返回</button>
    </div>

    <div class="scope-panel__body">
      <!-- 追踪曲线：每槽一个选择器，右侧显示该量当前值与单位 -->
      <section class="blk">
        <h4>追踪曲线（{{ traces.length }}/4）</h4>

        <div v-for="(idx, slot) in traces" :key="slot" class="trace-row">
          <i
            class="trace-dot"
            :style="{ background: slotColor(slot), boxShadow: `0 0 6px ${slotColor(slot)}` }"
          />
          <select :value="idx" @change="onPick(slot, ($event.target as HTMLSelectElement).value)">
            <option value="">— 移除该曲线 —</option>
            <option v-for="it in optionsExcept(slot)" :key="it.index" :value="it.index">
              {{ it.name }}
            </option>
          </select>
          <span class="trace-val">
            {{ liveText(idx) }}<i v-if="unitOf(idx)">{{ unitOf(idx) }}</i>
          </span>
        </div>

        <!-- 末满 4 条时的添加行 -->
        <div v-if="traces.length < TRACE_MAX" class="trace-row">
          <i class="trace-dot trace-dot--empty" />
          <select value="" @change="onAdd(($event.target as HTMLSelectElement).value)">
            <option value="">＋ 添加追踪项</option>
            <option v-for="it in optionsExcept(null)" :key="it.index" :value="it.index">
              {{ it.name }}
            </option>
          </select>
        </div>
      </section>

      <!-- 横轴：时间窗口，快设芯片 + 自定义秒数 -->
      <section class="blk">
        <h4>横轴 · 时间窗口</h4>
        <div class="chips">
          <button
            v-for="w in WINDOW_PRESETS"
            :key="w"
            class="chip"
            :class="{ 'chip--on': cfg.windowS === w }"
            @click="setWindow(w)"
          >
            {{ w }}s
          </button>
        </div>
        <div class="custom-row">
          <input
            class="num-input"
            type="number"
            min="0.5"
            max="60"
            step="0.5"
            :value="winDraft"
            @input="onWinInput(($event.target as HTMLInputElement).value)"
          />
          <span class="unit-text">秒（0.5~60）</span>
        </div>
        <p v-if="winErr" class="err">{{ winErr }}</p>
      </section>

      <!-- 纵轴：自动量程 / 手动上下限 -->
      <section class="blk">
        <h4>纵轴 · 量程粒度</h4>
        <label class="auto-row">
          <input v-model="cfg.yAuto" class="switch" type="checkbox" />
          <span>自动量程</span>
        </label>
        <template v-if="!cfg.yAuto">
          <div class="y-row">
            <label class="y-field">
              <span>下限</span>
              <input
                class="num-input"
                type="number"
                step="any"
                :value="minDraft"
                @input="minDraft = ($event.target as HTMLInputElement).value"
                @change="commitYRange"
              />
            </label>
            <label class="y-field">
              <span>上限</span>
              <input
                class="num-input"
                type="number"
                step="any"
                :value="maxDraft"
                @input="maxDraft = ($event.target as HTMLInputElement).value"
                @change="commitYRange"
              />
            </label>
          </div>
          <p v-if="yErr" class="err">{{ yErr }}</p>
        </template>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  TRACE_MAX,
  addTrace,
  channelCfg,
  channelTraces,
  focusCh,
  formatValue,
  monitorLatest,
  monitorMap,
  pickTrace,
  plottableItems,
} from '@/stores/globle'

/** 挂载时必有选中通道（App 的 v-if 保证）；:key 随通道变化整体重建 */
const ch = computed(() => focusCh.value!)
const cfg = computed(() => channelCfg[ch.value]!)
const traces = computed(() => channelTraces[ch.value]!)

const WINDOW_PRESETS = [2, 5, 10, 30]

/* 输入草稿只在本组件存活期有效，切通道由 :key 重建自然重置 */
const winDraft = ref(String(cfg.value.windowS))
const minDraft = ref(String(cfg.value.yMin))
const maxDraft = ref(String(cfg.value.yMax))
const winErr = ref('')
const yErr = ref('')

/** 槽位颜色直接用 CSS 变量（行内样式浏览器可解析），与图上曲线配色一致 */
function slotColor(slot: number): string {
  return `var(--channel-${slot + 1})`
}

/** 下拉候选项：排除已被本通道其他槽位占用的项，防同通道重复 */
function optionsExcept(slot: number | null) {
  return plottableItems.value.filter(
    (it) => !traces.value.some((idx, k) => k !== slot && idx === it.index),
  )
}

function unitOf(index: number): string {
  return monitorMap[index]?.unit ?? ''
}

/** 追踪量实时值（monitorLatest 以 20fps 响应式刷新）；尚无数据时显 — */
function liveText(index: number): string {
  const item = monitorMap[index]
  const v = monitorLatest[index]
  return item && v !== undefined ? formatValue(item, v) : '—'
}

function onPick(slot: number, raw: string): void {
  pickTrace(ch.value, slot, raw)
}

function onAdd(raw: string): void {
  addTrace(ch.value, raw)
}

function setWindow(w: number): void {
  cfg.value.windowS = w
  winDraft.value = String(w)
  winErr.value = ''
}

/** 自定义窗口：边输边校验，合法立即生效，非法保留草稿并红字提示（图沿用上次合法值） */
function onWinInput(raw: string): void {
  winDraft.value = raw
  if (raw.trim() === '') return
  const n = Number(raw)
  if (Number.isFinite(n) && n >= 0.5 && n <= 60) {
    cfg.value.windowS = n
    winErr.value = ''
  } else {
    winErr.value = '窗口需为 0.5~60 之间的数字'
  }
}

/** 手动量程：失焦/回车提交，必须两个都是数字且下限 < 上限才生效 */
function commitYRange(): void {
  const lo = Number(minDraft.value)
  const hi = Number(maxDraft.value)
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) {
    yErr.value = '上下限需为数字，且下限 < 上限'
    return
  }
  yErr.value = ''
  cfg.value.yMin = lo
  cfg.value.yMax = hi
}
</script>

<style scoped>
.scope-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.scope-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: var(--border-panel);
  flex-shrink: 0;
}

.scope-panel__title {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--text-primary);
}

.back-btn {
  padding: 2px 8px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
}

.back-btn:hover {
  color: var(--text-bright);
  border-color: var(--accent-border);
}

.scope-panel__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.blk h4 {
  margin: 0 0 10px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: var(--text-secondary);
}

/* 追踪曲线行 */
.trace-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.trace-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}

.trace-dot--empty {
  border: 1px dashed var(--text-muted);
  background: transparent;
}

.trace-row select {
  flex: 1;
  min-width: 0;
  height: 30px;
  font-size: 12.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
}

.trace-row select:focus {
  border-color: var(--accent);
}

/* 当前值等宽右挂，单位弱化小字 */
.trace-val {
  max-width: 92px;
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--text-bright);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.trace-val i {
  font-style: normal;
  font-family: inherit;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
  margin-left: 3px;
}

/* 快设芯片 */
.chips {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}

.chip {
  flex: 1;
  height: 28px;
  font-size: 12.5px;
  font-family: inherit;
  font-weight: 700;
  color: var(--text-secondary);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
}

.chip:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.chip--on {
  color: var(--text-on-accent);
  background: var(--accent);
  border-color: var(--accent);
  box-shadow: 0 0 10px rgba(79, 224, 255, 0.35);
}

.custom-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.num-input {
  width: 100%;
  height: 32px;
  padding: 0 10px;
  font-size: 13.5px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
  box-sizing: border-box;
}

.num-input:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}

.unit-text {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-muted);
}

.err {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--status-danger);
  text-shadow: var(--glow-danger);
}

/* 纵轴 */
.auto-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
}

.switch {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
  cursor: pointer;
}

.y-row {
  display: flex;
  gap: 10px;
  margin-top: 10px;
}

.y-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
