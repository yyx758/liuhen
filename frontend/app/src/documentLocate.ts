/**
 * 来源卡片、互链、搜索结果进入详情页后的定位（spec 4.6，PRD D17）。
 * 依次尝试：引文 → 所在小节 → 文档开头。
 */
import type { RenderedHeading } from './documentMarkdown'

export type LocateResult = { kind: 'quote' | 'section' | 'top'; heading: RenderedHeading | null }

const MARK_CLASS = 'doc-hit'

/** 先找路径完全相同的第一个小节，再找路径以 wanted 结尾的第一个（互链只给了最后一级）。 */
export function findSection(headings: RenderedHeading[], wanted: string[]): RenderedHeading | null {
  if (!wanted.length) return null
  const same = (path: string[], offset: number) => wanted.every((title, index) => path[offset + index] === title)
  return headings.find(heading => heading.path.length === wanted.length && same(heading.path, 0))
    ?? headings.find(heading => heading.path.length > wanted.length && same(heading.path, heading.path.length - wanted.length))
    ?? null
}

type TextPiece = { node: Text; start: number; section: number }

/** 按 DOM 顺序收集正文的文本节点，记下每段在拼接字符串里的起点和所属小节（0 表示前言）。 */
function collectText(container: HTMLElement): { text: string; pieces: TextPiece[] } {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT)
  const pieces: TextPiece[] = []
  let text = ''
  let section = 0
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const value = (node as HTMLElement).dataset.section
      if (value) section = Number(value)
      continue
    }
    const content = node.textContent || ''
    if (!content) continue
    pieces.push({ node: node as Text, start: text.length, section })
    text += content
  }
  return { text, pieces }
}

function sectionAt(pieces: TextPiece[], position: number): number {
  let section = 0
  for (const piece of pieces) {
    if (piece.start > position) break
    section = piece.section
  }
  return section
}

/** 把 [start, end) 拆到各个文本节点上，每段用 <mark> 包起来；返回第一个 mark。 */
function wrap(pieces: TextPiece[], start: number, end: number): HTMLElement | null {
  let first: HTMLElement | null = null
  for (const piece of pieces) {
    const length = piece.node.data.length
    const from = Math.max(start - piece.start, 0)
    const to = Math.min(end - piece.start, length)
    if (from >= to) continue
    const middle = piece.node.splitText(from)
    middle.splitText(to - from)
    const mark = document.createElement('mark')
    mark.className = MARK_CLASS
    middle.parentNode!.replaceChild(mark, middle)
    mark.appendChild(middle)
    first ??= mark
  }
  return first
}

/** 去掉上一次定位留下的高亮，把文字还原成普通文本节点。 */
export function clearMarks(container: HTMLElement): void {
  for (const mark of Array.from(container.querySelectorAll(`mark.${MARK_CLASS}`))) {
    mark.replaceWith(...Array.from(mark.childNodes))
  }
  container.normalize()
}

export function locate(container: HTMLElement, headings: RenderedHeading[], wanted: string[], quote: string): LocateResult {
  clearMarks(container)
  const heading = findSection(headings, wanted)
  if (quote) {
    const { text: raw, pieces } = collectText(container)
    // 引文区分大小写；找不到时再忽略大小写找一次（资料库搜索按 ILIKE 命中，关键词大小写可能不同）。
    // toLowerCase 对中文和常见英文不改变长度，位置仍能对应回原文。
    const exact = raw.includes(quote)
    const text = exact ? raw : raw.toLowerCase()
    const needle = exact ? quote : quote.toLowerCase()
    let chosen = -1
    for (let position = text.indexOf(needle); position >= 0; position = text.indexOf(needle, position + 1)) {
      if (chosen < 0) chosen = position
      // 同一句话出现多次时，优先取卡片所指小节里的那一处（同名兄弟小节靠它区分）。
      if (heading && sectionAt(pieces, position) === heading.index) { chosen = position; break }
    }
    if (chosen >= 0) {
      const mark = wrap(pieces, chosen, chosen + quote.length)
      mark?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      return { kind: 'quote', heading }
    }
  }
  if (heading) {
    document.getElementById(`sec-${heading.index}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
    return { kind: 'section', heading }
  }
  return { kind: 'top', heading: null }
}

/** 定位结果对应的页面提示；不需要提示时返回空字符串。 */
export function locateNotice(result: LocateResult, wanted: string[], quote: string): string {
  if (result.kind === 'section' && quote) return '原位置的内容已修改，已定位到所在小节'
  if (result.kind === 'top' && (wanted.length || quote)) return '原位置已不存在，已打开文档开头'
  return ''
}
