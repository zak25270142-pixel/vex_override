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
            <th v-if="paramTable === 'monitor'" class="c-sub">订阅</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="it in visibleRows"
            :key="it.index"
            :class="{
              'row--sub': paramTable === 'monitor' && tagSubscribed(it.tag),
              'row--sel': isRowSel(it.index),
              'row--clickable': true,
            }"
            :title="paramTable === 'tunable' ? '单击在右栏底部修改' : '单击在右栏底部查看实时值/订阅'"
            @click="onRowClick(it.index)"
          >
            <td class="c-idx">{{ it.index }}</td>
            <td class="c-name">{{ it.name }}</td>
            <td class="c-type">{{ typeName(it.type) }}</td>
            <td class="c-value">
              <!-- ===== 调参表：只读展示，单击行到右栏底部缩略卡编辑；下发后等回显期间显等待 ===== -->
              <template v-if="paramTable === 'tunable'">
                <span v-if="pendingTunable.has(it.index)" class="ack" title="已下发，等待主控回显确认">
                  等待确认…
                </span>
                <template v-else>
                  <label v-if="it.type === ValueType.Color" class="swatch">
                    <i :style="{ background: String(it.value) }" />
                    {{ it.value }}
                  </label>
                  <template v-else>
                    <span class="val-num">{{ formatValue(it, it.value) }}</span><span v-if="it.unit" class="val-unit">{{ it.unit }}</span>
                  </template>
                </template>
              </template>

              <!-- ===== 实参表：只读，展示实时推送值（值直接挂单位） ===== -->
              <template v-else>
                <span v-if="it.type === ValueType.Color" class="swatch">
                  <i :style="{ background: String(displayValue(it.index, it.value)) }" />
                  {{ displayValue(it.index, it.value) }}
                </span>
                <template v-else>
                  <span class="val-num">{{ formatValue(it, displayValue(it.index, it.value)) }}</span><span v-if="it.unit" class="val-unit">{{ it.unit }}</span>
                </template>
              </template>
            </td>
            <!-- 订阅：实参表才有；徽标标当前档位（灰=未订阅/黄=低速/青=高速），右侧两按钮按当前档位切换 -->
            <td v-if="paramTable === 'monitor'" class="c-sub" @click.stop>
              <div class="sub-cell">
                <template v-if="!tagSubscribed(it.tag)">
                  <span class="sub-state sub-state--off"><i />未订阅</span>
                  <button class="sub-btn sub-btn--slow" @click.stop="onSub(it.index, false)">订阅低速</button>
                  <button class="sub-btn sub-btn--fast" @click.stop="onSub(it.index, true)">订阅高速</button>
                </template>
                <template v-else-if="tagFast(it.tag)">
                  <span class="sub-state sub-state--fast"><i />高速</span>
                  <button class="sub-btn sub-btn--off" @click.stop="onUnsub(it.index)">退订</button>
                  <button class="sub-btn sub-btn--slow" @click.stop="onSub(it.index, false)">降级低速</button>
                </template>
                <template v-else>
                  <span class="sub-state sub-state--slow"><i />低速</span>
                  <button class="sub-btn sub-btn--off" @click.stop="onUnsub(it.index)">退订</button>
                  <button class="sub-btn sub-btn--fast" @click.stop="onSub(it.index, true)">升级高速</button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ValueType, tagFast, tagSubscribed, typeName, type CellValue } from '@/services/protocol'
import {
  connected,
  formatValue,
  keyword,
  monitorLatest,
  paramTable,
  pendingTunable,
  selectMonitor,
  selectTunable,
  selectedMonitorIndex,
  selectedTunableIndex,
  setSubscription,
  visibleRows,
} from '@/stores/globle'

/** 调参表没有实时推送，显示目录帧里带的初始值；实参表显示实时值 */
function displayValue(index: number, fallback: CellValue): CellValue {
  return paramTable.value === 'monitor' ? (monitorLatest[index] ?? fallback) : fallback
}

/** 当前行是否被选中（调参表看 selectedTunableIndex，实参表看 selectedMonitorIndex） */
function isRowSel(index: number): boolean {
  return paramTable.value === 'tunable'
    ? selectedTunableIndex.value === index
    : selectedMonitorIndex.value === index
}

/** 行点击：再点同一行取消选中；不同行选中并互斥 */
function onRowClick(index: number): void {
  if (paramTable.value === 'tunable') {
    selectTunable(selectedTunableIndex.value === index ? null : index)
  } else {
    selectMonitor(selectedMonitorIndex.value === index ? null : index)
  }
}

