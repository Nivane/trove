<!--
  TodoQueue — the eight todo sources in one card (设计稿 §2「待办队列」).

  Every number here was fanned out server-side once; this component only
  renders it. Two honesty rules from the design doc are implemented here:
    · a kind whose count could not be produced shows a lower bound —
      `≥ N` when `count_exact: false`, an em dash when the count is null —
      never a precise-looking low number (「少报会让管理员以为没事」);
    · a kind with `available: false` is marked as not-configured or
      degraded, not silently rendered as 0.
  Deep links come from the server (`href`); `null` means the target page
  does not exist yet (memory preference drafts) and is said so, not linked.
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { Component } from 'vue'
import { RouterLink } from 'vue-router'
import {
  BookOpen,
  BookOpenCheck,
  Brain,
  Clock,
  FileText,
  Layers3,
  Radar,
  UserX,
} from 'lucide-vue-next'
import { t } from '../../i18n'
import { useUiStore } from '../../stores/ui'
import type { OverviewTodoItem, OverviewTodoKind, OverviewTodos } from '../../api/overview'

const props = defineProps<{ todos: OverviewTodos }>()

const ui = useUiStore()

const KIND_LABEL: Record<OverviewTodoKind, keyof typeof import('../../i18n').messages['zh']> = {
  kb_lesson: 'ovTodoKbLesson',
  kb_example: 'ovTodoKbExample',
  semantic_draft: 'ovTodoSemanticDraft',
  skill_draft: 'ovTodoSkillDraft',
  memory_preference: 'ovTodoMemoryPref',
  drift: 'ovTodoDrift',
  job_failed: 'ovTodoJobFailed',
  user_nogrant: 'ovTodoUserNogrant',
}

const KIND_ICON: Record<OverviewTodoKind, Component> = {
  kb_lesson: BookOpen,
  kb_example: FileText,
  semantic_draft: Layers3,
  skill_draft: BookOpenCheck,
  memory_preference: Brain,
  drift: Radar,
  job_failed: Clock,
  user_nogrant: UserX,
}

/** `≥ N` for inexact counts, `—` for a count nobody could produce. */
function fmtCount(item: OverviewTodoItem): string {
  if (item.count === null) return '—'
  return item.count_exact ? String(item.count) : `≥ ${item.count}`
}

function stateNote(item: OverviewTodoItem): string {
  if (item.count === null || item.note === 'degraded') return t('ovTodoDegraded', ui.lang)
  if (item.note === 'capped') return t('ovTodoCapped', ui.lang)
  if (!item.available) return t('ovTodoUnconfigured', ui.lang)
  return ''
}

const rows = computed(() => props.todos.items)

const empty = computed(() => props.todos.count_exact && props.todos.total === 0)
</script>

<template>
  <div class="todo-queue">
    <p v-if="empty" class="tq-empty">{{ t('ovTodosEmpty', ui.lang) }}</p>

    <ul v-else class="tq-list">
      <li v-for="item in rows" :key="item.kind" class="tq-row">
        <span class="tq-icon" aria-hidden="true">
          <component :is="KIND_ICON[item.kind]" :size="15" />
        </span>
        <div class="tq-main">
          <div class="tq-head">
            <span class="tq-label">{{ t(KIND_LABEL[item.kind], ui.lang) }}</span>
            <span
              class="tq-count"
              :class="{ 'is-inexact': !item.count_exact || item.count === null }"
              >{{ fmtCount(item) }}</span>
            <span v-if="stateNote(item)" class="tq-note">{{ stateNote(item) }}</span>
          </div>
          <p v-if="item.samples.length" class="tq-samples" :title="item.samples.join('\n')">
            {{ item.samples.join(' · ') }}
          </p>
        </div>
        <RouterLink v-if="item.href" class="tq-link" :to="item.href">
          {{ t('ovTodoReview', ui.lang) }}
        </RouterLink>
        <span v-else class="tq-nolink">{{ t('ovTodoNoPage', ui.lang) }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.tq-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.tq-row {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.tq-row:last-child {
  border-bottom: none;
}

.tq-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-secondary);
}

.tq-main {
  flex: 1;
  min-width: 0;
}

.tq-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.tq-label {
  font-size: var(--fs-xs);
  color: var(--text-primary);
}

.tq-count {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.tq-count.is-inexact {
  color: var(--warn);
}

.tq-note {
  padding: 0 var(--sp-1);
  border-radius: var(--r-sm);
  background: var(--warn-bg);
  color: var(--warn-text);
  font-size: var(--fs-2xs);
}

.tq-samples {
  margin: 2px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}

.tq-link {
  flex: none;
  align-self: center;
  font-size: var(--fs-2xs);
  color: var(--accent-hover);
  text-decoration: none;
  border-radius: var(--r-sm);
}
.tq-link:hover {
  text-decoration: underline;
}
.tq-link:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}

.tq-nolink {
  flex: none;
  align-self: center;
  padding: 0 var(--sp-1);
  border-radius: var(--r-sm);
  background: var(--surface-muted);
  color: var(--text-tertiary);
  font-size: var(--fs-2xs);
}

.tq-empty {
  margin: 0;
  padding: var(--sp-4) 0;
  text-align: center;
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
}
</style>
