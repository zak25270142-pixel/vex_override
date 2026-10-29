<template>
  <!-- 电池电量徽标：SVG 电池外壳 + 按电量比例填充 + 百分比；按电量着色。
       level=null（未连接 / 目录里没有该项）时壳内画叉（灰色），表示断线/无读数。 -->
  <span class="bat" :class="`bat--${state}`" :title="title">
    <svg class="bat__icon" viewBox="0 0 26 14" width="26" height="14" aria-hidden="true">
      <rect class="bat__body" x="1" y="1" width="21" height="12" rx="2.5" />
      <rect class="bat__nub" x="22.5" y="4" width="2.5" height="6" rx="1" />
      <rect
        v-if="level !== null"
        class="bat__fill"
        x="2.5"
        y="2.5"
        :width="fillW"
        height="9"
        rx="1"
      />
      <path v-else class="bat__cross" d="M2.5 2.5 20.5 11.5M20.5 2.5 2.5 11.5" />
    </svg>
    <span class="bat__pct">{{ level === null ? '' : Math.round(level) + '%' }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ level: number | null }>()

/** 着色档位：无数据灰 / <20% 红 / 20~50% 黄 / ≥50% 绿 */
const state = computed<'none' | 'low' | 'mid' | 'high'>(() => {
  const l = props.level
  if (l === null) return 'none'
  if (l >= 50) return 'high'
  if (l >= 20) return 'mid'
  return 'low'
})

/** 填充宽度：外壳内可用约 18px，按电量比例 */
const fillW = computed(() => {
  const l = props.level
  if (l === null) return 0
  return (Math.max(0, Math.min(100, l)) / 100) * 18
})

const title = computed(() =>
  props.level === null
    ? '电池电量：未知（未连接，或目录里没有该项）'
    : `电池电量：${Math.round(props.level)}%`,
)
</script>

<style scoped>
.bat {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
}

.bat__icon {
  display: block;
  flex-shrink: 0;
}

.bat__body {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
}

.bat__nub {
  fill: currentColor;
}

.bat__fill {
  stroke: none;
}

.bat--high {
  color: var(--status-success);
}
.bat--high .bat__fill {
  fill: var(--status-success);
}

.bat--mid {
  color: var(--status-waiting);
}
.bat--mid .bat__fill {
  fill: var(--status-waiting);
}

.bat--low {
  color: var(--status-danger);
}
.bat--low .bat__fill {
  fill: var(--status-danger);
}

.bat--none {
  color: var(--text-muted);
}
.bat--none .bat__fill {
  fill: none;
}

/* 无读数（断线/无该项）：壳内画叉，灰色表「读数无效」 */
.bat__cross {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
}
</style>
