<template>
  <!-- 按键映射：desktop=纵向列表（可录入/清空），mobile=两列编辑网格，quick=只读绑定 + 快速触发 -->
  <section class="keymap" :class="`keymap--${variant}`">
    <header class="keymap__head">
      <label class="keymap__switch" :class="{ 'keymap__switch--on': keyboardEnabled }">
        <input type="checkbox" :checked="keyboardEnabled" @change="onToggle" />
        <span>{{ keyboardEnabled ? '键盘输入已启用' : '键盘输入已停用' }}</span>
      </label>
      <span
        v-if="dirView.text"
        class="keymap__count"
        :class="{ 'keymap__count--warn': dirView.warn }"
        :title="dirView.warn ? '按键目录项数与下位机登记不一致，可点导航栏「刷新」重拉' : ''"
      >
        {{ dirView.text }}
      </span>
      <p v-if="variant !== 'quick'" class="keymap__tip">
        键盘开关只约束触发，不影响录入；点「录入」后按一个键登记，Esc 或点别处取消。
      </p>
      <button
        v-if="variant === 'desktop'"
        class="kbtn kbtn--off"
        :disabled="boundCount === 0"
        @click="clearAll"
      >
        全部清空
      </button>
    </header>

    <p v-if="lastKey" class="keymap__last">
      最近录入：code={{ lastKey.code }}，key={{ lastKey.key }}
    </p>

    <div v-if="keyRows.length === 0 && cmdRows.length === 0" class="keymap__empty">
      {{
        connected ? '按键目录为空，请点导航栏「刷新」重新拉取' : '尚未连接，连接后自动拉取按键目录'
      }}
    </div>

    <!-- 电脑端：一行一个事件，键盘绑定与鼠标点击并存 -->
    <div v-else-if="variant === 'desktop'" class="klist">
      <!-- 按键表项：数据来自 0x06 按键目录 -->
      <div
        v-for="it in keyRows"
        :key="`key-${it.index}`"
        class="krow"
        :class="{ 'krow--capture': captureIdx === it.index }"
        :data-key-index="it.index"
      >
        <div class="krow__id"><small>index = {{ it.index }}</small></div>

        <button
          class="kbtn krow__trigger"
          :class="{ 'kbtn--on': pressed.has(it.index) }"
          title="按住=按下，松开=抬起"
          @pointerdown="onPress($event, it.index)"
          @pointerup="onRelease($event, it.index)"
          @pointercancel="onRelease($event, it.index)"
          @pointerleave="onRelease($event, it.index)"
        >
          {{ it.name }}
        </button>

        <div class="krow__bind">
          <template v-if="bindCodes(it.index).length > 0">
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <small v-if="c !== prettyCode(c)" class="bind__code">{{ c }}</small>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
            <span
              v-if="hiddenCodes(it.index).length > 0"
              class="bind bind--more"
              :title="hiddenCodes(it.index).map(codeTitle).join(' / ')"
            >
              +{{ hiddenCodes(it.index).length }}
            </span>
          </template>
          <span v-else-if="captureIdx !== it.index" class="bind bind--none">未绑定键盘</span>
          <!-- 录入中：行尾等待槽，定位由 flex 居中，不靠 padding 硬顶 -->
          <span v-if="captureIdx === it.index" class="bind bind--slot">按下键盘以登记</span>
        </div>

        <div class="krow__act">
          <button
            class="kbtn"
            :class="{ 'kbtn--sel': captureIdx === it.index }"
            @click="startCapture(it.index)"
          >
            {{ captureIdx === it.index ? '录入中…' : '录入' }}
          </button>
          <button
            class="kbtn kbtn--off"
            :disabled="bindCodes(it.index).length === 0"
            @click="clearOne(it.index)"
          >
            清空
          </button>
        </div>
      </div>

      <!-- 停车命令：数据来自 cmd 目录（tag 的 SPEC 位 == Stop），不是按键目录 -->
      <div
        v-for="it in cmdRows"
        :key="`cmd-${it.index}`"
        class="krow"
        :class="{ 'krow--capture': captureIdx === it.index }"
        :data-key-index="it.index"
      >
        <div class="krow__id"><small>cmd = {{ hex(it.index) }}</small></div>

        <button
          class="kbtn krow__trigger kbtn--cmd"
          :disabled="!connected"
          title="单击下发该命令"
          @click="onCommand(it.index)"
        >
          {{ it.name }}
        </button>

        <div class="krow__bind">
          <template v-if="bindCodes(it.index).length > 0">
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <small v-if="c !== prettyCode(c)" class="bind__code">{{ c }}</small>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
            <span
              v-if="hiddenCodes(it.index).length > 0"
              class="bind bind--more"
              :title="hiddenCodes(it.index).map(codeTitle).join(' / ')"
            >
              +{{ hiddenCodes(it.index).length }}
            </span>
          </template>
          <span v-else-if="captureIdx !== it.index" class="bind bind--none">未绑定键盘</span>
          <!-- 录入中：行尾等待槽，定位由 flex 居中，不靠 padding 硬顶 -->
          <span v-if="captureIdx === it.index" class="bind bind--slot">按下键盘以登记</span>
        </div>

        <div class="krow__act">
          <button
            class="kbtn"
            :class="{ 'kbtn--sel': captureIdx === it.index }"
            @click="startCapture(it.index)"
          >
            {{ captureIdx === it.index ? '录入中…' : '录入' }}
          </button>
          <button
            class="kbtn kbtn--off"
            :disabled="bindCodes(it.index).length === 0"
            @click="clearOne(it.index)"
          >
            清空
          </button>
        </div>
      </div>
    </div>

    <!-- 右栏「按键」标签 / 手机「按键」页：快速触发，绑定只读（可点 × 删除），不提供录入/清空 -->
    <div v-else-if="variant === 'quick'" class="qgrid">
      <div v-for="it in keyRows" :key="`key-${it.index}`" class="qitem">
        <button
          class="kbtn kbtn--sm qbtn"
          :class="{ 'kbtn--on': pressed.has(it.index) }"
          @pointerdown="onPress($event, it.index)"
          @pointerup="onRelease($event, it.index)"
          @pointercancel="onRelease($event, it.index)"
          @pointerleave="onRelease($event, it.index)"
        >
          <span class="qbtn__name">{{ it.name }}</span>
          <small class="qbtn__idx">{{ it.index }}</small>
        </button>
        <div class="qbind">
          <span v-if="bindCodes(it.index).length === 0" class="bind bind--none">未绑定</span>
          <template v-else>
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
            <span
              v-if="hiddenCodes(it.index).length > 0"
              class="bind bind--more"
              :title="hiddenCodes(it.index).map(codeTitle).join(' / ')"
            >
              +{{ hiddenCodes(it.index).length }}
            </span>
          </template>
        </div>
      </div>

      <div v-for="it in cmdRows" :key="`cmd-${it.index}`" class="qitem">
        <button
          class="kbtn kbtn--sm qbtn kbtn--cmd"
          :disabled="!connected"
          @click="onCommand(it.index)"
        >
          <span class="qbtn__name">{{ it.name }}</span>
          <small class="qbtn__idx">{{ hex(it.index) }}</small>
        </button>
        <div class="qbind">
          <span v-if="bindCodes(it.index).length === 0" class="bind bind--none">未绑定</span>
          <template v-else>
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
            <span
              v-if="hiddenCodes(it.index).length > 0"
              class="bind bind--more"
              :title="hiddenCodes(it.index).map(codeTitle).join(' / ')"
            >
              +{{ hiddenCodes(it.index).length }}
            </span>
          </template>
        </div>
      </div>
    </div>

    <!-- 手机端：纯触屏，一行两个等宽大按钮；命令按钮单击下发，绑定全量展示（可点 × 删除） -->
    <div v-else class="kgrid">
      <div v-for="it in keyRows" :key="`key-${it.index}`" class="kitem">
        <button
          class="kbtn kbtn--lg"
          :class="{ 'kbtn--on': pressed.has(it.index) }"
          @pointerdown="onPress($event, it.index)"
          @pointerup="onRelease($event, it.index)"
          @pointercancel="onRelease($event, it.index)"
          @pointerleave="onRelease($event, it.index)"
        >
          <span class="kgrid__name">{{ it.name }}</span>
          <small class="kgrid__idx">index {{ it.index }}</small>
        </button>
        <div class="kbind">
          <span v-if="bindCodes(it.index).length === 0" class="bind bind--none">未绑定</span>
          <template v-else>
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
          </template>
        </div>
      </div>

      <div v-for="it in cmdRows" :key="`cmd-${it.index}`" class="kitem">
        <button
          class="kbtn kbtn--lg kbtn--cmd"
          :disabled="!connected"
          @click="onCommand(it.index)"
        >
          <span class="kgrid__name">{{ it.name }}</span>
          <small class="kgrid__idx">cmd {{ hex(it.index) }}</small>
        </button>
        <div class="kbind">
          <span v-if="bindCodes(it.index).length === 0" class="bind bind--none">未绑定</span>
          <template v-else>
            <span
              v-for="c in shownCodes(it.index)"
              :key="c"
              class="bind bind--key"
              :title="codeTitle(c)"
            >
              <span class="bind__text">{{ prettyCode(c) }}</span>
              <button
                type="button"
                class="bind__del"
                :title="`删除 ${codeTitle(c)}`"
                @click.stop="removeCode(it.index, c)"
              >
                ×
              </button>
            </span>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { commandMap, connected, dirStatusView, keyMap, sendCommand, stopCmds } from '@/stores/globle'
