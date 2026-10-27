<template>
  <!-- 三态分段控件：未订 / 低速 / 高速；滑块按状态着色 灰→黄→绿 -->
  <div class="m-seg3" role="group" :aria-label="ariaLabel">
    <span
      class="m-seg3__thumb"
      :data-state="modelValue"
      :style="{ transform: `translateX(${modelValue * 100}%)` }"
    />
    <button
      v-for="(label, i) in labels"
      :key="i"
      type="button"
      class="m-seg3__opt"
      :class="{ 'is-on': modelValue === i }"
      :data-i="i"
      @click="pick(i)"
    >
      {{ label }}
    </button>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  modelValue: number
  labels: string[]
  ariaLabel?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: number): void }>()

function pick(i: number): void {
  if (i !== props.modelValue) emit('update:modelValue', i)
}
</script>
