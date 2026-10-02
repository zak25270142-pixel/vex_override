<template>
  <!-- 单一工作页：顶栏（接入+取表） + 主体（左视图 + 右标签工作区） -->
  <div class="app-shell">
    <AppHeader />
    <main class="workspace">
      <!-- 左：参数表/曲线/场地/视觉；右：标签分页工作区（宽度可拖拽，折叠收成竖条） -->
      <div
        ref="splitEl"
        class="workspace__split"
        :class="{ 'workspace__split--collapsed': rightCollapsed }"
      >
        <div class="workspace__left">
          <ViewStage />
        </div>

        <!-- 拖拽手柄：未折叠时显示，拖到极窄自动吸附折叠 -->
        <div
          v-show="!rightCollapsed"
          class="workspace__resizer"
          :class="{ 'workspace__resizer--dragging': dragging }"
          @pointerdown="onResizeStart"
        >
          <!-- 折叠按钮：位于垂直 50% 处，凹向右的 》 标签 -->
          <button
            class="workspace__collapse-btn"
            title="折叠右侧工作区"
            @pointerdown.stop
            @click="rightCollapsed = true"
          >
            》
          </button>
        </div>

        <RightWorkbench
          v-show="!rightCollapsed"
          class="workspace__right"
          :style="{ width: rightWidthPct * 100 + '%' }"
        />

        <!-- 折叠后：浮在右边缘的小巧《 按钮，绝对定位不占布局空间，左视图用满全宽 -->
        <button
          v-show="rightCollapsed"
          class="workspace__rail"
          title="展开右侧工作区"
          @click="rightCollapsed = false"
        >
          《
        </button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import AppHeader from '@/components/shell/AppHeader.vue'
import ViewStage from '@/components/stage/ViewStage.vue'
import RightWorkbench from '@/components/shell/RightWorkbench.vue'
import {
  autoConnect,
  baud,
  demoMode,
  rightCollapsed,
  rightWidthPct,
  setDemoMode,
  RIGHT_WIDTH_MAX,
  RIGHT_WIDTH_MIN,
} from '@/stores/globle'
import { serialClient } from '@/services/serialClient'

onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
  void startup()
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

async function startup(): Promise<void> {
  // 沿用上次的演示模式；实机模式下按开关自动连接已授权设备
  if (demoMode.value) {
    await setDemoMode(true)
    return
  }
  await serialClient.init()
  if (autoConnect.value) await serialClient.autoConnect(baud)
}

function onBeforeUnload() {
  // 尽力释放串口，避免刷新后短暂占用
  void serialClient.disconnect()
}

/* ===== 右栏拖拽宽度：Pointer Events 兼容鼠标/触控；宽度用比例，窗口缩放时不变 ===== */
const splitEl = ref<HTMLElement | null>(null)
const dragging = ref(false)

function onResizeStart(e: PointerEvent): void {
  if (rightCollapsed.value) return
  dragging.value = true
  ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

function onPointerMove(e: PointerEvent): void {
  if (!dragging.value || !splitEl.value) return
  const rect = splitEl.value.getBoundingClientRect()
  // 右栏宽度 = 右边缘 - 鼠标 x（手柄在右栏左侧）
  const rightPx = rect.right - e.clientX
  // 拖到极窄（<80px）继续拖 → 吸附折叠
  if (rightPx < 80) {
    rightCollapsed.value = true
    onPointerUp()
    return
  }
  let pct = rightPx / rect.width
  pct = Math.min(RIGHT_WIDTH_MAX, Math.max(RIGHT_WIDTH_MIN, pct))
  rightWidthPct.value = pct
}

function onPointerUp(): void {
  if (!dragging.value) return
  dragging.value = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}

onMounted(() => {
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
})

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
})
</script>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.workspace {
  flex: 1;
  min-height: 0;
  padding: var(--workspace-gap) var(--workspace-padding) var(--workspace-padding);
}

/* 左：主视图；右：标签工作区（宽度可拖拽，折叠时只剩竖条）。
   间隙由 resizer 承担，避免拖时跳动 */
.workspace__split {
  position: relative;
  display: flex;
  height: 100%;
}

.workspace__left {
  flex: 1 1 0;
  min-width: 0;
}

.workspace__right {
  flex: none;
  min-width: 0;
}

/* 拖拽手柄：6px 热区保证好按，视觉高亮只有中间 1px 竖线 */
.workspace__resizer {
  flex: none;
  width: 6px;
  cursor: col-resize;
  background: transparent;
  position: relative;
  z-index: 5;
}

.workspace__resizer::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 0;
  bottom: 0;
  width: 1px;
  transform: translateX(-0.5px);
  background: transparent;
  transition: background 0.12s;
}

.workspace__resizer:hover::after,
.workspace__resizer--dragging::after {
  background: var(--accent);
}

/* 折叠按钮：位于垂直 50% 处，凹向右的 》 标签（从左面板右边缘向右凸出） */
.workspace__collapse-btn {
  position: absolute;
  top: 50%;
  right: -18px;
  transform: translateY(-50%);
  width: 18px;
  height: 48px;
  padding: 0;
  font-size: 14px;
  line-height: 48px;
  text-align: center;
  font-family: inherit;
  color: var(--text-muted);
  background: var(--surface-floating);
  border: var(--border-panel);
  border-left: none;
  border-radius: 0 var(--radius) var(--radius) 0;
  cursor: pointer;
  z-index: 6;
  /* 默认隐藏：只有鼠标悬停分隔线（或正在拖拽）时才出现，平时不挡右栏内容；
     隐藏时连同点击命中一起关闭，凸出右栏的 18px 不会拦截鼠标 */
  opacity: 0;
  pointer-events: none;
  transition: color 0.12s, border-color 0.12s, opacity 0.15s;
}

.workspace__resizer:hover .workspace__collapse-btn,
.workspace__resizer--dragging .workspace__collapse-btn,
.workspace__collapse-btn:focus-visible {
  opacity: 1;
  pointer-events: auto;
}

.workspace__collapse-btn:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

/* 折叠后的展开按钮：浮在工作区右边缘的小巧《，绝对定位不占位，左视图可用满全宽 */
.workspace__rail {
  position: absolute;
  top: 50%;
  /* 抵消 .workspace 的右侧内边距，按钮外沿正好贴住浏览器壁 */
  right: calc(-1 * var(--workspace-padding));
  transform: translateY(-50%);
  width: 16px;
  height: 44px;
  padding: 0;
  font-size: 12px;
  line-height: 44px;
  text-align: center;
  font-family: inherit;
  color: var(--text-muted);
  background: var(--surface-floating);
  border: var(--border-panel);
  border-right: none;
  border-radius: var(--radius) 0 0 var(--radius);
  /* 浮在地图/曲线上方时保证能看清 */
  box-shadow: -2px 2px 10px rgba(0, 0, 0, 0.3);
  cursor: pointer;
  z-index: 6;
  transition: color 0.12s, border-color 0.12s, box-shadow 0.12s;
}

.workspace__rail:hover {
  color: var(--accent);
  border-color: var(--accent-border);
  box-shadow: -2px 2px 10px rgba(0, 0, 0, 0.3), var(--shadow-focus);
}
</style>
