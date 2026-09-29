<template>
  <!-- 通信链路诊断：把"点击→发送→接收→解析"每一段都摆出来，故障环节直接读结论 -->
  <Teleport to="body">
    <div v-if="diagOpen" class="diag-mask" @click.self="diagOpen = false">
      <div class="diag">
        <header class="diag__head">
          <strong>通信诊断</strong>
          <div class="diag__head-actions">
            <button class="mini" @click="clearDiag">清零</button>
            <button class="mini" @click="diagOpen = false">关闭</button>
          </div>
        </header>

        <!-- 自动结论：按计数把故障定位到具体环节 -->
        <section class="verdict" :class="`verdict--${verdict.level}`">
          <b>{{ verdict.title }}</b>
          <ul>
            <li v-for="(line, i) in verdict.lines" :key="i">{{ line }}</li>
          </ul>
        </section>

        <section class="meta">
          <div>当前端口：<b>{{ portLabel }}</b></div>
          <div>接入方式：{{ demoMode ? '演示数据源' : 'Web Serial 实机' }}（波特率 115200，CDC 下仅占位）</div>
        </section>

        <!-- 逐段计数 -->
        <section class="counters">
          <div class="counter">
            <span>已发请求</span><b :class="{ bad: s.txFrames === 0 }">{{ s.txFrames }}</b>
          </div>
          <div class="counter">
            <span>接收块数</span><b :class="{ bad: s.txFrames > 0 && s.rxChunks === 0 }">{{ s.rxChunks }}</b>
          </div>
          <div class="counter">
            <span>接收字节</span><b :class="{ bad: s.txFrames > 0 && s.rxBytes === 0 }">{{ s.rxBytes }}</b>
          </div>
          <div class="counter">
            <span>解析成帧</span><b :class="{ bad: s.rxBytes > 0 && s.frames === 0 }">{{ s.frames }}</b>
          </div>
          <div class="counter">
            <span>目录帧</span><b>{{ s.dirFrames }}</b>
          </div>
          <div class="counter">
            <span>监控值帧</span><b>{{ s.valueFrames }}</b>
          </div>
          <div class="counter">
            <span>校验失败</span><b :class="{ bad: s.badChecksum > 0 }">{{ s.badChecksum }}</b>
          </div>
          <div class="counter">
            <span>未知命令字</span><b :class="{ bad: s.unknownCmd > 0 }">{{ s.unknownCmd }}</b>
          </div>
        </section>

        <!-- 原始日志（最新在上）。默认只看关键事件，高速 RX 流切"原始字节"看 -->
        <section class="logbar">
          <div class="seg2">
            <button :class="{ on: logFilter === 'key' }" @click="logFilter = 'key'">关键事件</button>
            <button :class="{ on: logFilter === 'rx' }" @click="logFilter = 'rx'">原始字节</button>
            <button :class="{ on: logFilter === 'all' }" @click="logFilter = 'all'">全部</button>
          </div>
        </section>
        <section class="log">
          <div v-for="(l, i) in shownLog" :key="i" class="log__line">
            <span class="log__time">{{ l.t }}</span>
            <span class="log__dir" :class="`log__dir--${l.dir}`">{{ l.dir }}</span>
            <span class="log__text">{{ l.text }}</span>
          </div>
          <div v-if="shownLog.length === 0" class="log__empty">该分类下暂无记录</div>
        </section>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { clearDiag, commLog, commStats as s, demoMode, diagOpen, statusText } from '@/stores/globle'
import { serialClient } from '@/services/serialClient'

const portLabel = computed(() => serialClient.portLabel)

/** 日志过滤：默认关键事件（TX 请求/EVT 成帧/ERR 错误），避开 100Hz 的 RX 流 */
const logFilter = ref<'key' | 'rx' | 'all'>('key')
const shownLog = computed(() =>
  logFilter.value === 'all'
    ? commLog.value
    : commLog.value.filter((l) => (logFilter.value === 'rx' ? l.dir === 'RX' : l.dir !== 'RX')),
)

