<template>
  <!-- 单一工作页：顶栏（接入+取表） + 主体（导航 + 4个分页） -->
  <div class="app-shell">
    <AppHeader />
    <main class="workspace">
      <!-- 左70%：参数表/波形/场地/视觉；右30%：任务下发表单 -->
      <div class="workspace__split">
        <div class="workspace__left">
          <ViewStage />
        </div>
        <CommandPanel class="workspace__right" />
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import AppHeader from '@/components/shell/AppHeader.vue'
import ViewStage from '@/components/stage/ViewStage.vue'
import CommandPanel from '@/components/command/CommandPanel.vue'
import { serialClient } from '@/services/serialClient'

onMounted(() => {
  void serialClient.init()
  window.addEventListener('beforeunload', onBeforeUnload)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

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

/* 左70%四个视图 / 右30%任务下发表单；窄屏时右栏保底240px，左栏承担压缩 */
.workspace__split {
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
</style>
