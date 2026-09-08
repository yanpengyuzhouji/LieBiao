const notices = [];

const appState = {
  view: 'notices', tab: 'all', query: '', platform: 'all', mark: 'all', attachment: 'all', selected: new Set(), detailId: null, detailTab: 'info', importTab: 'url',
  keywordGroups: [], jobs: [], sites: [], logs: [], runs: [], dashboard: null, scheduler: null, config: null, page: 1, pageSize: 10, totalNotices: 0,
  noticeCounts: { all: 0, pending: 0, focus: 0, issues: 0, unmatched: 0, trash: 0 }
};

let backendOnline = false;
let searchTimer = null;
let updateInfo = null;
const UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000;
const activeRunWatchers = new Set();
const activeReparseWatchers = new Set();

async function watchReparse(taskId, noticeId) {
  if (activeReparseWatchers.has(taskId)) return;
  activeReparseWatchers.add(taskId);
  try {
    for (;;) {
      const task = await apiFetch(`/api/reparse-tasks/${taskId}`);
      if (!['queued', 'running'].includes(task.status)) {
        await loadBackendNotices(false);
        if (appState.detailId === noticeId) {
          const fresh = normalizeBackendNotice(await apiFetch(`/api/notices/${noticeId}`));
          const index = notices.findIndex(item => item.id === noticeId);
          if (index >= 0) notices[index] = fresh;
          renderDrawer(fresh);
        }
        showToast(task.error || ({completed:'重新解析完成',partial:'重新解析结束，部分附件仍需处理',failed:'重新解析失败'})[task.status]);
        return;
      }
      if (appState.detailId === noticeId) {
        const button = $('[data-reparse-notice]');
        if (button) button.textContent = task.status === 'queued' ? '解析已排队…' : '正在解析…';
      }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  } catch (error) { showToast(`解析状态查询失败：${error.message}`); }
  finally { activeReparseWatchers.delete(taskId); }
}

async function apiFetch(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { const payload = await response.json(); message = payload.detail || message; } catch (_) { /* keep status message */ }
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.json();
}

async function checkForUpdates() {
  if (!backendOnline) return;
  try {
    const result = await apiFetch('/api/update/check');
    if (!result.available) return;
    const alreadyNotified = updateInfo && updateInfo.latest_version === result.latest_version;
    updateInfo = result;
    const button = $('.notification-button');
    if (button) {
      button.title = `发现新版本 ${result.latest_version}`;
      button.setAttribute('aria-label', `发现新版本 ${result.latest_version}`);
      button.classList.add('has-update');
    }
    if (!alreadyNotified) showToast(`发现新版本 ${result.latest_version}，点击右上角通知按钮查看`);
  } catch (_) { /* 更新检查失败不影响本地功能 */ }
}

function normalizeBackendNotice(item) {
  return {
    ...item,
    id: String(item.id),
    date: beijingDateTime(item.date),
    opening: item.opening === '待确认' ? '待确认' : beijingDateTime(item.opening),
    platformClass: item.platformClass || item.platform || '',
    hitTone: item.hitTone || item.hits.map(() => ''),
    matchingRules: item.matchingRules || [],
    evidence: item.evidence || [],
    files: item.files || [],
    detail: item.detail || { project: item.projectName || item.title, agent: '—', location: '待确认', budget: '待确认', method: '公开招标' }
  };
}

async function loadBackendNotices(resetPage = true) {
  if (resetPage) appState.page = 1;
  const params = new URLSearchParams({
    limit: String(appState.pageSize),
    offset: String((appState.page - 1) * appState.pageSize),
    q: appState.query.trim(),
    platform: appState.platform,
    mark: appState.tab === 'pending' ? 'pending' : appState.tab === 'focus' ? 'focus' : appState.mark,
    attachment: appState.attachment,
    only_issues: String(appState.tab === 'issues')
  });
  if (appState.tab === 'unmatched') {
    params.set('only_matched', 'false');
    params.set('only_unmatched', 'true');
  } else if (appState.tab === 'trash') {
    params.set('only_matched', 'false');
    params.set('only_deleted', 'true');
  }
  try {
    const payload = await apiFetch(`/api/notices?${params.toString()}`);
    backendOnline = true;
    notices.splice(0, notices.length, ...(payload.items || []).map(normalizeBackendNotice));
    appState.totalNotices = Number(payload.total || 0);
    appState.noticeCounts = payload.category_counts || { all: appState.totalNotices, pending: 0, focus: 0, issues: 0, unmatched: 0, trash: 0 };
    const pageCount = Math.max(1, Math.ceil(appState.totalNotices / appState.pageSize));
    if (appState.page > pageCount) {
      appState.page = pageCount;
      return loadBackendNotices(false);
    }
    const count = $('#notice-nav-count');
    if (count) count.textContent = String(appState.totalNotices);
    const sync = $('#last-sync-text');
    if (sync) sync.textContent = `数据同步于 ${new Date().toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' })}（北京时间）`;
    try {
      renderCurrentView();
      await loadManagementData(appState.view);
    } catch (renderError) {
      console.error('页面渲染失败', renderError);
      showToast('页面渲染失败：' + renderError.message);
    }
  } catch (error) {
    backendOnline = false;
    notices.splice(0, notices.length);
    appState.totalNotices = 0;
    appState.noticeCounts = { all: 0, pending: 0, focus: 0, issues: 0, unmatched: 0, trash: 0 };
    const count = $('#notice-nav-count');
    if (count) count.textContent = '0';
    const sync = $('#last-sync-text');
    if (sync) sync.textContent = `后端未连接：${error.message}`;
    renderCurrentView();
  }
}

async function loadStorageSettings() {
  try {
    const storage = await apiFetch('/api/settings/storage');
    const fields = { root: $('#storage-root'), raw: $('#storage-raw'), extracted: $('#storage-extracted'), max_attachment_mb: $('#storage-max-file') };
    if (fields.root) fields.root.value = storage.root || '';
    if (fields.raw) fields.raw.value = storage.raw || '';
    if (fields.extracted) fields.extracted.value = storage.extracted || '';
    if (fields.max_attachment_mb) fields.max_attachment_mb.value = storage.max_attachment_mb || 500;
    if (fields.root) {
      const capabilities = await apiFetch('/api/settings/parsers');
      let panel = $('#parser-capabilities');
      if (!panel && document.contains(fields.root)) {
        panel = document.createElement('div'); panel.id = 'parser-capabilities'; panel.className = 'input-hint';
        fields.root.closest('.storage-grid').after(panel);
      }
      if (panel) panel.textContent = Object.entries(capabilities).map(([kind, status]) => `${kind.toUpperCase()}：${status}`).join('；');
    }
  } catch (_) {
    // The settings panel keeps local defaults when opened as a static file.
  }
}

async function loadManagementData(view = appState.view) {
  if (!backendOnline) return;
  try {
    const requests = await Promise.all([
      apiFetch('/api/keyword-groups'), apiFetch('/api/crawl-jobs'), apiFetch('/api/sites'), apiFetch('/api/logs?limit=50'), apiFetch('/api/crawl-runs?limit=20'), apiFetch('/api/dashboard'), apiFetch('/api/scheduler/status')
    ]);
    appState.keywordGroups = requests[0].items || [];
    appState.jobs = requests[1].items || [];
    appState.sites = requests[2].items || [];
    appState.logs = requests[3].items || [];
    appState.runs = requests[4].items || [];
    appState.dashboard = requests[5] || null;
    appState.scheduler = requests[6] || null;
    if (appState.view === view) renderCurrentView();
  } catch (error) {
    showToast(`管理数据加载失败：${error.message}`);
  }
}

function runStatusText(status) {
  return { queued: '已排队', running: '执行中', completed: '已完成', partial: '部分完成', failed: '失败' }[status] || status || '未知';
}

function waitForRunPoll() {
  return new Promise(resolve => setTimeout(resolve, 1000));
}

async function watchCrawlRun(runId) {
  if (activeRunWatchers.has(runId)) return;
  activeRunWatchers.add(runId);
  let lastStatus = 'queued';
  try {
    for (let attempt = 0; attempt < 1800; attempt += 1) {
      await waitForRunPoll();
      const run = await apiFetch(`/api/crawl-runs/${runId}`);
      if (run.status !== lastStatus) {
        lastStatus = run.status;
        if (run.status === 'running') showToast(`批次 #${runId} 已开始采集`);
      }
      if (attempt % 3 === 0) await loadManagementData(appState.view);
      if (['completed', 'partial', 'failed'].includes(run.status)) {
        await loadBackendNotices();
        await loadManagementData(appState.view);
        const resultText = run.status === 'completed' ? `命中入库 ${run.detail_success || 0} 条，未命中过滤 ${run.filtered_count || 0} 条` : run.status === 'partial' ? `命中入库 ${run.detail_success || 0} 条，未命中过滤 ${run.filtered_count || 0} 条，失败 ${run.failed_count || 0} 条` : (run.failure_reason || '请查看系统日志');
        showToast(`批次 #${runId}${runStatusText(run.status)}：${resultText}`);
        return;
      }
    }
    showToast(`批次 #${runId}仍在执行，可在采集任务或系统日志查看`);
  } catch (error) {
    showToast(`批次 #${runId}状态查询失败：${error.message}`);
  } finally {
    activeRunWatchers.delete(runId);
  }
}

async function uploadSelectedFile(input, endpoint) {
  if (!input.files || !input.files[0]) return;
  if (!backendOnline) { showToast('请通过 run_app.py 启动后端再上传文件'); return; }
  const form = new FormData();
  form.append('file', input.files[0]);
  try {
    const result = await apiFetch(endpoint, { method: 'POST', body: form });
    closeImport();
    showToast(result.message || '文件已导入并开始解析');
    await loadBackendNotices();
  } catch (error) {
    showToast(error.message);
  } finally {
    input.value = '';
  }
}

async function persistMark(ids, mark) {
  const numericIds = ids.filter(id => /^\d+$/.test(String(id)));
  if (backendOnline && numericIds.length) {
    await Promise.all(numericIds.map(id => apiFetch(`/api/notices/${id}/mark`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mark }) })));
  }
  ids.forEach(id => {
    const item = notices.find(notice => String(notice.id) === String(id));
    if (item) { item.mark = mark; item.markText = { focus: '重点关注', processed: '已处理', irrelevant: '不相关', supplement: '待补充', pending: '待确认', relevant: '相关' }[mark] || mark; }
  });
}

