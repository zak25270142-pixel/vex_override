<template>
  <!-- 4 个分页：参数表 / 曲线 / 场地图 / 视觉（预留） -->
  <nav class="view-nav">
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
  </nav>
</template>

<script setup lang="ts">
import { view } from '@/stores/globle'
import type { WorkView } from '@/config/workspace'

interface TabDef {
  id: WorkView
  label: string
  icon: string
}

const stroke =
  'stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"'

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
  gap: 4px;
  height: 44px;
  padding: 0 8px;
  flex-shrink: 0;
  background: var(--surface-floating);
  border: var(--border-panel);
  border-radius: var(--radius);
  overflow-x: auto;
  scrollbar-width: none;
}

.view-nav::-webkit-scrollbar {
  display: none;
}

.view-nav__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
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
  left: 12px;
  right: 12px;
  bottom: 4px;
  height: 2px;
  background: var(--accent);
  border-radius: 1px;
  box-shadow: var(--glow-accent);
}

.view-nav__icon {
  display: grid;
  place-items: center;
}
</style>
