import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import StyleguideView from '../src/views/admin/StyleguideView.vue'
import { router as appRouter } from '../src/router'
import { t } from '../src/i18n'

/**
 * /admin/styleguide (P7-W4): the living specimen room. These tests pin the
 * three things the wave promised — the page exists at its URL, it is built
 * from the six real base components, and the state switcher actually drives
 * them through the §6.3 matrix instead of showing static mockups.
 */

const LANG = 'zh' as const
let wrapper: VueWrapper | null = null
let router: Router

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/datasources', component: { render: () => null } },
      { path: '/admin/styleguide', component: StyleguideView },
    ],
  })
}

async function mountPage() {
  await router.push('/admin/styleguide')
  await router.isReady()
  wrapper = mount(StyleguideView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  router = makeRouter()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/** The DataTable specimen card — scoping matters: density demos also render tables. */
function tableCard(w: VueWrapper) {
  return w.findAll('.sg-card')[0]
}

async function pickState(w: VueWrapper, label: string) {
  const btn = w.findAll('.sg-switch-btn').find((b) => b.text() === label)
  expect(btn, `state button ${label}`).toBeTruthy()
  await btn!.trigger('click')
  await flushPromises()
}

describe('StyleguideView', () => {
  it('is reachable at /admin/styleguide in the real route table', () => {
    const resolved = appRouter.resolve('/admin/styleguide')
    expect(resolved.name).toBe('admin-styleguide')
    expect(resolved.meta.titleKey).toBe('styleguideTitle')
  })

  it('renders the six base components for real', async () => {
    const w = await mountPage()
    expect(w.findComponent({ name: 'PageHeader' }).exists()).toBe(true)
    expect(w.findAll('.data-table').length).toBe(3) // list + 2 density specimens
    expect(w.findAll('.state-panel').length).toBeGreaterThanOrEqual(3)
    expect(w.findAll('.kpi-tile').length).toBe(2)
    expect(w.findAll('.sg-switch-btn').length).toBe(6)
  })

  it('switches the table through the state matrix', async () => {
    const w = await mountPage()

    // live: populated rows
    expect(tableCard(w).findAll('.dt-row').length).toBe(4)

    // loading: skeleton rows, no data rows
    await pickState(w, t('sgStateLoading', LANG))
    expect(tableCard(w).findAll('.dt-skel-row').length).toBe(4)
    expect(tableCard(w).findAll('.dt-row').length).toBe(0)

    // refreshing: old rows stay + the top progress bar appears
    await pickState(w, t('sgStateRefresh', LANG))
    expect(tableCard(w).findAll('.dt-progress').length).toBe(1)
    expect(tableCard(w).findAll('.dt-row').length).toBe(4)

    // empty (first run): guidance action "go register"
    await pickState(w, t('sgStateEmptyFirst', LANG))
    const firstEmpty = tableCard(w).find('.state-panel.is-empty')
    expect(firstEmpty.exists()).toBe(true)
    expect(firstEmpty.text()).toContain(t('sgEmptyFirstTitle', LANG))
    expect(firstEmpty.find('a.sg-btn').attributes('href')).toContain('/admin/datasources')

    // empty (filtered): clear-filters lands back on a populated list
    await pickState(w, t('sgStateEmptyFiltered', LANG))
    const filteredEmpty = tableCard(w).find('.state-panel.is-empty')
    expect(filteredEmpty.text()).toContain(t('sgEmptyFilteredTitle', LANG))
    await filteredEmpty.find('button.sg-btn').trigger('click')
    await flushPromises()
    expect(tableCard(w).findAll('.dt-row').length).toBe(4)

    // error: retryable StatePanel replaces the table in that block
    await pickState(w, t('sgStateError', LANG))
    expect(tableCard(w).find('.data-table').exists()).toBe(false)
    const err = tableCard(w).find('.state-panel.is-error')
    expect(err.exists()).toBe(true)
    expect(err.attributes('role')).toBe('alert')
    expect(err.find('button.sp-btn').exists()).toBe(true)

    // forbidden: 403 copy, not a "load failed"
    await pickState(w, t('sgStateForbidden', LANG))
    const forbidden = tableCard(w).find('.state-panel')
    expect(forbidden.text()).toContain(t('sgForbiddenTitle', LANG))
    expect(forbidden.find('.is-error').exists()).toBe(false)
  })

  it('shows the identifier/mono and numeric column contract', async () => {
    const w = await mountPage()
    expect(tableCard(w).findAll('.dt-td.is-mono').length).toBe(8) // id + table, 4 rows
    expect(tableCard(w).findAll('.dt-td.is-num').length).toBe(4) // rows column
  })

  it('renders both density registers from tokens', async () => {
    const w = await mountPage()
    expect(w.findAll('.sg-density').length).toBe(2)
    // the comfortable specimen re-declares the root register on its subtree;
    // the console one inherits the page scope (no inline override)
    expect(w.find('.sg-density.is-chat').attributes('style')).toContain('--density-row-h')
    expect(w.find('.sg-density.is-console').attributes('style') ?? '').not.toContain(
      '--density-row-h',
    )
  })

  it('renders the four status families with text / bg / border', async () => {
    const w = await mountPage()
    expect(w.findAll('.sg-fam').length).toBe(4)
    for (const key of ['ok', 'warn', 'danger', 'info']) {
      expect(w.find(`.sg-fam.is-${key} .sg-fam-chip`).exists()).toBe(true)
      expect(w.find(`.sg-fam.is-${key} .sg-fam-mark`).exists()).toBe(true)
    }
  })

  it('opens the real ConfirmDialog and DetailDrawer specimens', async () => {
    const w = await mountPage()

    const openConfirm = w.findAll('button.sg-btn').find((b) => b.text().includes(t('sgOpenConfirm', LANG)))
    expect(openConfirm).toBeTruthy()
    await openConfirm!.trigger('click')
    await flushPromises()
    const dialog = document.body.querySelector('.confirm-panel')
    expect(dialog).toBeTruthy()
    expect(dialog!.getAttribute('role')).toBe('alertdialog') // danger styling

    const openDrawer = w.findAll('button.sg-btn').find((b) => b.text().includes(t('sgOpenDrawer', LANG)))
    expect(openDrawer).toBeTruthy()
    await openDrawer!.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('.drawer-panel')).toBeTruthy()
  })
})