async function persistDelete(ids) {
  const numericIds = ids.filter(id => /^\d+$/.test(String(id)));
  if (backendOnline && numericIds.length) {
    await Promise.all(numericIds.map(id => apiFetch(`/api/notices/${id}`, { method: 'DELETE' })));
  }
  numericIds.forEach(id => {
    const index = notices.findIndex(notice => String(notice.id) === String(id));
    if (index >= 0) notices.splice(index, 1);
  });
}

async function persistRestore(ids) {
  const numericIds = ids.filter(id => /^\d+$/.test(String(id)));
  if (backendOnline && numericIds.length) {
    await Promise.all(numericIds.map(id => apiFetch(`/api/notices/${id}/restore`, { method: 'POST' })));
  }
}

const viewMeta = {
  overview: ['采集总览', '统计数据以后端数据库为准，今日新增按首次入库时间计算。'],
  notices: ['公告库', '汇总已接入平台的最新招标信息，快速确认与跟进。'],
  keywords: ['关键词组', '用可解释的规则筛选目标项目，命中结果始终保留证据。'],
  jobs: ['采集任务', '管理采集周期、增量窗口和任务运行状态。'],
  platforms: ['平台与账号', '查看平台连通性、授权会话和人工续期状态。'],
  imports: ['导入中心', '通过 URL、Excel 台账或附件文件补充公告数据。'],
  logs: ['系统日志', '追踪采集、解析、重试和会话验证的运行记录。']
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
const iconFor = type => ({ zip: '▣', pdf: '▤', xlsx: '▦', html: '▧', docx: '▤' }[type] || '□');
function beijingDateTime(value, clockOnly = false) {
  if (!value || value === '—' || value === '待确认') return value || '—';
  const text = String(value).trim();
  const parsed = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : text.replace(' ', 'T') + '+08:00');
  if (Number.isNaN(parsed.getTime())) return text;
  const parts = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).formatToParts(parsed).reduce((result, part) => { result[part.type] = part.value; return result; }, {});
  return clockOnly ? `${parts.hour}:${parts.minute}` : `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function getFilteredNotices() {
  if (backendOnline) return notices;
  return [];
}

function paginationPages(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  return Array.from(new Set([1, 2, current - 1, current, current + 1, total - 1, total]))
    .filter(page => page >= 1 && page <= total)
    .sort((a, b) => a - b);
}

function statCard(icon, label, value, suffix, foot, tone = '') {
  return `<article class="stat-card"><div class="stat-label"><span>${label}</span><span class="stat-icon">${icon}</span></div><div class="stat-value">${value}<small class="${tone}">${suffix}</small></div><div class="stat-foot">${foot}</div></article>`;
}

function renderStats() {
  if (backendOnline) {
    const counts = (appState.dashboard && appState.dashboard.counts) || {};
    return `<div class="stats-grid">${statCard('▤', '今日新增', counts.today_new || 0, '', '按今日首次入库统计')}${statCard('◌', '待确认', counts.pending || 0, '', '需要人工判断', 'trend-warn')}${statCard('★', '重点关注', counts.focus || 0, '', '业务标记统计', 'trend-up')}${statCard('!', '解析异常', counts.issues || 0, '', counts.issues ? '需处理' : '当前无异常', 'trend-danger')}</div>`;
  }
  const pending = notices.filter(item => item.mark === 'pending').length;
  const focus = notices.filter(item => item.mark === 'focus').length;
  const issues = notices.filter(item => item.status === 'failed' || item.status === 'partial').length;
  return `<div class="stats-grid">
    ${statCard('▤', '已写入采集信息', notices.length, '', '原型样例 · 6 条可查看', 'trend-up')}
    ${statCard('◌', '待确认', pending, '', '需要人工判断', 'trend-warn')}
    ${statCard('★', '重点关注', focus, '', '业务标记统计', 'trend-up')}
    ${statCard('!', '解析异常', issues, '', issues ? '需处理' : '当前无异常', 'trend-danger')}
  </div>`;
}

function noticeRow(notice) {
  const hits = notice.hits.map((hit, index) => `<span class="hit-tag ${notice.hitTone[index] || ''}">${esc(hit)}</span>`).join('');
  const opening = notice.opening === '待确认' ? `<span class="pending">${notice.openingText}</span>` : `<span class="${notice.openingText.includes('天') && Number.parseInt(notice.openingText) < 16 ? 'opening-soon' : ''}">${notice.openingText}</span>`;
  return `<tr data-row-id="${notice.id}">
    <td><input class="checkbox row-checkbox" type="checkbox" data-select-id="${notice.id}" ${appState.selected.has(notice.id) ? 'checked' : ''} aria-label="选择公告" /></td>
    <td class="notice-cell"><span class="notice-title" data-open-detail="${notice.id}">${esc(notice.title)}</span><div class="notice-meta"><span class="source-chip ${notice.platformClass}">${notice.platformName}</span><span>${notice.type}</span><span>·</span><span>${notice.number}</span></div></td>
    <td><div class="time-cell"><strong>${esc(notice.date.slice(0, 10))}</strong><span>${esc(notice.date.slice(11))}</span></div></td>
    <td><div class="time-cell"><strong>${notice.opening === '待确认' ? '待确认' : esc(notice.opening.slice(5, 16))}</strong>${opening}</div></td>
    <td class="unit-cell">${esc(notice.unit)}</td>
    <td class="summary-cell" title="${esc(notice.summary)}">${esc(notice.summary)}</td>
    <td><div class="hit-list">${hits}</div><div class="notice-meta" style="margin-top:5px">${esc(notice.bestHit)}</div></td>
    <td><div class="attachment-cell"><span class="attachment-icon">⌕</span><strong>${notice.attachments}</strong><span>个</span><small title="关键文件需人工标记">/ ${notice.keyFiles} 关键</small></div></td>
    <td><span class="status-tag status-${notice.status}">${notice.statusText}</span><div style="margin-top:5px"><span class="mark-tag mark-${notice.mark}">${notice.markText}</span></div></td>
    <td><div class="row-actions"><button class="row-open" data-open-detail="${notice.id}" aria-label="查看详情">↗</button>${notice.isDeleted ? `<button class="batch-action" data-restore-notice="${notice.id}">恢复</button>` : '<button class="row-more" aria-label="更多操作">···</button>'}</div></td>
  </tr>`;
}

function renderNoticeTable() {
  const filtered = getFilteredNotices();
  const total = backendOnline ? appState.totalNotices : filtered.length;
  const pageCount = Math.max(1, Math.ceil(total / appState.pageSize));
  appState.page = Math.min(Math.max(1, appState.page), pageCount);
  const globalStart = (appState.page - 1) * appState.pageSize;
  const pageItems = backendOnline ? filtered : filtered.slice(globalStart, globalStart + appState.pageSize);
  const body = $('#notice-tbody');
  if (!body) return;
  body.innerHTML = pageItems.length ? pageItems.map(noticeRow).join('') : `<tr><td colspan="10"><div class="empty-state"><div><div class="empty-icon">⌕</div><strong>没有找到匹配的公告</strong><p>试试调整筛选条件或清空搜索关键词。</p><button class="button button-secondary" data-reset-filters>清空筛选</button></div></div></td></tr>`;
  const count = $('#result-count');
  if (count) count.textContent = total ? `${globalStart + 1}-${Math.min(globalStart + pageItems.length, total)} / ${total}` : '0';
  const pagination = $('#pagination-controls');
  if (pagination) {
    let previous = 0;
    const buttons = paginationPages(appState.page, pageCount).map(page => {
      const gap = previous && page - previous > 1 ? '<span class="page-gap">…</span>' : '';
      previous = page;
      return `${gap}<button class="page-button ${page === appState.page ? 'active' : ''}" data-page="${page}">${page}</button>`;
    }).join('');
    pagination.innerHTML = `<button class="page-button" data-page="${appState.page - 1}" ${appState.page <= 1 ? 'disabled' : ''}>‹</button>${buttons}<button class="page-button" data-page="${appState.page + 1}" ${appState.page >= pageCount ? 'disabled' : ''}>›</button>`;
  }
  const selectedCount = $('#selected-count');
  if (selectedCount) selectedCount.textContent = appState.selected.size;
  const batch = $('#batch-bar');
  if (batch) batch.classList.toggle('visible', appState.selected.size > 0);
  const selectAll = $('#select-all');
  if (selectAll) selectAll.checked = pageItems.length > 0 && pageItems.every(item => appState.selected.has(item.id));
}

function renderNotices() {
  const counts = backendOnline ? appState.noticeCounts : {
    all: notices.length,
    pending: notices.filter(item => item.mark === 'pending').length,
    focus: notices.filter(item => item.mark === 'focus').length,
    issues: notices.filter(item => item.status === 'failed' || item.status === 'partial').length
  };
  const total = counts.all;
  const pending = counts.pending;
  const focus = counts.focus;
  const issues = counts.issues;
  const unmatched = counts.unmatched || 0;
  const trash = counts.trash || 0;
  return `${renderStats()}${renderDataStrip()}<div class="toolbar-card">
    <div class="filter-tabs">
      <button class="filter-tab ${appState.tab === 'all' ? 'active' : ''}" data-filter-tab="all">全部 <span class="tab-number">${total}</span></button>
      <button class="filter-tab ${appState.tab === 'pending' ? 'active' : ''}" data-filter-tab="pending">待确认 <span class="tab-number">${pending}</span></button>
      <button class="filter-tab ${appState.tab === 'focus' ? 'active' : ''}" data-filter-tab="focus">重点关注 <span class="tab-number">${focus}</span></button>
      <button class="filter-tab ${appState.tab === 'issues' ? 'active' : ''}" data-filter-tab="issues">解析异常 <span class="tab-number">${issues}</span></button>
      <button class="filter-tab ${appState.tab === 'unmatched' ? 'active' : ''}" data-filter-tab="unmatched" title="未命中关键词且存在解析异常或待处理附件">可能漏匹配 <span class="tab-number">${unmatched}</span></button>
      <button class="filter-tab ${appState.tab === 'trash' ? 'active' : ''}" data-filter-tab="trash">回收站 <span class="tab-number">${trash}</span></button>
    </div>
    <div class="filter-row">
      <label class="search-box"><span class="search-icon">⌕</span><input id="notice-search" value="${esc(appState.query)}" placeholder="搜索标题、项目编号或需求单位" /></label>
      <label class="select-wrap platform"><select id="platform-filter"><option value="all">全部平台</option>${appState.sites.map(site => `<option value="${esc(site.code)}" ${appState.platform === site.code ? 'selected' : ''}>${esc(site.name)}</option>`).join('')}</select></label>
      <label class="select-wrap"><select id="mark-filter"><option value="all">业务标记</option><option value="pending" ${appState.mark === 'pending' ? 'selected' : ''}>待确认</option><option value="relevant" ${appState.mark === 'relevant' ? 'selected' : ''}>相关</option><option value="focus" ${appState.mark === 'focus' ? 'selected' : ''}>重点关注</option><option value="processed" ${appState.mark === 'processed' ? 'selected' : ''}>已处理</option></select></label>
      <label class="select-wrap"><select id="attachment-filter"><option value="all">附件情况</option><option value="yes" ${appState.attachment === 'yes' ? 'selected' : ''}>有附件</option><option value="no" ${appState.attachment === 'no' ? 'selected' : ''}>无附件</option></select></label>
      <button class="filter-more"><span>＋</span>更多筛选</button>
    </div>
    <div class="batch-bar ${appState.selected.size ? 'visible' : ''}" id="batch-bar"><span>已选择 <strong id="selected-count">${appState.selected.size}</strong> 条</span><div class="batch-actions">${appState.tab === 'trash' ? '<button class="batch-action" data-batch-restore>批量恢复</button>' : '<button class="batch-action" data-batch-mark="relevant">标记为相关</button><button class="batch-action" data-batch-mark="focus">设为重点</button><button class="batch-action danger" data-batch-delete>批量删除</button>'}</div></div>
  </div>
  <div class="table-card"><div class="table-scroll"><table><thead><tr><th><input class="checkbox" id="select-all" type="checkbox" aria-label="全选" /></th><th style="width:27%">公告标题 / 来源</th><th>发布时间</th><th>开标时间</th><th>需求单位</th><th style="width:18%">项目摘要</th><th>关键词命中</th><th>附件</th><th>状态 / 标记</th><th></th></tr></thead><tbody id="notice-tbody"></tbody></table></div><div class="table-footer"><span>显示 <strong id="result-count">0</strong> 条结果，共 ${total} 条公告（每页 10 条）</span><div class="pagination" id="pagination-controls"></div></div></div>`;
}

function collectionSnapshot() {
  return {
    hitCount: notices.reduce((sum, item) => sum + (item.hits || []).length, 0),
    fileCount: notices.reduce((sum, item) => sum + (item.attachments || 0), 0),
  };
}

function renderDataStrip() {
  const snapshot = collectionSnapshot();
  return `<div class="data-strip ${backendOnline ? 'is-live' : 'is-demo'}"><div class="data-strip-main"><span class="data-kicker">COLLECTION FEED</span><strong>${backendOnline ? '本地数据库实时采集' : '原型演示数据已写入'}</strong><span>${backendOnline ? '当前页面已切换为后端返回的有效公告' : '已预置 6 条公告，可直接点击查看详情与关键词证据'}</span></div><div class="data-strip-stats"><span><b>${notices.length}</b> 条公告</span><span><b>${snapshot.hitCount}</b> 个命中</span><span><b>${snapshot.fileCount}</b> 个附件</span></div></div>`;
}

function renderRecentCollection(title = '最近采集信息', subtitle = '已写入公告库的最新记录', limit = 4) {
  const items = notices.slice(0, limit);
  return `<section class="panel-card collection-panel"><div class="panel-head"><div><h3>${title}</h3><p>${subtitle}</p></div><button class="filter-more" data-view-link="notices">查看公告库 →</button></div><div class="collection-list">${items.length ? items.map(notice => `<button type="button" class="collection-item" data-open-detail="${notice.id}"><span class="collection-source ${notice.platformClass}">${esc(notice.platformName)}</span><span class="collection-item-copy"><strong>${esc(notice.title)}</strong><span>${esc(notice.unit)} · ${esc(String(notice.date || '').slice(0, 16))}</span></span><span class="collection-item-right"><span class="status-tag status-${notice.status}">${esc(notice.statusText)}</span><span class="collection-arrow">↗</span></span></button>`).join('') : '<div class="empty-state">暂无采集信息</div>'}</div></section>`;
}

function renderOverview() {
  const platformDefinitions = appState.sites.map(site => ({ code: site.code, name: site.name, tone: site.code === 'csg' ? 'green' : ['sgcc', 'epec', 'cdt'].includes(site.code) ? 'orange' : '' }));
  const platformCounts = platformDefinitions.map(platform => ({ ...platform, count: notices.filter(item => item.platform === platform.code).length }));
  const maxCount = Math.max(1, ...platformCounts.map(item => item.count));
  const platformRows = platformCounts.map(platform => `<div class="health-row"><span class="platform-logo ${platform.tone}">${platform.name.slice(0, 1)}</span><span class="health-name">${platform.name}</span><span class="health-bar"><span style="width:${Math.round(platform.count / maxCount * 100)}%"></span></span><strong>${platform.count} 条</strong></div>`).join('');
  return `${renderStats()}${renderDataStrip()}<div class="overview-grid"><section class="panel-card chart-card"><div class="panel-head"><div><h3>采集趋势</h3><p>原型数据按平台分布展示，可从公告库继续筛选</p></div><span class="status-tag status-done">数据就绪</span></div><div class="chart"><svg viewBox="0 0 700 180" preserveAspectRatio="none" aria-label="采集趋势图"><g stroke="#eef1f5" stroke-width="1"><line x1="0" y1="20" x2="700" y2="20"/><line x1="0" y1="65" x2="700" y2="65"/><line x1="0" y1="110" x2="700" y2="110"/><line x1="0" y1="155" x2="700" y2="155"/></g><path d="M0,125 C76,118 95,100 170,111 S273,91 352,77 S465,96 540,58 S631,54 700,31 L700,180 L0,180Z" fill="rgba(91,108,255,.07)"/><path d="M0,125 C76,118 95,100 170,111 S273,91 352,77 S465,96 540,58 S631,54 700,31" fill="none" stroke="#5b6cff" stroke-width="2.5"/><path d="M0,145 C76,139 95,128 170,136 S273,120 352,111 S465,127 540,96 S631,102 700,78" fill="none" stroke="#43c9a1" stroke-width="2" stroke-dasharray="4 4"/><g fill="#5b6cff"><circle cx="170" cy="111" r="3"/><circle cx="352" cy="77" r="3"/><circle cx="540" cy="58" r="3"/><circle cx="700" cy="31" r="3"/></g></svg></div><div class="chart-legend"><span class="legend-item"><i class="legend-dot blue"></i>发现公告</span><span class="legend-item"><i class="legend-dot mint"></i>解析完成</span><span class="legend-item"><i class="legend-dot yellow"></i>待人工确认</span></div></section><section class="panel-card"><div class="panel-head"><div><h3>平台采集分布</h3><p>已写入信息按来源平台统计</p></div><span class="status-tag status-done">${notices.length} 条</span></div><div class="panel-body health-list">${platformRows}<div class="overview-note">点击下方采集信息可打开公告详情，查看附件和命中证据。</div></div></section></div><div style="height:16px"></div>${renderRecentCollection()}`;
}

function renderLiveKeywords() {
  const groups = appState.keywordGroups;
  const ruleValue = group => {
    const any = group.include_any || [], all = group.include_all || [], phrases = group.phrases || [], exclude = group.exclude || [];
    return [...(any.length ? [`(${any.join(' OR ')})`] : []), ...(all.length ? [`(${all.join(' AND ')})`] : []), ...(phrases.length ? [`短语：${phrases.join(' / ')}`] : []), ...(exclude.length ? [`NOT ${exclude.join(' NOT ')}`] : [])].join(' AND ') || '尚未配置规则';
  };
  return `<section class="panel-card"><div class="panel-head"><div><h3>已配置关键词组 <span style="color:#9da7b7;font-family:'DM Mono';font-size:10px">${groups.length}</span></h3><p>优先级数字越大越先匹配；停用后不参与后续解析。</p></div><button class="button button-primary" data-new-keyword>＋ 新建关键词组</button></div><div class="panel-body">${groups.length ? groups.map(group => `<div class="rule-card"><div class="rule-head"><h4>${esc(group.name)}</h4><small>${group.enabled ? '启用中' : '已停用'}</small></div><div class="rule-code">${esc(ruleValue(group))}</div><div class="rule-meta"><span>${esc((group.scopes || []).join(' + '))}</span><span>优先级 ${group.priority || 0}</span><span class="status-tag ${group.enabled ? 'status-done' : 'status-processing'}">${group.enabled ? '启用' : '停用'}</span><button class="batch-action" data-toggle-keyword="${group.id}">${group.enabled ? '停用' : '启用'}</button><button class="batch-action" data-edit-keyword="${group.id}">编辑</button><button class="batch-action danger" data-delete-keyword="${group.id}">删除</button></div></div>`).join('<div style="height:10px"></div>') : '<div class="empty-state">暂无关键词组，请点击新建。</div>'}</div></section>`;
}

function renderKeywords() {
  if (backendOnline) return renderLiveKeywords();
  return `<section class="panel-card"><div class="panel-head"><div><h3>已配置关键词组</h3><p>后端未连接，暂不显示关键词组。</p></div></div><div class="panel-body"><div class="empty-state">无法读取后端数据，请先启动后端服务后刷新页面。</div></div></section>`;
}

function renderLiveJobs() {
  const jobs = appState.jobs;
  const scheduler = appState.scheduler || {};
  const latestRuns = {};
  appState.runs.forEach(run => { if (!latestRuns[run.job_id]) latestRuns[run.job_id] = run; });
  return `<section class="panel-card"><div class="panel-head"><div><h3>采集任务 <span style="color:#9da7b7;font-family:'DM Mono';font-size:10px">${jobs.length}</span></h3><p>可编辑采集窗口、关键词组、数量限制与执行计划。</p></div><div style="display:flex;align-items:center;gap:10px"><span class="status-tag ${scheduler.enabled ? 'status-done' : 'status-partial'}">${scheduler.enabled ? '自动调度运行中' : '自动调度未运行'}</span><button class="button button-primary" data-new-job>＋ 新建任务</button></div></div><div class="panel-body" style="padding-top:5px"><div class="config-list">${jobs.length ? jobs.map((job, index) => { const run = latestRuns[job.id]; const active = run && ['queued','running'].includes(run.status); const runSummary = run ? `批次 #${run.id} · ${runStatusText(run.status)} · ${beijingDateTime(run.finished_at || run.started_at || run.created_at)}` : '暂无运行记录'; return `<div class="config-row"><div class="config-leading ${index % 2 ? 'mint' : ''}">${job.enabled ? '◷' : '‖'}</div><div class="config-copy"><strong>${esc(job.name)}</strong><span>${esc(job.site_name || '')} · ${esc(job.keyword_group_name || '全部启用规则')} · ${esc(job.schedule_text)} · ${job.max_pages} 页/${job.max_notices} 条</span><span style="color:${active ? '#5d6ae0' : '#9da7b7'}">${esc(runSummary)}</span></div><span class="status-tag ${job.enabled ? 'status-done' : 'status-processing'}">${job.enabled ? '已启用' : '已停用'}</span><div class="row-actions-inline"><button class="batch-action" data-run-job="${job.id}" ${job.enabled && !active ? '' : 'disabled'}>${active ? '采集中' : '运行'}</button><button class="batch-action" data-edit-job="${job.id}">编辑</button><button class="batch-action" data-toggle-job="${job.id}">${job.enabled ? '停用' : '启用'}</button><button class="batch-action danger" data-delete-job="${job.id}">删除</button></div></div>`; }).join('') : '<div class="empty-state">暂无任务，请点击新建。</div>'}</div></div></section>`;
}

function renderJobs() {
  if (backendOnline) return renderLiveJobs();
  const jobs = [
    ['全平台 · 日常增量采集', '3 个平台 · 每 30 分钟', '运行中', '18 / 60', 'running'],
    ['南方电网 · 招标公告回溯', '首次回溯 30 天 · 09:00', '已完成', '128 / 128', 'done'],
    ['国网交易专区 · 重点分类', '每 2 小时 · 需人工会话', '等待续期', '—', 'partial'],
    ['ECP2.0 · 配网物资专项', '工作日 08:30', '已暂停', '—', 'paused']
  ];
  return `<section class="panel-card"><div class="panel-head"><div><h3>采集任务 <span style="color:#9da7b7;font-family:'DM Mono';font-size:10px">4</span></h3><p>任务会先发现元数据，再异步处理附件与解析。</p></div><button class="button button-primary" data-toast="采集任务创建入口已准备">＋ 新建任务</button></div><div class="panel-body" style="padding-top:5px"><div class="config-list">${jobs.map((job, index) => `<div class="config-row"><div class="config-leading ${index === 1 ? 'mint' : index === 2 ? 'yellow' : ''}">${index === 0 ? '◷' : index === 1 ? '✓' : index === 2 ? '!' : 'Ⅱ'}</div><div class="config-copy"><strong>${job[0]}</strong><span>${job[1]}</span></div><span class="status-tag status-${job[4] === 'paused' ? 'processing' : job[4]}">${job[2]}</span><span style="min-width:52px;color:#69758e;font-family:'DM Mono';font-size:10px;text-align:right">${job[3]}</span><button class="row-more" data-toast="任务更多操作">···</button></div>`).join('')}</div></div></section><div style="height:16px"></div><div class="section-grid"><section class="panel-card"><div class="panel-head"><div><h3>运行策略</h3><p>当前工作区的采集安全边界</p></div></div><div class="panel-body"><div class="config-list"><div class="config-row"><div class="config-leading mint">↻</div><div class="config-copy"><strong>失败自动重试</strong><span>最多 3 次，指数退避</span></div><span style="color:#168b6c;font-size:10px">已启用</span></div><div class="config-row"><div class="config-leading yellow">⌁</div><div class="config-copy"><strong>请求间隔</strong><span>平台级限速，避免触发风控</span></div><span style="color:#69758e;font-family:'DM Mono';font-size:10px">1.5 秒</span></div><div class="config-row"><div class="config-leading">▣</div><div class="config-copy"><strong>附件大小上限</strong><span>单文件 / 单压缩包</span></div><span style="color:#69758e;font-family:'DM Mono';font-size:10px">500 MB</span></div></div></div></section><section class="panel-card"><div class="panel-head"><div><h3>最近批次</h3><p>今日 09:42 自动运行</p></div><span class="status-tag status-done">成功</span></div><div class="panel-body"><div style="font-family:'DM Mono';font-size:26px;color:#1e2c49">02:18</div><div style="margin-top:6px;color:#9ca6b7;font-size:10px">处理 60 条公告 · 下载 83 个文件</div><div style="height:10px;margin-top:17px;border-radius:10px;background:#eafaf5;overflow:hidden"><div style="width:82%;height:100%;border-radius:inherit;background:#43c9a1"></div></div><div style="display:flex;justify-content:space-between;margin-top:7px;color:#9ca6b7;font-size:9px"><span>处理进度</span><strong style="color:#168b6c">82%</strong></div></div></section></div>`;
}

function renderLivePlatforms() {
  return `<section class="panel-card"><div class="panel-head"><div><h3>平台与账号 <span style="color:#9da7b7;font-family:'DM Mono';font-size:10px">${appState.sites.length}</span></h3><p>遇到平台安全验证时，可打开专用窗口人工完成验证，再回到这里保存会话。</p></div></div><div class="panel-body"><table class="platform-table"><thead><tr><th>平台</th><th>采集模式</th><th>人工会话</th><th>连通状态</th><th>最近检查（北京时间）</th><th>操作</th></tr></thead><tbody>${appState.sites.map(site => { const account = (site.accounts || []).find(item => item.enabled && item.session_status === 'verified'); return `<tr><td><div class="platform-name"><span class="platform-logo ${site.code === 'csg' ? 'green' : ['sgcc', 'epec', 'cdt'].includes(site.code) ? 'orange' : ''}">${esc(site.name.slice(0, 1))}</span>${esc(site.name)}</div></td><td>公开公告（免登录）</td><td><span class="account-status" title="${esc(account ? account.status_reason || '' : '尚未保存人工验证会话')}">${account ? '已验证' : '未验证'}</span></td><td><span class="account-status" title="${esc(site.health_message || '')}">${esc(site.health_status === 'healthy' ? '正常' : site.health_status === 'unhealthy' ? '异常' : '未检查')}</span></td><td>${esc(beijingDateTime(site.last_checked_at))}</td><td><div class="row-actions-inline"><button class="batch-action" data-health-site="${site.id}">健康检查</button><button class="batch-action" data-open-verification="${site.id}">打开人工验证</button><button class="batch-action" data-complete-verification="${site.id}">验证完成</button></div></td></tr>`; }).join('')}</tbody></table></div></section>`;
}

function renderPlatforms() {
  if (backendOnline) return renderLivePlatforms();
  return `<section class="panel-card"><div class="panel-head"><div><h3>平台与账号 <span style="color:#9da7b7;font-family:'DM Mono';font-size:10px">3</span></h3><p>账号凭据仅保存引用，敏感信息不会进入业务日志。</p></div><button class="button button-primary" data-toast="平台账号配置入口已准备">＋ 添加账号</button></div><div class="panel-body"><table class="platform-table"><thead><tr><th>平台</th><th>账号别名</th><th>登录方式</th><th>会话状态</th><th>最近验证</th><th>操作</th></tr></thead><tbody><tr><td><div class="platform-name"><span class="platform-logo green">南</span>中国南方电网供应链统一服务平台</div></td><td>南网公开采集账号</td><td>公开页面</td><td><span class="account-status">正常</span></td><td>今天 09:40</td><td><button class="batch-action" data-toast="开始健康检查">健康检查</button></td></tr><tr><td><div class="platform-name"><span class="platform-logo">E</span>国网电子商务平台 ECP2.0</div></td><td>ECP 商务账号 01</td><td>人工验证码 + Cookie</td><td><span class="account-status">正常</span></td><td>今天 08:30</td><td><button class="batch-action" data-toast="开始会话验证">验证会话</button></td></tr><tr><td><div class="platform-name"><span class="platform-logo orange">国</span>国网电子交易专区</div></td><td>山东项目账号</td><td>人工续期</td><td><span class="account-status" style="color:#c38828">即将过期</span></td><td>昨天 18:12</td><td><button class="batch-action" data-toast="已创建人工续期提醒">去续期</button></td></tr></tbody></table></div></section><div style="height:16px"></div><div class="section-grid"><section class="panel-card"><div class="panel-head"><div><h3>人工介入边界</h3><p>遇到访问控制时，任务会暂停下载并保留已采集结果。</p></div></div><div class="panel-body"><div class="config-list"><div class="config-row"><div class="config-leading yellow">⌁</div><div class="config-copy"><strong>验证码 / 短信验证</strong><span>人工完成后复用授权会话</span></div><span class="status-tag status-partial">需人工</span></div><div class="config-row"><div class="config-leading red">▣</div><div class="config-copy"><strong>CA 电子钥匙</strong><span>不设计绕过机制，需在授权环境运行</span></div><span class="status-tag status-processing">待验证</span></div></div></div></section><section class="panel-card"><div class="panel-head"><div><h3>采集边界</h3><p>当前环境的安全设置</p></div></div><div class="panel-body"><div class="rule-meta"><span>公开接口优先</span><span>受控解压</span><span>原文件留存</span></div><div style="margin-top:15px;color:#8995aa;font-size:10px;line-height:1.7">仅采集公开页面和已授权账号可访问的文件，不绕过验证码、短信或 CA 访问控制。</div></div></section></div>`;
}

function renderImports() {
  return `<div class="section-grid"><section class="panel-card"><div class="panel-head"><div><h3>快速导入</h3><p>所有导入均会先预检，再确认入库。</p></div></div><div class="panel-body"><div class="dropzone" style="min-height:190px"><div class="drop-icon">⇧</div><strong>拖拽文件到这里，或选择导入方式</strong><span>支持 URL、Excel、PDF、Office 和 ZIP / RAR / 7z</span><button class="button button-primary" style="margin-top:16px" id="open-import-secondary">打开导入窗口</button></div></div></section><section class="panel-card"><div class="panel-head"><div><h3>导入说明</h3><p>V1 数据接入约定</p></div></div><div class="panel-body"><div class="config-list"><div class="config-row"><div class="config-leading">01</div><div class="config-copy"><strong>URL 导入</strong><span>单个或批量粘贴公告详情链接</span></div></div><div class="config-row"><div class="config-leading mint">02</div><div class="config-copy"><strong>Excel 台账</strong><span>来源标记为「人工导入」</span></div></div><div class="config-row"><div class="config-leading yellow">03</div><div class="config-copy"><strong>文件解析</strong><span>可选填原公告来源 URL</span></div></div></div></div></section></div><div style="height:16px"></div><section class="panel-card"><div class="panel-head"><div><h3>本地文件目录</h3><p>原始文件、解析结果和日志均保存在本机，可随时备份。</p></div><button class="button button-secondary" id="save-storage">保存目录配置</button></div><div class="panel-body"><div class="storage-grid"><label class="field-label">数据根目录<input class="input" id="storage-root" value="data" placeholder="例如 C:\\ProgramData\\LieBiao\\data" /></label><label class="field-label">附件大小上限<input class="input" id="storage-max-file" type="number" value="500" min="1" /> </label><label class="field-label">原始附件目录<input class="input" id="storage-raw" value="data/raw" readonly /></label><label class="field-label">解析文件目录<input class="input" id="storage-extracted" value="data/extracted" readonly /></label></div><div class="input-hint"><span class="hint-icon">i</span>数据库仅保存文件路径、哈希和解析状态；原始附件不会写入数据库。</div></div></section><div style="height:16px"></div><section class="panel-card"><div class="panel-head"><div><h3>最近导入</h3><p>共 12 个导入批次</p></div><button class="filter-more">查看模板 ↓</button></div><div class="panel-body" style="padding-top:5px"><div class="config-list"><div class="config-row"><div class="config-leading mint">✓</div><div class="config-copy"><strong>2026-09-03_历史台账.xlsx</strong><span>人工导入 · 46 条记录</span></div><span class="status-tag status-done">已完成</span><span style="color:#9ca6b7;font-size:9px">09:11</span></div><div class="config-row"><div class="config-leading mint">✓</div><div class="config-copy"><strong>批量公告链接 · 9 条</strong><span>URL 导入 · 新增 7 条，疑似重复 2 条</span></div><span class="status-tag status-done">已完成</span><span style="color:#9ca6b7;font-size:9px">昨天 16:20</span></div><div class="config-row"><div class="config-leading yellow">!</div><div class="config-copy"><strong>储能项目附件包.zip</strong><span>文件解析 · 1 个文件需要人工密码</span></div><span class="status-tag status-partial">部分完成</span><span style="color:#9ca6b7;font-size:9px">8 月 30 日</span></div></div></div></section>`;
}

function renderLiveLogs() {
  const label = type => ({'crawl.start':'采集任务','crawl.finish':'采集批次','notice.ingest':'公告采集','attachment.download':'附件下载','notice.reparse':'重新解析','crawl.policy_filter':'策略过滤','crawl.retry':'采集重试'}[type] || type);
  return `<section class="panel-card"><div class="panel-head"><div><h3>运行日志</h3><p>以后端记录为准，显示最近 ${appState.logs.length} 条（北京时间）。</p></div><button class="button button-secondary" data-refresh-management>刷新</button></div><div class="log-list">${appState.logs.length ? appState.logs.map(log => `<div class="log-row"><span class="log-time">${esc(beijingDateTime(log.created_at))}</span><span class="log-type">${esc(label(log.event_type))}</span><span class="log-message">${esc(log.message)}</span><span class="log-result ${log.level === 'WARNING' ? 'warning' : log.level === 'ERROR' ? 'error' : ''}">${esc(log.level || '成功')}</span></div>`).join('') : '<div class="empty-state">暂无日志</div>'}</div></section>`;
}

function renderLogs() {
  if (backendOnline) return renderLiveLogs();
  const logs = [
    ['09:42:18', '采集批次', '全平台日常增量采集完成：发现 18 条，解析完成 12 条', '成功', ''],
    ['09:40:03', '附件解析', '广东电网储能项目：ZIP 内登记 3 个支持格式文件', '成功', ''],
    ['09:36:27', '会话验证', '国网交易专区账号「山东项目账号」将在 8 小时后失效', '待处理', 'warning'],
    ['09:21:44', '公告采集', '国网河北输变电项目附件下载请求超时', '失败', 'error'],
    ['09:18:12', '关键词匹配', '命中「储能与新能源」规则组，新增 7 条证据记录', '成功', ''],
    ['08:30:06', '会话验证', 'ECP 商务账号 01 会话验证通过', '成功', ''],
    ['昨天 18:12', '任务调度', '「重点分类」任务因账号会话状态暂停', '待处理', 'warning']
  ];
  return `<section class="panel-card"><div class="panel-head"><div><h3>运行日志</h3><p>保留最近 30 天的采集、解析和账号状态记录。</p></div><div style="display:flex;gap:8px"><button class="filter-more">全部类型 ⌄</button><button class="filter-more">最近 7 天 ⌄</button></div></div><div class="log-list">${logs.map(log => `<div class="log-row"><span class="log-time">${log[0]}</span><span class="log-type">${log[1]}</span><span class="log-message">${log[2]}</span><span class="log-result ${log[4]}">${log[3]}</span></div>`).join('')}</div><div class="table-footer"><span>显示最近 <strong>7</strong> 条记录</span><div class="pagination"><button class="page-button">‹</button><button class="page-button active">1</button><button class="page-button">2</button><button class="page-button">›</button></div></div></section>`;
}

function renderOverviewLive() {
  const dashboard = appState.dashboard || { platforms: [], recent_logs: [], recent_runs: [] };
  const platforms = dashboard.platforms || [];
  const logs = dashboard.recent_logs || [];
  const healthy = platforms.filter(item => item.health_status === 'healthy').length;
  const platformRows = platforms.map(site => `<div class="health-row"><span class="platform-logo ${site.code === 'csg' ? 'green' : ['sgcc', 'epec', 'cdt'].includes(site.code) ? 'orange' : ''}">${esc(site.name.slice(0, 1))}</span><span class="health-name">${esc(site.name)}</span><span class="health-bar"><span style="width:${site.health_status === 'healthy' ? 100 : site.health_status === 'unhealthy' ? 20 : 0}%"></span></span><strong>${site.health_status === 'healthy' ? '正常' : site.health_status === 'unhealthy' ? '异常' : '未检查'}</strong></div>`).join('');
  const feed = logs.map(log => `<div class="feed-item"><span class="feed-time">${esc(beijingDateTime(log.created_at, true))}</span><div class="feed-copy"><strong>${esc(log.event_type)}</strong><span>${esc(log.message)}</span></div><span class="status-tag ${log.level === 'ERROR' ? 'status-failed' : log.level === 'WARNING' ? 'status-partial' : 'status-done'}">${esc(log.level || 'INFO')}</span></div>`).join('');
  const latest = (dashboard.recent_runs || [])[0];
  return `${renderStats()}${renderDataStrip()}<div class="overview-grid"><section class="panel-card"><div class="panel-head"><div><h3>最近采集批次</h3><p>来自后端批次记录（北京时间）</p></div>${latest ? `<span class="status-tag status-${latest.status === 'completed' ? 'done' : latest.status === 'failed' ? 'failed' : 'processing'}">${esc(latest.status)}</span>` : ''}</div><div class="panel-body">${latest ? `<div class="config-list"><div class="config-row"><div class="config-leading mint">↻</div><div class="config-copy"><strong>${esc(latest.job_name)}</strong><span>发现 ${latest.discovered} 条 · 命中入库 ${latest.detail_success} 条 · 未命中过滤 ${latest.filtered_count || 0} 条 · 失败 ${latest.failed_count} 条 · 附件 ${latest.attachment_count} 个</span></div><span>${esc(beijingDateTime(latest.finished_at || latest.started_at))}</span></div></div>` : '<div class="empty-state">暂无采集批次</div>'}</div></section><section class="panel-card"><div class="panel-head"><div><h3>平台运行状态</h3><p>最近一次健康检查（北京时间）</p></div><span class="status-tag ${healthy === platforms.length && platforms.length ? 'status-done' : 'status-partial'}">${healthy}/${platforms.length} 正常</span></div><div class="panel-body health-list">${platformRows || '<div class="empty-state">暂无平台</div>'}</div></section></div><div style="height:16px"></div>${renderRecentCollection('最近采集信息', '来自公告库的最新入库记录')}<div style="height:16px"></div><section class="panel-card"><div class="panel-head"><div><h3>最近动态</h3><p>后端实时运行记录</p></div><button class="filter-more" data-view-link="logs">查看全部 →</button></div><div class="panel-body mini-feed">${feed || '<div class="empty-state">暂无动态</div>'}</div></section>`;
}

function renderCurrentView() {
  const container = $('#view-container');
  if (!backendOnline) {
    container.innerHTML = '<section class="panel-card"><div class="panel-body"><div class="empty-state"><strong>后端未连接</strong><p>正在连接本地服务；如果持续显示此信息，请重新启动项目。</p></div></div></section>';
    return;
  }
  const viewRenderers = { overview: backendOnline ? renderOverviewLive : renderOverview, notices: renderNotices, keywords: renderKeywords, jobs: renderJobs, platforms: renderPlatforms, imports: renderImports, logs: renderLogs };
  container.innerHTML = viewRenderers[appState.view]();
  if (appState.view === 'notices') renderNoticeTable();
  if (appState.view === 'imports') loadStorageSettings();
}

function setView(view) {
  appState.view = view;
  const meta = viewMeta[view];
  $('#page-title').textContent = meta[0];
  $('#breadcrumb-current').textContent = meta[0];
  $('#page-subtitle').textContent = meta[1];
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === view));
  renderCurrentView();
  loadManagementData(view);
}

function detailInfo(notice) {
  const d = notice.detail;
  return `<div class="drawer-section"><div class="drawer-section-heading"><h3>结构化项目信息</h3><span>规则抽取 · 置信度 92%</span></div><div class="field-grid"><div class="detail-field"><label>项目名称</label><p>${esc(d.project)}</p></div><div class="detail-field"><label>项目编号</label><p class="mono">${esc(notice.number)}</p></div><div class="detail-field"><label>需求单位</label><p>${esc(notice.unit)}</p></div><div class="detail-field"><label>招标代理机构</label><p>${esc(d.agent)}</p></div><div class="detail-field"><label>项目地点</label><p>${esc(d.location)}</p></div><div class="detail-field"><label>预算 / 最高限价</label><p>${esc(d.budget)}</p></div><div class="detail-field"><label>发布时间</label><p class="mono">${esc(notice.date)}</p></div><div class="detail-field"><label>开标时间</label><p class="mono ${notice.opening === '待确认' ? 'pending' : ''}">${esc(notice.opening)}</p></div></div></div><div class="drawer-section"><div class="drawer-section-heading"><h3>项目摘要</h3><button class="filter-more" data-toast="摘要编辑入口已准备">编辑</button></div><p style="margin:0;color:#69758e;font-size:11px;line-height:1.8">【项目】${esc(d.project)}；【单位】${esc(notice.unit)}；【范围】${esc(notice.summary)}；【时间】${esc(notice.opening)}；【附件】共 ${notice.attachments} 个，${notice.keyFiles} 个关键文件。</p></div><div class="drawer-section"><div class="drawer-section-heading"><h3>解析流程</h3><span>${notice.statusText}</span></div><div class="process-line"><div class="process-step done"><span class="process-circle">✓</span><span class="process-label">已发现</span></div><div class="process-step done"><span class="process-circle">✓</span><span class="process-label">详情采集</span></div><div class="process-step done"><span class="process-circle">✓</span><span class="process-label">附件下载</span></div><div class="process-step ${notice.status === 'done' ? 'done' : 'current'}"><span class="process-circle">${notice.status === 'done' ? '✓' : '·'}</span><span class="process-label">解析处理</span></div></div></div>`;
}

function detailEvidence(notice) {
  const rules = notice.matchingRules || [];
  const ruleLabel = rules.length ? rules.join('、') : '未命中当前启用关键词组';
  const evidence = notice.evidence.length ? notice.evidence.map(item => { const locator = item.locatorFileId ? `<button type="button" class="file-open-action" data-open-file="${esc(item.locatorFileId)}">${esc(item.locatorLabel || "定位本地文件")}</button>` : `<a href="${esc(item.locatorUrl || "#")}" target="_blank" rel="noreferrer">${esc(item.locatorLabel || "定位 ↗")}</a>`; return `<article class="evidence-card"><div class="evidence-top"><span class="evidence-key">${esc(item.key)}</span><span class="evidence-loc">${esc(item.loc)}</span></div><p class="evidence-copy">${esc(item.copy).replace(new RegExp(esc(item.key), 'g'), `<mark>${esc(item.key)}</mark>`)}</p><div class="evidence-source"><span class="file-mini">▤</span>${esc(item.source)}<span style="margin-left:auto">${locator}</span></div></article>`; }).join('') : '<div class="empty-state">该公告没有命中当前启用关键词组。</div>';
  return `<div class="drawer-section"><div class="drawer-section-heading"><h3>关键词命中证据</h3><span>${notice.evidence.length} 条证据</span></div>${evidence}</div><div class="drawer-section"><div class="drawer-section-heading"><h3>匹配规则</h3><span>${esc(ruleLabel)}</span></div><div class="rule-code" style="margin:0">${rules.length ? `命中规则组：${esc(ruleLabel)}` : '当前没有有效的正向命中'}</div></div>`;
}