import { isEditable, keyInput } from '@/services/keyInput'

/** desktop=键盘绑定 + 鼠标点击触发；mobile=纯触屏网格；quick=右栏/手机按键页（只读绑定 + 快速触发） */
const props = withDefaults(defineProps<{ variant?: 'desktop' | 'mobile' | 'quick' }>(), {
  variant: 'desktop',
})

const keyboardEnabled = keyInput.keyboardEnabled
/** 按键表项（0x06 目录）与停车命令（cmd 目录）各一份，渲染时分开循环，不合并成一个带类型字段的数组 */
const keyRows = computed(() => Object.values(keyMap).sort((a, b) => a.index - b.index))
const cmdRows = computed(() => stopCmds.value)
/** 按键目录完整性（后端 key 表）：项数与缺失状态；无数据时不显示 */
const dirView = computed(() => dirStatusView('key'))
/** 已配置键盘绑定的事件数量：0 时禁用「全部清空」 */
const boundCount = computed(
  () => Object.values(keyInput.bindings).filter((b) => b.codes.length > 0).length,
)

/** 右栏 quick 一行的绑定 chip 上限：其余折成 +N，避免换行撑破布局 */
const CHIP_MAX = 2

/** 只用于展示的短名（原始 code 仍保留在副标题） */
const PRETTY_CODES: Record<string, string> = {
  ArrowUp: '↑',
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→',
  Space: '空格',
  Enter: '回车',
  Escape: 'Esc',
  Backspace: '退格',
  Tab: 'Tab',
}

