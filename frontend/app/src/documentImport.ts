/**
 * 导入单个 .md 文件（spec 4.8，PRD D20、D23）：编码与长度检查、图片引用识别与匹配、上传后改写。
 * 都是纯函数；上传由调用方完成。
 */
import { fenceCloses, fenceOpen, isImageName } from './documentMarkdown'

export const DOCUMENT_MAX_CHARS = 50_000

export type ImageRef = {
  start: number
  end: number
  alt: string
  target: string
  /** Obsidian 的 ![[图.png]] 写法 */
  obsidian: boolean
}

/** 按 UTF-8 严格解码；去掉 BOM，换行统一为 \n。不是 UTF-8 时抛错。 */
export function decodeMarkdown(buffer: ArrayBuffer): string {
  let text: string
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
  } catch {
    throw new Error('文件不是 UTF-8 编码，请转换后再导入')
  }
  text = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n')
  if (text.length > DOCUMENT_MAX_CHARS) throw new Error(`超过 ${DOCUMENT_MAX_CHARS} 字，请拆分后再导入`)
  return text
}

// 与后端 _CODE 相同：成对的反引号，不跨行。
const INLINE_CODE = /(`+)([^\n]+?)(?<!`)\1(?!`)/g
// ![说明](目标) 或 ![说明](<带空格的目标>)，可带标题。
const MARKDOWN_IMAGE = /!\[([^[\]\n]*)\]\((?:<([^<>\n]+)>|([^()\s]*))(?:[ \t]+"[^"\n]*")?\)/g
// ![[图.png]] 或 ![[图.png|300]]
const OBSIDIAN_IMAGE = /!\[\[([^[\]\n|]+)(?:\|[^[\]\n]*)?\]\]/g

function needsUpload(target: string): boolean {
  return Boolean(target) && !/^(https?:|data:)/i.test(target) && !target.startsWith('/documents/images/')
}

/** 找出需要上传的本地图片引用，跳过围栏代码块和行内代码；按出现顺序返回。 */
export function findImageRefs(content: string): ImageRef[] {
  const refs: ImageRef[] = []
  let offset = 0
  let marker: string | null = null
  for (const line of content.split('\n')) {
    const lineStart = offset
    offset += line.length + 1
    if (marker !== null) {
      if (fenceCloses(line, marker)) marker = null
      continue
    }
    marker = fenceOpen(line)
    if (marker !== null) continue

    const code: [number, number][] = []
    for (const match of line.matchAll(INLINE_CODE)) code.push([match.index!, match.index! + match[0].length])
    const inCode = (position: number) => code.some(([start, end]) => position >= start && position < end)

    for (const match of line.matchAll(MARKDOWN_IMAGE)) {
      const target = (match[2] ?? match[3] ?? '').trim()
      if (inCode(match.index!) || !needsUpload(target)) continue
      refs.push({ start: lineStart + match.index!, end: lineStart + match.index! + match[0].length, alt: match[1]!, target, obsidian: false })
    }
    for (const match of line.matchAll(OBSIDIAN_IMAGE)) {
      const target = match[1]!.trim()
      if (inCode(match.index!) || !isImageName(target)) continue
      refs.push({ start: lineStart + match.index!, end: lineStart + match.index! + match[0].length, alt: target, target, obsidian: true })
    }
  }
  return refs.sort((a, b) => a.start - b.start)
}

/** 引用目标的文件名：去掉 ?… 和 #…，取最后一段，URL 解码。 */
export function fileNameOf(target: string): string {
  const path = target.split(/[?#]/)[0]!
  const name = path.split(/[/\\]/).pop() || path
  try { return decodeURIComponent(name) } catch { return name }
}

/** 按文件名（不区分大小写）把引用和所选图片对应起来。 */
export function matchImages(refs: ImageRef[], files: File[]): { matched: Map<ImageRef, File>; missing: ImageRef[] } {
  const byName = new Map(files.map(file => [file.name.toLowerCase(), file]))
  const matched = new Map<ImageRef, File>()
  const missing: ImageRef[] = []
  for (const ref of refs) {
    const file = byName.get(fileNameOf(ref.target).toLowerCase())
    if (file) matched.set(ref, file)
    else missing.push(ref)
  }
  return { matched, missing }
}

/** 从后往前替换已上传的引用：普通图片只换地址，Obsidian 写法改成标准图片语法，说明默认用文件名。 */
export function rewriteRefs(content: string, urls: Map<ImageRef, string>): string {
  let result = content
  for (const [ref, url] of [...urls].sort(([a], [b]) => b.start - a.start)) {
    const alt = ref.obsidian ? fileNameOf(ref.target) : ref.alt
    result = `${result.slice(0, ref.start)}![${alt}](${url})${result.slice(ref.end)}`
  }
  return result
}

/** 默认标题：文件名去掉 .md / .markdown 扩展名，最多 100 字。 */
export function titleFromFileName(name: string): string {
  return name.replace(/\.(md|markdown)$/i, '').trim().slice(0, 100)
}