function detailFiles(notice) {
  const root = notice.files.find(file => file.type === 'zip');
  const nested = notice.files.filter(file => file.nested && file.type !== 'zip');
  const topLevel = notice.files.filter(file => !file.nested && file.type !== 'zip');
  const fileOpen = file => file.openLocationUrl && file.id ? `<button type="button" class="file-open-action" data-open-file="${esc(file.id)}">定位文件</button>` : ""; const fileAction = file => file.id ? `<button class="key-file-action" data-key-file="${file.id}">${file.key ? '取消关键' : '设为关键'}</button>` : '';
  const rootRow = root ? `<div class="tree-row" style="padding-bottom:3px"><span class="tree-caret">⌄</span><span class="file-symbol zip">${iconFor(root.type)}</span><span>${esc(root.name)}</span>${fileOpen(root)}<strong>${esc(root.size)}</strong></div>${nested.map(file => `<div class="tree-row nested"><span class="file-symbol">${iconFor(file.type)}</span><span>${esc(file.name)}</span>${fileOpen(file)}${file.key ? '<span class="key-file">关键文件</span>' : ''}${fileAction(file)}<strong>${file.error ? esc(file.error) : esc(file.size)}</strong></div>`).join('')}` : '';
  const topRows = topLevel.map(file => `<div class="tree-row"><span class="tree-caret">·</span><span class="file-symbol">${iconFor(file.type)}</span><span>${esc(file.name)}</span>${fileOpen(file)}${file.key ? '<span class="key-file">关键文件</span>' : ''}${fileAction(file)}<strong>${file.error ? '!' : esc(file.size)}</strong></div>`).join('');
  return `<div class="drawer-section"><div class="drawer-section-heading"><h3>附件与文件树</h3><span>${notice.attachments} 个附件 · ${notice.keyFiles} 个关键文件</span></div><div class="file-tree">${rootRow}${topRows}</div></div><div class="drawer-section"><div class="drawer-section-heading"><h3>原始数据</h3><span>可追溯留存</span></div><div class="tree-row"><span class="file-symbol">▧</span><span>公告网页快照.html</span><strong>已留存</strong></div><div class="tree-row" style="margin-top:6px"><span class="file-symbol">#</span><span>SHA-256 指纹</span><strong>9a6e…7f21</strong></div></div>`;
}

