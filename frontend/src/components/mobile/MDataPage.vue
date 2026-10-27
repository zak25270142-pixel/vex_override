<template>
  <!-- 数据页：分段切换 实参 / 调参 -->
  <div>
    <div class="m-seg">
      <button :class="{ on: sub === 'monitor' }" @click="sub = 'monitor'">实参</button>
      <button :class="{ on: sub === 'tunable' }" @click="sub = 'tunable'">调参</button>
    </div>
    <MMonitorPage v-if="sub === 'monitor'" />
    <MTunablePage v-else />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import MMonitorPage from './MMonitorPage.vue'
import MTunablePage from './MTunablePage.vue'

const sub = ref<'monitor' | 'tunable'>('monitor')
</script>
