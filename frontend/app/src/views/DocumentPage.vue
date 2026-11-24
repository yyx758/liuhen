<script setup lang="ts">
/** 文档详情页（spec 4.6）：全文、目录、从来源卡片 / 互链 / 搜索进入时的定位、编辑。 */
import { computed, nextTick, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter, type LocationQuery } from 'vue-router'
import { api, ApiError, type DocumentRead } from '../api'
import { clearMarks, locate, locateNotice } from '../documentLocate'
import { renderDocument } from '../documentMarkdown'
import { state } from '../state'
import DocumentEditor from '../components/DocumentEditor.vue'
import Icon from '../components/Icon.vue'

const route = useRoute()
const router = useRouter()
const doc = ref<DocumentRead | null>(null)
const missing = ref(false)
const loadError = ref('')
const notice = ref('')
const body = ref<HTMLElement | null>(null)
const editing = ref<{ line: number | null } | null>(null)

const documentId = computed(() => Number(route.params.id))
const fromChat = computed(() => route.query.from === 'chat')
const rendered = computed(() => doc.value ? renderDocument(doc.value.content, doc.value.links) : { html: '', headings: [] })
const topLevel = computed(() => Math.min(...rendered.value.headings.map(heading => heading.level), 6))

function list(value: LocationQuery[string]): string[] {
  return (Array.isArray(value) ? value : value ? [value] : []).filter((item): item is string => typeof item === 'string' && item !== '')
}
function single(value: LocationQuery[string]): string {
  return list(value)[0] ?? ''
}

let loadRequest = 0
async function load() {
  const current = ++loadRequest
  missing.value = false
  loadError.value = ''
  notice.value = ''
  try {
    const result = await api.document(documentId.value)
    if (current !== loadRequest) return
    doc.value = result
    await relocate()
  } catch (error) {
    if (current !== loadRequest) return
    doc.value = null
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) missing.value = true
    else loadError.value = error instanceof Error ? error.message : '加载笔记失败'
  }
}

/** 按查询参数定位：h 是小节路径，q 是引文（D17）。 */
async function relocate() {
  await nextTick()
  const container = body.value
  if (!container || !doc.value) return
  const wanted = list(route.query.h)
  const quote = single(route.query.q)
  const sameName = Number(single(route.query.same))
  if (!wanted.length && !quote) {
    clearMarks(container)
    window.scrollTo({ top: 0 })
    notice.value = sameName > 1 ? `有 ${sameName} 篇同名笔记，已打开最近更新的一篇。` : ''
    return
  }
  const result = locate(container, rendered.value.headings, wanted, quote)
  if (result.kind === 'top') window.scrollTo({ top: 0 })
  // 从资料库搜索进入时，关键词可能只在图片说明里，定位不到不算"内容已修改"。
  const messages = [route.query.from === 'search' ? '' : locateNotice(result, wanted, quote)]
  if (sameName > 1) messages.push(`有 ${sameName} 篇同名笔记，已打开最近更新的一篇。`)
  notice.value = messages.filter(Boolean).join(' ')
}

watch(documentId, () => { void load() }, { immediate: true })
watch(() => [route.query.h, route.query.q, route.query.same], () => { if (doc.value) void relocate() })

/** 正文里的互链在站内跳转，不新开标签页。 */
function onBodyClick(event: MouseEvent) {
  const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a.doc-link')
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey) return
  event.preventDefault()
  const heading = link.dataset.heading
  const query: Record<string, string | string[]> = {}
  if (heading) query.h = [heading]
  if (link.dataset.sameName) query.same = link.dataset.sameName
  if (fromChat.value) query.from = 'chat'
  void router.push({ path: `/library/${link.dataset.docId}`, query })
}

function scrollToHeading(index: number) {
  document.getElementById(`sec-${index}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 阅读位置：视口顶部附近最后经过的小节标题所在行，编辑器打开时把光标放到那里。 */
function readingLine(): number | null {
  let line: number | null = null
  for (const heading of rendered.value.headings) {
    const element = document.getElementById(`sec-${heading.index}`)
    if (element && element.getBoundingClientRect().top <= 140) line = heading.line
  }
  return line
}

function saved(result: number | DocumentRead) {
  editing.value = null
  if (typeof result !== 'number') {
    doc.value = result
    state.toast = '笔记已保存。'
    notice.value = ''
  }
}
async function deleted() {
  editing.value = null
  state.toast = '笔记已删除。'
  await router.push('/library')
}
function updated(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Taipei', year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value))
}
</script>

<template>
  <main class="page document-page">
    <RouterLink class="subtle-link document-back" :to="fromChat ? '/chat' : '/library'"><Icon name="chevron-left" :size="15" />{{ fromChat ? '返回对话' : '返回资料库' }}</RouterLink>

    <div v-if="missing" class="empty-state document-missing"><div class="empty-symbol"><Icon name="book" :size="23" /></div><h3>文档已删除</h3><p>这篇笔记已经不在资料库里了。</p></div>
    <div v-else-if="loadError" class="notice" role="alert">{{ loadError }}<span><button type="button" @click="load">重试</button></span></div>

    <template v-else-if="doc">
      <div class="page-head document-head">
        <div>
          <div class="document-meta"><span v-if="doc.category" class="tag blue"><Icon name="book" :size="13" />{{ doc.category }}</span><span class="tiny">最后更新 · {{ updated(doc.updated_at) }} · {{ doc.content_chars }} 字</span></div>
          <h1 class="page-title document-title">{{ doc.title }}</h1>
        </div>
        <div class="head-actions"><button class="solid" type="button" @click="editing = { line: readingLine() }"><Icon name="edit" :size="15" />编辑</button></div>
      </div>
      <div v-if="notice" class="document-notice" role="status">{{ notice }}<button type="button" aria-label="关闭提示" @click="notice = ''">×</button></div>
      <div class="document-layout" :class="{ 'no-toc': !rendered.headings.length }">
        <nav v-if="rendered.headings.length" class="document-toc" aria-label="目录">
          <div class="document-toc-title">目录</div>
          <button v-for="heading in rendered.headings" :key="heading.index" type="button" :style="{ paddingLeft: `${(heading.level - topLevel) * 12 + 8}px` }" @click="scrollToHeading(heading.index)">{{ heading.path[heading.path.length - 1] }}</button>
        </nav>
        <article ref="body" class="markdown-content document-body" @click="onBodyClick" v-html="rendered.html" />
      </div>
      <p v-if="!doc.content.trim()" class="tiny">这篇笔记还没有内容，点"编辑"开始写。</p>
    </template>
    <p v-else class="tiny document-loading">正在加载…</p>

    <DocumentEditor v-if="editing && doc" mode="edit" :document="doc" :line="editing.line" @close="editing = null" @saved="saved" @deleted="deleted" />
  </main>
</template>
