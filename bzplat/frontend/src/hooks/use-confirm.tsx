/**
 * 异步确认对话框 hook（替代原生阻塞式 confirm()）。
 *
 * 基于 shadcn Dialog（Radix）+ Promise：调用 `const ok = await confirm({...})`，
 * 返回 true/false（确认/取消）。业务流程与原生 confirm 一致，仅多一个 await，
 * 不阻塞 JS 主线程、跨设备样式统一、支持 danger 红按钮。
 *
 * 用法：
 *   const [confirm, dialog] = useConfirm()
 *   // ... 事件处理：
 *   if (!await confirm({ title: '删除', desc: '确认？', danger: true })) return
 *   await apiJson(...)
 *   // 组件 JSX 末尾渲染一次：
 *   {dialog}
 *
 * 注意：每个使用 confirm 的组件需各自调用一次 useConfirm() 并渲染返回的 dialog。
 */
import { useCallback, useRef, useState, type ReactNode } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface ConfirmOptions {
  /** 标题（默认"确认操作"） */
  title?: ReactNode
  /** 正文描述 */
  desc?: ReactNode
  /** 确认按钮文字（默认"确认"） */
  confirmText?: string
  /** 取消按钮文字（默认"取消"） */
  cancelText?: string
  /** 危险操作（删除类）——确认按钮用 destructive 红色变体 */
  danger?: boolean
  /** 两个操作按钮的附加样式；页面可按触控场景扩大命中区。 */
  buttonClassName?: string
}

type Resolver = (ok: boolean) => void

export function useConfirm(): [
  (opts: ConfirmOptions) => Promise<boolean>,
  ReactNode,
  () => void,
] {
  const [open, setOpen] = useState(false)
  const [opts, setOpts] = useState<ConfirmOptions>({})
  const resolver = useRef<Resolver | null>(null)

  const confirm = useCallback((o: ConfirmOptions) => {
    setOpts(o)
    setOpen(true)
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve
    })
  }, [])

  const settle = useCallback((ok: boolean) => {
    setOpen(false)
    resolver.current?.(ok)
    resolver.current = null
  }, [])

  // 遮罩点击 / Esc / 关闭按钮 → 视为取消
  const onOpenChange = useCallback(
    (next: boolean) => {
      if (!next) settle(false)
      else setOpen(true)
    },
    [settle],
  )
  const cancel = useCallback(() => settle(false), [settle])

  const {
    title = '确认操作',
    desc,
    confirmText = '确认',
    cancelText = '取消',
    danger = false,
    buttonClassName,
  } = opts

  const dialog = (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton={false} className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {desc != null && <DialogDescription>{desc}</DialogDescription>}
        </DialogHeader>
        <DialogFooter>
          <Button className={cn(buttonClassName)} variant="outline" onClick={() => settle(false)}>
            {cancelText}
          </Button>
          <Button
            className={cn(buttonClassName)}
            variant={danger ? 'destructive' : 'default'}
            onClick={() => settle(true)}
          >
            {confirmText}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )

  return [confirm, dialog, cancel]
}
