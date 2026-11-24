<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api, type DocumentCategory, type DocumentSearchGroup, type DocumentSummary, type SearchHit } from '../api'
import { decodeMarkdown, fileNameOf, findImageRefs, matchImages, rewriteRefs, titleFromFileName, type ImageRef } from '../documentImport'
import { state } from '../state'
import DocumentEditor from '../components/DocumentEditor.vue'
import Icon from '../components/Icon.vue'

const router = useRouter()
const documents = ref<DocumentSummary[]>([])
const nextBeforeId = ref<number | null>(null)
const total = ref<number | null>(null)
const categories = ref<DocumentCategory[]>([])
const category = ref('')
const search = ref('')
const results = ref<DocumentSearchGroup[] | null>(null)
const searchError = ref('')
const loading = ref(false)
const importing = ref(false)
const editor = ref<{ mode: 'create' | 'import'; initial?: { title: string; content: string }; missing?: string[] } | null>(null)

// 与后端限制一致（spec 2.4）：最多 5 个关键词，每个不超过 30 字。
const queries = computed(() => search.value.trim().split(/\s+/).filter(Boolean))

let listRequest = 0
async function loadList(append = false) {
  const current = ++listRequest
  loading.value = true
  try {
    const page = await api.documents(category.value || null, append ? nextBeforeId.value : null)
    if (current !== listRequest) return
    documents.value = append ? [...documents.value, ...page.items] : page.items
    nextBeforeId.value = page.next_before_id
    if (!append) total.value = page.total
  } catch (error) {
    if (current === listRequest) state.error = error instanceof Error ? error.message : '加载笔记失败'
  } finally { if (current === listRequest) loading.value = false }
}
async function loadCategories() {
  try { categories.value = await api.documentCategories() } catch { /* 分类加载失败时只显示"全部分类" */ }
}

let searchRequest = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch([search, category], () => {
  clearTimeout(searchTimer)
  searchError.value = ''
  const words = queries.value
  if (!words.length) { searchRequest++; results.value = null; return }
  if (words.length > 5) { searchError.value = '一次最多 5 个关键词。'; return }
  if (words.some(word => word.length > 30)) { searchError.value = '每个关键词不能超过 30 字。'; return }
  searchTimer = setTimeout(async () => {
    const current = ++searchRequest
    try {
      const groups = await api.searchDocuments(words.join(' '), category.value || null)
      if (current === searchRequest) results.value = groups
    } catch (error) {
      if (current === searchRequest) searchError.value = error instanceof Error ? error.message : '搜索失败'
    }
  }, 300)
})
watch(category, () => { void loadList() })
onUnmounted(() => clearTimeout(searchTimer))
void loadList()
void loadCategories()

/** 把摘要切成普通文字和关键词两种片段，模板里逐段渲染，不用 v-html。 */
function highlight(text: string, words: string[]): { text: string; hit: boolean }[] {
  const lowered = words.map(word => word.toLowerCase()).filter(Boolean)
  const parts: { text: string; hit: boolean }[] = []
  let rest = text
  while (rest) {
    let best = -1
    let length = 0
    for (const word of lowered) {
      const position = rest.toLowerCase().indexOf(word)
      if (position >= 0 && (best < 0 || position < best || position === best && word.length > length)) { best = position; length = word.length }
    }
    if (best < 0) { parts.push({ text: rest, hit: false }); break }
    if (best > 0) parts.push({ text: rest.slice(0, best), hit: false })
    parts.push({ text: rest.slice(best, best + length), hit: true })
    rest = rest.slice(best + length)
  }
  return parts
}
function hitLink(hit: SearchHit) {
  return { path: `/library/${hit.document_id}`, query: { h: hit.heading_path, q: hit.matched_queries[0] ?? '', from: 'search' } }
}
function updated(value: string) { return value.slice(0, 10) }

// ---------- 导入 ----------

async function importFiles(event: Event) {
  const element = event.target as HTMLInputElement
  const files = Array.from(element.files || [])
  element.value = ''
  const markdown = files.filter(file => /\.(md|markdown)$/i.test(file.name))
  if (markdown.length !== 1) { state.error = '请选择一个 .md 文件（可同时选择它引用的图片）。'; return }
  const source = markdown[0]!
  importing.value = true
  try {
    const content = decodeMarkdown(await source.arrayBuffer())
    const images = files.filter(file => file !== source && file.type.startsWith('image/'))
    const refs = findImageRefs(content)
    const { matched, missing } = matchImages(refs, images)
    const problems = missing.map(ref => `${fileNameOf(ref.target)}（没有选择这个文件）`)
    // 同一个文件只上传一次；串行上传，避免并发请求同时通过总量检查。
    const uploaded = new Map<File, string>()
    const failed = new Set<File>()
    for (const file of new Set(matched.values())) {
      try { uploaded.set(file, (await api.uploadImage(file)).url) } catch (error) {
        failed.add(file)
        problems.push(`${file.name}（${error instanceof Error ? error.message : '上传失败'}）`)
      }
    }
    const urls = new Map<ImageRef, string>()
    for (const [ref, file] of matched) if (uploaded.has(file)) urls.set(ref, uploaded.get(file)!)
    editor.value = {
      mode: 'import',
      initial: { title: titleFromFileName(source.name), content: rewriteRefs(content, urls) },
      missing: [...new Set(problems)],
    }
  } catch (error) {
    state.error = error instanceof Error ? error.message : '导入失败'
  } finally { importing.value = false }
}

