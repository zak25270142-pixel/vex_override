<template>
  <!-- 右侧30%任务下发表单：先选命令，再按字段填参，确认后下发（无ACK，发完即走） -->
  <aside class="cmd-panel">
    <div class="cmd-panel__head">
      <span class="cmd-panel__title">任务下发</span>
      <span class="cmd-panel__count">{{ commands.length }} 条</span>
    </div>

    <div class="cmd-panel__body">
      <!-- 空态 -->
      <div v-if="commands.length === 0" class="empty">
        <template v-if="!connected">连接后自动获取命令表</template>
        <template v-else>命令表为空，请点标题栏重新获取</template>
      </div>

      <template v-else>
        <!-- 第一步：选命令 -->
        <label class="field">
          <span class="field__label">选择命令</span>
          <select v-model.number="selected" class="cmd-select">
            <option :value="-1">— 请选择 —</option>
            <option v-for="c in commands" :key="c.index" :value="c.index">
              0x{{ c.index.toString(16).toUpperCase() }} {{ c.name }}
            </option>
          </select>
        </label>

        <!-- 第二步：按目录里的字段描述动态渲染输入项 -->
        <template v-if="sel">
          <div v-for="(f, i) in sel.fields" :key="i" class="field">
            <span class="field__label">
              {{ f.name }}
              <em class="field__type">{{ typeName(f.type) }}</em>
            </span>

            <input
              v-if="isBool(f.type)"
              v-model="drafts[i].check"
              class="switch"
              type="checkbox"
              title="勾选=开"
            />
            <input
              v-else-if="f.type === ValueType.Color"
              v-model="drafts[i].text"
              class="color-edit"
              type="color"
            />
            <input
              v-else
              v-model="drafts[i].text"
              class="num-edit"
              type="number"
              step="any"
              :placeholder="'输入' + f.name"
            />
          </div>

          <!-- 无参命令的提示 -->
          <p v-if="sel.fields.length === 0" class="no-arg">该命令无需参数</p>

          <!-- 第三步：确认下发 -->
          <button class="send-btn" :disabled="!canSend" @click="onSend">
            下发「{{ sel.name }}」
          </button>
          <p v-if="!canSend && sel.fields.length > 0" class="hint">请把参数填成有效数字</p>
        </template>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  ValueType,
  typeName,
  type CellValue,
  type CmdField,
} from '@/services/protocol'
import { commandMap, connected, sendCommand } from '@/stores/globle'

/** 命令按命令字升序排列（注册表下发顺序可能乱） */
const commands = computed(() =>
  Object.keys(commandMap)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => commandMap[k]!),
)

const selected = ref(-1)
const sel = computed(() => (selected.value >= 0 ? commandMap[selected.value] : undefined))

/** 每个字段一份草稿：数值/颜色走text，bool/on_off走check
 *  注意：number输入框的v-model会自动把输入转成数字，所以text可能是number类型 */
interface Draft {
  text: string | number
  check: boolean
}
const drafts = reactive<Draft[]>([])

// 选中命令变化时按字段数重建草稿（数字类默认0，勾选项默认false）
watch(
  sel,
  (c) => {
    drafts.length = 0
    if (c) {
      for (const f of c.fields) {
        drafts.push({ text: isBool(f.type) || f.type === ValueType.Color ? '' : '0', check: false })
      }
    }
  },
  { immediate: true },
)

function isBool(type: ValueType): boolean {
  return type === ValueType.Bool || type === ValueType.OnOff
}

/** 单个字段当前是否已填成合法值 */
function fieldValid(f: CmdField, d: Draft): boolean {
  if (isBool(f.type)) return true // 复选框不存在非法输入
  // number框v-model可能给出数字，统一转字符串再判断
  const text = String(d.text ?? '')
  if (f.type === ValueType.Color) return /^#[0-9a-f]{6}$/i.test(text)
  return text.trim() !== '' && Number.isFinite(Number(text))
}

const canSend = computed(() => {
  if (!connected.value || !sel.value) return false
  return sel.value.fields.every((f, i) => fieldValid(f, drafts[i]!))
})

async function onSend(): Promise<void> {
  if (!sel.value || !canSend.value) return
  const values: CellValue[] = sel.value.fields.map((f, i) => {
    const d = drafts[i]!
    if (isBool(f.type)) return d.check ? 1 : 0
    if (f.type === ValueType.Color) return d.text
    return Number(d.text)
  })
  await sendCommand(sel.value.index, values)
}
</script>

<style scoped>
.cmd-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  overflow: hidden;
}

.cmd-panel__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: var(--border-panel);
  flex-shrink: 0;
}

.cmd-panel__title {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--text-primary);
}

.cmd-panel__count {
  font-size: 12px;
  color: var(--text-muted);
}

.cmd-panel__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.empty {
  margin: auto;
  color: var(--text-muted);
  font-size: 13.5px;
  text-align: center;
  padding: 20px 8px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field__label {
  font-size: 13px;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.field__type {
  font-style: normal;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  border: var(--border-subtle);
  border-radius: 4px;
  padding: 0 5px;
}

.cmd-select,
.num-edit,
.color-edit {
  width: 100%;
  height: 32px;
  padding: 0 8px;
  font-size: 13px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
  box-sizing: border-box;
}

.num-edit {
  font-family: var(--font-mono);
}

.cmd-select:focus,
.num-edit:focus,
.color-edit:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}

.color-edit {
  padding: 2px 4px;
  cursor: pointer;
}

.switch {
  width: 18px;
  height: 18px;
  cursor: pointer;
  accent-color: var(--accent);
}

.no-arg {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-muted);
}

.send-btn {
  margin-top: auto;
  height: 38px;
  font-size: 14px;
  font-family: inherit;
  font-weight: 700;
  color: var(--text-on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
}

.send-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.hint {
  margin: -6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
