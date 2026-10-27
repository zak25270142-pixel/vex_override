<template>
  <!-- 调参页：列表点哪项 → 底部 sheet 弹输入框（数值/颜色/开关），确认后 setTunableValue -->
  <div class="m-list">
    <div v-if="rows.length === 0" class="m-list__empty">
      <template v-if="!connected">尚未连接 V5，请在右上角设置开启「演示」或连接</template>
      <template v-else>调参表为空，请在右上角设置连接后自动获取</template>
    </div>

    <div
      v-for="it in rows"
      :key="it.index"
      class="m-list-row"
      @click="open(it)"
    >
      <span class="m-list-row__name" :title="it.name">{{ it.name }}</span>
      <span class="m-list-row__value">
        <template v-if="pendingTunable.has(it.index)">…</template>
        <template v-else>{{ formatValueCompact(it, it.value) }}</template><i
          v-if="it.unit"
          class="m-unit"
          >{{ it.unit }}</i
        >
      </span>
    </div>

    <!-- 编辑 sheet -->
    <MBottomSheet v-model="show" :title="`调参 · ${sel?.name ?? ''}`">
      <template v-if="sel">
        <p class="m-note">
          序号 {{ sel.index }} · 类型 {{ typeName(sel.type) }}
          <template v-if="sel.unit"> · 单位 {{ sel.unit }}</template>
        </p>

        <div v-if="isBool(sel.type)" class="m-field">
          <span class="m-field__label">开关</span>
          <input v-model="draftCheck" type="checkbox" class="m-switch" />
        </div>

        <div v-else-if="sel.type === ValueType.Color" class="m-field">
          <span class="m-field__label">颜色（#RRGGBB）</span>
          <input v-model="draftText" type="color" class="m-input" />
        </div>

        <div v-else class="m-field">
          <span class="m-field__label">数值</span>
          <input
            v-model="draftText"
            type="number"
            step="any"
            class="m-input m-input--mono"
            :placeholder="String(sel.value)"
          />
        </div>

        <p v-if="!writable" class="m-note">该类型不可写（只读）。</p>
      </template>

      <template #foot>
        <button class="m-btn m-btn--primary" style="width: 100%" :disabled="!writable" @click="save">
          下发
        </button>
      </template>
    </MBottomSheet>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { type CellValue, ValueType, typeName, valueSize, type DirItem } from '@/services/protocol'
import {
  connected,
  formatValueCompact,
  pendingTunable,
  setTunableValue,
  tunableMap,
} from '@/stores/globle'
import MBottomSheet from './MBottomSheet.vue'

const rows = computed(() =>
  Object.values(tunableMap).sort((a, b) => a.index - b.index),
)

const show = ref(false)
const sel = ref<DirItem | null>(null)
const draftText = ref('')
const draftCheck = ref(false)

const writable = computed(() => !!sel.value && valueSize(sel.value.type) > 0)

function isBool(t: ValueType): boolean {
  return t === ValueType.Bool || t === ValueType.OnOff
}

// 打开时按当前值初始化草稿
watch(show, (on) => {
  if (!on || !sel.value) return
  const v = sel.value.value
  if (isBool(sel.value.type)) draftCheck.value = v === 1
  else if (sel.value.type === ValueType.Color) draftText.value = typeof v === 'string' ? v : '#000000'
  else draftText.value = String(v)
})

function open(it: DirItem): void {
  sel.value = it
  show.value = true
}

async function save(): Promise<void> {
  if (!sel.value || !writable.value) return
  let val: CellValue
  if (isBool(sel.value.type)) val = draftCheck.value ? 1 : 0
  else if (sel.value.type === ValueType.Color) val = draftText.value
  else val = Number(draftText.value)
  await setTunableValue(sel.value.index, val)
  show.value = false
}
</script>
