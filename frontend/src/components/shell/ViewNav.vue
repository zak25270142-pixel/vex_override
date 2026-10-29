<template>
  <!-- 4 个分页：参数表 / 曲线 / 场地图 / 视觉（预留） -->
  <nav class="view-nav">
    <div class="view-nav__tabs">
      <button
        v-for="tab in TABS"
        :key="tab.id"
        class="view-nav__item"
        :class="{ 'view-nav__item--active': view === tab.id }"
        @click="view = tab.id"
      >
        <span class="view-nav__icon" v-html="tab.icon" />
        <span class="view-nav__label">{{ tab.label }}</span>
      </button>
    </div>

    <!-- 弹性占位：把「刷新 + 电池」一组推到导航栏右侧 -->
    <span class="view-nav__spacer" />

    <!-- 一键刷新：清空并重新拉取三张目录表（连接后本来自动拉，这里手动重拉） -->
    <button
      class="view-nav__refresh"
      :disabled="!connected"
      title="清空并重新拉取三张目录表（调参/实参/命令）"
      @click="refreshAll"
    >
      <span class="view-nav__icon" v-html="REFRESH_ICON" />
      <span class="view-nav__label">刷新</span>
    </button>

    <!-- 电池电量（V5 Brain，低速推送；未连接/无该项显示 —）置于刷新按钮右侧 -->
    <BatteryBadge :level="batteryLevel" />
  </nav>
</template>

<script setup lang="ts">
import { view, connected, refreshAll, batteryLevel } from '@/stores/globle'
import type { WorkView } from '@/config/workspace'
import BatteryBadge from '@/components/common/BatteryBadge.vue'

interface TabDef {
  id: WorkView
  label: string
  icon: string
}

const stroke =
  'stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"'
const REFRESH_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" ${stroke}><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>`

const TABS: TabDef[] = [
  {
    id: 'params',
    label: '参数表',
    icon: `<svg viewBox="0 0 24 24" width="19" height="19" ${stroke}><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11M12 13h6M12 16.5h6"/></svg>`,
  },
  {
    id: 'chart',
    label: '曲线',
    icon: `<svg viewBox="0 0 24 24" width="19" height="19" ${stroke}><path d="M2 12h4l2.5-7 5 14L16 12h6"/></svg>`,
  },
  {
    id: 'map',
    label: '场地图',
    icon: `<svg viewBox="0 0 24 24" width="19" height="19" ${stroke}><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v14M15 6v14"/></svg>`,
  },
  {
    id: 'vision',
    label: '视觉图',
    icon: `<svg viewBox="0 0 24 24" width="19" height="19" ${stroke}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`,
  },
]
</script>

<style scoped>
.view-nav {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 44px;
  padding: 0 8px;
  flex-shrink: 0;
  background: var(--surface-floating);
  border: var(--border-panel);
  border-radius: var(--radius);
  overflow-x: auto;
  scrollbar-width: none;
}

.view-nav__tabs {
  display: flex;
  gap: 4px;
  align-self: stretch;
}

/* 左右等宽占位：把「刷新 + 电池」一组推到导航栏中央 */
.view-nav__spacer {
  flex: 1 1 0;
  min-width: 0;
}

.view-nav::-webkit-scrollbar {
  display: none;
}

.view-nav__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  /* 撑满导航栏高度：下划线按导航条底边定位，不再跟着图标/电池徽标的高度漂移 */
  align-self: stretch;
  padding: 0 14px;
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 12.5px;
  font-family: inherit;
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s;
}

.view-nav__item:hover {
  color: var(--text-secondary);
}

.view-nav__item--active {
  color: var(--accent);
  text-shadow: 0 0 10px rgba(79, 224, 255, 0.4);
}

.view-nav__item--active::after {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  bottom: 6px;
  height: 2px;
  background: var(--accent);
  border-radius: 1px;
  box-shadow: var(--glow-accent);
}

/* 电池徽标不吃横向压缩：导航条空间不足时别把电量数字挤变形 */
.view-nav :deep(.bat) {
  flex-shrink: 0;
  white-space: nowrap;
}

.view-nav__icon {
  display: grid;
  place-items: center;
}

.view-nav__refresh {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 14px;
  height: 30px;
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  color: var(--text-secondary);
  font-size: 12.5px;
  font-family: inherit;
  font-weight: 700;
  white-space: nowrap;
  cursor: pointer;
}

.view-nav__refresh:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent-border);
  box-shadow: var(--shadow-focus);
}

.view-nav__refresh:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
