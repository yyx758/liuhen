<script setup lang="ts">
/**
 * 新建、导入、编辑共用的笔记编辑器（spec 4.7）：全文编辑、预览、图片上传、保存与删除。
 * 保存失败时不关闭、不显示成功；有未保存的修改时关闭前先确认。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { api, ApiError, type DocumentCategory, type DocumentInput, type DocumentRead } from '../api'
import { DOCUMENT_MAX_CHARS } from '../documentImport'
import { renderDocument } from '../documentMarkdown'
import Icon from './Icon.vue'

const props = defineProps<{
  mode: 'create' | 'import' | 'edit'
  /** 编辑模式下的当前文档 */
  document?: DocumentRead | null
  /** 导入时的初始内容 */
  initial?: { title: string; content: string } | null
  /** 导入时没有上传的图片说明 */
  missingImages?: string[]
  /** 编辑模式下打开时光标所在的行（对应详情页的阅读位置） */
  line?: number | null
}>()
const emit = defineEmits<{
  close: []
  /** 新建、导入返回新文档 ID；编辑返回最新的文档 */
  saved: [result: number | DocumentRead]
  deleted: []
}>()

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const IMAGE_MAX_BYTES = 5 * 1024 * 1024
const UPLOADING = /!\[[^\]\n]*\]\(uploading:[0-9a-f-]+\)/

const original: DocumentInput = {
  title: props.document?.title ?? props.initial?.title ?? '',
  category: props.document?.category ?? null,
  content: props.document?.content ?? props.initial?.content ?? '',
}
const form = reactive({ title: original.title, category: original.category ?? '', content: original.content })
const tab = ref<'edit' | 'preview'>('edit')
const textarea = ref<HTMLTextAreaElement | null>(null)
const categories = ref<DocumentCategory[]>([])
const saving = ref(false)
const error = ref('')
const imageHint = ref(false)
let createKey = ''
let createPayload = ''
/** 已保存、已删除或已放弃：之后父组件跳转页面时不再询问。 */
let finished = false

const input = computed<DocumentInput>(() => ({
  title: form.title.trim(), category: form.category.trim() || null, content: form.content,
}))
const dirty = computed(() => (Object.keys(original) as (keyof DocumentInput)[]).some(key => input.value[key] !== original[key]))
const uploading = computed(() => UPLOADING.test(form.content))
const tooLong = computed(() => form.content.length > DOCUMENT_MAX_CHARS)
const preview = computed(() => tab.value === 'preview' ? renderDocument(form.content, []).html : '')
const heading = computed(() => ({ create: '写一篇笔记', import: '导入笔记', edit: '编辑笔记' })[props.mode])

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('beforeunload', onBeforeUnload)
  if (props.mode === 'edit' && props.line != null && textarea.value) {
    // 光标放到阅读位置对应的行，focus 时浏览器会把光标滚进可见区域。
    const offset = form.content.split('\n').slice(0, props.line).reduce((sum, line) => sum + line.length + 1, 0)
    await nextTick()
    textarea.value.setSelectionRange(offset, offset)
    textarea.value.blur()
    textarea.value.focus()
  }
  try { categories.value = await api.documentCategories() } catch { /* 候选分类加载失败不影响编辑 */ }
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
onBeforeRouteLeave(() => finished || !dirty.value || window.confirm('放弃未保存的修改？'))

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!finished && dirty.value) event.preventDefault()
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}
function close() {
  if (saving.value) return
  if (dirty.value && !window.confirm('放弃未保存的修改？')) return
  finished = true
  emit('close')
}

// ---------- 图片 ----------

function insertAtCursor(text: string) {
  const element = textarea.value
  const start = element?.selectionStart ?? form.content.length
  const end = element?.selectionEnd ?? start
  form.content = form.content.slice(0, start) + text + form.content.slice(end)
  void nextTick(() => element?.setSelectionRange(start + text.length, start + text.length))
}

async function uploadFiles(files: File[]) {
  const images = files.filter(file => IMAGE_TYPES.includes(file.type))
  if (files.length && !images.length) { error.value = '只支持 PNG、JPEG、GIF、WebP 图片。'; return }
  for (const file of images) {
    const id = crypto.randomUUID()
    const placeholder = `![上传中：${file.name.replace(/[[\]]/g, '')}](uploading:${id})`
    insertAtCursor(placeholder)
    try {
      if (file.size > IMAGE_MAX_BYTES) throw new Error(`「${file.name}」超过 5 MB，无法上传。`)
      const image = await api.uploadImage(file)
      const alt = file.name.replace(/\.[^.]+$/, '').replace(/[[\]]/g, '')
      const reference = `![${alt}](${image.url})`
      const index = form.content.indexOf(placeholder)
      if (index < 0) continue // 用户在上传期间删掉了占位：不再插入
      form.content = form.content.replace(placeholder, reference)
      imageHint.value = true
      await nextTick()
      textarea.value?.setSelectionRange(index + 2, index + 2 + alt.length)
    } catch (reason) {
      // 失败时去掉占位，正文里不留下坏引用。
      form.content = form.content.replace(placeholder, '')
      error.value = reason instanceof Error ? `图片上传失败：${reason.message}` : '图片上传失败'
    }
  }
}
function onPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files || []).filter(file => file.type.startsWith('image/'))
  if (!files.length) return
  event.preventDefault()
  void uploadFiles(files)
}
function onDrop(event: DragEvent) {
  const files = Array.from(event.dataTransfer?.files || [])
  if (!files.length) return
  event.preventDefault()
  textarea.value?.focus()
  void uploadFiles(files)
}
function onPick(event: Event) {
  const element = event.target as HTMLInputElement
  const files = Array.from(element.files || [])
  element.value = ''
  void uploadFiles(files)
}