function prettyCode(code: string): string {
  const letter = /^Key([A-Z])$/.exec(code)
  if (letter) return letter[1]!
  const digit = /^Digit([0-9])$/.exec(code)
  if (digit) return digit[1]!
  return PRETTY_CODES[code] ?? code
}

/** chip 的 hover 提示：短名与原始 code 都给出，默认只显示短名 */
function codeTitle(code: string): string {
  const pretty = prettyCode(code)
  return pretty === code ? code : `${pretty}（${code}）`
}

function hex(index: number): string {
  return `0x${index.toString(16).padStart(2, '0')}`
}

const captureIdx = ref<number | null>(null)
const lastKey = ref<{ code: string; key: string } | null>(null)
/** 录入中挂到 document 捕获阶段的两个监听：点别处取消、按键登记 */
let capturePointer: ((e: PointerEvent) => void) | null = null
let captureKey: ((e: KeyboardEvent) => void) | null = null

/** 只按下修饰键时不结束录入，继续等主键 */
const MODIFIER_KEYS = ['Shift', 'Control', 'Alt', 'Meta']
/** 按住中的 index（只用于视觉反馈） */
const pressed = reactive(new Set<number>())
/** index → 当前按住的 pointerId，卸载时按来源精确释放 */
const pointerIds = reactive(new Map<number, number>())

/** 某事件当前绑定的全部键（空数组=没有键盘绑定，仍可点击/触屏触发） */
function bindCodes(index: number): string[] {
  return keyInput.bindings[index]?.codes ?? []
}