function renderDrawer(notice) {
  const body = $('#drawer-body');
  const tabContent = appState.detailTab === 'info' ? detailInfo(notice) : appState.detailTab === 'evidence' ? detailEvidence(notice) : detailFiles(notice);
  const actions = notice.isDeleted ? '<button class="button button-primary" data-drawer-restore>恢复到公告库</button>' : '<button class="button button-primary" data-drawer-mark="relevant">标记为相关</button><button class="button button-secondary" data-drawer-mark="focus">重点关注</button><button class="button button-secondary" data-reparse-notice>重新解析</button><button class="button button-danger" data-drawer-delete>软删除</button>';
  body.innerHTML = `<div class="drawer-title-block"><div class="drawer-badges"><span class="source-chip ${notice.platformClass}">${notice.platformName}</span><span class="status-tag status-${notice.status}">${notice.statusText}</span><span class="mark-tag mark-${notice.mark}">${notice.markText}</span></div><h2 class="drawer-title">${esc(notice.title)}</h2><div class="drawer-source"><span>来源链接</span><a href="${esc(notice.sourceUrl || '#')}" target="_blank" rel="noreferrer">打开原公告 ↗</a><span style="margin-left:auto;color:#a1aabb">${notice.isDeleted ? `删除于 ${esc(beijingDateTime(notice.deletedAt))}` : `采集于 ${esc(notice.date)}`}</span></div><div class="drawer-actions">${actions}</div></div><div class="drawer-tabs"><button class="filter-tab ${appState.detailTab === 'info' ? 'active' : ''}" data-detail-tab="info">项目信息</button><button class="filter-tab ${appState.detailTab === 'evidence' ? 'active' : ''}" data-detail-tab="evidence">命中证据 <span class="tab-number">${notice.evidence.length}</span></button><button class="filter-tab ${appState.detailTab === 'files' ? 'active' : ''}" data-detail-tab="files">附件文件 <span class="tab-number">${notice.attachments}</span></button></div>${tabContent}`;
}

