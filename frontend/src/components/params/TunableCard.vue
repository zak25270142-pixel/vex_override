<template>
  <!-- 右栏底部「调参缩略卡」：跨页改参，替代原整页 TunablePanel。
       与任务下发/曲线设置/场景数据同时可见，减少切回参数表 -->
  <section class="tun-card">
    <header class="tun-card__head">
      <span class="tun-card__name">{{ item.name }}</span>
      <span class="tun-card__idx">#{{ item.index }}</span>
      <button class="tun-card__close" title="关闭" @click="selectTunable(null)">×</button>
    </header>

    <div class="tun-card__body">
      <!-- 主控当前值 -->
      <div class="current">
        <span class="current__label">当前值</span>
        <span class="current__value">
          {{ formatValue(item, item.value) }}
          <i v-if="item.unit">{{ item.unit }}</i>
        </span>
      </div>

      <!-- str/other 只读 -->
      <template v-if="valueSize(item.type) > 0">
        <div class="edit-row">
          <!-- 布尔 -->
          <label v-if="isBool" class="bool-row">
            <input v-model="draftBool" class="switch" type="checkbox" :disabled="waiting" />
            <span :class="{ on: draftBool }">{{ draftBool ? '开' : '关' }}</span>
          </label>

          <!-- 颜色 -->
          <div v-else-if="item.type === ValueType.Color" class="color-row">
            <input v-model="draftColor" class="color-edit" type="color" :disabled="waiting" />
            <span class="color-hex">{{ draftColor.toUpperCase() }}</span>
          </div>

          <!-- 数值 -->
          <div v-else class="input-wrap">
            <input
              v-model="draftNum"
              class="num-edit"
              type="number"
              :step="isFloat ? 'any' : '1'"
              :disabled="waiting"
              @input="errorMsg = ''"
              @keyup.enter="onSubmit"
            />
            <span v-if="item.unit" class="num-suffix">{{ item.unit }}</span>
          </div>

          <button class="submit-btn" :disabled="waiting || !connected" @click="onSubmit">
            {{ waiting ? '等待…' : '提交' }}
          </button>
        </div>

        <p v-if="errorMsg" class="msg msg--err">{{ errorMsg }}</p>
        <p v-if="waiting" class="msg msg--wait">等待主控回显确认…</p>
      </template>
      <p v-else class="msg msg--err">该类型只读，不能修改</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ValueType, formatFloat32, valueSize } from '@/services/protocol'
import {
  connected,
  formatValue,
  pendingTunable,
  selectTunable,
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

const draftNum = ref<string | number>(0)
const draftBool = ref(false)
const draftColor = ref('#000000')
const errorMsg = ref('')

function resetDraft(): void {
  if (!item.value) return
  errorMsg.value = ''
  const v = item.value.value
  if (isBool.value) draftBool.value = Number(v) !== 0
  else if (item.value.type === ValueType.Color) draftColor.value = String(v)
  else if (typeof v !== 'number') draftNum.value = 0
  // float32 直接填进输入框会带一长串尾数（1.2 显成 1.2000000476837158），用最短无损写法
  else if (item.value.type === ValueType.Float) draftNum.value = formatFloat32(v)
  else draftNum.value = v
}

watch(() => item.value?.index, () => resetDraft(), { immediate: true })
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
</script>

<style scoped>
.tun-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.tun-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: var(--border-subtle);
  flex-shrink: 0;
}

.tun-card__name {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-bright);
}

.tun-card__idx {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
}

.tun-card__close {
  margin-left: auto;
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: 16px;
  line-height: 1;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
}

.tun-card__close:hover {
  color: var(--status-danger);
  background: var(--danger-soft);
}

.tun-card__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.current {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 8px 10px;
  background: var(--surface-floating);
  border: var(--border-subtle);
  border-radius: var(--radius);
}

.current__label {
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.current__value {
  font-family: var(--font-mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--text-bright);
}

.current__value i {
  font-style: normal;
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  margin-left: 3px;
}

.edit-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.input-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
}

.num-edit {
  width: 100%;
  height: 30px;
  padding: 0 48px 0 10px;
  font-size: 14px;
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
  right: 10px;
  font-size: 12px;
  color: var(--text-muted);
  pointer-events: none;
}

.bool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--text-secondary);
  cursor: pointer;
}

.bool-row .on {
  color: var(--status-success);
  text-shadow: var(--glow-success);
  font-weight: 700;
}

.switch {
  width: 18px;
  height: 18px;
  cursor: pointer;
  accent-color: var(--accent);
}

.color-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.color-edit {
  width: 42px;
  height: 28px;
  padding: 2px;
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  cursor: pointer;
}

.color-hex {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-bright);
}

.submit-btn {
  flex-shrink: 0;
  height: 30px;
  padding: 0 14px;
  font-size: 13px;
  font-family: inherit;
  font-weight: 700;
  color: var(--text-on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--radius);
  box-shadow: 0 0 12px rgba(79, 224, 255, 0.3);
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

.msg {
  margin: 0;
  font-size: 11.5px;
}

.msg--err {
  color: var(--status-danger);
  text-shadow: var(--glow-danger);
}

.msg--wait {
  color: var(--status-waiting);
  text-shadow: var(--glow-waiting);
}
</style>
