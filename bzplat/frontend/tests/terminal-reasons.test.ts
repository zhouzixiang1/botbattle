import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveTerminalReason } from '../src/games/reasons.ts'

test('recovery labels distinguish service restart, runtime recovery, and legacy history', () => {
  assert.deepEqual(resolveTerminalReason('orphan_after_service_restart', 'aborted'), {
    label: '服务重启后中止',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_after_runtime_recovery', 'aborted'), {
    label: '执行环境恢复时中止',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_after_restart', 'aborted'), {
    label: '执行恢复时中止（旧记录）',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_service_restart', 'aborted'), {
    label: '服务重启后取消排队',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_runtime_recovery', 'aborted'), {
    label: '执行环境恢复时取消排队',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_restart', 'aborted'), {
    label: '执行恢复时取消排队（旧记录）',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('admin_aborted', 'aborted'), {
    label: '管理员中止',
    tone: 'danger',
  })
})
