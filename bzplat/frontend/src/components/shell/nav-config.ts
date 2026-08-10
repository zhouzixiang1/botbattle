import {
  Home,
  Swords,
  Trophy,
  Medal,
  Bot,
  BookOpen,
  Shield,
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

/** 主导航：xl+ 侧栏纵向展示，较窄视口进入 Sheet 抽屉。 */
export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: '首页', icon: Home, end: true },
  { to: '/challenge', label: '挑战', icon: Swords },
  { to: '/leaderboard', label: '排行榜', icon: Trophy },
  { to: '/history', label: '对局记录', icon: HistoryIcon },
  { to: '/contests', label: '锦标赛', icon: Medal },
  { to: '/my-bots', label: '我的 Bot', icon: Bot },
  { to: '/wiki', label: 'Wiki', icon: BookOpen },
]

/** 管理入口（仅 admin/organizer 可见，单独放） */
export const ADMIN_NAV: NavItem = { to: '/admin', label: '管理', icon: Shield, staffOnly: true }
