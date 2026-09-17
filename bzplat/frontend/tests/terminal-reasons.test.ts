import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveTerminalReason, technicalReasonLabel } from '../src/games/reasons.ts'

test('recovery labels distinguish service restart, runtime recovery, and legacy history', () => {
  assert.deepEqual(resolveTerminalReason('orphan_after_service_restart', 'aborted'), {
    label: '服务重启后中止',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_after_runtime_recovery', 'aborted'), {
    label: '平台维护后中止',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_after_restart', 'aborted'), {
    label: '对局已中止（历史记录）',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_service_restart', 'aborted'), {
    label: '服务重启后取消排队',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_runtime_recovery', 'aborted'), {
    label: '平台维护后取消排队',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('orphan_pending_after_restart', 'aborted'), {
    label: '排队已取消（历史记录）',
    tone: 'danger',
  })
  assert.deepEqual(resolveTerminalReason('admin_aborted', 'aborted'), {
    label: '管理员中止',
    tone: 'danger',
  })
})

test('technical incident labels never leak unknown reason codes to users', () => {
  assert.equal(technicalReasonLabel('timeout'), 'Bot 决策超时')
  assert.equal(technicalReasonLabel('protocol_error'), 'Bot 响应协议错误')
  assert.equal(technicalReasonLabel('missing_response'), '技术原因')
  assert.equal(technicalReasonLabel(''), '技术原因')
  assert.equal(technicalReasonLabel(undefined), '技术原因')
})