/** 设置页（desktop/mobile）与正在录入的当前行要看到全部绑定；只有右栏 quick 收敛成 +N */
function chipLimit(index: number): number {
  return props.variant === 'quick' && captureIdx.value !== index ? CHIP_MAX : Infinity
}

function shownCodes(index: number): string[] {
  return bindCodes(index).slice(0, chipLimit(index))
}

function hiddenCodes(index: number): string[] {
  return bindCodes(index).slice(chipLimit(index))
}

function onToggle(e: Event): void {
  keyInput.setKeyboardEnabled((e.target as HTMLInputElement).checked)
}

/**
 * 进入录入中：只采集不触发。
 * document 捕获阶段挂两个监听，三个出口互斥：
 *   1) 在本行以外 pointerdown → 取消；2) 按下主键 → 绑定并退出；3) Esc → 取消。
 * 捕获阶段先用 stopPropagation 截住按键，window 上已启用的 keyInput 收不到，不会顺手发 0x05 或命令。
 * 录入不受「键盘输入已启用」开关影响。
 */
function startCapture(index: number): void {
  stopCapture()
  captureIdx.value = index
  lastKey.value = null

  capturePointer = (e: PointerEvent): void => {
    const row = (e.target as HTMLElement | null)?.closest?.('.krow') as HTMLElement | null
    if (row?.dataset.keyIndex === String(captureIdx.value)) return
    stopCapture()
  }

  captureKey = (e: KeyboardEvent): void => {
    // 真正的文本框里打字不采集（点进文本框本身已经由上面的 pointerdown 取消录入）
    if (isEditable(e.target)) return
    e.preventDefault()
    e.stopPropagation()
    if (e.key === 'Escape') {
      stopCapture()
      return
    }
    if (MODIFIER_KEYS.includes(e.key)) return
    const idx = captureIdx.value
    if (idx === null) return
    lastKey.value = { code: e.code, key: e.key }
    keyInput.addKeyboardCode(idx, e.code)
    stopCapture()
  }

  document.addEventListener('pointerdown', capturePointer, true)
  document.addEventListener('keydown', captureKey, true)
}

/** 退出录入中：两个 document 监听都在这里解绑，避免泄漏 */
function stopCapture(): void {
  if (capturePointer) document.removeEventListener('pointerdown', capturePointer, true)
  if (captureKey) document.removeEventListener('keydown', captureKey, true)
  capturePointer = null
  captureKey = null
  captureIdx.value = null
}

function clearOne(index: number): void {
  stopCapture()
  keyInput.clearBinding(index)
}

/** 删除单个键盘绑定：只删这一个 code，同 index 的其它绑定与点击/触屏触发不受影响 */
function removeCode(index: number, code: string): void {
  stopCapture()
  keyInput.removeKeyboardCode(index, code)
}

/** 清空全部：确认后释放按住键、停重发表并删除所有键盘绑定 */
function clearAll(): void {
  if (!window.confirm('清空全部键盘绑定？鼠标点击与触屏触发不受影响。')) return
  stopCapture()
  keyInput.clearAll()
}

/** 命令项单击下发：走既有 sendCommand（目录命令通道），无参数 */
function onCommand(index: number): void {
  stopCapture()
  void sendCommand(index, [])
}

function sourceOf(pointerId: number): string {
  return `ptr:${pointerId}`
}

/** 鼠标/触屏按下：setPointerCapture 保证滑出后仍能收到抬起；不受键盘开关约束 */
function onPress(e: PointerEvent, index: number): void {
  e.preventDefault()
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  pointerIds.set(index, e.pointerId)
  pressed.add(index)
  keyInput.press(index, sourceOf(e.pointerId))
}

/** 抬起 / 取消 / 滑出都释放；同一 index 的键盘来源不受影响 */
function onRelease(e: PointerEvent, index: number): void {
  const pointerId = pointerIds.get(index)
  if (pointerId === undefined) return
  pointerIds.delete(index)
  pressed.delete(index)
  keyInput.release(index, sourceOf(pointerId))
}

onBeforeUnmount(() => {
  stopCapture()
  for (const [index, pointerId] of [...pointerIds]) {
    pointerIds.delete(index)
    pressed.delete(index)
    keyInput.release(index, sourceOf(pointerId))
  }
  pressed.clear()
})
</script>

