<template>
  <!-- 右栏「场景数据」标签内容：场地位姿读数（从场地图迁入，只显 X / Y / Yaw） -->
  <aside class="scene-panel">
    <div v-if="pose.valid" class="scene-panel__body">
      <div class="row"><span>X</span><b>{{ pose.x.toFixed(3) }}</b><em>m</em></div>
      <div class="row"><span>Y</span><b>{{ pose.y.toFixed(3) }}</b><em>m</em></div>
      <div class="row"><span>Yaw</span><b>{{ normYaw.toFixed(1) }}</b><em>°</em></div>
    </div>
    <div v-else class="scene-panel__empty">
      <template v-if="!connected">尚未连接 V5，连接后显示位姿数据</template>
      <template v-else>尚未收到全局坐标 X / Y / Yaw，请确认实参表中对应量已订阅</template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { connected, pose } from '@/stores/globle'

/** 读数统一显示成 ±180 */
const normYaw = computed(() => {
  let a = pose.yaw % 360
  if (a > 180) a -= 360
  if (a < -180) a += 360
  return a
})
</script>

<style scoped>
.scene-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.scene-panel__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 14px;
  padding: 16px 14px;
}

.row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 12px 14px;
  background: var(--surface-floating);
  border: var(--border-subtle);
  border-radius: var(--radius);
  font-family: var(--font-mono);
}

.row span {
  width: 38px;
  font-size: 13px;
  color: var(--text-muted);
}

.row b {
  flex: 1;
  font-size: 26px;
  font-weight: 700;
  color: var(--text-bright);
}

.row em {
  font-style: normal;
  font-size: 12px;
  color: var(--text-muted);
}

.scene-panel__empty {
  margin: auto;
  padding: 20px 16px;
  font-size: 13px;
  line-height: 1.8;
  text-align: center;
  color: var(--text-muted);
}
</style>
