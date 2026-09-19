import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

// 共享同一 api.ts 实例（无 query 后缀），保证 instanceof ApiError 跨模块一致。
import * as api from '../src/api.ts'
import {
  retryLaterDetailCode,
  submitCooldownMs,
  submitCooldownView,
} from '../src/hooks/use-submit-cooldown.ts'

class MemoryStorage {
  readonly values = new Map<string, string>()
  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
  removeItem(key: string): void {
    this.values.delete(key)
  }
}

function installBrowserStubs() {
  Object.defineProperty(globalThis, 'localStorage', {
    value: new MemoryStorage(),
    configurable: true,
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: new MemoryStorage(),
    configurable: true,
  })
  Object.defineProperty(globalThis, 'location', {
    value: { hash: '#/my-bots', protocol: 'https:', host: 'example.test' },
    configurable: true,
  })
}

function mockFetch(status: number, body: string, headers: Record<string, string> = {}) {
  Object.defineProperty(globalThis, 'fetch', {
    value: async () => new Response(body, {
      status,
      headers: { 'content-type': 'application/json', ...headers },
    }),
    configurable: true,
  })
}

async function captureError(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(() => null, (err: unknown) => err)
}

test('retryAfterMs：整数/小数 Retry-After 换算毫秒，缺失或非法回退 fallback', () => {
  const withHeader = (value: string | undefined) => new api.ApiError(
    '/api/bots',
    429,
    '请求过于频繁',
    '请求过于频繁',
    value === undefined ? {} : { headers: { 'retry-after': value } },
  )
  assert.equal(api.retryAfterMs(withHeader('3')), 3_000)
  assert.equal(api.retryAfterMs(withHeader('2.5')), 2_500)
  assert.equal(api.retryAfterMs(withHeader(' 7 ')), 7_000)
  assert.equal(api.retryAfterMs(withHeader('0')), 5_000)
  assert.equal(api.retryAfterMs(withHeader('-2')), 5_000)
  assert.equal(api.retryAfterMs(withHeader('soon')), 5_000)
  assert.equal(api.retryAfterMs(withHeader(undefined)), 5_000)
  assert.equal(api.retryAfterMs(withHeader('20'), 8_000), 20_000)
  assert.equal(api.retryAfterMs(new api.ApiError('/api/bots', 500, 'x')), 5_000)
  assert.equal(api.retryAfterMs(new Error('网络请求失败')), 5_000)
  assert.equal(api.retryAfterMs(null, 1_234), 1_234)
})

test('fetch 429 限流错误携带顶层 code 与响应头，可判定限流并进入冷却', async () => {
  installBrowserStubs()
  mockFetch(429, JSON.stringify({
    detail: '请求过于频繁,请稍后再试',
    code: 'rate_limit_exceeded',
  }), { 'retry-after': '3' })

  const err = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(err instanceof api.ApiError)
  assert.equal(err.status, 429)
  // 429 的机器码在响应体顶层，不在 detail 字典内。
  assert.equal(err.serverCode, 'rate_limit_exceeded')
  assert.equal(err.headers['retry-after'], '3')
  assert.equal(err.detail, '请求过于频繁,请稍后再试')
  assert.equal(api.isRateLimitError(err), true)
  assert.equal(api.retryAfterMs(err), 3_000)
  assert.equal(submitCooldownMs(err), 3_000)
})

test('fetch 429 无顶层 code 时仍按状态码判定限流；无 Retry-After 用默认冷却', async () => {
  installBrowserStubs()
  mockFetch(429, JSON.stringify({ detail: '稍后再试' }))

  const err = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(err instanceof api.ApiError)
  assert.equal(err.serverCode, null)
  assert.equal(api.isRateLimitError(err), true)
  assert.equal(submitCooldownMs(err), 5_000)
})

test('fetch 503 繁忙错误按 detail 字典 code 判定冷却并读 Retry-After', async () => {
  installBrowserStubs()
  mockFetch(503, JSON.stringify({
    detail: { code: 'upload_busy', message: 'Bot 上传预检繁忙，请稍后重试' },
  }), { 'retry-after': '15' })

  const err = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(err instanceof api.ApiError)
  assert.equal(err.status, 503)
  assert.equal(err.serverCode, null)
  assert.equal(err.detail, 'Bot 上传预检繁忙，请稍后重试')
  assert.equal(err.rawDetail, '{"code":"upload_busy","message":"Bot 上传预检繁忙，请稍后重试"}')
  assert.equal(retryLaterDetailCode(err), 'upload_busy')
  assert.equal(api.isRateLimitError(err), false)
  assert.equal(submitCooldownMs(err), 15_000)
})

test('fetch 503 deployment_maintenance 无 Retry-After 时用 fallback 冷却', async () => {
  installBrowserStubs()
  mockFetch(503, JSON.stringify({
    detail: { code: 'deployment_maintenance', message: '平台正在部署维护，暂不接收 Bot 上传' },
  }))

  const err = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(err instanceof api.ApiError)
  assert.equal(submitCooldownMs(err, 7_000), 7_000)
})

