<template>
  <!-- 固件回执异常 toast：左下角堆叠、自动消失、点击关闭。
       级别与后端 AlertLevel 对齐：reject 拒绝 / retry 补发中 / giveup 放弃 -->
  <Teleport to="body">
    <div v-if="uiAlerts.length" class="toasts">
      <div
        v-for="a in uiAlerts"
        :key="a.id"
        class="toast"
        :class="`toast--${a.level}`"
        :title="`${LEVEL_TAG[a.level]}：${a.text}（点击关闭）`"
        @click="dismissAlert(a.id)"
      >
        <span class="toast__tag">{{ LEVEL_TAG[a.level] }}</span>
        <span class="toast__text">{{ a.text }}</span>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { dismissAlert, uiAlerts, type UiAlert } from '@/stores/globle'

const LEVEL_TAG: Record<UiAlert['level'], string> = {
  reject: '拒绝',
  retry: '重试',
  giveup: '放弃',
}
</script>

<style scoped>
.toasts {
  position: fixed;
  /* 左下角，不右下：右下是右工作区底部常驻的停车按钮，提示再短命也不能盖安全控件 */
  left: 16px;
  bottom: 16px;
  z-index: 1100; /* 高于诊断弹窗遮罩(1000)：排查时更要看见异常 */
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  /* 容器本身不吃鼠标事件，命中只由单条 toast 自己收 */
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: baseline;
  gap: 8px;
  max-width: 400px;
  padding: 8px 12px;
  font-size: 12.5px;
  line-height: 1.6;
  background: var(--surface-floating);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  box-shadow: var(--shadow-panel);
  cursor: pointer;
  pointer-events: auto;
  animation: toast-in 0.15s ease-out;
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}

.toast__tag {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 800;
}

.toast__text {
  color: var(--text-primary);
  word-break: break-all;
}

/* 配色对齐诊断弹窗的 verdict--bad / verdict--warn，同一套语义色 */
.toast--reject,
.toast--giveup {
  color: var(--status-danger);
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.toast--retry {
  color: var(--status-waiting);
  border-color: rgba(249, 199, 94, 0.5);
  background: rgba(249, 199, 94, 0.08);
}
</style>
