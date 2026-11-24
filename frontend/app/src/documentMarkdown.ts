/**
 * 资料库文档的 Markdown 渲染（spec 4.4）。与对话共用"禁用原始 HTML + DOMPurify 清理"，但多三条规则：
 * 1. 小节标题的识别与后端切片完全一致（定位靠 heading_path 对上号）；
 * 2. 只加载本站图片，外链图片不自动加载；
 * 3. [[互链]] 与指向 .md 的相对链接按后端解析结果渲染成站内链接。
 */
import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'
import type { Token } from 'markdown-it'
import { imageUrl, type DocumentLink } from './api'

export type RenderedHeading = {
  /** 从 1 开始；元素 id 为 sec-<index>。 */
  index: number
  level: number
  path: string[]
  /** 标题在整篇正文中的行号（从 0 开始，含 front matter），供编辑器定位。 */
  line: number
}

// ---------- 与后端一致的规则（backend/app/services/document_markdown.py、document_chunker.py） ----------

const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/
const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/
const FENCE_CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/
export const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp']
const URL_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/
const SITE_IMAGE = /^\/documents\/images\/[0-9a-f]{32}$/

export function fenceOpen(line: string): string | null {
  const match = FENCE_OPEN.exec(line)
  if (!match) return null
  const [, marker, info] = match
  // 反引号围栏的信息串里不能再有反引号（CommonMark）。
  if (marker![0] === '`' && info!.includes('`')) return null
  return marker!
}

export function fenceCloses(line: string, marker: string): boolean {
  const match = FENCE_CLOSE.exec(line)
  return match !== null && match[1]![0] === marker[0] && match[1]!.length >= marker.length
}

/** front matter 闭合行的下标；不是 front matter 时返回 null。 */
export function frontMatterEnd(lines: string[]): number | null {
  if (!lines.length || lines[0]!.trimEnd() !== '---') return null
  for (let index = 1; index < lines.length; index++) {
    const line = lines[index]!.trimEnd()
    if (line === '---' || line === '...') return index
  }
  return null
}

/** 按后端切片的规则逐行找出小节标题（跳过 front matter 和围栏代码块）。 */
export function scanHeadings(content: string): RenderedHeading[] {
  const lines = content.split('\n')
  const fmEnd = frontMatterEnd(lines)
  const headings: RenderedHeading[] = []
  const stack: { level: number; text: string }[] = []
  let marker: string | null = null
  for (let line = fmEnd === null ? 0 : fmEnd + 1; line < lines.length; line++) {
    const text = lines[line]!.replace(/\r$/, '')
    if (marker !== null) {
      if (fenceCloses(text, marker)) marker = null
      continue
    }
    marker = fenceOpen(text)
    if (marker !== null) continue
    const match = HEADING.exec(text)
    const title = match?.[2]?.trim()
    if (!match || !title) continue
    const level = match[1]!.length
    while (stack.length && stack[stack.length - 1]!.level >= level) stack.pop()
    stack.push({ level, text: title })
    headings.push({ index: headings.length + 1, level, path: stack.map(item => item.text), line })
  }
  return headings
}

// ---------- 互链 ----------

export type ParsedLink = { title: string; heading: string | null }

/** [[标题#小节|显示]] 的目标部分（不含显示文字）→ 标题与小节；与后端 extract_doc_links 一致。 */
export function parseWikiTarget(target: string): ParsedLink {
  const [title, ...rest] = target.split('#')
  return { title: title!.trim(), heading: rest.join('#').trim() || null }
}

/** 指向其他笔记的相对 .md 链接；不是时返回 null。 */
export function parseMarkdownPath(href: string): ParsedLink | null {
  if (URL_SCHEME.test(href) || href.startsWith('/')) return null
  const [path, ...rest] = href.split('#')
  if (!path!.toLowerCase().endsWith('.md')) return null
  const name = safeDecode(path!.split('/').pop()!).slice(0, -3)
  return { title: name.trim(), heading: safeDecode(rest.join('#')).trim() || null }
}

function safeDecode(value: string): string {
  try { return decodeURIComponent(value) } catch { return value }
}

const linkKey = (title: string, heading: string | null) => `${title}\u0000${heading ?? ''}`

export function isImageName(name: string): boolean {
  const lower = name.toLowerCase()
  return IMAGE_EXTENSIONS.some(extension => lower.endsWith(extension))
}

// ---------- markdown-it ----------

type RenderEnv = { links: Map<string, number[]>; headingLines: Map<number, number>; lineOffset: number }

const parser = new MarkdownIt({ html: false, linkify: true, breaks: true })
const escape = parser.utils.escapeHtml
const BASE_URL = import.meta.env.BASE_URL

/** 识别 [[…]] 与 ![[…]]；规则与后端 _WIKILINK 相同（不跨行、内部不含方括号）。 */
parser.inline.ruler.before('link', 'wikilink', (state, silent) => {
  const start = state.pos
  const embed = state.src.charCodeAt(start) === 0x21 /* ! */
  const open = embed ? start + 1 : start
  if (!state.src.startsWith('[[', open)) return false
  const close = state.src.indexOf(']]', open + 2)
  if (close < 0) return false
  const inner = state.src.slice(open + 2, close)
  if (!inner || /[[\]\n]/.test(inner)) return false
  if (!silent) {
    const token = state.push('wikilink', '', 0)
    token.meta = { inner, embed }
  }
  state.pos = close + 2
  return true
})

