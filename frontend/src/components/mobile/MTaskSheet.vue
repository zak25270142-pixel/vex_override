<template>
  <!-- 任务下发：作为布局的一部分从底部挤入，占下半屏；原页面被挤成上半屏，两者各自可滑动 -->
  <section class="m-taskpane">
    <header class="m-taskpane__head">
      <span v-if="step === 'form'" class="m-taskpane__back" @click="back">‹ 返回</span>
      <span class="m-taskpane__title">{{ title }}</span>
      <button class="m-taskpane__close" @click="close">关闭</button>
    </header>

    <div class="m-taskpane__body">
      <!-- 步骤一：选择命令 -->
      <div v-if="step === 'select'" class="m-task-list">
        <button
          v-for="c in commands"
          :key="c.index"
          class="m-task-item"
          @click="pick(c.index)"
        >
          <span class="m-task-item__idx">0x{{ c.index.toString(16).toUpperCase() }}</span>
          <span class="m-task-item__name">{{ c.name }}</span>
        </button>
      </div>

      <!-- 步骤二：填参表单 -->
      <template v-else>
        <div v-if="sentOk" class="m-task-ok">✓ 已下发「{{ sel?.name }}」</div>

        <div v-for="(f, i) in sel?.fields ?? []" :key="i" class="m-field">
          <span class="m-field__label">
            {{ f.name }}
            <em class="m-field__type">{{ typeName(f.type) }}</em>
            <em v-if="f.min !== undefined" class="m-field__type m-field__range">{{ f.min }}~{{ f.max }}</em>
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

        <p v-if="(sel?.fields.length ?? 0) === 0" class="m-note">该命令无需参数，直接下发</p>
      </template>
    </div>

    <footer class="m-taskpane__foot">
      <button
        v-if="step === 'form'"
        class="m-btn m-btn--primary"
        style="width: 100%"
        :disabled="!canSend"
        @click="onSend"
      >
        下发「{{ sel?.name ?? '' }}」
      </button>
    </footer>
  </section>
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

interface Draft {
  text: string | number
  check: boolean
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
const open = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

function close(): void {
  open.value = false
}

const step = ref<'select' | 'form'>('select')
const selected = ref(-1)
const sentOk = ref(false)

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
const title = computed(() =>
  step.value === 'select' ? '任务下发 · 选择命令' : (sel.value?.name ?? '任务下发'),
)

const drafts = reactive<Draft[]>([])
watch(
  sel,
  (c) => {
    drafts.length = 0
    sentOk.value = false
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
function pick(i: number): void {
  selected.value = i
  step.value = 'form'
}
function back(): void {
  step.value = 'select'
  selected.value = -1
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
  // 下发后不收起，保持表单可见，标记已下发
  sentOk.value = true
}
</script>