// ---------- 保存与删除 ----------

function conflictMessage(reason: unknown, fallback: string) {
  if (reason instanceof ApiError && reason.status === 409 && props.mode === 'edit') return '文档已在别处修改，请刷新后再改。'
  return reason instanceof Error ? reason.message : fallback
}

async function save() {
  if (saving.value || uploading.value || tooLong.value) return
  if (!input.value.title) { error.value = '请填写标题。'; return }
  error.value = ''
  saving.value = true
  try {
    if (props.mode === 'edit' && props.document) {
      const changes: Partial<DocumentInput> = {}
      for (const key of Object.keys(original) as (keyof DocumentInput)[]) {
        if (input.value[key] !== original[key]) Object.assign(changes, { [key]: input.value[key] })
      }
      if (!Object.keys(changes).length) { finished = true; emit('close'); return }
      const updated = await api.updateDocument(props.document.id, props.document.version, changes)
      finished = true
      emit('saved', updated)
    } else {
      // 内容不变的重试沿用同一个幂等键，网络超时后重试不会重复创建。
      const payload = JSON.stringify(input.value)
      if (payload !== createPayload) { createPayload = payload; createKey = crypto.randomUUID() }
      const created = await api.createDocument(input.value, createKey)
      finished = true
      emit('saved', created.id)
    }
  } catch (reason) {
    error.value = conflictMessage(reason, '保存失败')
  } finally { saving.value = false }
}

async function remove() {
  const document = props.document
  if (!document || saving.value) return
  if (!window.confirm(`删除「${document.title}」？删除后无法恢复。`)) return
  saving.value = true
  error.value = ''
  try {
    await api.deleteDocument(document.id, document.version)
    finished = true
    emit('deleted')
  } catch (reason) {
    error.value = conflictMessage(reason, '删除失败')
  } finally { saving.value = false }
}
</script>

<template>
  <div class="overlay" @click.self="close">
    <section class="modal wide document-editor" role="dialog" aria-modal="true" aria-labelledby="document-editor-title">
      <div class="modal-head"><div><h2 id="document-editor-title">{{ heading }}</h2><p>用 Markdown 记录，日后好找。</p></div><button class="close-btn" type="button" aria-label="关闭" @click="close"><Icon name="x" :size="20" /></button></div>
      <div v-if="missingImages?.length" class="editor-warning">以下图片没有上传，引用保留原样：{{ missingImages.join('、') }}</div>
      <form @submit.prevent="save">
        <div class="form-group"><label class="form-label" for="document-title">标题 *</label><input id="document-title" v-model="form.title" class="field" maxlength="100" required placeholder="给这份笔记起个标题" /></div>
        <div class="form-group"><label class="form-label" for="document-category">分类</label><input id="document-category" v-model="form.category" class="field" maxlength="30" list="document-categories" placeholder="可不填，例如：学习" /><datalist id="document-categories"><option v-for="item in categories" :key="item.name" :value="item.name" /></datalist></div>
        <div class="form-group">
          <div class="editor-toolbar">
            <div class="editor-tabs" role="tablist"><button type="button" role="tab" :aria-selected="tab === 'edit'" :class="{ active: tab === 'edit' }" @click="tab = 'edit'">编辑</button><button type="button" role="tab" :aria-selected="tab === 'preview'" :class="{ active: tab === 'preview' }" @click="tab = 'preview'">预览</button></div>
            <label class="text-btn editor-image-btn"><Icon name="plus" :size="14" />插入图片<input type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden @change="onPick" /></label>
            <span class="editor-count" :class="{ over: tooLong }">已写 {{ form.content.length }} / {{ DOCUMENT_MAX_CHARS }}</span>
          </div>
          <p v-if="imageHint" class="editor-hint">写一句说明，AI 只能通过说明找到这张图。</p>
          <textarea v-show="tab === 'edit'" id="document-content" ref="textarea" v-model="form.content" class="field fieldarea editor-content" aria-label="内容" placeholder="支持 Markdown，可以直接粘贴或拖入图片" @paste="onPaste" @drop="onDrop" @dragover.prevent />
          <div v-if="tab === 'preview'" class="editor-preview">
            <p class="editor-hint">预览里的互链显示为未解析，保存后才会生效。</p>
            <div class="markdown-content document-body" v-html="preview" />
          </div>
        </div>
        <div v-if="error" class="notice" role="alert">{{ error }}<span><button type="button" aria-label="关闭提示" @click="error = ''">×</button></span></div>
        <div class="modal-actions">
          <button v-if="mode === 'edit'" class="text-btn danger" type="button" style="margin-right:auto" :disabled="saving" @click="remove"><Icon name="trash" :size="14" />删除笔记</button>
          <button type="button" class="outline" :disabled="saving" @click="close">取消</button>
          <button type="submit" class="solid" :disabled="saving || uploading || tooLong">{{ uploading ? '图片上传中…' : saving ? '保存中…' : '保存笔记' }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