async function saved(result: number | unknown) {
  editor.value = null
  if (typeof result === 'number') {
    state.toast = '笔记已保存。'
    await router.push(`/library/${result}`)
  }
}
</script>

<template>
  <main class="page">
    <div class="page-head">
      <div><div class="page-kicker"><span class="dot" /> KNOWLEDGE SPACE</div><h1 class="page-title">资料库</h1><p class="lead">把有用的内容放在一起，随时回来看一看。</p></div>
      <div class="head-actions">
        <label class="outline import-btn" :class="{ disabled: importing }"><Icon name="up" :size="15" />{{ importing ? '导入中…' : '导入 .md' }}<input type="file" multiple hidden :disabled="importing" accept=".md,.markdown,text/markdown,image/png,image/jpeg,image/gif,image/webp" @change="importFiles" /></label>
        <button class="solid" type="button" @click="editor = { mode: 'create' }"><Icon name="plus" :size="15" />新建笔记</button>
      </div>
    </div>
    <div class="section-heading library-toolbar" style="margin-bottom:12px">
      <h2 class="section-title">我的资料 <span v-if="total !== null" class="small" style="font-weight:450">{{ total }} 篇</span></h2>
      <div class="library-filters">
        <select v-model="category" class="field library-category" aria-label="按分类筛选"><option value="">全部分类</option><option v-for="item in categories" :key="item.name" :value="item.name">{{ item.name }}（{{ item.count }}）</option></select>
        <label class="search-box"><Icon name="search" :size="16" /><input v-model="search" placeholder="搜索资料" aria-label="搜索资料" /></label>
      </div>
    </div>
    <p class="library-hint">这是精确关键词搜索，可以用空格分开多个词；记不清原话时，可以在对话里让小记帮你找。导入时可同时选择 .md 文件引用的图片。</p>
    <p v-if="searchError" class="library-search-error" role="alert">{{ searchError }}</p>

    <div v-if="results" class="library-results">
      <div v-for="group in results" :key="group.document.id" class="search-group">
        <RouterLink class="search-doc" :to="`/library/${group.document.id}`"><span v-if="group.document.category" class="tag blue"><Icon name="book" :size="13" />{{ group.document.category }}</span><b>{{ group.document.title }}</b><span class="tiny">最后更新 · {{ updated(group.document.updated_at) }}</span></RouterLink>
        <RouterLink v-for="hit in group.hits" :key="hit.chunk_id" class="search-hit" :to="hitLink(hit)">
          <span v-if="hit.heading_path.length" class="search-hit-path">{{ hit.heading_path.join(' > ') }}</span>
          <span class="search-hit-snippet"><template v-for="(part, index) in highlight(hit.snippet, queries)" :key="index"><mark v-if="part.hit">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
        </RouterLink>
      </div>
      <div v-if="!results.length" class="empty-state"><h3>没有包含这些关键词的笔记</h3><p>换个说法试试，或者在对话里问小记。</p></div>
    </div>

    <template v-else>
      <div class="library-grid">
        <RouterLink v-for="note in documents" :key="note.id" class="note-card" :to="`/library/${note.id}`"><span v-if="note.category" class="tag blue"><Icon name="book" :size="13" />{{ note.category }}</span><h3>{{ note.title }}</h3><p>{{ note.excerpt || '（空白笔记）' }}</p><div class="note-bottom">最后更新 · {{ updated(note.updated_at) }}</div></RouterLink>
        <div v-if="!documents.length && !loading" class="empty-state library-empty"><div class="empty-symbol"><Icon name="book" :size="23" /></div><h3>{{ category ? '这个分类下还没有笔记' : '还没有笔记' }}</h3><p>写一篇，或者导入一个 .md 文件。</p><div class="library-empty-actions"><button class="solid" type="button" @click="editor = { mode: 'create' }">新建笔记</button></div></div>
      </div>
      <div v-if="nextBeforeId" class="library-more"><button class="outline" type="button" :disabled="loading" @click="loadList(true)">{{ loading ? '加载中…' : '加载更多' }}</button></div>
    </template>

    <p class="library-privacy">在对话中提问时，相关笔记片段会发送给 AI 模型服务。</p>
    <DocumentEditor v-if="editor" :mode="editor.mode" :initial="editor.initial" :missing-images="editor.missing" @close="editor = null" @saved="saved" />
  </main>
</template>