<style scoped>
/* ===== 统一配色/按钮规格，集中定义一次，不在各处散落硬编码 =====
   整块面板只有一层浮起表面：内部列表/行不再另铺底色、不加边框框层，也不做内陷
   黄只标"选中/录入中"，绿只标"已启用"，红只标"删除/清空" */
.keymap {
  /* 统一圆角：按钮与 chip 共用，不在各处各写一个值 */
  --k-radius: var(--radius);

  /* 青蓝色阶：同一色相族 9 档，明度连续递增；kb-0/1 对齐 --surface-panel / --surface-floating */
  --kb-0: #07192d; /* 面板底（= --surface-panel，不是自造的黑底） */
  --kb-1: #0a2138; /* 行 hover / chip 底（= --surface-floating） */
  --kb-2: #0e2b47; /* 默认按钮底：比面板抬一档 */
  --kb-3: #14395c; /* 禁用边框 / chip hover 底 */
  --kb-4: #1e5279; /* 常规边框 */
  --kb-5: #2f7ba4; /* 序号、次级索引文字 */
  --kb-6: #63b9de; /* 次级文字 / 命令文字 */
  --kb-7: #93e0f6; /* 亮色强调 */
  --kb-8: #d2f3ff; /* 主文字 */

  /* 常规按键三态：常态=半透明青雾（12% 青叠在页面底上）；hover 提高不透明度 + 同色相提亮 + 柔光；
     按下回落一档、光减弱、下移 1px。全程半透明，无实底、无 inset；边框宽度恒 1px，不跳动 */
  --k-btn-bg: rgba(79, 224, 255, 0.12);
  --k-btn-border: rgba(79, 224, 255, 0.46);
  --k-btn-fg: var(--accent);
  --k-btn-hover-bg: rgba(79, 224, 255, 0.24);
  --k-btn-hover-border: rgba(79, 224, 255, 0.78);
  --k-btn-hover-fg: #9cedff;
  --k-btn-active-bg: rgba(79, 224, 255, 0.18);
  --k-btn-active-border: rgba(79, 224, 255, 0.62);
  --k-btn-active-fg: #7ce6ff;

  /* 发光三态：常态无 → hover 柔和外发光 → 按下扩散收窄、透明度降低（光减弱）；没有 inset */
  --k-glow-hover: 0 0 18px rgba(79, 224, 255, 0.2);
  --k-glow-active: 0 0 10px rgba(79, 224, 255, 0.09);

  /* 键位绑定 chip：比按钮更暗一档的蓝底 + 亮蓝文字 */
  --k-chip-bg: var(--kb-1);
  --k-chip-border: var(--kb-4);
  --k-chip-fg: var(--kb-8);
  --k-chip-hover-bg: var(--kb-2);

  /* 黄色族（警告语义）：命令按钮与「录入中」共用，同一套半透明骨架，只换色相（H≈40.6°，#f9c75e 系） */
  --k-warn-soft: rgba(249, 199, 94, 0.12);
  --k-warn-border: rgba(249, 199, 94, 0.5);
  --k-warn-fg: #ffd479;
  --k-warn-solid: var(--status-waiting);
  --k-warn-hover-bg: rgba(249, 199, 94, 0.24);
  --k-warn-hover-border: rgba(249, 199, 94, 0.8);
  --k-warn-hover-fg: #ffe4ac;
  --k-warn-active-bg: rgba(249, 199, 94, 0.18);
  --k-warn-active-border: rgba(249, 199, 94, 0.64);
  --k-warn-active-fg: #ffdc9c;
  --k-warn-glow-hover: 0 0 18px rgba(249, 199, 94, 0.2);
  --k-warn-glow-active: 0 0 10px rgba(249, 199, 94, 0.09);

  /* 肯定：绿色只用于"已启用"；危险：红色只用于删除/清空，骨架同上，色相 6.8°（#ff533d 系） */
  --k-ok: #5fd39a;
  --k-danger: var(--status-danger);
  --k-danger-border: var(--danger-border);
  --k-danger-hover-bg: rgba(255, 83, 61, 0.26);
  --k-danger-hover-border: rgba(255, 111, 90, 0.8);
  --k-danger-hover-fg: #ff8a79;
  --k-danger-active-bg: rgba(255, 83, 61, 0.2);
  --k-danger-active-border: rgba(255, 111, 90, 0.64);
  --k-danger-active-fg: #ff7360;
  --k-danger-glow-hover: 0 0 18px rgba(255, 83, 61, 0.2);
  --k-danger-glow-active: 0 0 10px rgba(255, 83, 61, 0.09);

  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}

