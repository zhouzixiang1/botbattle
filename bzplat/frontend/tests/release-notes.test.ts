import assert from 'node:assert/strict'
import test from 'node:test'

import {
  RELEASE_NOTES,
  RELEASE_VERSION,
  unseenReleaseNotes,
  type ReleaseNote,
} from '../src/release-notes.ts'

const notes: ReleaseNote[] = [
  { version: 'v1.3', date: '2026-10-01', title: 'c', items: ['x'] },
  { version: 'v1.2', date: '2026-09-20', title: 'b', items: ['y'] },
  { version: 'v1.1', date: '2026-09-16', title: 'a', items: ['z'] },
]

test('unseenReleaseNotes: 首次使用只看最新一条', () => {
  assert.deepEqual(
    unseenReleaseNotes(null, notes).map((n) => n.version),
    ['v1.3'],
  )
})

test('unseenReleaseNotes: 已看最新则无未读', () => {
  assert.deepEqual(unseenReleaseNotes('v1.3', notes), [])
})

test('unseenReleaseNotes: 记录较早版本返回其后的全部（最新在前）', () => {
  assert.deepEqual(
    unseenReleaseNotes('v1.1', notes).map((n) => n.version),
    ['v1.3', 'v1.2'],
  )
})

test('unseenReleaseNotes: 未知记录版本按首次使用处理', () => {
  assert.deepEqual(
    unseenReleaseNotes('v0.9', notes).map((n) => n.version),
    ['v1.3'],
  )
})

test('unseenReleaseNotes: 空列表安全', () => {
  assert.deepEqual(unseenReleaseNotes(null, []), [])
})

test('内置说明满足展示约束：版本号单调、最新条目与常量一致', () => {
  assert.ok(RELEASE_NOTES.length > 0)
  assert.equal(RELEASE_VERSION, RELEASE_NOTES[0].version)
  assert.ok(/^v\d+\.\d+$/.test(RELEASE_VERSION))
  const minors = RELEASE_NOTES.map((note) => Number(note.version.slice(1).split('.')[1]))
  for (let i = 1; i < minors.length; i += 1) {
    assert.ok(
      minors[i - 1] > minors[i],
      `${RELEASE_NOTES[i - 1].version} 必须新于 ${RELEASE_NOTES[i].version}（最新在最前）`,
    )
    assert.ok(
      RELEASE_NOTES[i - 1].date >= RELEASE_NOTES[i].date,
      '更靠前的条目日期不得早于其后条目',
    )
  }
  for (const note of RELEASE_NOTES) {
    assert.ok(/^v\d+\.\d+$/.test(note.version))
    assert.ok(/^\d{4}-\d{2}-\d{2}$/.test(note.date))
    assert.ok(note.title.length > 0)
    assert.ok(note.items.length > 0)
    assert.ok(note.items.every((item) => item.length > 0))
  }
})
