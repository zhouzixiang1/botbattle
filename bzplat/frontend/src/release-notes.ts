// 平台版本化更新说明。
// 版本号人工维护（v1.1 起单调递增），每个带用户可见变化的发布在这里补一条；
// 登录用户首次进站时弹窗展示未读条目，页脚「更新日志」可随时回看全部。

export interface ReleaseNote {
  version: string
  date: string
  title: string
  items: string[]
}

// 最新在最前。
export const RELEASE_NOTES: ReleaseNote[] = [
  {
    version: 'v1.1',
    date: '2026-09-16',
    title: 'Bot 体积上限提升',
    items: [
      'Bot 单文件上限从 100 MiB 提高到 256 MiB，可以打包更大的模型文件。',
      '上传改为流式传输，大文件上传更稳定。',
    ],
  },
]

export const RELEASE_VERSION = RELEASE_NOTES[0]?.version ?? ''
export const RELEASE_NOTES_STORAGE_KEY = 'bz-release-notes-seen'

// 登录成功后由 auth 层广播；AppShell 收到后检查未读并弹窗。
// 只在真实登录动作上触发，持久会话的静默恢复与测试中的 mock 会话不会打扰。
export const RELEASE_NOTES_CHECK_EVENT = 'bz-release-notes-check'

export function requestReleaseNotesCheck(): void {
  window.dispatchEvent(new Event(RELEASE_NOTES_CHECK_EVENT))
}

/**
 * 返回用户尚未看过的更新条目。
 * - 没有记录（首次使用）：只看最新一条，不刷历史。
 * - 记录的是最新版本：返回空。
 * - 记录的是较早版本：返回其后的全部条目（最新在前）。
 * - 记录的版本不在列表中（如清过历史）：按首次使用处理。
 */
export function unseenReleaseNotes(
  seenVersion: string | null,
  notes: ReleaseNote[],
): ReleaseNote[] {
  if (notes.length === 0) return []
  if (!seenVersion) return [notes[0]]
  const seenIndex = notes.findIndex((note) => note.version === seenVersion)
  if (seenIndex === -1) return [notes[0]]
  return notes.slice(0, seenIndex)
}