.keymap__head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  flex: none;
}

.keymap__switch {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  font-weight: 700;
  color: var(--kb-6);
  cursor: pointer;
  user-select: none;
}

/* 全局 input 样式会作用到复选框，这里恢复原生尺寸与外观 */
.keymap__switch input {
  width: 14px;
  height: 14px;
  padding: 0;
  margin: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
  accent-color: var(--kb-6);
  cursor: pointer;
}

/* 已启用属于"肯定态"，用绿色；不发光 */
.keymap__switch--on {
  color: var(--k-ok);
}

/* 目录项数：完成时弱化小字；缺项时黄色轻提示（不是错误，不改按钮语义色） */
.keymap__count {
  flex: none;
  font-size: 12px;
  color: var(--kb-6);
  white-space: nowrap;
}

.keymap__count--warn {
  color: var(--status-waiting);
  text-shadow: 0 0 8px rgba(249, 199, 94, 0.35);
}

.keymap__tip {
  flex: 1;
  min-width: 200px;
  margin: 0;
  font-size: 12px;
  color: var(--kb-6);
  line-height: 1.5;
}

.keymap__last {
  flex: none;
  margin: 0;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--kb-6);
  word-break: break-all;
}

.keymap__empty {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 30px 16px;
  text-align: center;
  font-size: 13px;
  color: var(--kb-5);
  border: 1px solid var(--kb-4);
  border-radius: var(--radius);
}

/* ===== 电脑端：纵向列表 ===== */
/* 列表不自带底色/边框框层：直接铺在面板那一层浮起表面上，行与行只用细描边分隔 */
.klist {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
}

.krow {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 14px 9px 11px;
  border-left: 3px solid transparent; /* 给录入中竖条预留位置，切换时不横跳 */
  border-bottom: var(--border-subtle);
}

.krow:last-child {
  border-bottom: none;
}

.krow:hover {
  background: var(--kb-1);
}

.krow__id {
  flex: 0 0 78px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--kb-5);
  white-space: nowrap;
}

/* ===== 统一按钮规格：底色 / 边框 / 圆角 / 发光 / 按下位移，一处在 .keymap 上定义 =====
   尺寸档：.kbtn 常规（配置页 36px）、.kbtn--sm 紧凑（右栏 32px）、.kbtn--lg 触屏（76px）
   角色档：默认（蓝触发）、.kbtn--cmd（黄命令）、.kbtn--sel（黄录入中）、.kbtn--off（红清空） */
.kbtn {
  --k-btn-h: 36px;
  position: relative; /* 序号/删除图标用绝对定位脱离流，主文字始终按整个按钮宽高居中 */
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: var(--k-btn-h);
  padding: 0 12px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
  color: var(--k-btn-fg);
  background: var(--k-btn-bg);
  border: 1px solid var(--k-btn-border);
  border-radius: var(--k-radius);
  box-shadow: none;
  white-space: nowrap;
  cursor: pointer;
  touch-action: none;
  user-select: none;
  transition:
    background 0.12s ease,
    border-color 0.12s ease,
    color 0.12s ease,
    box-shadow 0.12s ease,
    transform 0.08s ease;
}

/* hover：不透明度抬高 + 同色相微亮 + 柔和外发光，不做位移 */
.kbtn:hover:not(:disabled) {
  color: var(--k-btn-hover-fg);
  background: var(--k-btn-hover-bg);
  border-color: var(--k-btn-hover-border);
  box-shadow: var(--k-glow-hover);
}

/* 按下：底色从 hover 回落一档、发光减弱，向下平移 1px（不实心、不内陷）
   角色档（cmd/sel/off/on）各自处理按下态，这里只负责默认档 */
.kbtn:active:not(:disabled):not(.kbtn--on):not(.kbtn--cmd):not(.kbtn--off):not(.kbtn--sel) {
  color: var(--k-btn-active-fg);
  background: var(--k-btn-active-bg);
  border-color: var(--k-btn-active-border);
  box-shadow: var(--k-glow-active);
  transform: translateY(1px);
}

