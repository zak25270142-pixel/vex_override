<template>
  <!-- 右栏「实参」迷你列表：普通实参表的精简克隆。
       每行左侧一个发光点指示订阅挡位（灰=未订/黄=低速/青=高速），点击选中后查看详情 -->
  <div class="mini-list">
    <div v-if="rows.length === 0" class="mini-list__empty">
      <template v-if="!connected">尚未连接 V5</template>
      <template v-else>实参表为空，请在标题栏获取</template>
    </div>
    <div v-else class="mini-list__body">
      <div
        v-for="it in rows"
        :key="it.index"
        class="mini-row"
        :class="{ 'mini-row--sel': selectedMonitorIndex === it.index }"
        @click="onClick(it.index)"
      >
        <i class="dot" :class="dotClass(it.tag)" />
        <span class="mini-row__name" :title="it.name">{{ shortName(it.name) }}</span>
        <span class="mini-row__value" :title="String(liveValue(it))">
          {{ formatValueCompact(it, liveValue(it)) }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { tagFast, tagSubscribed, type CellValue, type DirItem } from '@/services/protocol'
import {
  connected,
  formatValueCompact,
  monitorLatest,
  monitorMap,
  selectMonitor,
  selectedMonitorIndex,
} from '@/stores/globle'

const rows = computed(() => {
  // 已订阅优先（高速排前），同档按 index
  return Object.values(monitorMap).sort((a, b) => {
    const sa = tagSubscribed(a.tag) ? 1 : 0
    const sb = tagSubscribed(b.tag) ? 1 : 0
    if (sa !== sb) return sb - sa
    if (sa === 1) {
      const fa = tagFast(a.tag) ? 1 : 0
      const fb = tagFast(b.tag) ? 1 : 0
      if (fa !== fb) return fb - fa
    }
    return a.index - b.index
  })
})

function shortName(name: string): string {
  return name.length > 5 ? name.slice(0, 5) + '…' : name
}

function liveValue(it: DirItem): CellValue {
  return monitorLatest[it.index] ?? it.value
}

function dotClass(tag: number): string {
  if (!tagSubscribed(tag)) return 'dot--off'
  if (tagFast(tag)) return 'dot--fast'
  return 'dot--slow'
}

function onClick(index: number): void {
  selectMonitor(selectedMonitorIndex.value === index ? null : index)
}
</script>

<style scoped>
.mini-list {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.mini-list__empty {
  margin: auto;
  padding: 20px 16px;
  font-size: 13px;
  text-align: center;
  color: var(--text-muted);
}

.mini-list__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.mini-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: var(--border-subtle);
  cursor: pointer;
  transition: background 0.1s;
}

.mini-row:hover {
  background: var(--surface-hover);
}

.mini-row--sel {
  background: var(--accent-soft);
  box-shadow: inset 3px 0 0 var(--accent);
}

/* 订阅挡位发光点 */
.dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.dot--off {
  background: var(--status-offline);
}

.dot--slow {
  background: var(--status-waiting);
  box-shadow: var(--glow-waiting);
}

.dot--fast {
  background: var(--accent);
  box-shadow: var(--glow-accent);
}

/* 名称列定宽，数值列左对齐，整列数值起始位置一致 */
.mini-row__name {
  flex: none;
  width: 6em;
  min-width: 0;
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini-row__value {
  flex: 1;
  min-width: 0;
  text-align: left;
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-bright);
  font-variant-numeric: tabular-nums;
}
</style>