/** 订阅（fast=true 高速约10ms，false 低速约80ms）；与退订同走一条 Subscribe 命令 */
async function onSub(index: number, fast: boolean): Promise<void> {
  await setSubscription(index, true, fast)
}

async function onUnsub(index: number): Promise<void> {
  await setSubscription(index, false, false)
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
  /* 面板过窄时横向滚动，而不是把列挤瘪 */
  min-width: 740px;
  border-collapse: collapse;
  font-size: 14px;
  /* 固定布局：列宽由下面的 width 决定，数值刷新时列不会左右晃 */
  table-layout: fixed;
}

.ptable thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 10px 20px;
  text-align: left;
  font-size: 12px;
  letter-spacing: 0.1em;
  color: var(--text-secondary);
  background: var(--surface-floating);
  border-bottom: var(--border-panel);
}

.ptable tbody td {
  padding: 9px 20px;
  color: var(--text-primary);
  border-bottom: var(--border-subtle);
  font-variant-numeric: tabular-nums;
  text-align: left; /* 数据左对齐，不再挤在最右 */
  vertical-align: middle;
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

/* 调参表行可单击（到右侧面板编辑），选中行软底+左侧高亮 */
.ptable tbody tr.row--clickable {
  cursor: pointer;
}

.row--sel {
  background: var(--accent-soft);
  box-shadow: inset 3px 0 0 var(--accent);
}

.c-idx {
  width: 64px;
  color: var(--text-muted);
}

/* 名称列吃掉剩余宽度，过长的名字省略号 */
.c-name {
  width: auto;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.c-type {
  width: 160px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 12.5px;
}

/* 值列定宽：数值位数来回变也不影响其它列 */
.c-value {
  width: 280px;
  font-family: var(--font-mono);
}

/* 数值左对齐 + 单位紧跟，高速波动时数值位置稳定（tabular-nums 等宽数字） */
.val-num {
  display: inline-block;
  text-align: left;
}

/* 值直接挂的单位：小一号、弱化，与数值留点气口 */
.val-unit {
  display: inline-block;
  width: 2.5em;
  margin-left: 5px;
  text-align: left;
  font-size: 11.5px;
  color: var(--text-muted);
}

.c-sub {
  width: 235px;
}

/* 状态徽标 + 两个按钮同一行排开 */
.sub-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

/* 当前档位徽标：圆点+文字；灰=未订阅（无发光），黄=低速，青=高速 */
.sub-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 52px;
  font-size: 12px;
  font-weight: 700;
}

.sub-state i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.sub-state--off {
  color: var(--status-offline);
}

.sub-state--off i {
  background: var(--status-offline);
}

.sub-state--slow {
  color: var(--status-waiting);
  text-shadow: var(--glow-waiting);
}

.sub-state--slow i {
  background: var(--status-waiting);
  box-shadow: var(--glow-waiting);
}

.sub-state--fast {
  color: var(--accent);
  text-shadow: var(--glow-accent);
}

.sub-state--fast i {
  background: var(--accent);
  box-shadow: var(--glow-accent);
}

/* 操作按钮：低速=黄软底，高速=青软底，退订=红软底 */
.sub-btn {
  height: 26px;
  padding: 0 8px;
  font-size: 12px;
  font-family: inherit;
  font-weight: 700;
  border-radius: var(--radius);
  cursor: pointer;
}

.sub-btn--slow {
  color: var(--status-waiting);
  background: rgba(249, 199, 94, 0.12);
  border: 1px solid rgba(249, 199, 94, 0.5);
  text-shadow: 0 0 8px rgba(249, 199, 94, 0.35);
}

.sub-btn--slow:hover {
  background: rgba(249, 199, 94, 0.24);
}

.sub-btn--fast {
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  text-shadow: 0 0 8px rgba(79, 224, 255, 0.35);
}

.sub-btn--fast:hover {
  background: rgba(79, 224, 255, 0.24);
}

.sub-btn--off {
  color: var(--status-danger);
  background: var(--danger-soft);
  border: 1px solid var(--danger-border);
  text-shadow: 0 0 8px rgba(255, 83, 61, 0.35);
}

.sub-btn--off:hover {
  background: rgba(255, 83, 61, 0.24);
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

.ack {
  font-size: 12.5px;
  font-family: inherit;
  color: var(--status-waiting);
  text-shadow: 0 0 8px rgba(249, 199, 94, 0.4);
}
</style>
