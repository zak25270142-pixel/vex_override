<template>
  <!-- 右栏底部 Dock 中的实参缩略卡：只显示实时值与订阅挡位，不画曲线。
       曲线在实参标签页选中后的详情页（MonitorDetail）里 -->
  <section class="mon-card">
    <header class="mon-card__head">
      <span class="mon-card__name">{{ item.name }}</span>
      <span class="mon-card__idx">#{{ item.index }}</span>
      <button class="mon-card__close" title="关闭" @click="selectMonitor(null)">×</button>
    </header>

    <div class="mon-card__body">
      <div class="live">
        <span class="live__num">{{ formatValue(item, liveValue) }}</span>
        <i v-if="item.unit" class="live__unit">{{ item.unit }}</i>
      </div>

      <div class="sub">
        <template v-if="!subscribed">
          <span class="sub-state sub-state--off"><i />未订阅</span>
          <button class="sub-btn sub-btn--slow" @click="onSub(false)">低速</button>
          <button class="sub-btn sub-btn--fast" @click="onSub(true)">高速</button>
        </template>
        <template v-else-if="fast">
          <span class="sub-state sub-state--fast"><i />高速</span>
          <button class="sub-btn sub-btn--off" @click="onUnsub">退订</button>
          <button class="sub-btn sub-btn--slow" @click="onSub(false)">降低速</button>
        </template>
        <template v-else>
          <span class="sub-state sub-state--slow"><i />低速</span>
          <button class="sub-btn sub-btn--off" @click="onUnsub">退订</button>
          <button class="sub-btn sub-btn--fast" @click="onSub(true)">升高速</button>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { tagFast, tagSubscribed, type CellValue } from '@/services/protocol'
import {
  formatValue,
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
.mon-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.mon-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: var(--border-subtle);
  flex-shrink: 0;
}

.mon-card__name {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-bright);
}

.mon-card__idx {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
}

.mon-card__close {
  margin-left: auto;
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: 16px;
  line-height: 1;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
}

.mon-card__close:hover {
  color: var(--status-danger);
  background: var(--danger-soft);
}

.mon-card__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.live {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.live__num {
  font-family: var(--font-mono);
  font-size: 20px;
  font-weight: 700;
  color: var(--text-bright);
  font-variant-numeric: tabular-nums;
}

.live__unit {
  font-style: normal;
  font-size: 12px;
  color: var(--text-muted);
}

.sub {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.sub-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  font-weight: 700;
}

.sub-state i {
  width: 6px;
  height: 6px;
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

.sub-btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
  font-family: inherit;
  font-weight: 700;
  border-radius: var(--radius);
  cursor: pointer;
}

.sub-btn--slow {
  color: var(--status-waiting);
  background: rgba(249, 199, 94, 0.12);
  border: 1px solid rgba(249, 199, 94, 0.5);
}
.sub-btn--slow:hover {
  background: rgba(249, 199, 94, 0.24);
}
.sub-btn--fast {
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
}
.sub-btn--fast:hover {
  background: rgba(79, 224, 255, 0.24);
}
.sub-btn--off {
  color: var(--status-danger);
  background: var(--danger-soft);
  border: 1px solid var(--danger-border);
}
.sub-btn--off:hover {
  background: rgba(255, 83, 61, 0.24);
}
</style>
