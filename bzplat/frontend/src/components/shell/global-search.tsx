import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, Bot, User, Swords } from 'lucide-react'
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { MatchNatureBadge, MatchParticipants } from '@/components/MatchParticipants'
import { apiGet } from '@/api'
import { gameLabel } from '@/games'
import { matchParticipantSearchText, type MatchParticipantSource } from '@/lib/match-participants'

interface SearchUser {
  id: number
  username: string
  display_name?: string
}
interface SearchBot {
  id: number
  name: string
  game_id: string
  owner_name?: string
}
interface SearchMatch extends MatchParticipantSource {
  id: string
  game_id: string
  match_type?: string
  winner_bot_id?: number
}

/**
 * 全局搜索命令面板（Cmd/Ctrl + K 唤起）。
 * 聚合搜 Bot / 用户 / 对局，回车跳转。对标 shadcn-admin。
 *
 * `compact`：用于窄容器（如侧边栏）——只渲染一个铺满宽度的触发按钮，
 * 文字截断、不带 ⌘K 快捷键徽章（省横向空间，Cmd+K 仍可用）。
 * 默认（顶栏）：图标按钮(<md) + 文字按钮(≥md) 两态。
 */
export function GlobalSearch({
  compact = false,
  hotkey = false,
}: {
  compact?: boolean
  /** AppShell 只给一个常驻实例注册快捷键，避免多个响应式入口同时打开重叠弹窗。 */
  hotkey?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [users, setUsers] = useState<SearchUser[]>([])
  const [bots, setBots] = useState<SearchBot[]>([])
  const [matches, setMatches] = useState<SearchMatch[]>([])
  const nav = useNavigate()

  // Cmd/Ctrl + K 唤起
  useEffect(() => {
    if (!hotkey) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [hotkey])

  // 防抖搜索
  useEffect(() => {
    const query = q.trim()
    if (query.length < 1) {
      setUsers([])
      setBots([])
      setMatches([])
      return
    }
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        const encoded = encodeURIComponent(query)
        const [u, b, m] = await Promise.all([
          apiGet<{ users?: SearchUser[] }>(`/api/search?q=${encoded}&type=users&limit=6`),
          apiGet<{ bots?: SearchBot[] }>(`/api/search?q=${encoded}&type=bots&limit=6`),
          apiGet<{ matches?: SearchMatch[] }>(`/api/search?q=${encoded}&type=matches&limit=6`),
        ])
        if (cancelled) return
        setUsers(u.users ?? [])
        setBots(b.bots ?? [])
        setMatches(m.matches ?? [])
      } catch {
        if (!cancelled) {
          setUsers([])
          setBots([])
          setMatches([])
        }
      }
    }, 250)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [q])

  const go = (path: string) => {
    setOpen(false)
    setQ('')
    nav(path)
  }

  return (
    <>
      {compact ? (
        // 侧边栏等窄容器：铺满宽度、单行、文字截断、无快捷键徽章
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex h-[var(--control-height)] w-full min-w-0 touch-manipulation items-center gap-2 rounded-lg border border-input bg-background px-3 text-sm text-muted-foreground transition-colors duration-150 hover:bg-accent focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
          aria-label="搜索"
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-keyshortcuts="Control+K Meta+K"
        >
          <Search aria-hidden="true" className="size-4 shrink-0" />
          <span className="truncate">搜索 Bot、用户、对局…</span>
        </button>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="inline-flex size-[var(--control-height)] shrink-0 touch-manipulation items-center justify-center rounded-lg text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-accent-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none md:hidden"
            aria-label="搜索"
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-keyshortcuts="Control+K Meta+K"
          >
            <Search aria-hidden="true" className="size-[1.15rem]" />
          </button>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="hidden h-[var(--control-height)] max-w-full min-w-0 touch-manipulation items-center gap-2 rounded-lg border border-input bg-background px-3 text-sm text-muted-foreground transition-colors duration-150 hover:bg-accent focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none md:inline-flex"
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-keyshortcuts="Control+K Meta+K"
          >
            <Search aria-hidden="true" className="size-4 shrink-0" />
            <span className="truncate">搜索 Bot、用户、对局…</span>
            <kbd aria-hidden="true" className="ml-2 shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium">
              ⌘K
            </kbd>
          </button>
        </>
      )}
      <CommandDialog open={open} onOpenChange={setOpen}>
        <CommandInput placeholder="搜索 Bot、用户、对局…" value={q} onValueChange={setQ} />
        <CommandList>
          <CommandEmpty>{q.trim() ? '无匹配结果' : '输入关键词搜索'}</CommandEmpty>
          {users.length > 0 && (
            <CommandGroup heading="用户">
              {users.slice(0, 6).map((u) => (
                <CommandItem key={`u${u.id}`} value={`user ${u.username} ${u.display_name ?? ''}`} onSelect={() => go(`/user/${encodeURIComponent(u.username)}`)}>
                  <User aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{u.display_name || u.username}</span>
                  {u.display_name && <span className="truncate text-xs text-muted-foreground">@{u.username}</span>}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {bots.length > 0 && (
            <CommandGroup heading="Bot">
              {bots.slice(0, 6).map((b) => (
                <CommandItem key={`b${b.id}`} value={`bot ${b.name} ${b.owner_name ?? ''}`} onSelect={() => go(`/bot/${b.id}`)}>
                  <Bot aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{b.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">{gameLabel(b.game_id)}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {matches.length > 0 && (
            <CommandGroup heading="对局">
              {matches.slice(0, 6).map((m) => (
                <CommandItem
                  key={`m${m.id}`}
                  value={`match ${m.id} ${matchParticipantSearchText(m)}`}
                  onSelect={() => go(`/match/${m.id}`)}
                  className="items-start"
                >
                  <Swords aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <MatchParticipants source={m} links={false} className="gap-1" />
                    <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                      <span>{gameLabel(m.game_id)}</span>
                      <MatchNatureBadge matchType={m.match_type} source={m} />
                    </div>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </CommandDialog>
    </>
  )
}