/* 禁用：落到色阶最低几档，无阴影，光标保持 not-allowed */
.kbtn:disabled {
  color: var(--kb-5);
  background: var(--kb-1);
  border-color: var(--kb-3);
  opacity: 0.5;
  box-shadow: none;
  cursor: not-allowed;
}

.kbtn--sm {
  --k-btn-h: 32px;
  padding: 0 9px;
  font-size: 12.5px;
}

.kbtn--lg {
  --k-btn-h: 76px;
  flex-direction: column;
  gap: 3px;
  padding: 10px;
  font-size: 17px;
}

/* 按住中（键盘/触屏按住态）：外观与"按下"一致——略暗底色 + 减弱后的光 + 下移 1px，
   不再跳成亮蓝实心；带上 .kbtn 前缀与 :hover，压过通用 hover 规则 */
.kbtn.kbtn--on:not(:disabled),
.kbtn.kbtn--on:not(:disabled):hover {
  color: var(--k-btn-active-fg);
  background: var(--k-btn-active-bg);
  border-color: var(--k-btn-active-border);
  box-shadow: var(--k-glow-active);
  transform: translateY(1px);
}

/* ===== 危险/警告两族的共用三态：半透明浅底 → hover 抬高不透明度 + 同色相提亮 + 柔光 → 按下一档回落、光减弱 + 下移 =====
   两个角色只声明色相变量，规则本身共用，不复制两份 CSS */
.kbtn--off {
  --k-role-bg: var(--danger-soft);
  --k-role-border: var(--k-danger-border);
  --k-role-fg: var(--k-danger);
  --k-role-hover-bg: var(--k-danger-hover-bg);
  --k-role-hover-border: var(--k-danger-hover-border);
  --k-role-hover-fg: var(--k-danger-hover-fg);
  --k-role-active-bg: var(--k-danger-active-bg);
  --k-role-active-border: var(--k-danger-active-border);
  --k-role-active-fg: var(--k-danger-active-fg);
  --k-role-glow-hover: var(--k-danger-glow-hover);
  --k-role-glow-active: var(--k-danger-glow-active);
}

/* 命令类与「录入中」同属黄色族，骨架同上，只换色相 */
.kbtn--cmd,
.kbtn--sel {
  --k-role-bg: var(--k-warn-soft);
  --k-role-border: var(--k-warn-border);
  --k-role-fg: var(--k-warn-fg);
  --k-role-hover-bg: var(--k-warn-hover-bg);
  --k-role-hover-border: var(--k-warn-hover-border);
  --k-role-hover-fg: var(--k-warn-hover-fg);
  --k-role-active-bg: var(--k-warn-active-bg);
  --k-role-active-border: var(--k-warn-active-border);
  --k-role-active-fg: var(--k-warn-active-fg);
  --k-role-glow-hover: var(--k-warn-glow-hover);
  --k-role-glow-active: var(--k-warn-glow-active);
}

.kbtn--off,
.kbtn--cmd,
.kbtn--sel {
  color: var(--k-role-fg);
  background: var(--k-role-bg);
  border-color: var(--k-role-border);
}

.kbtn--off:hover:not(:disabled),
.kbtn--cmd:hover:not(:disabled),
.kbtn--sel:hover:not(:disabled) {
  color: var(--k-role-hover-fg);
  background: var(--k-role-hover-bg);
  border-color: var(--k-role-hover-border);
  box-shadow: var(--k-role-glow-hover);
}

.kbtn--off:active:not(:disabled),
.kbtn--cmd:active:not(:disabled),
.kbtn--sel:active:not(:disabled) {
  color: var(--k-role-active-fg);
  background: var(--k-role-active-bg);
  border-color: var(--k-role-active-border);
  box-shadow: var(--k-role-glow-active);
  transform: translateY(1px);
}

