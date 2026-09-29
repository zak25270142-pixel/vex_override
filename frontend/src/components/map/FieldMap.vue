<template>
  <!-- 页面3：6×6 场地图。下位机 x/y 单位米（在此 ×100 换 cm），yaw 度、顺时针正、0°朝 X+ -->
  <div class="field-map">
    <svg class="field-map__svg" viewBox="0 0 524 524" preserveAspectRatio="xMidYMid meet">
      <!-- 棋盘格：SVG 的 Y 向下，row=1 画在最上排，对应场地 Y 最大的一行 -->
      <g>
        <template v-for="row in FIELD_SIZE" :key="row">
          <rect
            v-for="col in FIELD_SIZE"
            :key="col"
            :x="originX + (col - 1) * CELL_PX"
            :y="originY + (row - 1) * CELL_PX"
            :width="CELL_PX"
            :height="CELL_PX"
            stroke-width="1"
            :style="{
              fill: (row + col) % 2 === 0 ? 'var(--map-cell-a)' : 'var(--map-cell-b)',
              stroke: 'var(--map-grid-line)',
            }"
          />
        </template>
      </g>

      <!-- 场地边界 -->
      <rect
        :x="originX - 2"
        :y="originY - 2"
        :width="FIELD_PX + 4"
        :height="FIELD_PX + 4"
        fill="none"
        stroke-width="3"
        :style="{ stroke: 'var(--map-border)' }"
      />

      <!-- 坐标轴标注 -->
      <text
        :x="originX + FIELD_PX + 8"
        :y="originY + FIELD_PX + 22"
        font-size="11"
        :style="{ fill: 'var(--map-label)' }"
      >
        X (cm)
      </text>
      <text
        :x="originX - 22"
        :y="originY - 6"
        font-size="11"
        :style="{ fill: 'var(--map-label)' }"
      >
        Y (cm)
      </text>
      <text
        :x="originX - 26"
        :y="originY + FIELD_PX + 22"
        font-size="10"
        :style="{ fill: 'var(--map-label)' }"
      >
        (0,0)
      </text>
      <text
        :x="originX + FIELD_PX - 40"
        :y="originY - 6"
        font-size="10"
        :style="{ fill: 'var(--map-label)' }"
      >
        ({{ FIELD_CM }},{{ FIELD_CM }})
      </text>

      <template v-if="pose.valid">
        <!-- 0° 参考虚线：始终指向 X+（向右），不随机体旋转 -->
        <line
          :x1="robotX"
          :y1="robotY"
          :x2="robotX + CELL_PX / 2"
          :y2="robotY"
          stroke-width="1"
          stroke-dasharray="5,4"
          :style="{ stroke: 'var(--map-reference)' }"
        />

        <!-- 机器人：图形 0° 朝上（-Y），固件 yaw 0° 朝 X+，故整体再转 yaw-90；
             SVG rotate 正值即顺时针，与 yaw 方向一致 -->
        <g :transform="`translate(${robotX},${robotY}) rotate(${yawSvg})`">
          <circle
            r="13"
            fill-opacity="0.9"
            stroke-width="2"
            :style="{ fill: 'var(--map-robot)', stroke: 'var(--map-robot-edge)' }"
          />
          <line
            x1="0"
            y1="0"
            x2="0"
            y2="-40"
            stroke-width="3"
            :style="{ stroke: 'var(--map-robot)' }"
          />
          <polygon points="0,-42 -8,-26 8,-26" :style="{ fill: 'var(--map-robot)' }" />
        </g>
      </template>
    </svg>

    <!-- 数值读数 -->
    <div v-if="pose.valid" class="readout">
      <div><span>X</span><b>{{ pose.x.toFixed(3) }}</b><em>m</em></div>
      <div><span>Y</span><b>{{ pose.y.toFixed(3) }}</b><em>m</em></div>
      <div><span>Yaw</span><b>{{ normYaw.toFixed(1) }}</b><em>°</em></div>
    </div>

    <!-- 未收到 x/y/yaw 时的提示 -->
    <div v-else class="hint">
      <template v-if="!connected">尚未连接 V5，请在标题栏连接（或勾选“演示”）</template>
      <template v-else>
        尚未收到全局坐标 X / Y / Yaw。<br />
        请确认实参表中带「位置X / 位置Y / 航向」标记的量已随连接自动订阅
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { connected, pose } from '@/stores/globle'
import { CELL_CM, FIELD_CM, FIELD_SIZE } from '@/config/workspace'

const CELL_PX = 80
const FIELD_PX = FIELD_SIZE * CELL_PX
const originX = 22
const originY = 22

const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v))

// 下位机 x/y 是米，场地坐标是 cm，进图前 ×100
const gridX = computed(() => clamp((pose.x * 100) / CELL_CM, 0, FIELD_SIZE))
const gridY = computed(() => clamp((pose.y * 100) / CELL_CM, 0, FIELD_SIZE))
const robotX = computed(() => originX + gridX.value * CELL_PX)
const robotY = computed(() => originY + (FIELD_SIZE - gridY.value) * CELL_PX)

/** 给 SVG rotate 用：图形初始朝 Y+，固件 0° 朝 X+，减 90 对齐 */
const yawSvg = computed(() => pose.yaw - 90)

/** 读数统一显示成 ±180 */
const normYaw = computed(() => {
  let a = pose.yaw % 360
  if (a > 180) a -= 360
  if (a < -180) a += 360
  return a
})
</script>

<style scoped>
.field-map {
  position: relative;
  display: grid;
  place-items: center;
  width: 100%;
  height: 100%;
  min-height: 0;
  border: var(--border-panel);
  border-radius: var(--radius);
  background: var(--surface-panel);
  box-shadow: var(--shadow-panel);
  padding: 8px;
}

.field-map__svg {
  width: 100%;
  height: 100%;
  max-width: 100%;
  max-height: 100%;
}

.readout {
  position: absolute;
  left: 18px;
  top: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 16px;
  background: rgba(6, 20, 35, 0.78);
  border: var(--border-subtle);
  border-radius: var(--radius);
  font-family: var(--font-mono);
  font-size: 16px;
}

.readout div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.readout span {
  width: 38px;
  font-size: 12px;
  color: var(--text-muted);
}

.readout b {
  color: var(--text-bright);
  font-weight: 700;
  min-width: 92px;
}

.readout em {
  font-style: normal;
  font-size: 11px;
  color: var(--text-muted);
}

.hint {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  text-align: center;
  line-height: 1.8;
  font-size: 15px;
  color: var(--text-muted);
  pointer-events: none;
}
</style>
