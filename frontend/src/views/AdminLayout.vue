<!--
  AdminLayout — 路由挂载点（薄封装）。
  壳的实现搬到 components/layout/ConsoleShell.vue（设计稿 P6 §3：保持
  文件名与路由 component 不变，减少 diff；W0 是唯一写这个文件的波次）。
-->
<template>
  <ConsoleShell />
</template>

<script setup lang="ts">
import ConsoleShell from '../components/layout/ConsoleShell.vue'
</script>
