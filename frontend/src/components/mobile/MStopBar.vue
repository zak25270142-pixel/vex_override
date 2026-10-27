<template>
  <!-- 常驻停车按钮：放进顶栏（标题右侧），不再单独占底部一行。
       为什么不放在底部：任务面板是从底部上滑的**分屏**面板，底部那行既盖不住
       也压不下去，夹在面板与 tab 栏之间视觉上很别扭；顶栏则永远可见、不被挤压。
       规则与桌面 RightWorkbench 停车条一致：
       - 常驻：所有 tab 都在，任务面板弹起时也不消失
       - 文案取命令目录里的中文名，不硬编码（固件改名即跟随）
       - 目录未登记停车命令 → 不渲染，不放按了没反应的假按钮
       - 未连接 → 禁用（不是隐藏，让"现在能不能停"一眼可见）
       - 直发无参、不过确认（急停不该有二次确认）
       多条停车命令时并排渲染，各自截断不溢出 -->
  <button
    v-for="c in stopCmds"
    :key="c.index"
    class="m-topbar__stop"
    :title="c.name"
    :disabled="!connected"
    @click="onStop(c)"
  >
    {{ c.name }}
  </button>
</template>

<script setup lang="ts">
import { connected, sendCommand, stopCmds } from '@/stores/globle'
import type { CmdDirItem } from '@/services/protocol'

/** 一键停车：直发对应停车特化命令（无参），不依赖任何页面的选中项 */
async function onStop(cmd: CmdDirItem): Promise<void> {
  if (!connected.value) return
  await sendCommand(cmd.index, [])
}
</script>

<style scoped>
.m-topbar__stop {
  flex: none;
  width: 76px; /* 定宽：横滑行内每个按键等宽 */
  height: 30px;
  padding: 0 8px;
  font-size: 13px;
  font-family: inherit;
  font-weight: 700;
  letter-spacing: 1px;
  color: #fff;
  background: var(--status-danger);
  border: none;
  border-radius: 8px;
  cursor: pointer;
  box-shadow: var(--glow-danger, none);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.m-topbar__stop:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  box-shadow: none;
}
/* 覆盖全局 button:hover/focus 的蓝色（--surface-hover 是半透明青），
   否则移动端点击后 :hover 会粘住、按钮停在蓝底上不恢复；桌面端悬停也保持红底 */
.m-topbar__stop:hover:not(:disabled),
.m-topbar__stop:focus,
.m-topbar__stop:focus-visible {
  background: var(--status-danger);
  border-color: var(--danger-border);
  color: #fff;
  box-shadow: var(--glow-danger);
  outline: none;
}
/* 急停没有二次确认，按下必须给一次明确的触达反馈 */
.m-topbar__stop:not(:disabled):active {
  background: var(--danger-deep);
  transform: translateY(1px);
}
</style>
