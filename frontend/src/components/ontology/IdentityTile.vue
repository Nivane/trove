<!--
  IdentityTile — 类型身份砖（圆角底 + 图标），任何尺寸都清晰（viewBox 缩放）。

  用在 SVG 之外的地方（侧栏身份头、出/入链接行、图例）；图上的节点砖由
  OntologyGraph 直接画在 <g> 里 —— 同一份 GlyphPrimitives 与同一个
  objectIdentity 派生，只是宿主不同。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { tileFillOpacity, type ObjectIdentity } from '../../utils/ontologyIdentity'
import GlyphPrimitives from './GlyphPrimitives.vue'

const props = defineProps<{ identity: ObjectIdentity; size?: number }>()

const px = computed(() => props.size ?? 16)
const fillOpacity = computed(() => tileFillOpacity(props.identity))
</script>

<template>
  <svg
    class="onto-tile"
    :width="px"
    :height="px"
    viewBox="0 0 30 30"
    aria-hidden="true"
    focusable="false"
  >
    <rect width="30" height="30" rx="9" :fill="identity.color" :fill-opacity="fillOpacity" />
    <GlyphPrimitives :icon="identity.icon" :color="identity.color" />
  </svg>
</template>

<style scoped>
.onto-tile {
  flex: none;
  display: block;
}
</style>
