<template>
  <header class="app-header">
    <div class="brand">
      <img class="brand__mark" :src="logoUrl" alt="HDU VEX" />
      <div class="brand__text">
        <strong>VEX V5 调试平台</strong>
        <small>USB SERIAL CONSOLE</small>
      </div>
    </div>

    <!-- 接入方式 -->
    <div
      class="access"
      :class="accessMode === 'serial' ? 'access--real' : 'access--demo'"
      title="当前数据接入方式"
    >
      <span class="access__dot" />
      <span class="access__label">{{
        accessMode === 'demo'
          ? '演示数据源'
          : accessMode === 'backend'
            ? 'PYTHON 后端'
            : serialSupported
              ? 'WEB SERIAL 实机'
              : '串口不可用'
      }}</span>
    </div>

    <div class="status">
      <!-- 常驻链路计数：↑发出帧 ↓接收字节 ⚑解析帧，红=发出请求但收不到任何字节 -->
      <span class="mini-stat" :class="{ 'mini-stat--bad': connected && commStats.txFrames > 0 && commStats.rxBytes === 0 }">
        ↑{{ commStats.txFrames }} ↓{{ commStats.rxBytes }} ⚑{{ commStats.frames }}
      </span>
      <span class="status__dot" :class="`status__dot--${connState}`" />
      <span class="status__text" :class="{ 'status__text--error': connState === 'error' }">
        {{ statusText }}
      </span>
    </div>

    <!-- 诊断弹窗：链路逐段计数、自动结论、收发 hex 日志 -->
    <button class="btn btn--ghost" title="查看通信链路诊断" @click="diagOpen = true">诊断</button>
    <DiagnosticsDialog />

    <!-- 接入方式三选一：Web Serial 实机 / Python backend 中转 / 演示数据源。
         浏览器不支持 Web Serial 时「实机」档禁用（点到也不切换） -->
    <Segmented
      :model-value="accessIndex"
      :options="accessOptions"
      aria-label="接入方式"
      @update:model-value="onAccessChange"
    />
    <input
      v-if="accessMode === 'backend'"
      class="backend-url"
      type="text"
      :value="backendUrlShort"
      placeholder="192.168.1.5:8000"
      spellcheck="false"
      title="后端地址，只填 IP:端口 即可（首尾自动补）；改完点「连接」生效，清空恢复默认"
      @change="onBackendUrlChange"
    />

    <!-- 自动连接：打开页面 / 断线后自动重连已授权的串口设备 -->
    <label class="demo-switch" title="打开页面或断线后自动重连已授权串口（首次需手动选口授权）">
      <input type="checkbox" :checked="autoConnect" @change="onAutoConnectChange" />
      <span>自动连接</span>
    </label>

    <!-- 连接链路：未选口时可先选口，再连接；已连接可断开 -->
    <div class="conn-group">
      <button
        v-if="accessMode === 'serial'"
        class="btn btn--ghost"
        :disabled="connected || connState === 'connecting'"
        title="选择 V5 User 口对应的串口"
        @click="pickPort"
      >
        选口
      </button>
      <button
        class="btn"
        :class="connected ? 'btn--danger' : 'btn--accent'"
        :disabled="connState === 'connecting' || (accessMode === 'serial' && !serialSupported)"
        @click="toggleConnect"
      >
        {{ connState === 'connecting' ? '连接中…' : connected ? '断开' : '连接' }}
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  accessMode,
  autoConnect,
  backendUrlShort,
  commStats,
  connState,
  connected,
  diagOpen,
  pickPort,
  serialSupported,
  setAccessMode,
  setAutoConnect,
  setBackendUrl,
  statusText,
  toggleConnect,
} from '@/stores/globle'
import DiagnosticsDialog from '@/components/shell/DiagnosticsDialog.vue'
import Segmented from '@/components/common/Segmented.vue'
import logoUrl from '@/assets/hdu-vex-logo.jpg'

const ACCESS_MODES = ['serial', 'backend', 'demo'] as const
const accessOptions = computed(() => [
  {
    label: '实机',
    value: 'serial',
    disabled: !serialSupported.value,
    title: serialSupported.value
      ? 'Web Serial 直连 V5 User 口'
      : '当前浏览器不支持 Web Serial，请用 Chrome / Edge',
  },
  { label: '后端', value: 'backend', title: '经 Python backend 中转，可与测试脚本共存' },
  { label: '演示', value: 'demo', title: '无硬件时用内置模拟数据源走通全链路' },
])
const accessIndex = computed(() =>
  accessMode.value === 'demo' ? 2 : accessMode.value === 'backend' ? 1 : 0,
)

