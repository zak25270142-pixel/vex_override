<template>
  <!-- 通用面板容器：标题 + 内容，左上荧光装饰条，风格对齐参考项目 -->
  <section class="panel-box" :class="{ 'panel-box--fill': fill }">
    <header v-if="title" class="panel-box__header">
      <span class="panel-box__title">{{ title }}</span>
      <div class="panel-box__extra">
        <slot name="extra" />
      </div>
    </header>
    <div class="panel-box__body">
      <slot />
    </div>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  title?: string
  /** 撑满父容器高度，内容区内部布局交给使用方（用于日志类长面板） */
  fill?: boolean
}>()
</script>

<style scoped>
.panel-box {
  position: relative;
  border: var(--border-panel);
  background: var(--surface-panel);
  box-shadow: var(--shadow-panel);
  border-radius: var(--radius);
  overflow: hidden;
}

.panel-box::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 56px;
  height: 2px;
  background: var(--accent);
  box-shadow: var(--glow-accent);
  z-index: 1;
}

.panel-box__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  border-bottom: var(--border-subtle);
}

.panel-box__title {
  color: var(--text-bright);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.panel-box__body {
  padding: 10px 12px;
}

/* fill：卡片撑满分页高度，body 成为可内部布局的弹性容器 */
.panel-box--fill {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.panel-box--fill .panel-box__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
