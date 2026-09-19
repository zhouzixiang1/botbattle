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
    version: 'v1.7',
    date: '2026-09-19',
    title: '修复：ML 运行库 Bot 对局启动失败（内存不足）',
    items: [
      '选择「ML 库」运行库的 Bot 此前在对局启动阶段会被内存上限（512 MiB）强制终止：加载 torch 与云盘模型后超出上限，对局开局即判负。现在这类 Bot 自动使用 2 GiB 内存档，可正常完成模型加载与推理。',
      '上传预检环境与正式对局完全对齐：预检同样会挂载你的云存储文件（/mnt/data 与 /app/data）、使用同样的内存档。依赖云盘模型的 Bot 不再需要写「无模型回退」来通过预检——预检阶段就会真实加载一次。',
    ],
  },
  {
    version: 'v1.6',
    date: '2026-09-18',
    title: '上传与检索体验升级：单文件直传、在线编辑器、ML 运行库与搜索改版',
    items: [
      '上传源码 Bot 不再必须打 zip：选择对应语言后，直接上传单个 .py / .c / .cpp / .go 文件即可，平台自动按标准入口处理。',
      '上传表单新增「在线编辑器」：不用在本地建文件，粘贴代码就能上传（单次粘贴不超过 2 MB，更大的代码仍用文件或 zip 上传）。',
      'Python 源码上传时可选择「ML 库」运行库：numpy、scipy、onnxruntime 与 torch（CPU 版）由平台直接提供，不再需要打进程序包。',
      '推荐配合云存储使用：训练好的权重转存为 npz/onnx 上传，对局中从 data/ 目录读取；沙箱内存 512 MiB，模型不宜过大。',
      '上传预检失败时，错误详情现在会附上程序自身输出的末尾，方便定位启动失败的原因。',
      '搜索页一次展示用户、Bot、对局三类结果，不用来回切换；顶部标签可直接跳到对应分区。',
      '云存储文件清单改为多列卡片，文件多时更省滚动。',
      '发起挑战页面在窄屏（手机）改为「对局 → 座位 → 确认」三步表单，不用再长距离滚动；桌面版保持原样。',
    ],
  },
  {
    version: 'v1.5',
    date: '2026-09-18',
    title: '修复：Python 源码 Bot 与云盘对局读取',
    items: [
      '修复 Python 源码 Bot 一直无法通过上传预检的问题；此前所有 Python 源码 zip 都会失败，现在可以正常创建与参赛。',
      '修复云存储文件在对局内不可读的问题；现在 Bot 可以按此前说明从 /mnt/data 与 /app/data 读取权重与数据文件。',
      '沙箱临时目录 /tmp 从 64 MB 扩大到 256 MB（仍计入 512 MB 内存上限），运行时解压依赖的程序更不容易启动失败。',
    ],
  },
  {
    version: 'v1.4',
    date: '2026-09-17',
    title: '云存储升级：独立入口与模型权重',
    items: [
      '云存储有了独立页面：侧边栏「云存储」直达，不再藏在设置里；页面支持拖拽上传。',
      '模型权重文件（.bin、.pt/.pth、.onnx、.safetensors 等）与其他数据文件一样直接上传，无需打包压缩。',
      '更正说明：云存储文件在对局开始时就会只读挂载到 /mnt/data 与 /app/data，程序内用 data/文件名 即可读取（此前页面提示有误）。',
    ],
  },
  {
    version: 'v1.3',
    date: '2026-09-17',
    title: '源码 Bot 上线：C / C++ / Go / Python',
    items: [
      '上传源码 zip 即可建 Bot，平台自动编译（Python 直接运行）。同名文件 main.cpp / main.go / __main__.py 会作为默认入口。',
      '编译时自动定义 _BOTZONE_ONLINE / BOTARENA_ONLINE 宏，方便区分本地与线上。',
      '云存储文件会在对局开始时挂载到 Bot 的 /mnt/data 目录（只读）；对 Python 暂只支持标准库。',
    ],
  },
  {
    version: 'v1.2',
    date: '2026-09-16',
    title: '用户云存储上线',
    items: [
      '「设置 → 云存储」可以上传和管理你自己的数据文件（共 256 MiB）。',
      '文件同名上传会覆盖旧版；本版本先开放管理入口，Bot 读取能力随下个版本开放。',
    ],
  },
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
