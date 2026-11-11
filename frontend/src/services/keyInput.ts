/**
 * 按键输入服务（全局单例）：把键盘 / 鼠标 / 触屏输入映射成固件事件帧。
 *
 * 按键表项走 A5 05 <index> <state> XOR（state 1=按下 0=抬起）；
 * 命令项（命令字 0x80~0x9F）走既有目录命令通道，单击单发、没有按住语义。
 * 安全约定：
 * - 按住期间每 100ms 重发一次 state=1，固件按边沿识别，重发不会重复触发动作；
 * - 窗口失焦、页面隐藏、链路断开时立即补发 state=0，不依赖最后一次 keyup。
 *
 * 同一个 index 可能被多个来源同时按住：held 按来源分组，
 * 只有最后一个来源松开才发 state=0，避免松开鼠标把键盘按住的那份一起释放。
 */
import { reactive, ref, watch } from 'vue'
import { loadPrefs, savePrefs, type KeyBinding } from '@/services/persist'
import { connState, sendCommand, sendKeyEvent } from '@/stores/globle'

const RESEND_MS = 100
/** 动作命令字段起点：index ≥ 0x80 视为命令项，走命令通道而不是按键事件帧 */
const CMD_INDEX_MIN = 0x80

/** 非文本类 input：点完开关/按钮后焦点会停在它上面，键盘映射仍要生效，不能当"正在输入"跳过 */
const NON_TEXT_INPUT_TYPES = new Set([
  'checkbox',
  'radio',
  'button',
  'submit',
  'reset',
  'range',
  'color',
  'file',
])

/** 可编辑控件内不劫持按键（录入框、文本域等）；复选框/按钮不算，否则点完键盘开关就再也按不动 */
export function isEditable(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.closest !== 'function') return false
  if (el.closest('textarea, select, [contenteditable]')) return true
  const input = el.closest('input') as HTMLInputElement | null
  return !!input && !NON_TEXT_INPUT_TYPES.has(input.type)
}

class KeyInput {
  /** 事件 index（按键表下标 / 命令字）→ 键盘绑定（响应式，面板直接读）；codes 为空表示没有键盘绑定 */
  readonly bindings = reactive<Record<number, KeyBinding>>({})
  /** 键盘输入开关（持久化）：只约束键盘监听；鼠标点击与触屏按键始终可用 */
  readonly keyboardEnabled = ref(false)

  /** index → 当前按住它的来源集合（'kbd:<code>' / 'ptr:<pointerId>'） */
  private readonly held = new Map<number, Set<string>>()
  private resendTimer: number | null = null

  constructor() {
    const prefs = loadPrefs()
    this.keyboardEnabled.value = prefs.keyInputEnabled
    for (const [k, v] of Object.entries(prefs.keyMap)) this.bindings[Number(k)] = { codes: [...v.codes] }

    window.addEventListener('keydown', this.onKeyDown)
    window.addEventListener('keyup', this.onKeyUp)
    window.addEventListener('blur', this.clearHeld)
    window.addEventListener('pagehide', this.clearHeld)
    document.addEventListener('visibilitychange', this.onVisibility)
    // 链路断开时固件可能仍认为按键按着：立即补发 0，避免重连后残留
    watch(connState, (state) => {
      if (state !== 'connected') this.clearHeld()
    })
  }

  getHeld(): number[] {
    return [...this.held.keys()]
  }

  setKeyboardEnabled(on: boolean): void {
    if (this.keyboardEnabled.value === on) return
    this.keyboardEnabled.value = on
    savePrefs({ keyInputEnabled: on })
    if (on) {
      if (this.held.size > 0) this.startTimer()
    } else {
      this.releaseKeyboardHolds()
    }
  }

  /** 关掉键盘开关时只释放键盘来源；仍按住的鼠标/触屏来源保持不动 */
  private releaseKeyboardHolds(): void {
    for (const [index, sources] of [...this.held]) {
      for (const source of [...sources]) if (source.startsWith('kbd:')) this.release(index, source)
    }
  }

  /** 给某个事件追加一个键盘键：同一个 code 只允许绑一个事件，后录入的顶掉其它行 */
  addKeyboardCode(index: number, code: string): void {
    this.clearHeld()
    for (const [k, v] of Object.entries(this.bindings)) {
      if (Number(k) === index || !v.codes.includes(code)) continue
      const rest = v.codes.filter((c) => c !== code)
      if (rest.length > 0) this.bindings[Number(k)] = { codes: rest }
      else delete this.bindings[Number(k)]
    }
    const cur = this.bindings[index]?.codes ?? []
    if (!cur.includes(code)) this.bindings[index] = { codes: [...cur, code] }
    this.persist()
  }

