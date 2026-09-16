import { Rocket } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  RELEASE_NOTES,
  RELEASE_NOTES_STORAGE_KEY,
  RELEASE_VERSION,
  type ReleaseNote,
} from '@/release-notes'
import { cn } from '@/lib/utils'

function NoteBlock({ note }: { note: ReleaseNote }) {
  return (
    <div className="space-y-2" data-release-version={note.version}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-primary/10 px-2 py-0.5 font-mono text-xs font-semibold text-primary">
          {note.version}
        </span>
        <span className="text-sm font-semibold text-foreground">{note.title}</span>
        <span className="ml-auto text-xs text-muted-foreground">{note.date}</span>
      </div>
      <ul className="space-y-1.5">
        {note.items.map((item) => (
          <li key={item} className="flex gap-2 text-sm leading-relaxed text-foreground">
            <span aria-hidden="true" className="mt-[0.55rem] size-1.5 shrink-0 rounded-full bg-primary/70" />
            {item}
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * 纯受控的更新说明弹窗：自动弹出时机由 AppShell 监听登录事件决定，
 * 页脚「更新日志」传入 showAll 回看全部历史。
 */
export function ReleaseNotesDialog({
  open,
  onOpenChange,
  showAll,
  notes,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 页脚回看时展示全部历史；登录触发时只展示未读条目。 */
  showAll: boolean
  /** 登录触发的未读条目；缺省回退到全部。 */
  notes?: ReleaseNote[]
}) {
  const markSeen = () => {
    try {
      localStorage.setItem(RELEASE_NOTES_STORAGE_KEY, RELEASE_VERSION)
    } catch {
      // 写不进也不阻断关闭；下次登录会再看一次，可接受。
    }
  }
  const handleOpenChange = (next: boolean) => {
    if (!next) markSeen()
    onOpenChange(next)
  }
  const markSeenAndClose = () => handleOpenChange(false)

  const visibleNotes = showAll ? RELEASE_NOTES : (notes ?? RELEASE_NOTES)
  const title = showAll ? '更新日志' : '平台更新'

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={cn('sm:max-w-md', showAll && 'sm:max-w-lg')}
        data-release-notes-dialog
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2.5">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Rocket className="size-4" aria-hidden="true" />
            </span>
            {title}
          </DialogTitle>
          <DialogDescription className="sr-only">平台版本更新说明</DialogDescription>
        </DialogHeader>
        {visibleNotes.length > 0 ? (
          <div className={cn('space-y-5', showAll && 'max-h-[60vh] overflow-y-auto px-1')}>
            {visibleNotes.map((note) => (
              <NoteBlock key={note.version} note={note} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">当前已是最新版本。</p>
        )}
        <DialogFooter>
          <Button onClick={markSeenAndClose} data-release-notes-confirm>
            {showAll ? '关闭' : '知道了'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