/** 诊断结论：从计数推断卡在链路哪一段（演示模式也照走判定，只加一条提示） */
const verdict = computed(() => {
  const demoNote = '当前为演示数据源：全程走同一套二进制协议但不经过真实串口，排查实机问题请取消勾选「演示」。'
  if (s.txFrames === 0) {
    return {
      level: 'warn',
      title: demoMode.value ? '演示模式未发送请求' : '还没有发出过任何请求帧',
      lines: [
        demoMode.value ? demoNote : '保持本窗口打开，再点一次标题栏的「获取调参表 / 获取实参表」，观察下面计数变化。',
      ],
    }
  }
  if (s.rxBytes === 0) {
    return {
      level: 'bad',
      title: '请求已发出，但主控一个字节都没有回（最常见）',
      lines: [
        '① 选错口：V5 会枚举出两个串口，必须选「User 口 / User Port」，不是 VEXcode 下载程序用的那个通信口；可断开后点「选口」重选。',
        '② 固件没更新：主控里运行的程序必须是带新二进制通信协议的版本——旧程序不认识 A5 帧，收到也不会回。请在 VEXcode 重新下载程序后重试。',
        '③ 确认主控屏幕上用户程序正在运行（不是停在下载/选择界面），换一根支持数据的 USB 线、直连电脑 USB 口再试。',
      ],
    }
  }
  if (s.frames === 0) {
    if (s.unknownCmd > 0) {
      return {
        level: 'bad',
        title: '收到字节，但帧头后的命令字无法识别',
        lines: [
          '物理链路是通的，但收到的不是本协议的数据——基本可以确定主控运行的是旧版程序，请重新下载最新固件。',
        ],
      }
    }
    if (s.badChecksum > 0) {
      return {
        level: 'bad',
        title: '收到字节，但每帧校验都失败',
        lines: ['字节有损坏/错位：检查是否串了其他设备的数据，或重新插拔后重试。'],
      }
    }
    return {
      level: 'bad',
      title: '收到字节，但始终凑不成完整帧',
      lines: ['数据长度对不上协议，多半也是固件与前端版本不匹配；请确认固件已更新到最新版。'],
    }
  }
  if (s.dirFrames === 0) {
    return {
      level: 'warn',
      title: '链路正常，已能收到数据帧，但还没有目录帧',
      lines: ['点一次「获取调参表 / 获取实参表」请求目录。'],
    }
  }
  return {
    level: 'ok',
    title: '链路正常：目录帧已收到，表格应已填充',
    lines: [
      ...(demoMode.value ? [demoNote] : []),
      `状态文本：${statusText.value}`,
      '若个别量无数据，检查该项订阅档位是否为「未订阅」。',
    ],
  }
})
</script>

<style scoped>
.diag-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(2, 10, 20, 0.65);
  display: grid;
  place-items: center;
}

.diag {
  width: min(760px, 92vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  background: var(--surface-floating);
  border: var(--border-panel);
  border-radius: var(--radius);
  box-shadow: var(--shadow-panel);
  overflow: hidden;
}

.diag__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: var(--border-subtle);
}

.diag__head strong {
  font-size: 14px;
  color: var(--text-bright);
}

.diag__head-actions {
  display: flex;
  gap: 8px;
}

.mini {
  padding: 4px 12px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-secondary);
  background: transparent;
  border: var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
}

.mini:hover {
  color: var(--accent);
  border-color: var(--accent-border);
}

.verdict {
  margin: 12px 16px 0;
  padding: 10px 14px;
  border-radius: var(--radius);
  border: 1px solid;
  font-size: 12.5px;
  line-height: 1.7;
}

.verdict b {
  font-size: 13px;
}

.verdict ul {
  margin: 6px 0 0;
  padding-left: 18px;
}

.verdict--ok {
  color: var(--status-success);
  border-color: var(--success-border);
  background: var(--success-soft);
}

.verdict--warn {
  color: var(--status-waiting);
  border-color: rgba(249, 199, 94, 0.5);
  background: rgba(249, 199, 94, 0.08);
}

.verdict--bad {
  color: var(--status-danger);
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.meta {
  margin: 10px 16px 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.8;
}

.counters {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin: 12px 16px;
}

.counter {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 8px 4px;
  background: var(--surface-panel);
  border: var(--border-subtle);
  border-radius: var(--radius);
}

.counter span {
  font-size: 11px;
  color: var(--text-muted);
}

.counter b {
  font-family: var(--font-mono);
  font-size: 18px;
  color: var(--text-bright);
}

.counter b.bad {
  color: var(--status-danger);
}

.logbar {
  margin: 0 16px;
}

.seg2 {
  display: inline-flex;
  border: var(--border-subtle);
  border-radius: var(--radius);
  overflow: hidden;
}

.seg2 button {
  padding: 4px 14px;
  font-size: 11.5px;
  font-family: inherit;
  color: var(--text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
}

.seg2 button.on {
  color: var(--text-on-accent);
  background: var(--accent);
  font-weight: 700;
}

.log {
  flex: 1;
  min-height: 160px;
  margin: 8px 16px 16px;
  padding: 8px 10px;
  overflow-y: auto;
  background: #040b14;
  border: var(--border-subtle);
  border-radius: var(--radius);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.7;
}

.log__empty {
  color: var(--text-muted);
  text-align: center;
  padding: 30px 0;
}

.log__line {
  display: flex;
  gap: 8px;
  white-space: pre-wrap;
  word-break: break-all;
}

.log__time {
  color: var(--text-muted);
  flex-shrink: 0;
}

.log__dir {
  width: 30px;
  flex-shrink: 0;
  font-weight: 700;
}

.log__dir--TX {
  color: var(--accent);
}

.log__dir--RX {
  color: var(--status-success);
}

.log__dir--EVT {
  color: var(--status-waiting);
}

.log__dir--ERR {
  color: var(--status-danger);
}

.log__text {
  color: var(--text-secondary);
}
</style>
