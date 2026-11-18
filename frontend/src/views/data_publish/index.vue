<template>
  <section class="page" data-module="data_publish">
    <header class="page-head">
      <div>
        <h2>数据发布管理</h2>
        <p class="page-desc">确认发布前执行质量闸门：阻断状态与失败行同一条记录落库，被阻断的版本不前进。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记发布版本</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据发布清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
          <th>质量闸门</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row.quality_gate" :class="['gate-tag', row.quality_gate.status === '阻断' ? 'blocked' : 'passed']">
              {{ row.quality_gate.status }}
            </span>
            <span v-else class="gate-meta">未校验</span>
          </td>
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
            <button class="link" type="button" :data-testid="`gate-${row.id}`" @click="openGate(row)">查看闸门</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据发布数据，可先登记发布版本</td>
        </tr>
      </tbody>
    </table>

    <section v-if="gate" class="detail-panel" data-testid="publish-gate">
      <header class="detail-head">
        <div>
          <h3>{{ gate.版本编号 }} · 质量闸门</h3>
          <p class="page-desc">数据集 {{ gate.数据集名称 }} · 规则 {{ gate.rule_count ?? 0 }} 条 · 评估时间 {{ gate.evaluated_at || '—' }}</p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-gate" @click="gate = null">关闭</button>
      </header>

      <div class="gate-banner" :class="gate.status === '阻断' ? 'blocked' : 'passed'" data-testid="gate-status">
        <strong>{{ gate.status }}</strong>
        <span>{{ gate.message }}</span>
      </div>

      <table class="data-table" v-if="gate.failed_rows?.length">
        <thead>
          <tr>
            <th>规则编号</th>
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
          <tr v-for="row in gate.failed_rows" :key="`${row.rule_id}-${row.row_index}`" data-testid="gate-failed-row">
            <td>{{ row.rule_id }}</td>
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
      <p v-else class="page-desc" data-testid="gate-no-failed-row">本次闸门没有失败行。</p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记发布版本</h3>
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
      <span>共 {{ total }} 条数据发布记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/data_publish'
const columns = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
const actions = ["提交校验", "确认发布", "撤回版本"]
const statuses = ["草稿", "待校验", "已发布", "已撤回"]
const metricNames = ["发布版本总数", "待校验版本", "已发布版本"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const gate = ref<Row | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])

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
    link.download = 'data_publish.csv'
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
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '数据发布动作未生效，请稍后重试')
    }
    await reload()
    if (action === '确认发布') await openGate(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据发布操作失败'
    if (action === '确认发布') await openGate(row)
  }
}

async function openGate(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/quality-gate`)
    if (!response.ok) throw new Error('质量闸门读取失败')
    gate.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量闸门读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('发布版本列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据发布列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.gate-tag { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; }
.gate-tag.blocked { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.gate-tag.passed { background: #ecfdf3; color: #067647; border: 1px solid #a6f4c5; }
.gate-meta { color: var(--muted); font-size: 12px; }
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.detail-panel h3 { margin: 0 0 4px; font-size: 15px; }
.gate-banner { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border-radius: 6px; font-size: 13px; margin: 10px 0; }
.gate-banner.blocked { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.gate-banner.passed { background: #ecfdf3; border: 1px solid #a6f4c5; color: #067647; }
</style>
