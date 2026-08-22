<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    aria-hidden="true"
    class="brand-mark-svg"
  >
    <!-- 数据柱:左低右高的增长形态 -->
    <rect x="3.6" y="13" width="4" height="8" rx="1.3" fill="currentColor" />
    <rect x="10" y="9.4" width="4" height="11.6" rx="1.3" fill="currentColor" opacity="0.8" />
    <rect x="16.4" y="5.4" width="4" height="15.6" rx="1.3" fill="currentColor" opacity="0.6" />
    <!-- AI 火花:悬在增长柱之上 -->
    <path
      d="M12 1.8 l1 2.4 2.4 1 -2.4 1 -1 2.4 -1-2.4 -2.4-1 2.4-1 z"
      fill="currentColor"
    />
  </svg>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 16 })
</script>