<template>
  <!-- 手机「按键」页：与桌面右栏同一套 quick 面板（两列大按钮 + 只读绑定 + 键盘开关） -->
  <section class="m-keys">
    <div class="m-row-card">
      <header class="m-keys__head">按键触发</header>
      <KeyMapPanel variant="quick" />
    </div>
  </section>
</template>

<script setup lang="ts">
import KeyMapPanel from '@/components/keys/KeyMapPanel.vue'
</script>

<style scoped>
/* 与配置页同规则：页面撑满内容区，卡片吃剩余高度，面板内部滚动 */
.m-keys {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.m-keys .m-row-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.m-keys .m-row-card > .keymap {
  flex: 1;
  min-height: 0;
}

.m-keys__head {
  flex: none;
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
}
</style>