function openDetail(id) {
  const notice = notices.find(item => item.id === id);
  if (!notice) return;
  appState.detailId = id; appState.detailTab = 'info';
  if (backendOnline) apiFetch(`/api/notices/${id}/reparse-status`).then(result => {
    if (result.task && ['queued','running'].includes(result.task.status)) void watchReparse(result.task.id, id);
  }).catch(error => showToast(error.message));
  renderDrawer(notice);
  $('#overlay').classList.add('visible');
  $('#detail-drawer').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeDetail() {
  $('#overlay').classList.remove('visible'); $('#detail-drawer').classList.remove('open'); document.body.style.overflow = '';
}

function showToast(message) {
  const toast = document.createElement('div'); toast.className = 'toast'; toast.innerHTML = `<i>✓</i><span>${esc(message)}</span>`; $('#toast-region').appendChild(toast);
  setTimeout(() => { toast.classList.add('fade'); setTimeout(() => toast.remove(), 350); }, 2600);
}

function openImport() { $('#import-modal-backdrop').classList.add('visible'); }
function closeImport() { $('#import-modal-backdrop').classList.remove('visible'); }

const words = value => String(value || '').split(/[,，\n]/).map(item => item.trim()).filter(Boolean);
const inputField = (name, label, value = '', type = 'text', wide = false, extra = '') => `<label class="form-field ${wide ? 'wide' : ''}">${label}<input class="input" name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
function closeConfig() { $('#config-modal-backdrop').classList.remove('visible'); appState.config = null; }
function openConfig(kind, existing = null) {
  if (!backendOnline) return showToast('请先启动后端');
  appState.config = { kind, existing };
  const fields = $('#config-fields');
  const title = $('#config-title');
  if (kind === 'keyword') {
    const item = existing || {};
    title.textContent = existing ? '编辑关键词组' : '新建关键词组';
    fields.innerHTML = `${inputField('name','名称',item.name || '')}${inputField('category','分类',item.category || '业务筛选')}${inputField('priority','优先级（越大越优先）',item.priority || 0,'number',false,'min="-1000" max="1000"')}${inputField('include_any','任一命中（逗号分隔）',(item.include_any || []).join(', '),'text',true)}${inputField('include_all','全部命中（逗号分隔）',(item.include_all || []).join(', '),'text',true)}${inputField('phrases','精确短语（逗号分隔）',(item.phrases || []).join(', '),'text',true)}${inputField('exclude','排除词（逗号分隔）',(item.exclude || []).join(', '),'text',true)}<label class="form-field check-field"><input name="enabled" type="checkbox" ${existing && !item.enabled ? '' : 'checked'}> 启用关键词组</label>`;
  } else if (kind === 'job') {
    const item = existing || {};
    title.textContent = existing ? '编辑采集任务' : '新建采集任务';
    const sites = appState.sites.map(site => `<option value="${site.id}" ${String(item.site_id || (appState.sites[0] && appState.sites[0].id)) === String(site.id) ? 'selected' : ''}>${esc(site.name)}</option>`).join('');
    const groups = `<option value="">全部启用规则</option>` + appState.keywordGroups.map(group => `<option value="${group.id}" ${String(item.keyword_group_id || '') === String(group.id) ? 'selected' : ''}>${esc(group.name)}</option>`).join('');
    fields.innerHTML = `${inputField('name','任务名称',item.name || '')}<label class="form-field">采集平台<select name="site_id">${sites}</select></label><label class="form-field">关键词组<select name="keyword_group_id">${groups}</select></label>${inputField('schedule_text','执行计划（每30分钟/每天08:30/工作日08:30/手动）',item.schedule_text || '每 30 分钟', 'text', true)}${inputField('categories','公告类型（逗号分隔）',(item.categories || ['招标公告']).join(', '),'text',true)}${inputField('lookback_days','回溯天数（按北京时间自然日）',item.lookback_days != null ? item.lookback_days : 1,'number',false,'min="0" max="3650"')}${inputField('max_pages','最大页数',item.max_pages || 5,'number',false,'min="1" max="100"')}${inputField('max_notices','最大公告数',item.max_notices || 100,'number',false,'min="1" max="10000"')}${inputField('interval_ms','请求间隔（毫秒）',item.interval_ms || 1500,'number',false,'min="200" max="60000"')}${inputField('retry_max_attempts','最多尝试次数',(item.retry && item.retry.max_attempts) || 3,'number',false,'min="1" max="10"')}<label class="form-field check-field"><input name="download_attachments" type="checkbox" ${existing && !item.download_attachments ? '' : 'checked'}> 下载附件</label><label class="form-field check-field"><input name="enabled" type="checkbox" ${existing && !item.enabled ? '' : 'checked'}> 启用任务</label>`;
  } else {
    title.textContent = '配置本次采集';
    const jobs = appState.jobs.filter(job => job.enabled).map(job => `<option value="${job.id}" ${String((existing && existing.id) || '') === String(job.id) ? 'selected' : ''}>${esc(job.name)}</option>`).join('');
    fields.innerHTML = `<label class="form-field wide">采集任务<select name="job_id">${jobs}</select></label>${inputField('lookback_days','本次回溯天数',existing && existing.lookback_days != null ? existing.lookback_days : 1,'number',false,'min="0" max="3650"')}${inputField('max_pages','本次最大页数',(existing && existing.max_pages) || 5,'number',false,'min="1" max="100"')}${inputField('max_notices','本次最大公告数',(existing && existing.max_notices) || 100,'number',false,'min="1" max="10000"')}<label class="form-field check-field"><input name="download_attachments" type="checkbox" ${existing && !existing.download_attachments ? '' : 'checked'}> 下载附件</label>`;
  }
  $('#config-modal-backdrop').classList.add('visible');
}

async function submitConfig(form) {
  const data = new FormData(form), config = appState.config, existing = config.existing;
  if (config.kind === 'keyword') {
    const payload = { name: data.get('name').trim(), category: data.get('category').trim(), priority: Number(data.get('priority')), enabled: data.has('enabled'), include_any: words(data.get('include_any')), include_all: words(data.get('include_all')), phrases: words(data.get('phrases')), exclude: words(data.get('exclude')), synonyms: (existing && existing.synonyms) || {}, scopes: (existing && existing.scopes) || ['title','body','attachment_name','attachment_body'] };
    if (!payload.name) throw new Error('请输入关键词组名称');
    await apiFetch(existing ? `/api/keyword-groups/${existing.id}` : '/api/keyword-groups', { method: existing ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
  } else if (config.kind === 'job') {
    const payload = { name:data.get('name').trim(), site_id:Number(data.get('site_id')), account_id:(existing && existing.account_id) || null, keyword_group_id:data.get('keyword_group_id') ? Number(data.get('keyword_group_id')) : null, schedule_text:data.get('schedule_text').trim(), categories:words(data.get('categories')), timezone:'Asia/Shanghai', lookback_days:Number(data.get('lookback_days')), max_pages:Number(data.get('max_pages')), max_notices:Number(data.get('max_notices')), concurrency:(existing && existing.concurrency) || 1, interval_ms:Number(data.get('interval_ms')), retry_max_attempts:Number(data.get('retry_max_attempts')), download_attachments:data.has('download_attachments'), ocr_enabled:Boolean(existing && existing.ocr_enabled), enabled:data.has('enabled') };
    if (!payload.name || !payload.schedule_text) throw new Error('任务名称和执行计划不能为空');
    if (!payload.categories.length) throw new Error('请至少填写一种公告类型');
    await apiFetch(existing ? `/api/crawl-jobs/${existing.id}` : '/api/crawl-jobs', { method:existing ? 'PUT':'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
  } else {
    const jobId = Number(data.get('job_id'));
    if (!jobId) throw new Error('没有可运行的启用任务');
    const payload = { lookback_days:Number(data.get('lookback_days')), max_pages:Number(data.get('max_pages')), max_notices:Number(data.get('max_notices')), download_attachments:data.has('download_attachments') };
    const result = await apiFetch(`/api/crawl-jobs/${jobId}/run`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    closeConfig();
    await loadManagementData(appState.view);
    showToast(`批次 #${result.run_id} 已入队，正在启动采集；已从现在重新计时`);
    void watchCrawlRun(result.run_id);
    return;
  }
  closeConfig(); await loadManagementData(appState.view); if (config.kind === 'keyword') await loadBackendNotices(false); showToast('配置已保存');
}

