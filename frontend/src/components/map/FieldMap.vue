<template>
  <!-- 页面3：6×6 场地图。下位机 x/y 单位米（在此 ×100 换 cm），yaw 度、顺时针正、0°朝 X+（前方）。
       屏幕约定：后端 +x(前) 画成竖直向上、+y(右) 画成水平向右——保手性的纯旋转，
       车头左转/右转与图上完全同向；摆放场地时让开机车头朝向与屏幕一致即可 -->
  <div class="field-map">
    <svg class="field-map__svg" viewBox="0 0 524 524" preserveAspectRatio="xMidYMid meet">
      <!-- 棋盘格：SVG 的 Y 向下，row=1 画在最上排，对应后端 X 最大的一行 -->
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

      <!-- 坐标轴标注：水平向右=后端Y(车体右侧)，竖直向上=后端X(前方) -->
      <text
        :x="originX + FIELD_PX + 8"
        :y="originY + FIELD_PX + 22"
        font-size="11"
        :style="{ fill: 'var(--map-label)' }"
      >
        Y (cm)
      </text>
      <text
        :x="originX - 22"
        :y="originY - 6"
        font-size="11"
        :style="{ fill: 'var(--map-label)' }"
      >
        X (cm)
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
        <!-- 历史行驶轨迹（最近3分钟，与主控推送同节拍）：与车体箭头同一保手性映射 -->
        <polyline
          :points="trailPoints"
          fill="none"
          stroke-width="2"
          stroke-linejoin="round"
          stroke-linecap="round"
          :style="{ stroke: 'var(--map-trail)' }"
        />

        <!-- 0° 参考虚线：始终指向 X+（屏幕正上方），不随机体旋转 -->
        <line
          :x1="robotX"
          :y1="robotY"
          :x2="robotX"
          :y2="robotY - CELL_PX / 2"
          stroke-width="1"
          stroke-dasharray="5,4"
          :style="{ stroke: 'var(--map-reference)' }"
        />

        <!-- 机器人：图形 0° 朝上(-Y)，与后端 yaw 0° 朝 X+（屏幕正上）一致；
             SVG rotate 正值即顺时针，与 yaw 方向一致，直接 rotate(yaw) -->
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

    <!-- 数值读数已迁至右栏「场景数据」标签，地图区只留图形 -->

    <!-- 未收到 x/y/yaw 时的提示 -->
    <div v-if="!pose.valid" class="hint">
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
import { connected, getSeries, pose, poseIndex } from '@/stores/globle'
import { CELL_CM, FIELD_CM, FIELD_SIZE, TRACE_BUF_CAP } from '@/config/workspace'

const CELL_PX = 80
const FIELD_PX = FIELD_SIZE * CELL_PX
const originX = 22
const originY = 22

const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v))

// 下位机 x/y 是米，场地坐标是 cm，进图前 ×100
const gridX = computed(() => clamp((pose.x * 100) / CELL_CM, 0, FIELD_SIZE))
const gridY = computed(() => clamp((pose.y * 100) / CELL_CM, 0, FIELD_SIZE))
// 保手性变换：后端 +x(前)→屏幕向上（纵向翻转），+y(右)→屏幕向右。
// 必须是纯旋转而非镜像，否则车头转向会与实际相反（旧版 sx=x、sy=-y 是反射，左转画成右转）
const robotX = computed(() => originX + gridY.value * CELL_PX)
const robotY = computed(() => originY + (FIELD_SIZE - gridX.value) * CELL_PX)

/** 图形 0° 朝上即后端 yaw 0° 朝 X+，SVG 顺时针正与 yaw 同向，无需偏置 */
const yawSvg = computed(() => pose.yaw)

/** 轨迹最多画这么多点（点太多 SVG 会卡；超出按等间隔抽稀显示，末尾点始终保留） */
const TRAIL_MAX_POINTS = 1200

/**
 * 历史轨迹点串（最近3分钟，与主控推送同节拍，高速区即 100Hz）：
 * x/y 各是同一份 seriesBuf 里的一条环形缓冲，取数时做 as-of join——
 * 时间升序遍历 y 样本，为每个 y 配"最近且不晚于它"的 x（同拍到达时天然配对）。
 * 缓冲本身非响应式，借 pose.x 的响应式值触发重算；映射与车体箭头同一保手性公式（含越界夹边）
 */
const trailPoints = computed(() => {
  void pose.x
  const xb = poseIndex.x >= 0 ? getSeries(poseIndex.x) : undefined
  const yb = poseIndex.y >= 0 ? getSeries(poseIndex.y) : undefined
  if (!xb || !yb || xb.len < 2 || yb.len < 2) return ''
  const xs = (xb.head - xb.len + TRACE_BUF_CAP) % TRACE_BUF_CAP
  const ys = (yb.head - yb.len + TRACE_BUF_CAP) % TRACE_BUF_CAP
  const pts: string[] = []
  let kx = 0
  for (let ky = 0; ky < yb.len; ky++) {
    const ty = yb.t[(ys + ky) % TRACE_BUF_CAP]!
    while (kx + 1 < xb.len && xb.t[(xs + kx + 1) % TRACE_BUF_CAP]! <= ty) kx++
    const x = xb.v[(xs + kx) % TRACE_BUF_CAP]!
    const y = yb.v[(ys + ky) % TRACE_BUF_CAP]!
    const gx = clamp((x * 100) / CELL_CM, 0, FIELD_SIZE)
    const gy = clamp((y * 100) / CELL_CM, 0, FIELD_SIZE)
    pts.push(`${originX + gy * CELL_PX},${originY + (FIELD_SIZE - gx) * CELL_PX}`)
  }
  if (pts.length < 2) return ''
  const stride = Math.ceil(pts.length / TRAIL_MAX_POINTS)
  if (stride <= 1) return pts.join(' ')
  const out: string[] = []
  for (let i = 0; i < pts.length; i += stride) out.push(pts[i]!)
  const last = pts[pts.length - 1]!
  if (out[out.length - 1] !== last) out.push(last)
  return out.join(' ')
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
