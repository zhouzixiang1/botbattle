import {
  Home,
  Swords,
  Trophy,
  Medal,
  Bot,
  BookOpen,
  Database,
  Shield,
  ScrollText,
  History as HistoryIcon,
  type LucideIcon,
} from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  /** 是否需要 admin/organizer 角色 */
  staffOnly?: boolean
  /** 是否 end 匹配（首页用） */
  end?: boolean
}

/** 主导航：桌面端水平展示，移动端进抽屉 */
export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: '首页', icon: Home, end: true },
  { to: '/challenge', label: '挑战', icon: Swords },
  { to: '/leaderboard', label: '排行榜', icon: Trophy },
  { to: '/history', label: '对局', icon: HistoryIcon },
  { to: '/contests', label: '比赛', icon: Medal },
  { to: '/my-bots', label: '我的 Bot', icon: Bot },
  { to: '/wiki', label: 'Wiki', icon: BookOpen },
  { to: '/judges', label: '裁判', icon: ScrollText },
  { to: '/data', label: '数据', icon: Database },
]

/** 管理入口（仅 admin/organizer 可见，单独放） */
export const ADMIN_NAV: NavItem = { to: '/admin', label: '管理', icon: Shield, staffOnly: true }