function createKeywordFromUi(existing = null) { openConfig('keyword', existing); }
function createJobFromUi(existing = null) { if (!appState.sites.length) return showToast('暂无可用平台'); openConfig('job', existing); }
function runJobFromUi(jobId) { const job = appState.jobs.find(item => String(item.id) === String(jobId)); openConfig('run', job); }

async function saveStorageConfig() {
  const root = $('#storage-root');
  const maxFile = $('#storage-max-file');
  const payload = { root: root ? root.value.trim() : '', max_attachment_mb: maxFile ? Number(maxFile.value) : 500 };
  const request = overwrite => apiFetch('/api/settings/storage', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payload, overwrite })
  });
  try {
    await request(false);
  } catch (error) {
    if (error.status !== 409 || !window.confirm(error.message)) throw error;
    await request(true);
  }
  showToast('本地文件目录配置已保存');
  await loadStorageSettings();
}


document.addEventListener('click', event => {
  if (event.target.closest('.notification-button')) {
    if (!updateInfo) { showToast('当前没有新版本通知'); return; }
    window.open(updateInfo.release_url, '_blank', 'noopener');
    return;
  }
  const nav = event.target.closest('[data-view]');
  if (nav) { setView(nav.dataset.view); return; }
  const viewLink = event.target.closest('[data-view-link]');
  if (viewLink) { setView(viewLink.dataset.viewLink); return; }
  const detail = event.target.closest('[data-open-detail]');
  if (detail) { openDetail(detail.dataset.openDetail); return; }
  const filterTab = event.target.closest('[data-filter-tab]');
  if (filterTab) { appState.tab = filterTab.dataset.filterTab; appState.page = 1; $$('.filter-tab[data-filter-tab]').forEach(tab => tab.classList.toggle('active', tab === filterTab)); loadBackendNotices(false); return; }
  const pageButton = event.target.closest('[data-page]');
  if (pageButton && !pageButton.disabled) { appState.page = Number(pageButton.dataset.page); loadBackendNotices(false); return; }
  const reset = event.target.closest('[data-reset-filters]');
  if (reset) { appState.query = ''; appState.platform = 'all'; appState.mark = 'all'; appState.attachment = 'all'; appState.tab = 'all'; appState.page = 1; appState.selected.clear(); loadBackendNotices(false); return; }
  const select = event.target.closest('[data-select-id]');
  if (select) { if (select.checked) appState.selected.add(select.dataset.selectId); else appState.selected.delete(select.dataset.selectId); renderNoticeTable(); return; }
  const selectAll = event.target.closest('#select-all');
  if (selectAll) { const start = (appState.page - 1) * appState.pageSize; const items = getFilteredNotices(); (backendOnline ? items : items.slice(start, start + appState.pageSize)).forEach(item => selectAll.checked ? appState.selected.add(item.id) : appState.selected.delete(item.id)); renderNoticeTable(); return; }
  const batchMark = event.target.closest('[data-batch-mark]');
  if (batchMark) { const ids = Array.from(appState.selected); const mark = batchMark.dataset.batchMark; persistMark(ids, mark).then(() => { appState.selected.clear(); showToast('已更新所选公告的业务标记'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  if (event.target.closest('[data-batch-delete]')) { const ids = Array.from(appState.selected); persistDelete(ids).then(() => { appState.selected.clear(); showToast('已将所选公告移入回收站'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  if (event.target.closest('[data-batch-restore]')) { const ids = Array.from(appState.selected); persistRestore(ids).then(() => { appState.selected.clear(); showToast('已恢复所选公告'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  const restoreNotice = event.target.closest('[data-restore-notice]');
  if (restoreNotice) { persistRestore([restoreNotice.dataset.restoreNotice]).then(() => { showToast('公告已恢复到公告库'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  const openFile = event.target.closest('[data-open-file]');
  if (openFile) {
    if (!backendOnline) { showToast('请先启动后端'); return; }
    apiFetch(`/api/files/${encodeURIComponent(openFile.dataset.openFile)}/open-location`, { method: 'POST' }).then(result => showToast(result.message || '已定位文件')).catch(error => showToast(error.message));
    return;
  }
  const keyFile = event.target.closest('[data-key-file]');
  if (keyFile && backendOnline) { const notice = notices.find(item => String(item.id) === String(appState.detailId)); const file = notice && notice.files.find(item => String(item.id) === String(keyFile.dataset.keyFile)); if (file) { const nextValue = !file.key; apiFetch(`/api/files/${keyFile.dataset.keyFile}/key`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_key_file: nextValue }) }).then(() => { file.key = nextValue; renderDrawer(notice); renderNoticeTable(); showToast(nextValue ? '已设为关键文件' : '已取消关键文件'); }).catch(error => showToast(error.message)); } return; }
  const drawerTab = event.target.closest('[data-detail-tab]');
  if (drawerTab && appState.detailId) { appState.detailTab = drawerTab.dataset.detailTab; renderDrawer(notices.find(item => item.id === appState.detailId)); return; }
  const drawerMark = event.target.closest('[data-drawer-mark]');
  if (drawerMark && appState.detailId) { const mark = drawerMark.dataset.drawerMark; persistMark([appState.detailId], mark).then(() => { closeDetail(); showToast('业务标记已更新'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  if (event.target.closest('[data-drawer-delete]')) { const id = appState.detailId; persistDelete([id]).then(() => { closeDetail(); showToast('公告已移入回收站，可在回收站恢复'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  if (event.target.closest('[data-drawer-restore]')) { const id = appState.detailId; persistRestore([id]).then(() => { closeDetail(); showToast('公告已恢复到公告库'); return loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  const reparse = event.target.closest('[data-reparse-notice]');
  if (reparse && appState.detailId) { const noticeId = appState.detailId; apiFetch(`/api/notices/${noticeId}/reparse`, { method: 'POST' }).then(result => { showToast(result.message); void watchReparse(result.task_id, noticeId); }).catch(error => showToast(error.message)); return; }
  const newKeyword = event.target.closest('[data-new-keyword]');
  if (newKeyword) { createKeywordFromUi(); return; }
  const editKeyword = event.target.closest('[data-edit-keyword]');
  if (editKeyword) { createKeywordFromUi(appState.keywordGroups.find(group => String(group.id) === String(editKeyword.dataset.editKeyword))); return; }
  const toggleKeyword = event.target.closest('[data-toggle-keyword]');
  if (toggleKeyword) { const item = appState.keywordGroups.find(group => String(group.id) === String(toggleKeyword.dataset.toggleKeyword)); apiFetch(`/api/keyword-groups/${item.id}/enabled`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!item.enabled})}).then(async () => { await loadManagementData('keywords'); await loadBackendNotices(false); }).catch(error => showToast(error.message)); return; }
  const deleteKeyword = event.target.closest('[data-delete-keyword]');
  if (deleteKeyword) { if (window.confirm('确定删除该关键词组？如已绑定采集任务，请先编辑任务解绑。')) apiFetch(`/api/keyword-groups/${deleteKeyword.dataset.deleteKeyword}`, {method:'DELETE'}).then(() => loadManagementData('keywords')).catch(error => showToast(error.message)); return; }
  const newJob = event.target.closest('[data-new-job]');
  if (newJob) { createJobFromUi(); return; }
  const editJob = event.target.closest('[data-edit-job]');
  if (editJob) { createJobFromUi(appState.jobs.find(job => String(job.id) === String(editJob.dataset.editJob))); return; }
  const toggleJob = event.target.closest('[data-toggle-job]');
  if (toggleJob) { const item = appState.jobs.find(job => String(job.id) === String(toggleJob.dataset.toggleJob)); apiFetch(`/api/crawl-jobs/${item.id}/enabled`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!item.enabled})}).then(() => { showToast(item.enabled ? '任务已停用' : '任务已启用，已从现在重新计时'); return loadManagementData('jobs'); }).catch(error => showToast(error.message)); return; }
  const deleteJob = event.target.closest('[data-delete-job]');
  if (deleteJob) { if (window.confirm('确定删除该采集任务及其历史批次？')) apiFetch(`/api/crawl-jobs/${deleteJob.dataset.deleteJob}`, {method:'DELETE'}).then(() => loadManagementData('jobs')).catch(error => showToast(error.message)); return; }
  const runJob = event.target.closest('[data-run-job]');
  if (runJob) { runJobFromUi(runJob.dataset.runJob); return; }
  const healthSite = event.target.closest('[data-health-site]');
  if (healthSite) { apiFetch(`/api/sites/${healthSite.dataset.healthSite}/health-check`, { method: 'POST' }).then(result => { showToast(result.message || `健康检查：${result.ok ? '正常' : '异常'}`); return loadManagementData('platforms'); }).catch(error => showToast(error.message)); return; }
  const openVerification = event.target.closest('[data-open-verification]');
  if (openVerification) { apiFetch(`/api/sites/${openVerification.dataset.openVerification}/manual-verification/open`, { method: 'POST' }).then(result => showToast(result.message)).catch(error => showToast(error.message)); return; }
  const completeVerification = event.target.closest('[data-complete-verification]');
  if (completeVerification) { completeVerification.disabled = true; apiFetch(`/api/sites/${completeVerification.dataset.completeVerification}/manual-verification/complete`, { method: 'POST' }).then(result => { showToast(result.message); return loadManagementData('platforms'); }).catch(error => showToast(error.message)).finally(() => { completeVerification.disabled = false; }); return; }
  if (event.target.closest('[data-refresh-management]')) { loadManagementData(appState.view); return; }
  if (event.target.closest('#open-import') || event.target.closest('#open-import-secondary')) { openImport(); return; }
  const fileTrigger = event.target.closest('[data-trigger-file]');
  if (fileTrigger) { const input = $(`#${fileTrigger.dataset.triggerFile === 'excel' ? 'excel-file' : 'attachment-file'}`); if (input) input.click(); return; }
  if (event.target.closest('#save-storage')) { if (!backendOnline) { showToast('请通过 run_app.py 启动后端再保存目录配置'); return; } saveStorageConfig().catch(error => showToast(error.message)); return; }
  if (event.target.closest('[data-close-modal]')) { closeImport(); return; }
  if (event.target.closest('[data-close-config]')) { closeConfig(); return; }
  const importTab = event.target.closest('[data-import-tab]');
  if (importTab) { appState.importTab = importTab.dataset.importTab; $$('.import-tab').forEach(tab => tab.classList.toggle('active', tab === importTab)); $$('.import-panel').forEach(panel => panel.classList.toggle('active', panel.dataset.importPanel === appState.importTab)); return; }
  if (event.target.closest('#confirm-import')) { const input = $('#url-input'); const urls = input ? input.value.split(/\n+/).map(value => value.trim()).filter(Boolean) : []; closeImport(); if (urls.length && backendOnline) { apiFetch('/api/imports/url', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls }) }).then(result => { showToast(`导入完成：新增 ${result.created} 条，更新 ${result.updated} 条，失败 ${result.failed} 条`); return loadBackendNotices(); }).catch(error => showToast(error.message)); } else { showToast(urls.length ? '已完成导入预检，等待确认入库' : '请先输入至少一个公告 URL'); } return; }
  const toggle = event.target.closest('.toggle');
  if (toggle) { toggle.classList.toggle('on'); const row = toggle.closest('.config-row'); const copy = row && row.querySelector('.config-copy'); if (copy) copy.classList.add('updated'); showToast('匹配设置已更新'); return; }
  const toastButton = event.target.closest('[data-toast]');
  if (toastButton) { showToast(toastButton.dataset.toast); return; }
  if (event.target.closest('#refresh-button')) { if (!backendOnline) return showToast('请先启动后端'); if (!appState.jobs.some(job => job.enabled)) return showToast('请先启用或创建采集任务'); openConfig('run', appState.jobs.find(job => job.enabled)); return; }
  if (event.target === $('#overlay')) closeDetail();
});

document.addEventListener('input', event => {
  if (event.target.id === 'notice-search') {
    appState.query = event.target.value;
    appState.page = 1;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadBackendNotices(false), 300);
  }
});
document.addEventListener('change', event => {
  if (event.target.id === 'platform-filter') { appState.platform = event.target.value; appState.page = 1; loadBackendNotices(false); }
  if (event.target.id === 'mark-filter') { appState.mark = event.target.value; appState.page = 1; loadBackendNotices(false); }
  if (event.target.id === 'attachment-filter') { appState.attachment = event.target.value; appState.page = 1; loadBackendNotices(false); }
  if (event.target.id === 'excel-file') uploadSelectedFile(event.target, '/api/imports/excel');
  if (event.target.id === 'attachment-file') uploadSelectedFile(event.target, '/api/imports/file');
});
document.addEventListener('submit', event => { if (event.target.id === 'config-form') { event.preventDefault(); const button = event.target.querySelector('[type="submit"]'); button.disabled = true; submitConfig(event.target).catch(error => showToast(error.message)).finally(() => { button.disabled = false; }); } });
document.addEventListener('keydown', event => { if (event.key === 'Escape') { closeDetail(); closeImport(); closeConfig(); } });

renderCurrentView();
loadBackendNotices().then(checkForUpdates);
setInterval(checkForUpdates, UPDATE_CHECK_INTERVAL_MS);
loadManagementData();
