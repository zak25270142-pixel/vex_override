<template>
  <!-- Python 脚本推荐的曲线配置：左下角浮出，5s 内可应用/忽略，超时自消。
       只挂桌面端（App.vue 的非 mobile 分支）：手机曲线页是单通道，4 组建议没有落点。
       :key 绑 id：新建议顶掉旧建议时重建节点，好让倒计时动画从头跑。 -->
  <Teleport to="body">
    <div v-if="sug" :key="sug.id" class="suggest" role="status">
      <div class="suggest__head">
        <span class="suggest__title">Python 推荐了一套曲线配置</span>
        <span v-if="sug.source" class="suggest__src">脚本 {{ sug.source }}</span>
      </div>
      <p v-if="sug.note" class="suggest__note">{{ sug.note }}</p>
      <div class="suggest__groups">
        <div v-for="(group, ch) in sug.groups" :key="ch" class="suggest__row">
          <span class="suggest__ch">通道{{ ch + 1 }}</span>
          <span class="suggest__items">{{ names(group) }}</span>
        </div>
      </div>
      <div class="suggest__foot">
        <button class="suggest__btn suggest__btn--apply" @click="applyChartSuggestion">
          应用
        </button>
        <button class="suggest__btn" @click="dismissChartSuggestion">忽略</button>
        <span class="suggest__hint">覆盖全部通道 · {{ ttlS }}s 后消失</span>
      </div>
      <div class="suggest__timer"><i :style="{ animationDuration: `${ttlS}s` }" /></div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import type { ChartSuggestItem } from '@/services/transport'
import {
  applyChartSuggestion,
  chartSuggestion,
  dismissChartSuggestion,
  SUGGEST_TTL_MS,
} from '@/stores/globle'

const sug = chartSuggestion
const ttlS = SUGGEST_TTL_MS / 1000

/** 空组在该通道上的含义是"清空"，直接写"清空"比留一片空白好懂 */
function names(group: ChartSuggestItem[]): string {
  return group.length ? group.map((it) => it.name).join(' · ') : '清空'
}
</script>

<style scoped>
.suggest {
  position: fixed;
  /* 左下角：右下是右工作区底部常驻的停车按钮，不能被盖 */
  left: 16px;
  bottom: 16px;
  z-index: 1150; /* 高于 toast(1100)：需要操作，不能被异常提示压住 */
  width: 380px;
  padding: 10px 12px 0;
  background: var(--surface-floating);
  border: var(--border-panel);
  border-radius: var(--radius);
  box-shadow: var(--shadow-panel);
  animation: suggest-in 0.18s ease-out;
}

@keyframes suggest-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}

.suggest__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.suggest__title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-bright);
}

.suggest__src {
  padding: 1px 5px;
  font-size: 11px;
  color: var(--text-muted);
  background: var(--accent-soft);
  border-radius: 2px;
}

.suggest__note {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.suggest__groups {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 8px;
}

.suggest__row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.6;
}

.suggest__ch {
  flex-shrink: 0;
  width: 44px;
  color: var(--text-muted);
}

.suggest__items {
  color: var(--text-primary);
  word-break: break-all;
}

.suggest__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 10px;
}

.suggest__btn {
  padding: 3px 10px;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  cursor: pointer;
}

.suggest__btn:hover {
  color: var(--text-bright);
  background: var(--surface-hover);
}

.suggest__btn--apply {
  color: var(--text-on-accent);
  background: var(--accent);
  border-color: var(--accent);
}

.suggest__btn--apply:hover {
  color: var(--text-on-accent);
  background: var(--accent-muted);
}

.suggest__hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-muted);
}

/* 剩余时间条：宽度动画只是"看着在走"，真到点是 store 的定时器清状态 */
.suggest__timer {
  height: 2px;
  overflow: hidden;
  background: var(--surface-input);
}

.suggest__timer i {
  display: block;
  width: 100%;
  height: 100%;
  background: var(--accent);
  transform-origin: left;
  animation-name: suggest-timer;
  animation-timing-function: linear;
  animation-fill-mode: forwards;
}

@keyframes suggest-timer {
  from {
    transform: scaleX(1);
  }
  to {
    transform: scaleX(0);
  }
}
</style>
