/** 图形验证码：拉取 /api/auth/captcha，点击刷新。 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

export interface CaptchaValue {
  captcha_id: string
  captcha_answer: string
}

interface Props {
  onChange: (v: CaptchaValue) => void
  className?: string
}

export default function CaptchaField({ onChange, className = '' }: Props) {
  const [id, setId] = useState('')
  const [img, setImg] = useState('')
  const [answer, setAnswer] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    setErr('')
    setAnswer('')
    try {
      const r = await fetch('/api/auth/captcha', { credentials: 'include' })
      const d = (await r.json()) as {
        captcha_id?: string
        image_base64?: string
        detail?: string
      }
      if (!r.ok) throw new Error(d?.detail || '获取验证码失败')
      setId(d.captcha_id || '')
      setImg(d.image_base64 || '')
      onChange({ captcha_id: d.captcha_id || '', captcha_answer: '' })
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : '获取验证码失败')
    } finally {
      setLoading(false)
    }
  }, [onChange])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <div className={`flex flex-col gap-1.5 text-sm text-foreground ${className}`}>
      <span className="font-medium">验证码</span>
      <div className="flex items-center gap-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={() => void refresh()}
              className="flex h-11 w-40 shrink-0 items-center justify-center gap-1 overflow-hidden rounded-lg border border-input bg-muted text-xs text-muted-foreground transition-colors hover:bg-accent"
            >
              {img ? (
                <img src={img} alt="验证码" className="h-full w-full object-contain" />
              ) : loading ? (
                <><RefreshCw className="size-3.5 animate-spin" />加载中</>
              ) : (
                <><RefreshCw className="size-3.5" />点击获取</>
              )}
            </button>
          </TooltipTrigger>
          <TooltipContent>点击刷新</TooltipContent>
        </Tooltip>
        <Input
          value={answer}
          onChange={(e) => {
            const v = e.target.value
            setAnswer(v)
            onChange({ captcha_id: id, captcha_answer: v })
          }}
          placeholder="图中字符或算式结果"
          required
          autoComplete="off"
          className="min-w-0 flex-1"
        />
      </div>
      {err && <span className="text-xs text-destructive">{err}</span>}
      <span className="text-xs text-muted-foreground">看不清可点击图片刷新</span>
    </div>
  )
}