function onAccessChange(i: number): void {
  void setAccessMode(ACCESS_MODES[i])
}

function onBackendUrlChange(e: Event): void {
  setBackendUrl((e.target as HTMLInputElement).value)
}

function onAutoConnectChange(e: Event): void {
  setAutoConnect((e.target as HTMLInputElement).checked)
}
</script>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--app-header-height);
  padding: 0 14px;
  background: var(--surface-floating);
  border-bottom: var(--border-panel);
  flex-shrink: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  padding-right: 12px;
  border-right: var(--border-subtle);
  min-width: 0; /* 允许整块收缩，标题改单行截断而非换行 */
}

.brand__mark {
  display: block;
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 50%;
  box-shadow: var(--glow-accent);
  flex-shrink: 0;
}

.brand__text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
  overflow: hidden;
}

.brand__text strong,
.brand__text small {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.brand__text strong {
  font-size: 14px;
  color: var(--text-bright);
}

.brand__text small {
  font-size: 9px;
  letter-spacing: 0.18em;
  color: var(--text-muted);
}

/* ===== 通用按钮 ===== */
.btn {
  height: 30px;
  padding: 0 14px;
  font-size: 12.5px;
  font-family: inherit;
  font-weight: 700;
  border-radius: var(--radius);
  cursor: pointer;
  white-space: nowrap;
  transition:
    box-shadow 0.15s,
    opacity 0.15s,
    border-color 0.15s;
}

.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn--accent {
  color: var(--text-on-accent);
  background: var(--accent);
  border: 1px solid var(--accent);
}

.btn--accent:not(:disabled):hover {
  box-shadow: var(--glow-accent);
}

.btn--danger {
  color: var(--status-danger);
  background: var(--danger-soft);
  border: 1px solid var(--danger-border);
}

.btn--danger:not(:disabled):hover {
  box-shadow: var(--glow-danger);
}

.btn--ghost {
  color: var(--text-secondary);
  background: transparent;
  border: var(--border-subtle);
  font-weight: 600;
}

.btn--ghost:not(:disabled):hover {
  color: var(--accent);
  border-color: var(--accent-border);
  box-shadow: var(--shadow-focus);
}

.conn-group {
  display: flex;
  gap: 6px;
}

/* 接入方式徽标 */
.access {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border: 1px solid;
  min-width: 0; /* 允许收缩，文字单行截断 */
  overflow: hidden;
}

.access__label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.access--demo {
  color: var(--status-waiting);
  border-color: rgba(249, 199, 94, 0.5);
  background: rgba(249, 199, 94, 0.1);
}

.access--real {
  color: var(--status-success);
  border-color: var(--success-border);
  background: var(--success-soft);
}

.access__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 6px currentColor;
  flex-shrink: 0;
}

/* 窄窗口：链路计数让位给数据源徽标——.status 空间不足时 nowrap 的计数会向左
   溢出盖住左侧接入徽标；计数属次要诊断（诊断弹窗里有完整版），窄屏隐藏 */
@media (max-width: 1024px) {
  .mini-stat {
    display: none;
  }
}

.status {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 7px;
  min-width: 0;
}

.mini-stat {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 2px 8px;
  border: var(--border-subtle);
  border-radius: 999px;
  white-space: nowrap;
}

.mini-stat--bad {
  color: var(--status-danger);
  border-color: var(--danger-border);
  box-shadow: var(--glow-danger);
}

.status__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-offline);
  flex-shrink: 0;
}

.status__dot--connected {
  background: var(--status-success);
  box-shadow: var(--glow-success);
}

.status__dot--connecting {
  background: var(--status-waiting);
  box-shadow: var(--glow-waiting);
}

.status__dot--error {
  background: var(--status-danger);
  box-shadow: var(--glow-danger);
}

.status__text {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.status__text--error {
  color: var(--status-danger);
}

/* 演示开关 */
.demo-switch {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}

.demo-switch input {
  accent-color: var(--accent);
  cursor: pointer;
}

/* 后端地址输入框：默认按页面来源推导，只有跨机/手机场景才需要手填 */
.backend-url {
  width: 170px;
  height: 26px;
  padding: 0 8px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-secondary);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  flex-shrink: 0;
}

.backend-url:focus {
  outline: none;
  border-color: var(--accent-border);
  color: var(--text-primary);
}

/* 窄窗口让位：顶栏空间优先给状态与连接按钮，地址在手机设置页改即可 */
@media (max-width: 1366px) {
  .backend-url {
    display: none;
  }
}
</style>
