<template>
  <!-- 手机配置页：当前只有按键映射一组，面板内部是两列触屏网格 -->
  <section class="m-config">
    <div class="m-row-card">
      <header class="m-config__head">按键与命令</header>
      <KeyMapPanel variant="mobile" />
    </div>
  </section>
</template>

<script setup lang="ts">
import KeyMapPanel from '@/components/keys/KeyMapPanel.vue'
</script>

<style scoped>
/* 页面撑满内容区高度，卡片吃掉剩余空间；内容在面板内部滚动，避免外层再滚一层 */
.m-config {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.m-config .m-row-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.m-config .m-row-card > .keymap {
  flex: 1;
  min-height: 0;
}

.m-config__head {
  flex: none;
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
}
</style>
