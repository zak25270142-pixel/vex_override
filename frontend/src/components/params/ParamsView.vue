<template>
  <!-- 页面1：参数表。调参(tunable)/实参(monitor) 两表切换；实参表可改订阅档位，已订阅排前 -->
  <div class="params">
    <div class="params__toolbar">
      <div class="seg">
        <button
          :class="{ 'seg__on': paramTable === 'tunable' }"
          title="下位机可调参数目录"
          @click="paramTable = 'tunable'"
        >
          调参表
        </button>
        <button
          :class="{ 'seg__on': paramTable === 'monitor' }"
          title="下位机实时监视量目录"
          @click="paramTable = 'monitor'"
        >
          实参表
        </button>
      </div>

      <input
        v-model="keyword"
        class="search"
        type="search"
        placeholder="搜索名称 / 序号 / 类型…"
      />

      <span class="count">共 {{ visibleRows.length }} 项</span>
    </div>

    <div class="params__body">
      <!-- 未连接 / 已连接但还没取到表 的空态提示 -->
      <div v-if="visibleRows.length === 0" class="empty">
        <template v-if="!connected">尚未连接 V5，请在标题栏选口并连接（或勾选“演示”）</template>
        <template v-else-if="paramTable === 'monitor'">
          实参表为空，请点击标题栏「获取实参表」
        </template>
        <template v-else>调参表为空，请点击标题栏「获取调参表」</template>
      </div>

      <table v-else class="ptable">
        <thead>
          <tr>
            <th class="c-idx">#</th>
            <th class="c-name">名称</th>
            <th class="c-type">类型</th>
            <th class="c-value">当前值</th>
            <th v-if="paramTable === 'monitor'" class="c-sub">订阅档位</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="it in visibleRows"
            :key="it.index"
            :class="{ 'row--sub': paramTable === 'monitor' && tagSubscribed(it.tag) }"
          >
            <td class="c-idx">{{ it.index }}</td>
            <td class="c-name">{{ it.name }}</td>
            <td class="c-type">{{ typeName(it.type) }}</td>
            <td class="c-value">
              <!-- ===== 调参表：可写，以主控回显为确认 ===== -->
              <template v-if="paramTable === 'tunable'">
                <span v-if="pendingTunable.has(it.index)" class="ack" title="已下发，等待主控回显确认">
                  等待确认…
                </span>
                <!-- str/other 下位机不可写，只展示 -->
                <template v-else-if="isWritable(it.type)">
                  <input
                    v-if="isBoolType(it.type)"
                    class="switch"
                    type="checkbox"
                    title="勾选=开"
                    :checked="Number(it.value) !== 0"
                    @change="onBoolCommit(it.index, ($event.target as HTMLInputElement).checked)"
                  />
                  <label v-else-if="it.type === ValueType.Color" class="swatch">
                    <input
                      type="color"
                      :value="String(it.value)"
                      @change="onColorCommit(it.index, ($event.target as HTMLInputElement).value)"
                    />
                    {{ it.value }}
                  </label>
                  <!-- 数值：双击当前值进入编辑，回车或失焦提交，Esc取消 -->
                  <input
                    v-else-if="editing === it.index"
                    v-model="draft"
                    class="num-edit"
                    type="number"
                    :step="isFloatType(it.type) ? 'any' : '1'"
                    autofocus
                    @keyup.enter="onNumCommit(it.index)"
                    @keyup.esc="cancelEdit()"
                    @blur="onNumCommit(it.index)"
                  />
                  <span
                    v-else
                    class="edit-hint"
                    title="双击修改"
                    @dblclick="startEdit(it.index, it.value)"
                  >
                    {{ formatValue(it, it.value) }}
                  </span>
                </template>
                <span v-else>{{ formatValue(it, it.value) }}</span>
              </template>

              <!-- ===== 实参表：只读，展示实时推送值 ===== -->
              <template v-else>
                <span v-if="it.type === ValueType.Color" class="swatch">
                  <i :style="{ background: String(displayValue(it.index, it.value)) }" />
                  {{ displayValue(it.index, it.value) }}
                </span>
                <template v-else>{{ formatValue(it, displayValue(it.index, it.value)) }}</template>
              </template>
            </td>
            <!-- 订阅：实参表才有；x/y/yaw 语义量固定自动订阅，不允许改 -->
            <td v-if="paramTable === 'monitor'" class="c-sub">
              <span v-if="isPoseTag(it.tag)" class="pose-badge">
                {{ tagLabel(it.tag) }}·自动
              </span>
              <select
                :value="subLevel(it.tag)"
                @change="onTagChange(it.index, ($event.target as HTMLSelectElement).value)"
              >
                <option value="none">未订阅</option>
                <option value="slow">低速（约80ms）</option>
                <option value="fast">高速（约10ms）</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import {
  ValueType,
  tagFast,
  tagSubscribed,
  typeName,
  valueSize,
  type CellValue,
} from '@/services/protocol'
import {
  connected,
  formatValue,
  isPoseTag,
  keyword,
  monitorLatest,
  paramTable,
  pendingTunable,
  setSubscription,
  setTunableValue,
  tagLabel,
  visibleRows,
} from '@/stores/globle'

/** 调参表没有实时推送，显示目录帧里带的初始值；实参表显示实时值 */
function displayValue(index: number, fallback: CellValue): CellValue {
  return paramTable.value === 'monitor' ? (monitorLatest[index] ?? fallback) : fallback
}

