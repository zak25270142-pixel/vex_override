<template>
  <!-- 分段滑动控件：项数由 options 决定（手机 2 格 / 桌面 3 格），滑块平移到选中项。
       选中值对外只暴露下标，语义由调用方解释，本组件不认识"演示/后端"等业务含义。 -->
  <div class="seg" role="group" :aria-label="ariaLabel">
    <span
      class="seg__thumb"
      :style="{
        width: `calc((100% - 6px) / ${options.length})`,
        transform: `translateX(${modelValue * 100}%)`,
      }"
    />
    <button
      v-for="(o, i) in options"
      :key="o.value"
      type="button"
      class="seg__opt"
      :class="{ 'is-on': modelValue === i }"
      :disabled="o.disabled"
      :title="o.title"
      @click="pick(i)"
    >
      {{ o.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  modelValue: number
  options: { label: string; value: string; disabled?: boolean; title?: string }[]
  ariaLabel?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: number): void }>()

function pick(i: number): void {
  if (i !== props.modelValue && !props.options[i].disabled) emit('update:modelValue', i)
}
</script>

<style scoped>
.seg {
  position: relative;
  flex: none;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  height: 28px;
  padding: 3px;
  box-sizing: border-box;
  background: var(--surface-sunken, #23262f);
  border: var(--border-panel, 1px solid #2a2e39);
  border-radius: 14px;
}

.seg__thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  height: calc(100% - 6px);
  border-radius: 11px;
  background: var(--accent);
  transition: transform 0.18s ease;
  pointer-events: none;
}

.seg__opt {
  position: relative;
  z-index: 1;
  border: none;
  background: transparent;
  padding: 0 10px;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--text-secondary);
  white-space: nowrap;
  cursor: pointer;
}

.seg__opt.is-on {
  color: var(--text-on-accent);
  font-weight: 700;
}

.seg__opt:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
