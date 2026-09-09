import assert from 'node:assert/strict'
import test from 'node:test'

import { fmtMemoryMiB } from '../src/lib/format.ts'

test('fmtMemoryMiB 统一内存单位：分子分母同格式，杜绝 GiB/MiB 混排', () => {
  // 小于 1 GiB 保持 MiB
  assert.equal(fmtMemoryMiB(0), '0 MiB')
  assert.equal(fmtMemoryMiB(512), '512 MiB')
  assert.equal(fmtMemoryMiB(1023), '1023 MiB')
  // 整除 GiB 不带小数（与既有 e2e 断言「1 GiB / 8 GiB」口径一致）
  assert.equal(fmtMemoryMiB(1024), '1 GiB')
  assert.equal(fmtMemoryMiB(16384), '16 GiB')
  // 主机探测出的非整除容量（如 61.4 GiB 新机）用一位小数，而不是裸 MiB
  assert.equal(fmtMemoryMiB(62874), '61.4 GiB')
  assert.equal(fmtMemoryMiB(1536), '1.5 GiB')
  // 空值/非法值 fail closed 到 fallback
  assert.equal(fmtMemoryMiB(null), '—')
  assert.equal(fmtMemoryMiB(undefined), '—')
  assert.equal(fmtMemoryMiB(Number.NaN), '—')
  assert.equal(fmtMemoryMiB(-1), '—')
  assert.equal(fmtMemoryMiB(null, '暂无'), '暂无')
})