test('fetch 400 与未知 503 不进入冷却', async () => {
  installBrowserStubs()
  mockFetch(400, JSON.stringify({ detail: { code: 'invalid_name', message: '名称已被占用' } }))
  const badRequest = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(badRequest instanceof api.ApiError)
  assert.equal(badRequest.status, 400)
  assert.equal(badRequest.detail, '名称已被占用')
  assert.equal(api.isRateLimitError(badRequest), false)
  assert.equal(submitCooldownMs(badRequest), null)

  mockFetch(503, JSON.stringify({ detail: '服务暂不可用' }))
  const genericUnavailable = await captureError(api.apiPost('/api/bots', 'POST', { name: 'a_demo' }))
  assert.ok(genericUnavailable instanceof api.ApiError)
  assert.equal(submitCooldownMs(genericUnavailable), null)

  assert.equal(submitCooldownMs(new TypeError('网络请求失败')), null)
})

test('submitCooldownMs 把冷却时长夹在 [1s, 10min]', () => {
  const tooShort = new api.ApiError('/api/bots', 429, 'x', 'x', { headers: { 'retry-after': '0.2' } })
  assert.equal(submitCooldownMs(tooShort), 1_000)
  const noHeader = new api.ApiError('/api/bots', 429, 'x')
  assert.equal(submitCooldownMs(noHeader), 5_000)
  const tooLong = new api.ApiError('/api/bots', 429, 'x', 'x', { headers: { 'retry-after': '99999' } })
  assert.equal(submitCooldownMs(tooLong), 10 * 60 * 1_000)
})

test('submitCooldownView：剩余秒数向上取整，到期自动复位（注入 now）', () => {
  const until = 10_000
  assert.deepEqual(submitCooldownView(until, 7_000), { active: true, remainingSeconds: 3 })
  assert.deepEqual(submitCooldownView(until, 7_500), { active: true, remainingSeconds: 3 })
  assert.deepEqual(submitCooldownView(until, 9_999), { active: true, remainingSeconds: 1 })
  // 到期瞬间与之后：冷却解除、剩余归零。
  assert.deepEqual(submitCooldownView(until, 10_000), { active: false, remainingSeconds: 0 })
  assert.deepEqual(submitCooldownView(until, 60_000), { active: false, remainingSeconds: 0 })
  assert.deepEqual(submitCooldownView(0, 5_000), { active: false, remainingSeconds: 0 })
})

test('XHR 上传 429 同样携带顶层 code 与响应头快照', async () => {
  installBrowserStubs()

  class FakeEventTarget {
    listeners = new Map<string, Array<() => void>>()
    addEventListener(type: string, listener: () => void): void {
      this.listeners.set(type, [...(this.listeners.get(type) || []), listener])
    }
    emit(type: string): void {
      for (const listener of this.listeners.get(type) || []) listener()
    }
  }

  class FakeXMLHttpRequest extends FakeEventTarget {
    static latest: FakeXMLHttpRequest | null = null
    upload = new FakeEventTarget()
    status = 429
    statusText = 'Too Many Requests'
    responseText = JSON.stringify({
      detail: '请求过于频繁,请稍后再试',
      code: 'rate_limit_exceeded',
    })
    withCredentials = false
    sent = false
    constructor() {
      super()
      FakeXMLHttpRequest.latest = this
    }
    open(): void {}
    send(): void { this.sent = true }
    abort(): void { this.emit('abort') }
    getResponseHeader(name: string): string | null {
      return name.toLowerCase() === 'content-type' ? 'application/json' : null
    }
    getAllResponseHeaders(): string {
      return 'content-type: application/json\r\nretry-after: 4\r\nx-ratelimit-remaining: 0\r\n'
    }
  }
  Object.defineProperty(globalThis, 'XMLHttpRequest', {
    value: FakeXMLHttpRequest,
    configurable: true,
  })

  const upload = api.apiFormWithProgress('/api/bots', { file: new Blob(['elf']) })
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(FakeXMLHttpRequest.latest?.sent, true)
  FakeXMLHttpRequest.latest?.emit('load')

  const err = await captureError(upload)
  assert.ok(err instanceof api.ApiError)
  assert.equal(err.status, 429)
  assert.equal(err.serverCode, 'rate_limit_exceeded')
  assert.equal(err.headers['retry-after'], '4')
  assert.equal(err.headers['x-ratelimit-remaining'], '0')
  assert.equal(api.retryAfterMs(err), 4_000)
  assert.equal(submitCooldownMs(err), 4_000)
})

test('上传提交冷却的 UI 接线：两个表单都接入冷却门并显示倒计时', async () => {
  const hookSource = await readFile(
    new URL('../src/hooks/use-submit-cooldown.ts', import.meta.url),
    'utf8',
  )
  // 冷却期内每秒对齐时钟，到期翻转 active 后清理定时器（自动解除）。
  assert.match(hookSource, /window\.setInterval\(\(\) => setNow\(nowFn\(\)\), 250\)/)
  assert.match(hookSource, /return \(\) => window\.clearInterval\(timer\)/)
  assert.match(hookSource, /if \(!active\) return/)

  for (const path of ['../src/pages/MyBots.tsx', '../src/components/BotVersionManager.tsx']) {
    const source = await readFile(new URL(path, import.meta.url), 'utf8')
    assert.match(source, /const submitCooldown = useSubmitCooldown\(\)/, path)
    assert.match(source, /submitCooldown\.beginCooldown\(err\)/, path)
    assert.match(source, /disabled=\{busy \|\| submitCooldown\.active\}/, path)
    assert.match(source, /请求过于频繁，请在 \{submitCooldown\.remainingSeconds\} 秒后重试/, path)
    // 冷却期除了按钮禁用，submit 入口也必须拦截（Enter 键提交）。
    assert.match(source, /if \(submitCooldown\.active\) return/, path)
  }
})
