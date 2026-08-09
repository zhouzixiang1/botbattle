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
import { apiGet } from '@/api'
import { gameLabel } from '@/games'

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
interface SearchMatch {
  id: string
  game_id: string
  winner_bot_id?: number
  bot_a_name?: string
  bot_b_name?: string
  bot_a_display?: string
  bot_b_display?: string
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
          className="inline-flex h-9 w-full items-center gap-2 rounded-lg border border-input bg-background px-3 text-sm text-muted-foreground transition-colors hover:bg-accent"
          aria-label="搜索"
        >
          <Search className="size-4 shrink-0" />
          <span className="truncate">搜索 Bot、用户、对局…</span>
        </button>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground md:hidden"
            aria-label="搜索"
          >
            <Search className="size-[1.15rem]" />
          </button>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="hidden h-9 items-center gap-2 rounded-lg border border-input bg-background px-3 text-sm text-muted-foreground transition-colors hover:bg-accent md:inline-flex"
          >
            <Search className="size-4" />
            <span>搜索 Bot、用户、对局…</span>
            <kbd className="ml-4 rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-medium">
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
                  <User className="size-4 text-muted-foreground" />
                  <span>{u.display_name || u.username}</span>
                  {u.display_name && <span className="text-xs text-muted-foreground">@{u.username}</span>}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {bots.length > 0 && (
            <CommandGroup heading="Bot">
              {bots.slice(0, 6).map((b) => (
                <CommandItem key={`b${b.id}`} value={`bot ${b.name} ${b.owner_name ?? ''}`} onSelect={() => go(`/bot/${b.id}`)}>
                  <Bot className="size-4 text-muted-foreground" />
                  <span>{b.name}</span>
                  <span className="text-xs text-muted-foreground">{gameLabel(b.game_id)}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {matches.length > 0 && (
            <CommandGroup heading="对局">
              {matches.slice(0, 6).map((m) => (
                <CommandItem
                  key={`m${m.id}`}
                  value={`match ${m.id} ${m.bot_a_name ?? ''} ${m.bot_b_name ?? ''} ${m.bot_a_display ?? ''} ${m.bot_b_display ?? ''}`}
                  onSelect={() => go(`/match/${m.id}`)}
                >
                  <Swords className="size-4 text-muted-foreground" />
                  <span className="font-mono text-xs">{m.id.slice(0, 8)}</span>
                  <span className="text-xs text-muted-foreground">{gameLabel(m.game_id)}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </CommandDialog>
    </>
  )
}
