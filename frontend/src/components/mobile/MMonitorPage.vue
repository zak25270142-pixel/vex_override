<template>
  <!-- 实参页：每行 名称 + 实时值 + 三态订阅分段（未订/低速/高速） -->
  <div class="m-list">
    <div v-if="rows.length === 0" class="m-list__empty">
      <template v-if="!connected">尚未连接 V5，请在右上角设置开启「演示」或连接</template>
      <template v-else>实参表为空，请在右上角设置连接后自动获取</template>
    </div>

    <div v-for="it in rows" :key="it.index" class="m-list-row">
      <span class="m-list-row__name" :title="it.name">{{ it.name }}</span>
      <span class="m-list-row__value" :title="liveText(it)"
        >{{ liveText(it) }}<i v-if="it.unit" class="m-unit">{{ it.unit }}</i></span
      >
      <MSeg3
        :model-value="subState(it)"
        :labels="['未订', '低速', '高速']"
        aria-label="订阅挡位"
        @update:model-value="(s: number) => setSubState(it, s)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { tagFast, tagSubscribed, type DirItem } from '@/services/protocol'
import {
  connected,
  formatValueCompact,
  monitorLatest,
  monitorMap,
  setSubscription,
} from '@/stores/globle'
import MSeg3 from './MSeg3.vue'

// 稳定按 index 排序：订阅挡位改变时不重排，避免被点的行跳到列表别处（手机上易误判为点错）
const rows = computed(() =>
  Object.values(monitorMap).sort((a, b) => a.index - b.index),
)

function liveText(it: DirItem): string {
  const v = monitorLatest[it.index] ?? it.value
  return formatValueCompact(it, v)
}

// 0=未订 1=低速 2=高速
function subState(it: DirItem): number {
  if (!tagSubscribed(it.tag)) return 0
  return tagFast(it.tag) ? 2 : 1
}

async function setSubState(it: DirItem, s: number): Promise<void> {
  if (s === 0) await setSubscription(it.index, false, false)
  else await setSubscription(it.index, true, s === 2)
}
</script>
