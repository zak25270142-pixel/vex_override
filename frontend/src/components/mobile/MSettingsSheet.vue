<template>
  <!-- 设置：右上角齿轮触发；接入方式（后端/演示两档）+ 自动连接 + 连接状态 + 连接/断开 -->
  <MBottomSheet v-model="model" title="设置">
    <template #head-right>
      <BatteryBadge :level="batteryLevel" />
      <button class="m-sheet__refresh" :disabled="!connected || refreshing" @click="onRefresh">
        {{ refreshing ? '刷新中…' : '刷新' }}
      </button>
    </template>

    <div class="m-row-card">
      <div class="set-row">
        <span>接入方式</span>
        <Segmented
          :model-value="accessIndex"
          :options="accessOptions"
          aria-label="接入方式"
          @update:model-value="onAccess"
        />
      </div>
      <p class="m-note">
        {{
          isBackend
            ? '经 Python backend 中转连接下位机，地址默认按页面来源推导。'
            : '内置模拟机器人，零硬件零后端直接看效果。'
        }}
      </p>
      <input
        v-if="isBackend"
        class="m-url"
        type="text"
        :value="backendUrlShort"
        placeholder="192.168.1.5:8000"
        spellcheck="false"
        autocapitalize="off"
        autocorrect="off"
        @change="onBackendUrl"
      />
      <p v-if="isBackend" class="m-note">只填 IP:端口 即可，首尾自动补；清空恢复默认。</p>
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
        连接
      </button>
      <button
        v-else
        class="m-btn m-btn--plain"
        style="width: 100%; margin-top: 10px"
        @click="onDisconnect"
      >
        断开
      </button>

    </div>
  </MBottomSheet>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  accessMode,
  autoConnect,
  backendUrlShort,
  batteryLevel,
  connected,
  connState,
  refreshAll,
  setAccessMode,
  setAutoConnect,
  setBackendUrl,
  statusText,
  toggleConnect,
} from '@/stores/globle'
import MBottomSheet from './MBottomSheet.vue'
import BatteryBadge from '@/components/common/BatteryBadge.vue'
import Segmented from '@/components/common/Segmented.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const model = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

// 手机没有 Web Serial，只有两档：没选演示就必然是后端
const ACCESS_MODES = ['backend', 'demo'] as const
const accessOptions = [
  { label: '后端', value: 'backend', title: '经 Python backend 中转连接下位机' },
  { label: '演示', value: 'demo', title: '内置模拟数据源，零硬件零后端' },
]
const isBackend = computed(() => accessMode.value !== 'demo')
const accessIndex = computed(() => (isBackend.value ? 0 : 1))

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

async function onAccess(i: number): Promise<void> {
  await setAccessMode(ACCESS_MODES[i])
}
function onBackendUrl(e: Event): void {
  setBackendUrl((e.target as HTMLInputElement).value)
}
function onAuto(): void {
  setAutoConnect(!autoConnect.value)
}
/** 按当前接入方式连。以前这里在手机（无 Web Serial）上强制切演示，
 *  导致后端模式下点连接也会被拉去演示，后端只能靠自动连上 */
async function onConnect(): Promise<void> {
  await toggleConnect()
}
async function onDisconnect(): Promise<void> {
  await toggleConnect()
}

/** 刷新中标志：refreshAll 只发请求不等回传，瞬间就返回，这里主要防连点并发重拉 */
const refreshing = ref(false)
async function onRefresh(): Promise<void> {
  refreshing.value = true
  try {
    await refreshAll()
  } finally {
    refreshing.value = false
  }
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

.m-sheet__refresh {
  flex: none;
  padding: 5px 12px;
  font-size: 13px;
  font-family: inherit;
  font-weight: 600;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle, #2a2e38);
  border-radius: 8px;
  white-space: nowrap;
  cursor: pointer;
}
.m-sheet__refresh:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent-border);
}
.m-sheet__refresh:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.m-url {
  width: 100%;
  margin-top: 10px;
  padding: 8px 10px;
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  color: var(--text-primary);
  background: transparent;
  border: 1px solid var(--border-subtle, #2a2e38);
  border-radius: 8px;
  box-sizing: border-box;
}
.m-url:focus {
  outline: none;
  border-color: var(--accent-border, var(--accent));
}
</style>
