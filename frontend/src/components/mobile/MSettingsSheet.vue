<template>
  <!-- 设置：右上角齿轮触发；演示/后端/自动连接开关 + 连接状态 + 连接/断开 -->
  <MBottomSheet v-model="model" title="设置">
    <div class="m-row-card">
      <div class="set-row">
        <span>演示模式</span>
        <input type="checkbox" class="m-switch" :checked="demoMode" @change="onDemo" />
      </div>
      <p class="m-note">开启后用内置模拟机器人，零硬件零后端直接看效果。</p>
    </div>

    <div class="m-row-card">
      <div class="set-row">
        <span>后端模式（Python）</span>
        <input type="checkbox" class="m-switch" :checked="backendMode" @change="onBackend" />
      </div>
      <p class="m-note">经本机 Python backend 中转连接下位机（需本地运行 FastAPI）。</p>
    </div>

    <div class="m-row-card">
      <div class="set-row">
        <span>自动连接</span>
        <input type="checkbox" class="m-switch" :checked="autoConnect" @change="onAuto" />
      </div>
    </div>

    <div class="m-row-card">
      <div class="set-row">
        <i class="m-dot" :class="`m-dot--${dot}`" />
        <span>{{ statusText }}</span>
      </div>

      <button
        v-if="!connected"
        class="m-btn m-btn--primary"
        style="width: 100%; margin-top: 10px"
        @click="onConnect"
      >
        {{ serialSupported ? '连接' : '开启演示' }}
      </button>
      <button
        v-else
        class="m-btn m-btn--plain"
        style="width: 100%; margin-top: 10px"
        @click="onDisconnect"
      >
        断开
      </button>

      <button
        v-if="serialSupported && !connected && !demoMode"
        class="m-btn m-btn--plain"
        style="width: 100%; margin-top: 8px"
        @click="onPick"
      >
        选择串口
      </button>
    </div>
  </MBottomSheet>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  autoConnect,
  backendMode,
  connected,
  connState,
  demoMode,
  pickPort,
  serialSupported,
  setAutoConnect,
  setBackendMode,
  setDemoMode,
  statusText,
  toggleConnect,
} from '@/stores/globle'
import MBottomSheet from './MBottomSheet.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const model = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const dot = computed(() => {
  switch (connState.value) {
    case 'connected':
      return 'connected'
    case 'connecting':
      return 'connecting'
    case 'error':
      return 'error'
    default:
      return 'idle'
  }
})

async function onDemo(): Promise<void> {
  await setDemoMode(!demoMode.value)
}
async function onBackend(): Promise<void> {
  await setBackendMode(!backendMode.value)
}
function onAuto(): void {
  setAutoConnect(!autoConnect.value)
}
async function onConnect(): Promise<void> {
  if (connected.value) {
    await toggleConnect()
    return
  }
  if (serialSupported.value) await toggleConnect()
  else await setDemoMode(true)
}
async function onDisconnect(): Promise<void> {
  await toggleConnect()
}
async function onPick(): Promise<void> {
  await pickPort()
}
</script>

<style scoped>
.set-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  color: var(--text-primary);
}
</style>
