import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

function mockFetch(ok: boolean, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 401,
    statusText: ok ? 'OK' : 'Unauthorized',
    text: vi.fn().mockResolvedValue('err'),
    json: vi.fn().mockResolvedValue(body),
  })
}

describe('auth bootstrap — restores session from token on load', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.resetModules()
    vi.unstubAllGlobals()
  })

  it('returns true and populates user when token restores via /me', async () => {
    localStorage.setItem('trove_auth_token', 'tok')
    globalThis.fetch = mockFetch(true, { user: { id: 1, username: 'bob', role: 'admin' } })

    const { useAuthStore } = await import('../src/stores/auth')
    const auth = useAuthStore()
    const ok = await auth.bootstrap()
    expect(ok).toBe(true)
    expect(auth.isAuthed).toBe(true)
  })

  it('returns false and clears token when /me 401s', async () => {
    localStorage.setItem('trove_auth_token', 'expired')
    globalThis.fetch = mockFetch(false, {})

    const { useAuthStore } = await import('../src/stores/auth')
    const auth = useAuthStore()
    const ok = await auth.bootstrap()
    expect(ok).toBe(false)
    expect(auth.isAuthed).toBe(false)
    expect(localStorage.getItem('trove_auth_token')).toBeNull()
  })

  it('bootstrap without a token is false without network', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('should not be called'))
    const { useAuthStore } = await import('../src/stores/auth')
    const ok = await useAuthStore().bootstrap()
    expect(ok).toBe(false)
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })
})

describe('apiDelete — tolerates 204 No Content body', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.resetModules()
    vi.unstubAllGlobals()
  })

  it('returns without parsing json on 204', async () => {
    globalThis.fetch = mockFetch(true, undefined)
    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      status: 204,
      statusText: 'No Content',
      text: vi.fn().mockResolvedValue(''),
      json: vi.fn().mockRejectedValue(new Error('empty body')),
    })
    const { apiDelete } = await import('../src/api/http')
    const res = await apiDelete('/v1/admin/users/1')
    expect(res).toBeUndefined()
  })
})