.krow__trigger {
  flex: 0 0 124px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 录入中只标当前行：琥珀底色 + 左侧竖条（用 border-left，不用 inset 阴影） */
.krow--capture,
.krow--capture:hover {
  background: var(--k-warn-soft);
  border-left-color: var(--k-warn-solid);
}

.krow--capture .krow__id {
  color: var(--k-warn-solid);
}

.krow__bind {
  flex: 1 1 auto;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}

.krow__act {
  flex: 0 0 auto;
  display: flex;
  gap: 6px;
  white-space: nowrap;
}

/* 绑定徽标：暗蓝=无键盘绑定，亮蓝=键盘；单行截断，不换行 */
.bind {
  position: relative; /* 删除图标绝对定位，露出时不挤动文字 */
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  padding: 2px 8px;
  border-radius: var(--k-radius);
  border: 1px solid;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
}

.bind__text {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.bind--none {
  flex: none;
  color: var(--kb-5);
  border-color: var(--kb-4);
}

.bind--key {
  padding-right: 18px; /* 常驻右侧留白，× 出现前后文字位置不动 */
  color: var(--k-chip-fg);
  background: var(--k-chip-bg);
  border-color: var(--k-chip-border);
}

.bind--key:hover {
  background: var(--k-chip-hover-bg);
}

/* 录入中的等待槽：水平+垂直居中（flex 居中，不靠 padding 顶） */
.bind--slot {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 22px;
  padding: 2px 10px;
  text-align: center;
  color: var(--k-warn-fg);
  background: var(--k-warn-soft);
  border: 1px dashed var(--k-warn-border);
}

.bind--more {
  flex: none;
  color: var(--kb-6);
  border-color: var(--kb-4);
  background: transparent;
  cursor: help;
}

.bind__code {
  flex: none;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--kb-6);
}

/* chip 内删除：绝对覆盖在右侧留白里，默认隐藏；hover / 键盘聚焦时露出；触屏设备没有 hover，常显
   与「清空」同一条红色三态：半透明浅红 → hover 实红 → 按下深红（位移） */
.bind__del {
  position: absolute;
  right: 3px;
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 15px;
  padding: 0;
  opacity: 0;
  border: none;
  border-radius: 2px;
  background: var(--danger-soft);
  color: var(--k-danger);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  transition:
    opacity 0.12s,
    background 0.12s,
    color 0.12s;
}

.bind--key:hover .bind__del,
.bind--key:focus-within .bind__del {
  opacity: 1;
}

.bind__del:hover:not(:disabled) {
  background: var(--k-danger-hover-bg);
  color: var(--k-danger-hover-fg);
  opacity: 1;
}

/* 触屏没有真正的 hover，按下态即回落一档的红；after :hover 规则，touch 下也压得住 */
.bind__del:active:not(:disabled) {
  background: var(--k-danger-active-bg);
  color: var(--k-danger-active-fg);
  transform: translateY(calc(-50% + 1px));
}

@media (hover: none) {
  .bind__del {
    opacity: 0.85;
  }
}

/* ===== 右栏 / 手机按键页：紧凑快速触发 ===== */
.qgrid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(126px, 1fr));
  align-content: start;
  gap: 8px;
  overflow-y: auto;
  overflow-x: hidden;
}

.qitem {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

/* 右栏按钮沿用 .kbtn 规格，这里只补宽度；主文字按整个按钮居中 */
.qbtn {
  width: 100%;
}

.qbtn__name {
  flex: 0 1 auto;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 序号脱离流，叠在右下角；不参与居中计算 */
.qbtn__idx {
  position: absolute;
  right: 7px;
  top: 50%;
  transform: translateY(-50%);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--kb-5);
  pointer-events: none;
}

/* 命令按下态底色偏亮，序号跟着换成同族亮字 */
.kbtn--cmd:active .qbtn__idx {
  color: var(--k-warn-active-fg);
  opacity: 0.7;
}

.qbind {
  display: flex;
  flex-wrap: nowrap;
  gap: 4px;
  min-width: 0;
  min-height: 18px;
  overflow: hidden;
}

/* ===== 手机端：两列等宽触屏网格 ===== */
.kgrid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-content: start;
  gap: 10px;
  overflow-y: auto;
}

/* 每个格子 = 大按钮 + 该行全部绑定（设置页不截断） */
.kitem {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.kgrid .kbtn {
  width: 100%;
}

.kbind {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}

.kgrid__name {
  font-size: 17px;
  font-weight: 700;
}

/* 序号脱离流，叠在右下角；不参与居中计算 */
.kgrid__idx {
  position: absolute;
  right: 10px;
  bottom: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--kb-5);
  pointer-events: none;
}


</style>
