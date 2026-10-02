import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { useListQuery } from '../src/composables/useListQuery'
import type { VueWrapper } from '@vue/test-utils'

/** Host component: binds the composable the way a list page would. */
const Host = defineComponent({
  setup() {
    const { values, isActive, reset } = useListQuery({ q: '', role: '' })
    return () =>
      h('div', [
        h('input', {
          class: 'q',
          value: values.q,
          onInput: (e: Event) => {
            values.q = (e.target as HTMLInputElement).value
          },
        }),
        h('span', { class: 'role' }, values.role),
        h('span', { class: 'active' }, isActive.value ? 'active' : 'idle'),
        h('button', { class: 'reset', onClick: reset }, 'reset'),
      ])
  },
})

let router: Router
let wrapper: VueWrapper | null = null

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/other', component: { render: () => null } },
      { path: '/list', component: { render: () => null } },
    ],
  })
}

beforeEach(() => {
  router = makeRouter()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

async function mountAt(url: string) {
  await router.push(url)
  await router.isReady()
  wrapper = mount(Host, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('useListQuery', () => {
  it('reads the initial state from the URL query', async () => {
    const w = await mountAt('/list?q=abc&role=admin')
    expect((w.find('.q').element as HTMLInputElement).value).toBe('abc')
    expect(w.find('.role').text()).toBe('admin')
    expect(w.find('.active').text()).toBe('active')
  })

  it('writes changes back to the URL with the path untouched', async () => {
    const w = await mountAt('/list')
    await w.find('.q').setValue('lin')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/list')
    expect(router.currentRoute.value.query.q).toBe('lin')
  })

  it('keeps default and empty values out of the URL', async () => {
    const w = await mountAt('/list?q=abc')
    await w.find('.q').setValue('')
    await flushPromises()
    expect('q' in router.currentRoute.value.query).toBe(false)

    await w.find('.q').setValue('abc')
    await flushPromises()
    expect(router.currentRoute.value.query.q).toBe('abc')

    await w.find('.reset').trigger('click')
    await flushPromises()
    expect(Object.keys(router.currentRoute.value.query)).toEqual([])
    expect(w.find('.active').text()).toBe('idle')
  })

  it('leaves unrelated query params alone', async () => {
    const w = await mountAt('/list?page=2')
    await w.find('.q').setValue('xyz')
    await flushPromises()
    expect(router.currentRoute.value.query.page).toBe('2')
    expect(router.currentRoute.value.query.q).toBe('xyz')
  })

  it('follows external navigation (back/forward, pasted links)', async () => {
    const w = await mountAt('/list?q=a')
    await router.push('/list?role=analyst')
    await flushPromises()
    expect(w.find('.role').text()).toBe('analyst')
    expect((w.find('.q').element as HTMLInputElement).value).toBe('')

    await router.back()
    await flushPromises()
    expect((w.find('.q').element as HTMLInputElement).value).toBe('a')
  })

  it('replaces the history entry instead of pushing a new one', async () => {
    await router.push('/other')
    const w = await mountAt('/list?q=a')
    await w.find('.q').setValue('b')
    await flushPromises()

    router.back()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/other')
  })

  it('ignores writes that do not change the query', async () => {
    const w = await mountAt('/list?q=abc')
    const before = router.currentRoute.value.fullPath
    await w.find('.q').setValue('abc')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(before)
  })
})