  /** 删除单个键位绑定：只摘掉这一个 code，同 index 的其它绑定保留 */
  removeKeyboardCode(index: number, code: string): void {
    const cur = this.bindings[index]?.codes
    if (!cur || !cur.includes(code)) return
    this.clearHeld()
    const rest = cur.filter((c) => c !== code)
    if (rest.length > 0) this.bindings[index] = { codes: rest }
    else delete this.bindings[index]
    this.persist()
  }

  /** 清除某个事件的全部键盘绑定（鼠标/触屏触发不受影响） */
  clearBinding(index: number): void {
    this.clearHeld()
    delete this.bindings[index]
    this.persist()
  }

  /** 清空全部键盘绑定：先释放所有按住的键，再停重发表 */
  clearAll(): void {
    this.clearHeld()
    this.stopTimer()
    for (const k of Object.keys(this.bindings)) delete this.bindings[Number(k)]
    this.persist()
  }

  /** 某个来源按下 index；同一来源重复按下不重复发帧 */
  press(index: number, source: string): void {
    // 命令项：单击单发，不进 held，不参与重发/按住
    if (index >= CMD_INDEX_MIN) {
      void sendCommand(index, [])
      return
    }
    let sources = this.held.get(index)
    if (!sources) {
      sources = new Set()
      this.held.set(index, sources)
    }
    if (sources.has(source)) return
    const first = sources.size === 0
    sources.add(source)
    this.startTimer()
    if (first) void sendKeyEvent(index, 1)
  }

  /** 某个来源松开 index；最后一个来源松开时才发 state=0（命令项无需抬起） */
  release(index: number, source: string): void {
    if (index >= CMD_INDEX_MIN) return
    const sources = this.held.get(index)
    if (!sources || !sources.has(source)) return
    sources.delete(source)
    if (sources.size > 0) return
    this.held.delete(index)
    void sendKeyEvent(index, 0)
    if (this.held.size === 0) this.stopTimer()
  }

  private persist(): void {
    const out: Record<number, KeyBinding> = {}
    for (const [k, v] of Object.entries(this.bindings)) out[Number(k)] = { codes: [...v.codes] }
    savePrefs({ keyMap: out })
  }

  private indexForCode(code: string): number | null {
    for (const [k, v] of Object.entries(this.bindings)) {
      if (v.codes.includes(code)) return Number(k)
    }
    return null
  }

  private startTimer(): void {
    if (this.resendTimer !== null) return
    this.resendTimer = window.setInterval(() => {
      for (const index of this.held.keys()) void sendKeyEvent(index, 1)
    }, RESEND_MS)
  }

  private stopTimer(): void {
    if (this.resendTimer !== null) {
      clearInterval(this.resendTimer)
      this.resendTimer = null
    }
  }

  /** 对 held 全部补发抬起并清空（失焦/隐藏/断线共用） */
  private clearHeld = (): void => {
    if (this.held.size === 0) return
    for (const index of this.held.keys()) void sendKeyEvent(index, 0)
    this.held.clear()
    this.stopTimer()
  }

  private onVisibility = (): void => {
    if (document.hidden) this.clearHeld()
  }

  private onKeyDown = (e: KeyboardEvent): void => {
    if (!this.keyboardEnabled.value || e.repeat) return
    if (e.ctrlKey || e.altKey || e.metaKey) return // 不劫持浏览器/系统组合键
    if (isEditable(e.target)) return
    const index = this.indexForCode(e.code)
    if (index === null) return
    e.preventDefault() // F5 刷新、空格滚动等默认行为一并挡掉
    // 来源带上 code：同一事件绑多个键时，松开其中一个不会释放另一个
    this.press(index, `kbd:${e.code}`)
  }

  private onKeyUp = (e: KeyboardEvent): void => {
    const index = this.indexForCode(e.code)
    if (index === null) return
    this.release(index, `kbd:${e.code}`)
  }
}

/**
 * 与 serialClient 同理挂到 globalThis：Vite HMR 重新执行本模块时，
 * 直接 new 会在 window 上叠加一套监听与定时器，表现为同一次按键发两帧。
 */
const globalScope = globalThis as unknown as { __v5KeyInput?: KeyInput }
export const keyInput: KeyInput =
  globalScope.__v5KeyInput ?? (globalScope.__v5KeyInput = new KeyInput())
