<template>
  <!-- 任务页：点击按钮 → 底部 sheet（占 1/2 屏）选命令、填参、下发 -->
  <div class="m-task">
    <button class="m-btn m-btn--primary m-task__open" @click="show = true">任务下发</button>
    <p class="m-note" style="padding: 12px 14px">
      选择动作命令并按字段填参后下发（无 ACK，发完即走）。停车等特化命令已单独处理。
    </p>

    <MBottomSheet v-model="show" title="任务下发" :half="true">
      <label class="m-field">
        <span class="m-field__label">选择命令</span>
        <select v-model.number="selected" class="m-select">
          <option :value="-1">— 请选择 —</option>
          <option v-for="c in commands" :key="c.index" :value="c.index">
            0x{{ c.index.toString(16).toUpperCase() }} {{ c.name }}
          </option>
        </select>
      </label>

      <template v-if="sel">
        <div v-for="(f, i) in sel.fields" :key="i" class="m-field">
          <span class="m-field__label">
            {{ f.name }}
            <em class="m-field__type">{{ typeName(f.type) }}</em>
            <em v-if="f.min !== undefined" class="m-field__type m-field__range">
              {{ f.min }}~{{ f.max }}
            </em>
          </span>

          <input
            v-if="isBool(f.type)"
            v-model="drafts[i]!.check"
            type="checkbox"
            class="m-switch"
          />
          <input
            v-else-if="f.type === ValueType.Color"
            v-model="drafts[i]!.text"
            type="color"
            class="m-input"
          />
          <input
            v-else
            v-model="drafts[i]!.text"
            type="number"
            step="any"
            :min="f.min"
            :max="f.max"
            class="m-input m-input--mono"
            :placeholder="'输入' + f.name"
          />
        </div>

        <p v-if="sel.fields.length === 0" class="m-note">该命令无需参数</p>
      </template>

      <template #foot>
        <button class="m-btn m-btn--primary" style="width: 100%" :disabled="!canSend" @click="onSend">
          下发「{{ sel?.name ?? '' }}」
        </button>
      </template>
    </MBottomSheet>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  CmdSpec,
  ValueType,
  cmdSpec,
  typeName,
  type CellValue,
  type CmdField,
  type CmdDirItem,
} from '@/services/protocol'
import { commandMap, connected, sendCommand } from '@/stores/globle'
import MBottomSheet from './MBottomSheet.vue'

interface Draft {
  text: string | number
  check: boolean
}

const show = ref(false)
const selected = ref(-1)
const commands = computed(() =>
  Object.keys(commandMap)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => commandMap[k]!)
    .filter((c) => cmdSpec(c.tag) !== CmdSpec.Stop),
)
const sel = computed<CmdDirItem | undefined>(() =>
  selected.value >= 0 ? commandMap[selected.value] : undefined,
)

const drafts = reactive<Draft[]>([])
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

function isBool(t: ValueType): boolean {
  return t === ValueType.Bool || t === ValueType.OnOff
}

function fieldValid(f: CmdField, d: Draft): boolean {
  if (isBool(f.type)) return true
  const text = String(d.text ?? '')
  if (f.type === ValueType.Color) return /^#[0-9a-f]{6}$/i.test(text)
  const n = Number(text)
  if (text.trim() === '' || !Number.isFinite(n)) return false
  if (f.min !== undefined && n < f.min) return false
  if (f.max !== undefined && n > f.max) return false
  return true
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
  show.value = false
}
</script>

<style scoped>
.m-task {
  padding: 16px 12px;
}
.m-task__open {
  width: 100%;
}
</style>
