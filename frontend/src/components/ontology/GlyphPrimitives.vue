<!--
  GlyphPrimitives — 图标图元（30×30 视野内的 <g>，无底色）。

  几何只此一份：卡片上的身份砖、侧栏身份头、图例三处都从这里取 ——
  描边宽度与端点样式也在这里统一（各处自己写一遍 stroke 属性，
  迟早有一处忘了 round 而露出尖角）。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { GLYPHS, GLYPH_STROKE_WIDTH, type IconName } from '../../utils/ontologyIdentity'

const props = defineProps<{ icon: IconName; color: string }>()

const glyph = computed(() => GLYPHS[props.icon])
</script>

<template>
  <g
    transform="translate(3,3)"
    fill="none"
    :stroke="color"
    :stroke-width="GLYPH_STROKE_WIDTH"
    stroke-linecap="round"
    stroke-linejoin="round"
  >
    <rect
      v-for="(r, i) in glyph.rects ?? []"
      :key="`r${i}`"
      :x="r[0]"
      :y="r[1]"
      :width="r[2]"
      :height="r[3]"
      :rx="r[4]"
    />
    <circle
      v-for="(c, i) in glyph.circles ?? []"
      :key="`c${i}`"
      :cx="c[0]"
      :cy="c[1]"
      :r="c[2]"
    />
    <path v-for="(d, i) in glyph.paths ?? []" :key="`p${i}`" :d="d" />
  </g>
</template>
