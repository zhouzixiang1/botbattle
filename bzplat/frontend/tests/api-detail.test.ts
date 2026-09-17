import assert from 'node:assert/strict'
import test from 'node:test'

import { humanizeDetail } from '../src/api.ts'

test('humanizeDetail：纯字符串 detail 原样返回', () => {
  assert.equal(humanizeDetail('上传大小超出限制'), '上传大小超出限制')
})

test('humanizeDetail：字典 detail 提取人类可读 message', () => {
  assert.equal(
    humanizeDetail('{"code":"storage_quota_exceeded","message":"云存储空间不足，请先删除部分文件后再上传"}'),
    '云存储空间不足，请先删除部分文件后再上传',
  )
})

test('humanizeDetail：无 message 或空 message 时回退 code，不展示原始 JSON', () => {
  assert.equal(humanizeDetail('{"code":"invalid_size"}'), 'invalid_size')
  assert.equal(humanizeDetail('{"code":"build_failed","message":"  "}'), 'build_failed')
})

test('humanizeDetail：非 JSON 花括号串与非 {code,message} 形状原样返回', () => {
  assert.equal(humanizeDetail('{不是 json'), '{不是 json')
  assert.equal(humanizeDetail('{"detail":"nested"}'), '{"detail":"nested"}')
})
