<template>
  <section class="page" data-module="quality_check">
    <header class="page-head">
      <div>
        <h2>质量稽核管理</h2>
        <p class="page-desc">维护稽核规则并执行校验，失败行、命中表达式与阈值边界随结果一起落库，供发布闸门与详情核对。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记稽核规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出质量稽核清单</button>
        <button class="btn" data-testid="toggle-results" type="button" @click="toggleView">
          {{ view === 'rules' ? '查看稽核结果' : '返回规则清单' }}
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <template v-if="view === 'rules'">
      <form class="filter-bar" @submit.prevent="reload">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
        </label>
        <button class="btn" data-testid="query" type="submit">查询</button>
        <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`action-${row.id}-${action}`"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`execute-${row.id}`"
                @click="execute(row)"
              >执行稽核</button>
              <button
                class="link"
                type="button"
                :data-testid="`results-${row.id}`"
                @click="openDetail(row)"
              >查看结果</button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无质量稽核数据，可先登记稽核规则</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-else>
      <div class="filter-bar">
        <label class="filter-item">
          <span>规则编号</span>
          <input v-model="resultFilters.keyword" data-testid="result-keyword" placeholder="按规则编号检索" />
        </label>
        <label class="filter-item">
          <span>结果状态</span>
          <select v-model="resultFilters.status" data-testid="result-status">
            <option value="">全部</option>
            <option v-for="item in resultStatuses" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>阻断级别</span>
          <select v-model="resultFilters.blocking_level" data-testid="result-blocking">
            <option value="">全部</option>
            <option v-for="item in blockingLevels" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <button class="btn" data-testid="result-query" type="button" @click="reloadResults">查询</button>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in resultColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in resultRows"
            :key="item.规则编号"
            :data-testid="`result-row-${item.规则编号}`"
            :data-status="String(item.结果状态)"
          >
            <td v-for="column in resultColumns" :key="column">{{ item[column] ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`detail-${item.规则编号}`" @click="openDetailByRule(item.规则编号)">
                查看详情
              </button>
            </td>
          </tr>
          <tr v-if="!resultRows.length">
            <td :colspan="resultColumns.length + 1" class="empty-state">暂无质量稽核结果数据，请先从列表发起执行</td>
          </tr>
        </tbody>
      </table>
    </template>

    <section v-if="detail" class="detail-panel" data-testid="result-detail">
      <header class="detail-head">
        <div>
          <h3>{{ detail.entry?.['规则编号'] }} · 质量稽核结果详情</h3>
          <p class="page-desc">
            校验 {{ detail.entry?.['校验表名'] }}.{{ detail.entry?.['校验字段'] }}
            · 表达式「{{ detail.entry?.['校验表达式'] || '—' }}」
          </p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
      </header>

      <div v-if="detail.empty_message" class="empty-state" data-testid="detail-empty">
        {{ detail.empty_message }}
      </div>

      <div v-if="detail.unfinished_run" class="notice" data-testid="unfinished-run">
        上次稽核中断：批次 {{ detail.unfinished_run.batch }} 已完成
        {{ detail.unfinished_run.processed }} 行，重新执行会只续写未完成部分，规则配置保持完整版本。
      </div>

      <template v-if="detail.latest">
        <div class="gate-banner" :class="detail.latest.qualified ? 'passed' : 'blocked'" data-testid="latest-summary">
          <strong>{{ detail.latest.status }}</strong>
          <span>{{ detail.latest.message }}</span>
          <span class="gate-meta">
            批次 {{ detail.latest.batch || '—' }} · 阻断级别 {{ detail.latest.blocking_level }}
            · 阈值下界 {{ detail.latest.threshold_lower || '—' }}
            · 阈值上界 {{ detail.latest.threshold_upper || '—' }}
          </span>
        </div>

        <table class="data-table" v-if="detail.latest.failed_rows?.length">
          <thead>
            <tr>
              <th>行号</th>
              <th>字段值</th>
              <th>命中表达式</th>
              <th>阈值下界</th>
              <th>阈值上界</th>
              <th>失败原因</th>
              <th>阻断级别</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in detail.latest.failed_rows"
              :key="`${row.row_index}-${row.value}`"
              data-testid="failed-row"
            >
              <td>{{ row.row_index }}</td>
              <td>{{ row.value === null || row.value === '' ? '（空）' : row.value }}</td>
              <td>{{ row.expression || '—' }}</td>
              <td>{{ row.threshold_lower === '' ? '—' : row.threshold_lower }}</td>
              <td>{{ row.threshold_upper === '' ? '—' : row.threshold_upper }}</td>
              <td class="error-text">{{ row.reason }}</td>
              <td>{{ row.blocking_level }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="page-desc" data-testid="no-failed-row">本次结果没有失败行。</p>
      </template>

      <h4>批次结果</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次</th>
            <th>结果状态</th>
            <th>总行数</th>
            <th>已检查</th>
            <th>失败行数</th>
            <th>是否当前</th>
            <th>检查时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.items" :key="item.result_id + '-' + item.seq" :data-testid="`batch-${item.batch}`">
            <td>{{ item.batch || '—' }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.total }}</td>
            <td>{{ item.checked }}</td>
            <td>{{ item.failed }}</td>
            <td>{{ item['是否当前'] ? '当前' : '历史' }}</td>
            <td>{{ item.checked_at }}</td>
          </tr>
          <tr v-if="!detail.items.length">
            <td colspan="7" class="empty-state">暂无质量稽核结果数据，请先执行稽核</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记稽核规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条质量稽核记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/quality_check'
const columns = ["规则编号", "校验表名", "校验字段", "校验表达式", "阈值下界", "阈值上界", "阻断级别", "采样比例"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const resultStatuses = ["完成", "中断", "无数据", "配置错误", "暂无结果"]
const blockingLevels = ["阻断", "告警", "非阻断"]
const resultColumns = ["规则编号", "校验表名", "校验字段", "批次", "结果状态", "总行数", "失败行数", "阻断级别", "检查时间"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const view = ref<'rules' | 'results'>('rules')
const resultRows = ref<Row[]>([])
const resultFilters = ref<Record<string, string>>({ keyword: '', status: '', blocking_level: '' })
const detail = ref<Row | null>(null)
const resultRowsCount = ref(0)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const stats = computed(() => [
  { label: '稽核规则总数', value: total.value },
  { label: '待校验规则', value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: '已生成结果', value: resultRowsCount.value },
])

function toggleView() {
  view.value = view.value === 'rules' ? 'results' : 'rules'
  if (view.value === 'results') void reloadResults()
  else void reload()
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'quality_check.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('质量稽核动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核操作失败'
  }
}

async function execute(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/execute`, { method: 'POST' })
    const payload = await response.json()
    if (payload.interrupted) {
      errorMessage.value = payload.message || '稽核中断，可重试续写未完成部分'
    } else if (!response.ok) {
      errorMessage.value = payload.message || '稽核执行失败'
    }
    await openDetail(row)
    await reloadResults()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '稽核执行失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/results`)
    if (!response.ok) throw new Error('质量稽核结果详情读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核结果详情读取失败'
  }
}

async function openDetailByRule(ruleNo: string) {
  const match = rows.value.find((row) => row.规则编号 === ruleNo)
  if (match) {
    await openDetail(match)
    return
  }
  try {
    const response = await request(`${ENDPOINT}?keyword=${encodeURIComponent(ruleNo)}`)
    const payload = await response.json()
    const hit = (payload.items ?? []).find((row: Row) => row.规则编号 === ruleNo)
    if (hit) await openDetail(hit)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核结果详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('稽核规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核列表读取失败'
  }
}

async function reloadResults() {
  const query = new URLSearchParams(
    Object.entries(resultFilters.value).filter(([, value]) => value) as [string, string][],
  ).toString()
  try {
    const response = await request(`${ENDPOINT}/results?${query}`)
    if (!response.ok) throw new Error('质量稽核结果列表读取失败')
    const payload = await response.json()
    resultRows.value = payload.items ?? []
    resultRowsCount.value = resultRows.value.filter((row) => row.结果状态 !== '暂无结果').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量稽核结果列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await reloadResults()
})
</script>

<style scoped>
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.detail-panel h3 { margin: 0 0 4px; font-size: 15px; }
.detail-panel h4 { margin: 16px 0 8px; font-size: 14px; }
.gate-banner { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 10px; }
.gate-banner.blocked { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.gate-banner.passed { background: #ecfdf3; border: 1px solid #a6f4c5; color: #067647; }
.gate-meta { color: var(--muted); font-size: 12px; }
.notice { background: #fffaeb; border: 1px solid #fedf89; color: #93370d; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin-bottom: 10px; }
</style>
