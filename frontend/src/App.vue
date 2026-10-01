<template>
  <!-- 单一工作页：顶栏（接入+取表） + 主体（导航 + 4个分页） -->
  <div class="app-shell">
    <AppHeader />
    <main class="workspace">
      <!-- 左70%：参数表/波形/场地/视觉；右30%：调参表选中项时为参数调整卡片，否则为任务下发表单 -->
      <div class="workspace__split">
        <div class="workspace__left">
          <ViewStage />
        </div>
        <!-- 曲线通道配置：分图时常驻右栏，大图时默认隐藏、点"通道设置"成浮层（曲线占满宽） -->
        <ScopeConfig
          v-if="view === 'chart' && focusCh !== null && (!bigView || scopeDrawer)"
          :key="`scope-${focusCh}`"
          :class="bigView ? 'workspace__overlay' : 'workspace__right'"
        />
        <!-- 大图模式且浮层未呼出：右栏整体留空，让曲线占满整个工作区宽度 -->
        <template v-else-if="view === 'chart' && bigView" />
        <TunablePanel
          v-else-if="paramTable === 'tunable' && selectedTunableItem"
          class="workspace__right"
        />
        <CommandPanel v-else class="workspace__right" />
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import AppHeader from '@/components/shell/AppHeader.vue'
import ViewStage from '@/components/stage/ViewStage.vue'
import CommandPanel from '@/components/command/CommandPanel.vue'
import TunablePanel from '@/components/params/TunablePanel.vue'
import ScopeConfig from '@/components/chart/ScopeConfig.vue'
import { bigView, autoConnect, baud, demoMode, focusCh, paramTable, scopeDrawer, selectedTunableItem, setDemoMode, view } from '@/stores/globle'
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

/* 左70%四个视图 / 右30%任务下发表单；窄屏时右栏保底240px，左栏承担压缩。
   大图模式的通道配置脱离文档流做右侧浮层，让曲线占满整个工作区宽度 */
.workspace__split {
  position: relative;
  display: flex;
  gap: var(--workspace-gap);
  height: 100%;
}

.workspace__left {
  flex: 7 1 0;
  min-width: 0;
}

.workspace__right {
  flex: 3 1 0;
  min-width: 240px;
}

/* 浮层只盖图表区，让开顶部导航(44)+间距(10)+曲线工具栏(34)=88px，避免遮挡分图/大图等按钮 */
.workspace__overlay {
  position: absolute;
  top: 88px;
  right: 0;
  bottom: 0;
  width: min(320px, 34%);
  z-index: 20;
  box-shadow: -8px 0 28px rgba(0, 0, 0, 0.45);
}
</style>
