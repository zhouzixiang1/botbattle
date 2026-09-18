/**
 * Bot 源码上传共享字段 —— MyBots 上传表单与 BotVersionManager 新版本表单共用。
 *
 * 内容：
 *   - SourceUploadModeToggle：非 ELF 源码类型下的「文件上传 / 在线编辑」互斥切换。
 *   - SourceEditorField：等宽在线编辑器；超过 2 MiB 时就近展示错误。
 *   - 语言 → 规范入口名 / 单文件扩展名 / 最小示例的常量与助手，
 *     让编辑器产物的单文件直接进入既有 file 状态与 FormData 提交链。
 */
import { Label } from '@/components/ui/label'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

export type SourceUploadMode = 'file' | 'editor'

/** 源码语言 → 在线编辑与单文件直传的规范入口文件名。 */
export const SOURCE_CANONICAL_ENTRY: Record<string, string> = {
  python: 'main.py',
  cpp: 'main.cpp',
  c: 'main.c',
  go: 'main.go',
}

/** 源码语言 → zip 之外额外接受的单文件扩展名。 */
export const SOURCE_SINGLE_FILE_ACCEPT: Record<string, string> = {
  python: '.py',
  cpp: '.cpp,.cc,.cxx',
  c: '.c',
  go: '.go',
}

/** 文件选择框 accept：ELF 维持现状（undefined），源码类型追加当前语言扩展名。 */
export function sourceFileAccept(sourceFormat: string): string | undefined {
  if (sourceFormat === 'elf') return undefined
  const extra = SOURCE_SINGLE_FILE_ACCEPT[sourceFormat]
  return extra ? `.zip,application/zip,${extra}` : '.zip,application/zip'
}

const SOURCE_EDITOR_PLACEHOLDERS: Record<string, string> = {
  python: 'import json, sys\n\n# 逐行读取请求，输出一行 JSON 响应\n...',
  c: '#include <stdio.h>\n\nint main(void) {\n    ...\n}',
  cpp: '#include <iostream>\n\nint main() {\n    ...\n}',
  go: 'package main\n\nfunc main() {\n    ...\n}',
}

/** 在线编辑客户端上限：2 MiB；更大的代码请改用文件上传。 */
export const SOURCE_EDITOR_MAX_BYTES = 2 * 1024 * 1024
export const SOURCE_EDITOR_MAX_LABEL = '2 MB'

export function sourceEditorSizeError(code: string): string | null {
  if (new TextEncoder().encode(code).length <= SOURCE_EDITOR_MAX_BYTES) return null
  return `在线编辑代码超过 ${SOURCE_EDITOR_MAX_LABEL}，请改用文件上传`
}

/** 把编辑器代码构造成规范入口名的单文件（后端按扩展名自动打包）。 */
export function buildEditorSourceFile(sourceFormat: string, code: string): File {
  return new File([code], SOURCE_CANONICAL_ENTRY[sourceFormat] ?? 'main.txt', { type: 'text/plain' })
}

export function SourceUploadModeToggle({
  mode,
  onModeChange,
}: {
  mode: SourceUploadMode
  onModeChange: (mode: SourceUploadMode) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label>上传方式</Label>
      <Tabs
        value={mode}
        onValueChange={(value) => onModeChange(value === 'editor' ? 'editor' : 'file')}
        className="w-full"
        data-testid="upload-mode-toggle"
      >
        <TabsList aria-label="上传方式" className="w-full">
          <TabsTrigger value="file" data-testid="upload-mode-file">文件上传</TabsTrigger>
          <TabsTrigger value="editor" data-testid="upload-mode-editor">在线编辑</TabsTrigger>
        </TabsList>
      </Tabs>
    </div>
  )
}

export function SourceEditorField({
  id,
  sourceFormat,
  code,
  onCodeChange,
}: {
  id: string
  sourceFormat: string
  code: string
  onCodeChange: (code: string) => void
}) {
  const sizeError = sourceEditorSizeError(code)
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>在线编辑代码</Label>
      <p className="text-xs text-muted-foreground">
        将保存为 {SOURCE_CANONICAL_ENTRY[sourceFormat] ?? 'main.txt'}；{sourceFormat === 'python' ? '平台直接运行。' : '平台自动完成编译。'}
      </p>
      <Textarea
        id={id}
        data-testid="source-editor"
        value={code}
        onChange={(e) => onCodeChange(e.target.value)}
        rows={12}
        spellCheck={false}
        placeholder={SOURCE_EDITOR_PLACEHOLDERS[sourceFormat]}
        aria-invalid={sizeError ? true : undefined}
        className="field-sizing-fixed font-mono text-sm"
      />
      {sizeError && <p className="text-xs text-destructive">{sizeError}</p>}
    </div>
  )
}
