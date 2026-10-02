<!--
  PageHeader — breadcrumb trail + title + description + primary actions.
  Crowns every console page; the last crumb is marked aria-current="page".
  Optional document.title sync lives here so pages do not repeat the wiring.
-->
<script lang="ts">
import type { RouteLocationRaw } from 'vue-router'

export interface PageCrumb {
  label: string
  /** When set the crumb renders as a router link. */
  to?: RouteLocationRaw
}
</script>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'

const props = withDefaults(
  defineProps<{
    title: string
    /** Trail, root first; the last item is the current page. */
    breadcrumbs?: PageCrumb[]
    description?: string
    /** document.title override; defaults to `title`. */
    documentTitle?: string
    /** Sync document.title while mounted, restoring the previous value on unmount. */
    syncDocumentTitle?: boolean
  }>(),
  { breadcrumbs: () => [], description: '', documentTitle: '', syncDocumentTitle: true },
)

let previousTitle: string | null = null

function applyTitle() {
  document.title = props.documentTitle || props.title
}

onMounted(() => {
  if (!props.syncDocumentTitle) return
  previousTitle = document.title
  applyTitle()
})

watch(
  () => [props.documentTitle, props.title, props.syncDocumentTitle] as const,
  () => {
    if (!props.syncDocumentTitle) return
    if (previousTitle === null) previousTitle = document.title
    applyTitle()
  },
)

onBeforeUnmount(() => {
  if (previousTitle !== null) document.title = previousTitle
})
</script>

<template>
  <header class="page-header">
    <nav v-if="breadcrumbs.length" class="ph-crumbs">
      <template v-for="(crumb, i) in breadcrumbs" :key="i">
        <RouterLink
          v-if="crumb.to"
          class="ph-crumb is-link"
          :to="crumb.to"
          :aria-current="i === breadcrumbs.length - 1 ? 'page' : undefined"
          >{{ crumb.label }}</RouterLink
        >
        <span
          v-else
          class="ph-crumb"
          :aria-current="i === breadcrumbs.length - 1 ? 'page' : undefined"
          >{{ crumb.label }}</span
        >
        <span v-if="i < breadcrumbs.length - 1" class="ph-sep" aria-hidden="true">/</span>
      </template>
    </nav>

    <div class="ph-main">
      <div class="ph-text">
        <h1 class="ph-title"><slot name="title">{{ title }}</slot></h1>
        <p v-if="description || $slots.description" class="ph-desc">
          <slot name="description">{{ description }}</slot>
        </p>
      </div>
      <div v-if="$slots.actions" class="ph-actions">
        <slot name="actions" />
      </div>
    </div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
}

.ph-crumbs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-2xs);
  color: var(--text-tertiary);
}
.ph-crumb {
  color: var(--text-secondary);
}
.ph-crumb.is-link {
  color: var(--text-secondary);
  text-decoration: none;
  border-radius: var(--r-sm);
}
.ph-crumb.is-link:hover {
  color: var(--accent-hover);
  text-decoration: underline;
}
.ph-crumb.is-link:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
.ph-crumb[aria-current='page'] {
  color: var(--text-primary);
  font-weight: 500;
}
.ph-sep {
  color: var(--text-tertiary);
}

.ph-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
}

.ph-text {
  min-width: 0;
}

.ph-title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 600;
  line-height: var(--lh-tight);
  color: var(--text-primary);
}

.ph-desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.ph-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
}
</style>
