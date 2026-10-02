<template>
  <!-- 右侧工作区：标签导航 + 底部 Dock。
       标签顺序：[该页独有上下文] 任务下发 调参 实参；空间不够自动换行。
       详情页只在「调参表+调参标签」「实参表+实参标签」这组特化组合下出现，
       其余组合（调参表的实参标签 / 实参表的调参标签 / 场地图的调参实参）一律是迷你列表。
       底部 Dock：仅在非 params 页且当前标签为调参/实参时出现。 -->
  <aside class="right-work">
    <nav class="right-work__tabs">
      <button
        v-for="t in tabs"
        :key="t.id"
        class="right-work__tab"
        :class="{ 'right-work__tab--active': activeTab === t.id }"
        @click="rightTab = t.id"
      >
        {{ t.label }}
      </button>
    </nav>

    <div class="right-work__body">
      <!-- 任务下发 -->
      <CommandPanel v-show="activeTab === 'command'" />

      <!-- 调参标签：仅在「调参表」下显示详情（100%高），其余页显示迷你列表 -->
      <template v-if="activeTab === 'tunable'">
        <TunableCard v-if="tunableDetail && selectedTunableIndex !== null" />
        <div v-else-if="tunableDetail" class="right-work__empty">
          在左侧调参表选中一项，在此修改并下发
        </div>
        <TunableMiniList v-else />
      </template>

      <!-- 实参标签：仅在「实参表」下显示详情（100%高），其余页显示迷你列表 -->
      <template v-if="activeTab === 'monitor'">
        <MonitorDetail v-if="monitorDetail && selectedMonitorIndex !== null" />
        <div v-else-if="monitorDetail" class="right-work__empty">
          在左侧实参表选中一项，在此查看实时值与订阅挡位
        </div>
        <MonitorMiniList v-else />
      </template>

      <!-- 曲线设置 / 场景数据 上下文 -->
      <div v-show="activeTab === 'context'" class="right-work__pane">
        <component :is="context.comp" v-if="context.comp" :key="context.key" />
        <div v-else class="right-work__empty">{{ context.empty }}</div>
      </div>
    </div>

    <!-- 底部 Dock：仅在非 params 页且当前标签为调参/实参时出现 -->
    <div v-if="showDock" class="right-work__dock">
      <TunableCard v-if="selectedTunableIndex !== null" />
      <MonitorCard v-else-if="selectedMonitorIndex !== null" />
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import CommandPanel from '@/components/command/CommandPanel.vue'
import ScopeConfig from '@/components/chart/ScopeConfig.vue'
import SceneDataPanel from '@/components/map/SceneDataPanel.vue'
import TunableMiniList from '@/components/params/TunableMiniList.vue'
import MonitorMiniList from '@/components/params/MonitorMiniList.vue'
import MonitorDetail from '@/components/params/MonitorDetail.vue'
import TunableCard from '@/components/params/TunableCard.vue'
import MonitorCard from '@/components/params/MonitorCard.vue'
import {
  focusCh,
  paramTable,
  rightTab,
  selectedMonitorIndex,
  selectedTunableIndex,
  view,
  type RightTab,
} from '@/stores/globle'

interface TabDef {
  id: RightTab
  label: string
}

/** 上下文标签文案（曲线设置/场景数据）；其他主视图没有上下文标签 */
const contextLabel = computed(() => {
  switch (view.value) {
    case 'chart':
      return '曲线设置'
    case 'map':
      return '场景数据'
    default:
      return ''
  }
})

/** 标签顺序：独有上下文 → 任务下发 → 调参 → 实参 */
const tabs = computed<TabDef[]>(() => {
  const list: TabDef[] = []
  if (contextLabel.value) list.push({ id: 'context', label: contextLabel.value })
  list.push({ id: 'command', label: '任务下发' })
  list.push({ id: 'tunable', label: '调参' })
  list.push({ id: 'monitor', label: '实参' })
  return list
})

/** 当前标签不在标签列表里时回落到任务下发 */
const activeTab = computed<RightTab>(() => {
  const cur = rightTab.value
  return tabs.value.some((t) => t.id === cur) ? cur : 'command'
})

/** 详情页只在特化组合下出现：调参表+调参标签 / 实参表+实参标签 */
const tunableDetail = computed(
  () => view.value === 'params' && paramTable.value === 'tunable' && activeTab.value === 'tunable',
)
const monitorDetail = computed(
  () => view.value === 'params' && paramTable.value === 'monitor' && activeTab.value === 'monitor',
)

/** 上下文面板：曲线页未选通道时空态占位 */
const context = computed(() => {
  switch (view.value) {
    case 'chart':
      return focusCh.value !== null
        ? { comp: ScopeConfig, key: `scope-${focusCh.value}`, empty: '' }
        : {
            comp: null,
            key: 'scope-empty',
            empty: '点击左侧分图选中通道后，在此配置该通道的追踪曲线与坐标轴',
          }
    case 'map':
      return { comp: SceneDataPanel, key: 'scene', empty: '' }
    default:
      return { comp: null, key: 'none', empty: '' }
  }
})

/** 底部 Dock 仅在非 params 页且当前标签为调参/实参时出现 */
const showDock = computed(
  () =>
    view.value !== 'params' &&
    (activeTab.value === 'tunable' || activeTab.value === 'monitor') &&
    (selectedTunableIndex.value !== null || selectedMonitorIndex.value !== null),
)
</script>

<style scoped>
.right-work {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 标签导航：空间不够自动换行；每个标签自然宽度，不强制均分 */
.right-work__tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 8px;
  flex-shrink: 0;
  background: var(--surface-floating);
  border: var(--border-panel);
  border-radius: var(--radius);
}

.right-work__tab {
  position: relative;
  padding: 5px 14px;
  background: transparent;
  border: none;
  border-radius: var(--radius);
  color: var(--text-muted);
  font-size: 12.5px;
  font-family: inherit;
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
}

.right-work__tab:hover {
  color: var(--text-secondary);
  background: var(--surface-hover);
}

.right-work__tab--active {
  color: var(--accent);
  background: var(--accent-soft);
  text-shadow: 0 0 10px rgba(79, 224, 255, 0.4);
}

.right-work__body {
  flex: 1;
  min-height: 0;
}

.right-work__body > * {
  height: 100%;
}

.right-work__pane {
  min-height: 0;
}

.right-work__empty {
  height: 100%;
  display: grid;
  place-items: center;
  padding: 20px 16px;
  font-size: 13px;
  line-height: 1.8;
  text-align: center;
  color: var(--text-muted);
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  box-sizing: border-box;
}

.right-work__dock {
  flex: 0 0 30%;
  min-height: 140px;
  max-height: 30%;
}
</style>
