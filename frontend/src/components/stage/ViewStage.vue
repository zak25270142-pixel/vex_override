<template>
  <!-- 导航 + 视图屏。v-show 保持 ECharts 实例存活，由 ResizeObserver 自适应尺寸 -->
  <section class="view-stage">
    <ViewNav />
    <div class="view-stage__screen">
      <ParamsView v-show="view === 'params'" />
      <ChartView v-show="view === 'chart'" />
      <FieldMap v-show="view === 'map'" />
      <PlaceholderView
        v-show="view === 'vision'"
        title="视觉图"
        desc="视觉画面与目标标注将在此呈现，当前版本预留"
        :icon="visionIcon"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import ViewNav from '@/components/shell/ViewNav.vue'
import ParamsView from '@/components/params/ParamsView.vue'
import ChartView from '@/components/chart/ChartView.vue'
import FieldMap from '@/components/map/FieldMap.vue'
import PlaceholderView from '@/components/stage/PlaceholderView.vue'
import { view } from '@/stores/globle'

const stroke =
  'stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"'
const visionIcon = `<svg viewBox="0 0 24 24" width="58" height="58" ${stroke}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`
</script>

<style scoped>
.view-stage {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
}

.view-stage__screen {
  flex: 1;
  min-height: 0;
  display: grid;
}

.view-stage__screen > * {
  min-width: 0;
  min-height: 0;
}
</style>
