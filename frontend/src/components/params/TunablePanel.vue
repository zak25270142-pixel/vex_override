<template>
  <!-- 右侧30%调参详情：参数表单击某行后在此修改，提交后等主控回显确认真实生效值 -->
  <aside class="tun-panel">
    <div class="tun-panel__head">
      <span class="tun-panel__title">参数调整</span>
      <button class="back-btn" title="返回任务下发表单" @click="onClose">× 返回</button>
    </div>

    <div v-if="item" class="tun-panel__body">
      <!-- 条目身份 -->
      <div class="item-head">
        <span class="item-head__name">{{ item.name }}</span>
        <span class="item-head__idx">#{{ item.index }}</span>
      </div>
      <div class="badges">
        <em class="badge badge--type">{{ typeName(item.type) }}</em>
        <em v-if="item.unit" class="badge badge--unit">{{ item.unit }}</em>
      </div>

      <!-- 主控里的真实当前值（回显到达后更新） -->
      <div class="current">
        <span class="current__label">主控当前值</span>
        <span class="current__value">
          {{ formatValue(item, item.value) }}
          <i v-if="item.unit">{{ item.unit }}</i>
        </span>
      </div>

      <!-- str/other 目录里不会下发，兜底只读 -->
      <template v-if="valueSize(item.type) > 0">
        <span class="edit-label">写入新值</span>

        <!-- 布尔类：勾选即开，提交时写 1/0 -->
        <label v-if="isBool" class="bool-row">
          <input v-model="draftBool" class="switch" type="checkbox" :disabled="waiting" />
          <span :class="{ on: draftBool }">{{ draftBool ? '开' : '关' }}</span>
        </label>

        <!-- 颜色：取色器 + 十六进制 -->
        <div v-else-if="item.type === ValueType.Color" class="color-row">
          <input v-model="draftColor" class="color-edit" type="color" :disabled="waiting" />
          <span class="color-hex">{{ draftColor.toUpperCase() }}</span>
        </div>

        <!-- 数值类：大号输入框，右侧挂单位；回车即提交 -->
        <div v-else class="input-wrap">
          <input
            v-model="draftNum"
            class="num-edit"
            type="number"
            :step="isFloat ? 'any' : '1'"
            :disabled="waiting"
            autofocus
            @input="errorMsg = ''"
            @keyup.enter="onSubmit"
          />
          <span v-if="item.unit" class="num-suffix">{{ item.unit }}</span>
        </div>

        <p v-if="errorMsg" class="msg msg--err">{{ errorMsg }}</p>
        <p v-if="waiting" class="msg msg--wait">已下发，等待主控回显确认…</p>

        <button class="submit-btn" :disabled="waiting || !connected" @click="onSubmit">
          {{ waiting ? '等待确认…' : '提交修改' }}
        </button>
      </template>
      <p v-else class="msg msg--err">该类型只读，不能修改</p>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ValueType, typeName, valueSize } from '@/services/protocol'
import {
  connected,
  formatValue,
  pendingTunable,
  selectedTunableIndex,
  selectedTunableItem,
  setTunableValue,
} from '@/stores/globle'

const item = selectedTunableItem

const isBool = computed(
  () => item.value?.type === ValueType.Bool || item.value?.type === ValueType.OnOff,
)
const isFloat = computed(
  () => item.value?.type === ValueType.Float || item.value?.type === ValueType.Double,
)
const waiting = computed(() => item.value !== null && pendingTunable.has(item.value!.index))

/* 三类草稿：数值走 number 输入框（v-model 自动转型），布尔走勾选，颜色走 hex 字符串 */
const draftNum = ref<string | number>(0)
const draftBool = ref(false)
const draftColor = ref('#000000')
const errorMsg = ref('')

/** 切条目或回显/超时结束时，用主控当前值重置草稿 */
function resetDraft(): void {
  if (!item.value) return
  errorMsg.value = ''
  const v = item.value.value
  if (isBool.value) draftBool.value = Number(v) !== 0
  else if (item.value.type === ValueType.Color) draftColor.value = String(v)
  else draftNum.value = typeof v === 'number' ? v : 0
}