/** 档位下拉当前值：由位域折算成字符串档位 */
function subLevel(tag: number): string {
  if (!tagSubscribed(tag)) return 'none'
  return tagFast(tag) ? 'fast' : 'slow'
}

/** 档位下拉变化：未订阅/低速/高速 都是同一条 Subscribe 命令的两个位 */
async function onTagChange(index: number, raw: string): Promise<void> {
  await setSubscription(index, raw !== 'none', raw === 'fast')
}

/* ===== 调参编辑 ===== */
/** 正在编辑的行 index；draft 是输入框原始字符串，提交时才解析 */
const editing = ref<number | null>(null)
const draft = ref('')

/** 有存储宽度的类型才可写（str/other 宽度为 0，下位机会拒绝） */
function isWritable(type: ValueType): boolean {
  return valueSize(type) > 0
}
function isBoolType(type: ValueType): boolean {
  return type === ValueType.Bool || type === ValueType.OnOff
}
function isFloatType(type: ValueType): boolean {
  return type === ValueType.Float || type === ValueType.Double
}

function startEdit(index: number, v: CellValue): void {
  editing.value = index
  draft.value = String(v)
}

function cancelEdit(): void {
  // 先清空 editing，随后触发的 blur 会因此跳过提交
  editing.value = null
}

/** 数值提交：回车或失焦触发；非法输入直接取消。editing 守卫保证 blur/enter 不重复发 */
async function onNumCommit(index: number): Promise<void> {
  if (editing.value !== index) return
  editing.value = null
  const n = Number(draft.value)
  if (!Number.isFinite(n)) return
  await setTunableValue(index, n)
}

async function onBoolCommit(index: number, checked: boolean): Promise<void> {
  await setTunableValue(index, checked ? 1 : 0)
}

async function onColorCommit(index: number, hex: string): Promise<void> {
  await setTunableValue(index, hex)
}
</script>

<style scoped>
.params {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.params__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

/* 分段切换 */
.seg {
  display: flex;
  border: var(--border-subtle);
  border-radius: var(--radius);
  overflow: hidden;
}

.seg button {
  padding: 7px 20px;
  font-size: 13.5px;
  font-family: inherit;
  font-weight: 700;
  color: var(--text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
}

.seg button + button {
  border-left: var(--border-subtle);
}

.seg button.seg__on {
  color: var(--text-on-accent);
  background: var(--accent);
}

.search {
  flex: 1;
  max-width: 360px;
  height: 34px;
  padding: 0 12px;
  font-size: 13.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
}

.search:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}

.count {
  font-size: 12.5px;
  color: var(--text-muted);
}

.params__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
}

.empty {
  display: grid;
  place-items: center;
  height: 100%;
  color: var(--text-muted);
  font-size: 15px;
  padding: 20px;
  text-align: center;
}

.ptable {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.ptable thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 10px 14px;
  text-align: left;
  font-size: 12px;
  letter-spacing: 0.1em;
  color: var(--text-secondary);
  background: var(--surface-floating);
  border-bottom: var(--border-panel);
}

.ptable tbody td {
  padding: 9px 14px;
  color: var(--text-primary);
  border-bottom: var(--border-subtle);
  font-variant-numeric: tabular-nums;
}

.ptable tbody tr:hover {
  background: var(--surface-hover);
}

/* 已订阅行高亮左侧 */
.row--sub {
  box-shadow: inset 3px 0 0 var(--accent);
}

.row--sub .c-value {
  color: var(--text-bright);
  font-weight: 700;
}

.c-idx {
  width: 60px;
  color: var(--text-muted);
}

.c-type {
  width: 110px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 12.5px;
}

.c-value {
  font-family: var(--font-mono);
}

.c-sub {
  width: 170px;
}

.c-sub select {
  width: 150px;
  height: 28px;
  font-size: 12.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
}

.c-sub select:focus {
  border-color: var(--accent);
}

/* x/y/yaw 语义徽标 */
.pose-badge {
  display: inline-block;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 700;
  color: var(--status-waiting);
  border: 1px solid rgba(249, 199, 94, 0.5);
  background: rgba(249, 199, 94, 0.1);
  border-radius: 999px;
  text-shadow: 0 0 8px rgba(249, 199, 94, 0.4);
  white-space: nowrap;
}

.swatch {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.swatch i {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid rgba(255, 255, 255, 0.35);
}

/* 调参行内编辑控件 */
.swatch input[type='color'] {
  width: 26px;
  height: 22px;
  padding: 0;
  background: transparent;
  border: var(--border-input);
  border-radius: var(--radius);
  cursor: pointer;
}

.edit-hint {
  cursor: text;
  border-bottom: 1px dashed transparent;
}

.edit-hint:hover {
  border-bottom-color: var(--text-muted);
}

.num-edit {
  width: 110px;
  height: 26px;
  padding: 0 6px;
  font-size: 13px;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--surface-input);
  border: var(--border-input);
  border-radius: var(--radius);
  outline: none;
}

.num-edit:focus {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus);
}

.switch {
  width: 16px;
  height: 16px;
  cursor: pointer;
  accent-color: var(--accent);
}

.ack {
  font-size: 12.5px;
  font-family: inherit;
  color: var(--status-waiting);
  text-shadow: 0 0 8px rgba(249, 199, 94, 0.4);
}
</style>
