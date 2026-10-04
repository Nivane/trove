/**
 * 取数层路径钉子 —— presets.ts 自己发出的 URL 是 /v1/admin/presets…
 *
 * tests/presets.spec.ts 把整个 `src/api/presets` 模块 mock 掉,组件测试永远
 * 看不到真实路径;于是「漏 /v1 前缀」这类错误在 vitest 全绿的同时把页面
 * 打成 `Unexpected token '<'`(dev 服务器把裸路径回退成 index.html)。
 * 这里 mock http 层、断言真实路径,把这类前缀错误钉死在单测里。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'

vi.mock('../src/api/http', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

import { apiGet, apiPost } from '../src/api/http'
import { applyPreset, fetchPresets } from '../src/api/presets'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('api/presets 请求路径', () => {
  it('列表走 /v1/admin/presets', async () => {
    ;(apiGet as any).mockResolvedValue({ presets: [] })
    await fetchPresets()
    expect(apiGet).toHaveBeenCalledWith('/v1/admin/presets')
  })

  it('套用走 /v1/admin/presets/{name}/apply 且带 datasource', async () => {
    ;(apiPost as any).mockResolvedValue({
      preset: 'p',
      datasource: 'demo',
      source: 'builtin',
      counts: {},
      items: [],
    })
    await applyPreset('financial-analysis', 'demo')
    expect(apiPost).toHaveBeenCalledWith(
      '/v1/admin/presets/financial-analysis/apply',
      { datasource: 'demo' },
    )
  })

  it('套用路径里的名字做 URL 编码', async () => {
    ;(apiPost as any).mockResolvedValue({
      preset: 'p',
      datasource: 'demo',
      source: 'builtin',
      counts: {},
      items: [],
    })
    await applyPreset('a b/c', 'demo')
    expect(apiPost).toHaveBeenCalledWith(
      '/v1/admin/presets/a%20b%2Fc/apply',
      { datasource: 'demo' },
    )
  })
})