watch(
  () => item.value?.index,
  () => resetDraft(),
  { immediate: true },
)
// 等待确认 true→false（收到回显或超时）后，把草稿对齐到主控真实值
watch(waiting, (w, old) => {
  if (old && !w) resetDraft()
})

async function onSubmit(): Promise<void> {
  if (!item.value || waiting.value) return
  const index = item.value.index
  if (isBool.value) {
    await setTunableValue(index, draftBool.value ? 1 : 0)
    return
  }
  if (item.value.type === ValueType.Color) {
    if (!/^#[0-9a-f]{6}$/i.test(draftColor.value)) {
      errorMsg.value = '颜色格式应为 #RRGGBB'
      return
    }
    await setTunableValue(index, draftColor.value)
    return
  }
  const n = Number(draftNum.value)
  if (!Number.isFinite(n)) {
    errorMsg.value = '请输入有效数字'
    return
  }
  await setTunableValue(index, n)
}

function onClose(): void {
  selectedTunableIndex.value = null
}
</script>

<style scoped>
.tun-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.tun-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: var(--border-panel);
  flex-shrink: 0;
}

.tun-panel__title {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--text-primary);
}

.back-btn {
  padding: 2px 8px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
}

.back-btn:hover {
  color: var(--text-bright);
  border-color: var(--accent-border);
}

.tun-panel__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.item-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.item-head__name {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-bright);
}

.item-head__idx {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-muted);
}

.badges {
  display: flex;
  gap: 8px;
}

.badge {
  font-style: normal;
  font-family: var(--font-mono);
  font-size: 11.5px;
  border-radius: 4px;
  padding: 1px 7px;
  border: var(--border-subtle);
}

.badge--type {
  color: var(--text-secondary);
}

.badge--unit {
  color: var(--accent);
  border-color: var(--accent-border);
  text-shadow: var(--glow-accent);
}

.current {
  margin-top: 4px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--surface-floating);
  border: var(--border-subtle);
  border-radius: var(--radius);
}

.current__label {
  font-size: 12px;
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.current__value {
  font-family: var(--font-mono);
  font-size: 26px;
  font-weight: 700;
  color: var(--text-bright);
}

.current__value i {
  font-style: normal;
  font-family: inherit;
  font-size: 14px;
  font-weight: 400;
  color: var(--text-secondary);
  margin-left: 4px;
}

.edit-label {
  margin-top: 6px;
  font-size: 12px;
  letter-spacing: 0.08em;
  color: var(--text-secondary);
}

.input-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.num-edit {
  width: 100%;
  height: 40px;
  padding: 0 64px 0 12px;
  font-size: 17px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
  box-sizing: border-box;
}

.num-edit:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}

.num-edit:disabled {
  opacity: 0.55;
}

.num-suffix {
  position: absolute;
  right: 12px;
  font-size: 13px;
  color: var(--text-muted);
  pointer-events: none;
}

.bool-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  color: var(--text-secondary);
  cursor: pointer;
}

.bool-row .on {
  color: var(--status-success);
  text-shadow: var(--glow-success);
  font-weight: 700;
}

.switch {
  width: 20px;
  height: 20px;
  cursor: pointer;
  accent-color: var(--accent);
}

.color-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.color-edit {
  width: 56px;
  height: 36px;
  padding: 2px;
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  cursor: pointer;
}

.color-hex {
  font-family: var(--font-mono);
  font-size: 15px;
  color: var(--text-bright);
}

.msg {
  margin: 0;
  font-size: 12.5px;
}

.msg--err {
  color: var(--status-danger);
  text-shadow: var(--glow-danger);
}

.msg--wait {
  color: var(--status-waiting);
  text-shadow: var(--glow-waiting);
}

.submit-btn {
  margin-top: auto;
  height: 40px;
  font-size: 14.5px;
  font-family: inherit;
  font-weight: 700;
  color: var(--text-on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--radius);
  box-shadow: 0 0 14px rgba(79, 224, 255, 0.35);
  cursor: pointer;
}

.submit-btn:hover:not(:disabled) {
  background: var(--accent-muted);
}

.submit-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  box-shadow: none;
}
</style>
