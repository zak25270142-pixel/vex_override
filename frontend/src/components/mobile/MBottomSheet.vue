<template>
  <!-- 复用底部上滑 sheet：可选不遮罩（任务下发用，背景不变暗），头部左侧可插返回键 -->
  <Teleport to="body">
    <div v-if="modelValue && !noMask" class="m-sheet-mask" @click="close" />
    <Transition name="m-sheet">
      <div v-if="modelValue" class="m-sheet" :class="{ 'm-sheet--half': half }" @click.stop>
        <div class="m-sheet__head">
          <span v-if="$slots['head-left']" class="m-sheet__head-left"><slot name="head-left" /></span>
          <span class="m-sheet__title">{{ title }}</span>
          <span v-if="$slots['head-right']" class="m-sheet__head-right"><slot name="head-right" /></span>
          <button class="m-sheet__close" @click="close">关闭</button>
        </div>
        <div class="m-sheet__body">
          <slot />
        </div>
        <div v-if="$slots.foot" class="m-sheet__foot">
          <slot name="foot" />
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: boolean
  title?: string
  /** 占屏一半高度（数据/调参详情用） */
  half?: boolean
  /** 不渲染变暗遮罩，背景保持原色调（任务下发用） */
  noMask?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
function close(): void {
  emit('update:modelValue', false)
}
</script>

<style scoped>
.m-sheet-enter-active,
.m-sheet-leave-active {
  transition: transform 0.22s ease;
}
.m-sheet-enter-from,
.m-sheet-leave-to {
  transform: translateY(100%);
}
.m-sheet-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 50;
}
.m-sheet {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 51;
  display: flex;
  flex-direction: column;
  background: #1b1e26;
  border-radius: 16px 16px 0 0;
  max-height: 90vh;
  padding-bottom: env(safe-area-inset-bottom, 0);
}
.m-sheet--half {
  height: 50vh;
}
.m-sheet__head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border-subtle, #23262f);
}
.m-sheet__head-left {
  flex: none;
}
.m-sheet__head-right {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
}
.m-sheet__title {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.m-sheet__close {
  flex: none;
  font-size: 13px;
  color: var(--text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 4px 8px;
}
.m-sheet__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
}
.m-sheet__foot {
  flex: none;
  padding: 10px 16px 14px;
}
</style>
