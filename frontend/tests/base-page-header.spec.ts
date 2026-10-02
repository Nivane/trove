import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import PageHeader from '../src/components/base/PageHeader.vue'
import type { VueWrapper } from '@vue/test-utils'

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/admin', component: { render: () => null } },
      { path: '/admin/users', component: { render: () => null } },
    ],
  })
}

let router: Router
let wrapper: VueWrapper | null = null

beforeEach(async () => {
  document.title = 'original title'
  router = makeRouter()
  await router.push('/admin/users')
  await router.isReady()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.title = ''
})

function mountHeader(props: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  wrapper = mount(PageHeader, {
    props: { title: 'Users', ...props },
    slots,
    global: { plugins: [router] },
  })
  return wrapper
}

describe('PageHeader', () => {
  it('renders title, description and the actions slot', () => {
    const w = mountHeader(
      { title: 'Users', description: 'Accounts and roles' },
      { actions: '<button class="primary">New user</button>' },
    )
    expect(w.find('h1').text()).toBe('Users')
    expect(w.find('.ph-desc').text()).toBe('Accounts and roles')
    expect(w.find('.ph-actions button').text()).toBe('New user')
  })

  it('renders breadcrumbs with the last crumb marked as the current page', () => {
    const w = mountHeader({
      breadcrumbs: [{ label: 'Admin', to: '/admin' }, { label: 'Users' }],
    })
    const crumbs = w.findAll('.ph-crumb')
    expect(crumbs.map((c) => c.text())).toEqual(['Admin', 'Users'])
    expect(crumbs[0].attributes('href')).toBe('/admin')
    expect(crumbs[0].attributes('aria-current')).toBeUndefined()
    expect(crumbs[1].attributes('aria-current')).toBe('page')
    expect(w.findAll('.ph-sep')).toHaveLength(1)
  })

  it('navigates when a linked crumb is clicked', async () => {
    const w = mountHeader({ breadcrumbs: [{ label: 'Admin', to: '/admin' }] })
    await w.find('.ph-crumb.is-link').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/admin')
  })

  it('syncs document.title while mounted and restores it on unmount', () => {
    const w = mountHeader({ title: 'Users' })
    expect(document.title).toBe('Users')
    w.unmount()
    wrapper = null
    expect(document.title).toBe('original title')
  })

  it('keeps document.title when sync is disabled', () => {
    mountHeader({ title: 'Users', syncDocumentTitle: false })
    expect(document.title).toBe('original title')
  })
})
