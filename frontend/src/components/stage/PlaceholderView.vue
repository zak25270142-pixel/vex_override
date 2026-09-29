<template>
  <!-- 预留分页占位：统一的"待扩展"视觉，不放置业务数据 -->
  <div class="placeholder">
    <div class="placeholder__icon" v-html="icon" />
    <h3>{{ title }}</h3>
    <p>{{ desc }}</p>
    <span class="placeholder__tag">模块待扩展</span>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  title: string
  desc: string
  icon: string
}>()
</script>

<style scoped>
.placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  border: var(--border-panel);
  border-radius: var(--radius);
  background:
    radial-gradient(circle at 50% 42%, rgba(79, 224, 255, 0.05), transparent 60%),
    var(--surface-panel);
  color: var(--text-muted);
  user-select: none;
}

.placeholder__icon {
  color: var(--text-muted);
  opacity: 0.55;
  margin-bottom: 4px;
}

.placeholder h3 {
  margin: 0;
  font-size: 16px;
  color: var(--text-secondary);
  letter-spacing: 0.06em;
}

.placeholder p {
  margin: 0;
  font-size: 12px;
}

.placeholder__tag {
  margin-top: 6px;
  padding: 3px 12px;
  font-size: 11px;
  letter-spacing: 0.14em;
  border: 1px dashed rgba(89, 179, 235, 0.4);
  border-radius: 999px;
  color: var(--accent-muted);
}
</style>
