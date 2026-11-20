import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

const parser = new MarkdownIt({ html: false, linkify: true, breaks: true })
const originalLinkOpen = parser.renderer.rules.link_open
parser.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  const link = tokens[index]!
  link.attrSet('target', '_blank')
  link.attrSet('rel', 'noopener noreferrer')
  return originalLinkOpen
    ? originalLinkOpen(tokens, index, options, environment, renderer)
    : renderer.renderToken(tokens, index, options)
}

const cache = new Map<string, string>()

export function renderMarkdown(source: string): string {
  const cached = cache.get(source)
  if (cached !== undefined) return cached

  const html = DOMPurify.sanitize(parser.render(source), {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target', 'rel'],
  })
  if (cache.size >= 200) cache.clear()
  cache.set(source, html)
  return html
}
