import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveHumanWebSocketClosePolicy } from '../src/pages/human-play-websocket-policy.ts'

for (const [reason, message] of [
    ['rate_limit_exceeded', '操作过于频繁，连接已关闭。请稍后再试。'],
    ['session_revoked', '会话已失效，连接已关闭。请重新登录。'],
    ['forbidden', '无权访问该对局，连接已关闭。'],
    ['message_too_large', '动作消息过大，连接已关闭。请刷新页面后重试。'],
    ['invalid_game_id', '对局游戏协议不存在，连接已关闭。'],
    ['connection_limit', '人类对战连接数已达上限，连接已关闭。请稍后刷新页面重试。'],
  ] as const) {
  test(`terminal reject reason ${reason} stops even when the close code is retryable`, () => {
    assert.deepEqual(resolveHumanWebSocketClosePolicy({
      code: 1006,
      reason: '',
      lastRejectReason: reason,
      lastProtocolError: '',
    }), { retry: false, message })
  })
}

for (const [code, reason, message] of [
    [1008, '', '连接因安全策略关闭，已停止自动重连。'],
    [1009, '', '动作消息过大，连接已关闭。请刷新页面后重试。'],
    [1013, '', '人类对战连接数已达上限，连接已关闭。请稍后刷新页面重试。'],
  ] as const) {
  test(`non-retryable close code ${code} stops`, () => {
    assert.deepEqual(resolveHumanWebSocketClosePolicy({
      code,
      reason,
      lastRejectReason: '',
      lastProtocolError: '',
    }), { retry: false, message })
  })
}

test('close reason is used when the matching reject frame was not observed', () => {
    assert.deepEqual(resolveHumanWebSocketClosePolicy({
      code: 1008,
      reason: 'rate_limit_exceeded',
      lastRejectReason: '',
      lastProtocolError: '',
    }), {
      retry: false,
      message: '操作过于频繁，连接已关闭。请稍后再试。',
    })
})

for (const code of [1001, 1006]) {
  test(`bounded transport close code ${code} retries`, () => {
    assert.deepEqual(resolveHumanWebSocketClosePolicy({
      code,
      reason: '',
      lastRejectReason: '',
      lastProtocolError: '',
    }), { retry: true })
  })
}

test('specific protocol error is preserved for an otherwise unknown terminal close', () => {
    assert.deepEqual(resolveHumanWebSocketClosePolicy({
      code: 1000,
      reason: '',
      lastRejectReason: '',
      lastProtocolError: '服务端拒绝了当前操作。',
    }), { retry: false, message: '服务端拒绝了当前操作。' })
})