function docLinkHtml(env: RenderEnv, link: ParsedLink, text: string): string {
  const ids = link.title ? env.links.get(linkKey(link.title, link.heading)) : undefined
  if (!ids?.length) {
    return `<span class="doc-link unresolved" title="资料库里没有这篇笔记">${escape(text)}</span>`
  }
  const query = link.heading ? `?h=${encodeURIComponent(link.heading)}` : ''
  const sameName = ids.length > 1 ? ` data-same-name="${ids.length}"` : ''
  return `<a class="doc-link" href="${BASE_URL}library/${ids[0]}${query}" data-doc-id="${ids[0]}"`
    + ` data-heading="${escape(link.heading ?? '')}"${sameName}>${escape(text)}</a>`
}

parser.renderer.rules.wikilink = (tokens, index, _options, rawEnv) => {
  const env = rawEnv as RenderEnv
  const { inner, embed } = tokens[index]!.meta as { inner: string; embed: boolean }
  const [target, ...alias] = inner.split('|')
  if (embed && isImageName(target!)) {
    return `<span class="image-missing">图片未上传：${escape(target!.trim())}</span>`
  }
  return docLinkHtml(env, parseWikiTarget(target!), alias.join('|') || target!)
}

parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const token = tokens[index]!
  const src = String(token.attrGet('src') ?? '')
  const alt = renderer.renderInlineAsText(token.children || [], options, env)
  if (SITE_IMAGE.test(src)) {
    return `<img src="${escape(imageUrl(src))}" alt="${escape(alt)}" loading="lazy">`
  }
  if (/^https?:\/\//i.test(src)) {
    // 外链图片不自动加载，避免把访问记录泄露给第三方（D25）。
    return `<a href="${escape(src)}" target="_blank" rel="noopener noreferrer">外部图片：${escape(alt || src)}</a>`
  }
  const name = safeDecode(src.split(/[/\\]/).pop() || src)
  return `<span class="image-missing">图片未上传：${escape(name)}</span>`
}

// 指向 .md 的相对链接按互链渲染；其余链接新标签页打开。
parser.renderer.rules.link_open = (tokens, index, options, rawEnv, renderer) => {
  const env = rawEnv as RenderEnv
  const token = tokens[index]!
  const link = parseMarkdownPath(String(token.attrGet('href') ?? ''))
  if (link) {
    // 链接文字原样保留：找到对应的 link_close，把中间的行内内容渲染为文字后整体输出。
    let close = index + 1
    while (close < tokens.length && tokens[close]!.type !== 'link_close') close++
    const text = renderer.renderInlineAsText(tokens.slice(index + 1, close), options, env)
    for (let skip = index + 1; skip <= close && skip < tokens.length; skip++) tokens[skip]!.hidden = true
    return docLinkHtml(env, link, text)
  }
  token.attrSet('target', '_blank')
  token.attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}
const renderInline = parser.renderer.renderInline.bind(parser.renderer)
parser.renderer.renderInline = (tokens: Token[], options, env) =>
  renderInline(tokens.filter(token => !token.hidden), options, env)

parser.renderer.rules.heading_open = (tokens, index, options, rawEnv, renderer) => {
  const env = rawEnv as RenderEnv
  const token = tokens[index]!
  const section = token.map ? env.headingLines.get(token.map[0] + env.lineOffset) : undefined
  if (section !== undefined) {
    token.attrSet('id', `sec-${section}`)
    token.attrSet('data-section', String(section))
  }
  return renderer.renderToken(tokens, index, options)
}

/**
 * 渲染整篇文档。links 来自 GET /documents/{id}（后端已按当前用户解析）；
 * 编辑器预览时传空数组，互链都显示为未解析。
 */
export function renderDocument(content: string, links: DocumentLink[]): { html: string; headings: RenderedHeading[] } {
  const normalized = content.replace(/\r\n/g, '\n')
  const lines = normalized.split('\n')
  const fmEnd = frontMatterEnd(lines)
  const headings = scanHeadings(normalized)
  const env: RenderEnv = {
    links: new Map(links.map(link => [linkKey(link.title.trim(), link.heading), link.document_ids])),
    headingLines: new Map(headings.map(heading => [heading.line, heading.index])),
    lineOffset: fmEnd === null ? 0 : fmEnd + 1,
  }
  let html = ''
  let body = normalized
  if (fmEnd !== null) {
    // front matter 原样显示，不交给 markdown-it（否则会被渲染成分隔线和标题）。
    html += `<pre class="front-matter">${escape(lines.slice(0, fmEnd + 1).join('\n'))}</pre>`
    body = lines.slice(fmEnd + 1).join('\n')
  }
  html += parser.render(body, env)
  return {
    html: DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, ADD_ATTR: ['target', 'rel', 'loading'] }),
    headings,
  }
}
