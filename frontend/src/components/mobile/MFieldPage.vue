<template>
  <!-- 场地页：上方三值标注（位姿 x/y/yaw）+ 下方复用 FieldMap（已是 6×6 SVG） -->
  <div class="m-field-page">
    <div class="m-field-readout">
      <div class="rd">
        <div class="rd__k">X (m)</div>
        <div class="rd__v">{{ rd(pose.x) }}</div>
      </div>
      <div class="rd">
        <div class="rd__k">Y (m)</div>
        <div class="rd__v">{{ rd(pose.y) }}</div>
      </div>
      <div class="rd">
        <div class="rd__k">Yaw (°)</div>
        <div class="rd__v">{{ rd(pose.yaw, 1) }}</div>
      </div>
    </div>

    <div class="m-field-map-wrap">
      <FieldMap />
    </div>
  </div>
</template>

<script setup lang="ts">
import { pose } from '@/stores/globle'
import FieldMap from '@/components/map/FieldMap.vue'

function rd(v: number, d = 2): string {
  if (!pose.valid) return '—'
  return Number(v).toFixed(d)
}
</script>

<style scoped>
.m-field-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.m-field-map-wrap {
  flex: 1;
  min-height: 0;
  margin: 0 10px 10px;
}
/* FieldMap 根节点为 100% 容器，这里让它填满包裹层 */
.m-field-map-wrap :deep(.field-map) {
  height: 100%;
}
</style>
