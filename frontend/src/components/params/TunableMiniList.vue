<template>
  <!-- 右栏「调参」迷你列表：普通调参表的精简克隆，紧凑排列，点击选中后在底部 Dock 编辑 -->
  <div class="mini-list">
    <div v-if="rows.length === 0" class="mini-list__empty">
      <template v-if="!connected">尚未连接 V5</template>
      <template v-else>调参表为空，请在标题栏获取</template>
    </div>
    <div v-else class="mini-list__body">
      <div
        v-for="it in rows"
        :key="it.index"
        class="mini-row"
        :class="{ 'mini-row--sel': selectedTunableIndex === it.index }"
        @click="onClick(it.index)"
      >
        <span class="mini-row__name" :title="it.name">{{ shortName(it.name) }}</span>
        <span class="mini-row__value" :title="fullValue(it)">
          <template v-if="pendingTunable.has(it.index)">…</template>
          <template v-else>{{ formatValueCompact(it, it.value) }}</template>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  connected,
  formatValueCompact,
  pendingTunable,
  selectTunable,
  selectedTunableIndex,
  tunableMap,
} from '@/stores/globle'

const rows = computed(() => Object.values(tunableMap).sort((a, b) => a.index - b.index))

/** 名称只显示前 5 个字，超出省略 */
function shortName(name: string): string {
  return name.length > 5 ? name.slice(0, 5) + '…' : name
}

function fullValue(it: { value: unknown }): string {
  return String(it.value)
}

function onClick(index: number): void {
  selectTunable(selectedTunableIndex.value === index ? null : index)
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
