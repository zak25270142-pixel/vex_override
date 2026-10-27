<template>
  <!-- 手机版独立 UI 树：顶栏 + 内容区 + 底部 tab；与桌面布局完全分开，共享同一套 store/transport -->
  <div class="m-root" :style="vvHeight ? { height: vvHeight + 'px' } : null">
    <!-- 顶栏：左=HDU VEX 队标（点击弹窗设置），右=可横滑按键行（停车等：定宽、右对齐、最多 2/3 宽、超宽左右拖动）。
         信号指示灯已移除——连接状态改由设置面板内的连接控制呈现。 -->
    <header class="m-topbar">
      <!-- 左：品牌区（固定占顶栏 1/3）——队标图标 + 文字，整体点击弹窗设置 -->
      <button class="m-topbar__brand" title="设置" aria-label="设置" @click="showSettings = true">
        <img class="m-topbar__logo" :src="logoUrl" alt="HDU VEX" />
        <span class="m-topbar__title">V5 上位机</span>
      </button>
      <!-- 右：可横滑按键行（停车等：定宽、右对齐、约占剩余 2/3、超宽则左右拖动） -->
      <div class="m-topbar__actions">
        <MStopBar />
      </div>
    </header>

    <!-- 内容区：按底部 tab 切换页面（任务为底部挤入的面板，不切页，与内容盒上下平分） -->
    <!-- 任务面板弹起时给内容区加 --split：页面据此隐藏次要 UI、让主视图铺满上半屏 -->
    <main class="m-body" :class="{ 'm-body--split': showTask }">
      <MDataPage v-if="tab === 'data'" />
      <MChartPage v-else-if="tab === 'chart'" />
      <MFieldPage v-else-if="tab === 'field'" />
      <MVisionPage v-else-if="tab === 'vision'" />
    </main>

    <!-- 任务下发表：作为布局子项挤入下半屏，把内容盒挤成上半屏（两者各自可滑动） -->
    <MTaskSheet v-if="showTask" v-model="showTask" />

    <!-- 底部 tab 栏 -->
    <nav class="m-tabbar">
      <button
        v-for="t in tabs"
        :key="t.key"
        class="m-tab"
        :class="{ on: tab === t.key || (t.key === 'task' && showTask) }"
        @click="onTab(t.key)"
      >
        <span class="m-tab__icon">{{ t.icon }}</span>
        <span>{{ t.label }}</span>
      </button>
    </nav>

    <!-- 设置 sheet -->
    <MSettingsSheet v-model="showSettings" />
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import logoUrl from '@/assets/hdu-vex-logo.jpg'
import MDataPage from './MDataPage.vue'
import MChartPage from './MChartPage.vue'
import MFieldPage from './MFieldPage.vue'
import MVisionPage from './MVisionPage.vue'
import MTaskSheet from './MTaskSheet.vue'
import MSettingsSheet from './MSettingsSheet.vue'
import MStopBar from './MStopBar.vue'
import './mobile.css'

type TabKey = 'data' | 'chart' | 'field' | 'vision' | 'task'
const tabs: { key: TabKey; label: string; icon: string }[] = [
  { key: 'data', label: '数据', icon: '📊' },
  { key: 'chart', label: '曲线', icon: '📈' },
  { key: 'field', label: '场地', icon: '🗺' },
  { key: 'vision', label: '视觉', icon: '👁' },
  { key: 'task', label: '任务', icon: '🎯' },
]
const tab = ref<TabKey>('data')
const showTask = ref(false)
const showSettings = ref(false)

/* 根节点高度始终跟随 visualViewport（而非布局视口 innerHeight）。
   为什么不用"键盘检测 + 阈值"：安卓上键盘弹起时 innerHeight 会跟着缩，
   拿它当基线会被自己更新掉导致永远检测不到（上一版失效的原因）。
   现在不做阈值判断，直接让根容器 = 实际可视高度：
   - iOS 键盘浮层：可视高度收缩 → 根容器跟着收，任务面板不被顶出屏幕
   - 下半任务面板按宽高比定高，上半内容区 flex:1 承担被压缩的部分 */
const vvHeight = ref(0)

function syncViewportHeight(): void {
  const vv = window.visualViewport
  vvHeight.value = vv ? vv.height : window.innerHeight
}

onMounted(() => {
  const vv = window.visualViewport
  if (vv) {
    vv.addEventListener('resize', syncViewportHeight)
    vv.addEventListener('scroll', syncViewportHeight)
  }
  window.addEventListener('resize', syncViewportHeight)
  syncViewportHeight()
})

onBeforeUnmount(() => {
  const vv = window.visualViewport
  if (vv) {
    vv.removeEventListener('resize', syncViewportHeight)
    vv.removeEventListener('scroll', syncViewportHeight)
  }
  window.removeEventListener('resize', syncViewportHeight)
})

function onTab(k: TabKey): void {
  if (k === 'task') {
    // 任务：弹出下发表，不切换内容页（再次点击可关闭）
    showTask.value = !showTask.value
    return
  }
  showTask.value = false
  tab.value = k
}
</script>
