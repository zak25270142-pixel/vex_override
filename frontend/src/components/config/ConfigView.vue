<template>
  <!-- 页面5：配置。当前只有按键映射一组，后续按组追加 section -->
  <div class="config">
    <section class="config__group">
      <header class="config__group-head">
        <strong class="config__group-name">按键与命令</strong>
        <small class="config__group-desc"
          >把键盘、鼠标点击与触屏映射到按键表项；停车命令也在这里单击下发</small
        >
      </header>
      <KeyMapPanel variant="desktop" />
    </section>
  </div>
</template>

<script setup lang="ts">
import KeyMapPanel from '@/components/keys/KeyMapPanel.vue'
</script>

<style scoped>
.config {
  height: 100%;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 2px;
}

/* 大外框撑满可用高度；内容超出时在 KeyMapPanel 内滚动，不让整页出现第二层滚动。
   全页只有这一层浮起表面（surface-panel），内部列表/行不再叠底色 */
.config__group {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  padding: 14px;
}

.config__group > .keymap {
  flex: 1;
  min-height: 0;
}

.config__group-head {
  flex: none;
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.config__group-name {
  color: var(--text-primary);
  font-size: 14px;
}

.config__group-desc {
  color: var(--text-muted);
  font-size: 12px;
}
</style>
