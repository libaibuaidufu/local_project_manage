/**
 * Python 项目启动器 —— 前端逻辑。
 *
 * 没有框架，原生 JS 够用：一个列表页 + 三个弹窗。
 * 状态轮询 2 秒一次，日志轮询 800ms 一次（只在日志窗口打开时）。
 */
'use strict';

const STATUS_META = {
  RUNNING:  { dot: 'running',  label: '运行中', cls: 'running'  },
  STARTING: { dot: 'starting', label: '启动中', cls: 'starting' },
  STOPPING: { dot: 'stopping', label: '停止中', cls: 'stopping' },
  STOPPED:  { dot: 'stopped',  label: '已停止', cls: 'stopped'  },
  ERROR:    { dot: 'error',    label: '异常退出', cls: 'error'  },
  STATIC:   { dot: 'static',   label: '静态页面', cls: 'static' },
};

const POLL_STATUS_MS = 2000;
const POLL_LOG_MS = 800;

const state = {
  projects: [],
  /** 正在执行 start/stop/restart 的项目 id，用于禁用按钮防重复点击 */
  pending: new Set(),
  /** 正在编辑的项目 id，null 表示新建 */
  editingId: null,
  /** 确认弹窗当前动作 {onConfirm}；关闭即清空 */
  confirm: null,
  logs: {
    /** 当前激活的标签页项目 ID */
    activeTab: null,
    /** 已打开的标签页 Map<projectId, {projectId, runId, offsets, lines, filter, autoscroll}> */
    tabs: new Map(),
  },
  timers: { status: null, log: null },
};

const $ = (id) => document.getElementById(id);

/** 转义 HTML，项目名/路径/命令都是用户输入，直接插 innerHTML 会有 XSS 风险 */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

/** 秒数格式化：不到 1 小时显示「23分15秒」，超过则显示 02:31:42（需求 16） */
function formatUptime(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h === 0) return `${m}分${String(sec).padStart(2, '0')}秒`;
  return [h, m, sec].map((n) => String(n).padStart(2, '0')).join(':');
}

function formatBytes(mb) {
  if (mb == null) return '—';
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
}

// ------------------------------------------------------------------ 网络请求

/** 统一请求封装。总是返回 {ok, message, data}，调用方不用管异常。 */
async function api(path, options = {}) {
  try {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = { success: false, message: `服务端返回了非 JSON 响应（${response.status}）` };
    }
    return {
      ok: response.ok && payload.success !== false,
      message: payload.message || '',
      data: payload.data ?? {},
    };
  } catch (err) {
    // 管理器被关掉时会走到这里
    return { ok: false, message: `无法连接到管理器：${err.message}`, data: {} };
  }
}

// ------------------------------------------------------------------ Toast

function toast(message, kind = 'ok', timeout = 4000) {
  const el = document.createElement('div');
  el.className = `toast toast--${kind}`;
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), timeout);
}

// ------------------------------------------------------------------ 列表渲染

function isLive(status) {
  return status === 'RUNNING' || status === 'STARTING' || status === 'STOPPING';
}

/** 卡片内容的指纹。只有它变了才动 DOM，避免每 2 秒重建整个列表。 */
function signature(p) {
  return [
    p.id, p.name, p.working_dir, p.command, p.port, p.auto_start,
    p.status, p.pid, p.cpu, p.memory, p.uptime, p.exit_code,
    p.port_listening, state.pending.has(p.id),
    p.kind, p.resolved_entry, p.html_count, p.url,
  ].join('|');
}

function renderList() {
  const grid = $('grid');
  const projects = state.projects;

  $('counter-running').textContent = projects.filter((p) => isLive(p.status)).length;
  $('counter-total').textContent = projects.length;
  $('empty').hidden = projects.length > 0;

  const seen = new Set();
  projects.forEach((project, index) => {
    seen.add(String(project.id));
    let card = grid.querySelector(`[data-id="${project.id}"]`);
    if (!card) {
      card = document.createElement('article');
      card.dataset.id = String(project.id);
      grid.appendChild(card);
    }
    const sig = signature(project);
    if (card.dataset.sig !== sig) {
      card.dataset.sig = sig;
      card.className = `card card--${STATUS_META[project.status]?.cls || 'stopped'}`;
      card.innerHTML = cardBody(project);
    }
    // 保证 DOM 顺序和数据顺序一致（新增项目时）
    if (grid.children[index] !== card) {
      grid.insertBefore(card, grid.children[index] || null);
    }
  });

  // 删掉已经不存在的项目
  Array.from(grid.children).forEach((child) => {
    if (!seen.has(child.dataset.id)) child.remove();
  });
}

function cardBody(p) {
  const meta = STATUS_META[p.status] || STATUS_META.STOPPED;
  const live = isLive(p.status);
  const busy = state.pending.has(p.id);

  const parts = [`
    <div class="card__head">
      <h2 class="card__name">
        <span class="dot dot--${meta.dot}" aria-hidden="true"></span>
        <span title="${esc(p.name)}">${esc(p.name)}</span>
      </h2>
      ${p.auto_start ? '<span class="badge badge--auto" title="管理器启动时自动拉起">自动</span>' : ''}
    </div>
    <p class="card__path" title="${esc(p.working_dir)}">${esc(p.working_dir)}</p>
    ${p.kind === 'static'
      ? ''
      : `<p class="card__cmd" title="${esc(p.command)}">$ ${esc(p.command)}</p>`}
  `];

  if (p.kind === 'static') {
    const entry = p.resolved_entry;
    parts.push(`
      <div class="card__status-line">
        ${entry
          ? `<span class="card__entry" title="入口文件">▸ ${esc(entry)}</span>`
          : '<span class="card__entry card__entry--none">没有 index.html，打开后列出目录</span>'}
      </div>
      <div class="card__status-line">
        <span class="card__hint">${p.html_count} 个 HTML 文件</span>
      </div>
    `);
    parts.push(`<div class="card__actions">${cardActions(p, false, busy)}</div>`);
    return parts.join('');
  }

  if (live && p.pid) {
    parts.push(`
      <dl class="stats">
        <div class="stats__row"><dt class="stats__key">PID</dt><dd class="stats__val">${p.pid}</dd></div>
        <div class="stats__row"><dt class="stats__key" title="100% = 占满一个 CPU 核心">CPU</dt><dd class="stats__val">${p.cpu == null ? '—' : p.cpu.toFixed(1) + '%'}</dd></div>
        <div class="stats__row"><dt class="stats__key">内存</dt><dd class="stats__val">${formatBytes(p.memory)}</dd></div>
        <div class="stats__row"><dt class="stats__key">运行</dt><dd class="stats__val">${formatUptime(p.uptime)}</dd></div>
      </dl>
    `);
    if (p.port) {
      const ready = p.port_listening;
      parts.push(`
        <div class="card__status-line">
          <a class="card__link" href="http://127.0.0.1:${p.port}" target="_blank"
             rel="noopener">127.0.0.1:${p.port}</a>
          <span class="${ready ? 'port-ok' : 'port-wait'}">${ready ? '● 已监听' : '○ 未监听'}</span>
        </div>
      `);
    }
  } else {
    const detail = p.status === 'ERROR'
      ? `异常退出${p.exit_code != null ? `（退出码 ${p.exit_code}）` : ''}`
      : meta.label;
    parts.push(`<div class="card__status-line ${p.status === 'ERROR' ? 'card__status-line--error' : ''}">${esc(detail)}</div>`);

    // 未运行时，如果配置了端口，也显示出来方便检查端口冲突
    if (p.port) {
      parts.push(`
        <div class="card__status-line">
          <span class="card__link card__link--muted">127.0.0.1:${p.port}</span>
        </div>
      `);
    }
  }

  parts.push(`<div class="card__actions">${cardActions(p, live, busy)}</div>`);
  return parts.join('');
}

function cardActions(p, live, busy) {
  const d = busy ? 'disabled' : '';
  const label = (text) => (busy ? '处理中…' : text);
  const buttons = [];

  if (p.kind === 'static') {
    return [
      `<a class="btn btn--sm btn--start" href="${esc(p.url)}" target="_blank" rel="noopener">打开</a>`,
      `<button class="btn btn--sm btn--ghost" data-act="edit" data-id="${p.id}">编辑</button>`,
      `<button class="btn btn--sm btn--ghost" data-act="delete" data-id="${p.id}">删除</button>`,
    ].join('');
  }

  if (live) {
    buttons.push(`<button class="btn btn--sm btn--danger" data-act="stop" data-id="${p.id}" ${d}>${label('停止')}</button>`);
    buttons.push(`<button class="btn btn--sm" data-act="restart" data-id="${p.id}" ${d}>${label('重启')}</button>`);
  } else {
    const text = p.status === 'ERROR' ? '重新启动' : '启动';
    buttons.push(`<button class="btn btn--sm btn--start" data-act="start" data-id="${p.id}" ${d}>${label(text)}</button>`);
  }

  buttons.push(`<button class="btn btn--sm btn--ghost" data-act="logs" data-id="${p.id}">日志</button>`);
  buttons.push(`<button class="btn btn--sm btn--ghost" data-act="edit" data-id="${p.id}">编辑</button>`);
  if (!live) {
    buttons.push(`<button class="btn btn--sm btn--ghost" data-act="delete" data-id="${p.id}">删除</button>`);
  }
  return buttons.join('');
}

// ------------------------------------------------------------------ 状态轮询

async function refresh() {
  const result = await api('/api/projects');
  if (!result.ok) {
    if (!refresh._warned) {
      toast(result.message, 'err');
      refresh._warned = true;   // 管理器挂了不要每 2 秒弹一次
    }
    return;
  }
  refresh._warned = false;
  state.projects = result.data.projects || [];
  renderList();
}

function startPolling() {
  stopPolling();
  state.timers.status = setInterval(refresh, POLL_STATUS_MS);
}

function stopPolling() {
  if (state.timers.status) clearInterval(state.timers.status);
  state.timers.status = null;
}

// ------------------------------------------------------------------ 进程操作

async function runAction(id, action) {
  state.pending.add(id);
  renderList();
  const result = await api(`/api/projects/${id}/${action}`, { method: 'POST' });
  state.pending.delete(id);

  toast(result.message || (result.ok ? '操作完成' : '操作失败'), result.ok ? 'ok' : 'err',
        result.ok ? 3000 : 7000);
  await refresh();
}

async function deleteProject(id) {
  const result = await api(`/api/projects/${id}`, { method: 'DELETE' });
  toast(result.message, result.ok ? 'ok' : 'err');
  if (result.ok) await refresh();
  return result.ok;
}

// ------------------------------------------------------------------ 导入备份

async function importBackup(file) {
  let text;
  try {
    text = await file.text();
  } catch {
    toast('读取文件失败', 'err');
    return;
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    toast('文件不是合法的 JSON', 'err');
    return;
  }
  const items = Array.isArray(payload) ? payload : payload?.projects;
  if (!Array.isArray(items) || items.length === 0) {
    toast('文件里没有可导入的项目', 'err');
    return;
  }

  const names = items.map((item) => item?.name).filter(Boolean);
  const preview = names.length
    ? names.slice(0, 5).join('、') + (names.length > 5 ? ` 等 ${names.length} 个` : '')
    : '（均未命名）';
  confirmDialog({
    title: '导入数据',
    text: `将导入 ${items.length} 个项目：${preview}\n` +
          '按名称匹配：已存在的项目会更新，其余新增；不会删除任何现有项目。',
    confirmLabel: '导入',
    onConfirm: async () => {
      const result = await api('/api/projects/import', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      toast(result.message, result.ok ? 'ok' : 'err', result.ok ? 7000 : 9000);
      if (result.ok) await refresh();
    },
  });
}

// ------------------------------------------------------------------ 弹窗控制

/** 记住打开弹窗前的焦点，关闭后还回去（键盘可访问性） */
let lastFocus = null;

function openModal(name) {
  lastFocus = document.activeElement;
  const modal = $(`modal-${name}`);
  modal.hidden = false;
  const focusable = modal.querySelector('input, select, button');
  if (focusable) focusable.focus();
}

function closeModal(name) {
  $(`modal-${name}`).hidden = true;
  if (name === 'logs') stopLogPolling();
  if (name === 'confirm') state.confirm = null;
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}

/** 通用确认弹窗。确认后执行 onConfirm，取消/关闭什么都不做。 */
function confirmDialog({ title, text, confirmLabel = '确认', danger = false, onConfirm }) {
  state.confirm = { onConfirm };
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = text;
  const button = $('btn-confirm');
  button.textContent = confirmLabel;
  button.className = danger ? 'btn btn--danger' : 'btn btn--primary';
  openModal('confirm');
}

// ------------------------------------------------------------------ 项目表单

function envRow(key = '', value = '') {
  const row = document.createElement('div');
  row.className = 'env__row';
  row.innerHTML = `
    <input type="text" placeholder="KEY" value="${esc(key)}" data-env="key"
           aria-label="环境变量名" spellcheck="false">
    <input type="text" placeholder="值" value="${esc(value)}" data-env="value"
           aria-label="环境变量值" spellcheck="false">
    <button type="button" class="env__del" aria-label="删除这一行">×</button>
  `;
  row.querySelector('.env__del').addEventListener('click', () => row.remove());
  return row;
}

function openForm(project = null) {
  state.editingId = project?.id ?? null;
  $('form-title').textContent = project ? '编辑项目' : '添加项目';
  $('form-error').hidden = true;
  $('f-name').value = project?.name ?? '';
  $('f-dir').value = project?.working_dir ?? '';
  $('f-command').value = project?.command ?? '';
  $('f-entry').value = project?.entry_file ?? '';
  $('f-port').value = project?.port ?? '';
  $('f-auto').checked = Boolean(project?.auto_start);

  const kind = project?.kind ?? 'process';
  const radio = document.querySelector(`input[name="kind"][value="${kind}"]`);
  if (radio) radio.checked = true;
  // 类型是建项目时定的，改起来等于换一个项目，编辑时就锁住
  document.querySelectorAll('input[name="kind"]').forEach((el) => {
    el.disabled = project != null;
  });
  syncKindFields();

  const rows = $('env-rows');
  rows.innerHTML = '';
  const env = project?.environment ?? {};
  Object.entries(env).forEach(([key, value]) => rows.appendChild(envRow(key, value)));

  openModal('form');
}

/** 按当前选中的类型，显示/隐藏只属于某一类的字段。 */
function syncKindFields() {
  const kind = document.querySelector('input[name="kind"]:checked')?.value ?? 'process';
  document.querySelectorAll('[data-only]').forEach((el) => {
    el.hidden = el.dataset.only !== kind;
  });
}

function collectForm() {
  const environment = {};
  $('env-rows').querySelectorAll('.env__row').forEach((row) => {
    const key = row.querySelector('[data-env="key"]').value.trim();
    const value = row.querySelector('[data-env="value"]').value;
    if (key) environment[key] = value;
  });

  const kind = document.querySelector('input[name="kind"]:checked')?.value ?? 'process';

  if (kind === 'static') {
    return {
      kind,
      name: $('f-name').value.trim(),
      working_dir: $('f-dir').value.trim(),
      entry_file: $('f-entry').value.trim(),
    };
  }

  return {
    kind,
    name: $('f-name').value.trim(),
    working_dir: $('f-dir').value.trim(),
    command: $('f-command').value.trim(),
    port: $('f-port').value.trim() === '' ? null : Number($('f-port').value),
    environment,
    auto_start: $('f-auto').checked,
  };
}

async function submitForm(event) {
  event.preventDefault();
  const payload = collectForm();
  const error = $('form-error');
  error.hidden = true;

  // 先做客户端校验，省一次往返
  if (!payload.name || !payload.working_dir) {
    error.textContent = '项目名称和工作目录都不能为空。';
    error.hidden = false;
    return;
  }
  if (payload.kind !== 'static' && !payload.command) {
    error.textContent = '服务进程项目必须填启动命令。';
    error.hidden = false;
    return;
  }

  const save = $('btn-save');
  save.disabled = true;
  save.textContent = '保存中…';

  const editing = state.editingId != null;
  const result = await api(editing ? `/api/projects/${state.editingId}` : '/api/projects', {
    method: editing ? 'PUT' : 'POST',
    body: JSON.stringify(payload),
  });

  save.disabled = false;
  save.textContent = '保存';

  if (!result.ok) {
    error.textContent = result.message;
    error.hidden = false;
    return;
  }

  closeModal('form');
  // 「运行中改配置不会自动重启」这类提示要停久一点，值得看完
  toast(result.message, result.message.includes('不会自动更新') ? 'warn' : 'ok',
        result.message.length > 30 ? 8000 : 3500);
  await refresh();
}

// ------------------------------------------------------------------ 日志窗口

function openLogPanel(project) {
  const panel = $('log-panel');
  const tabs = state.logs.tabs;

  // 如果标签页已存在，切换到它
  if (tabs.has(project.id)) {
    switchLogTab(project.id);
    panel.hidden = false;
    return;
  }

  // 创建新标签页
  tabs.set(project.id, {
    projectId: project.id,
    projectName: project.name,
    runId: null,
    offsets: { stdout: 0, stderr: 0 },
    lines: [],
    filter: 'all',
    autoscroll: true,
  });

  state.logs.activeTab = project.id;
  panel.hidden = false;
  renderLogTabs();
  pollLogs();

  if (!state.timers.log) {
    state.timers.log = setInterval(pollLogs, POLL_LOG_MS);
  }
}

function switchLogTab(projectId) {
  state.logs.activeTab = projectId;
  renderLogTabs();
  renderLogToolbar();
  renderConsole();
}

function closeLogTab(projectId, event) {
  if (event) event.stopPropagation();

  const tabs = state.logs.tabs;
  tabs.delete(projectId);

  // 如果关闭的是当前激活的标签页
  if (state.logs.activeTab === projectId) {
    const remaining = Array.from(tabs.keys());
    if (remaining.length > 0) {
      state.logs.activeTab = remaining[0];
    } else {
      state.logs.activeTab = null;
      $('log-panel').hidden = true;
      stopLogPolling();
      return;
    }
  }

  renderLogTabs();
  renderLogToolbar();
  renderConsole();
}

function closeLogPanel() {
  state.logs.tabs.clear();
  state.logs.activeTab = null;
  $('log-panel').hidden = true;
  stopLogPolling();
}

function renderLogTabs() {
  const container = $('log-tabs');
  const tabs = state.logs.tabs;
  const activeId = state.logs.activeTab;

  container.innerHTML = Array.from(tabs.values()).map((tab) => {
    const isActive = tab.projectId === activeId;
    const cls = isActive ? 'log-panel__tab is-active' : 'log-panel__tab';
    return `
      <button type="button" class="${cls}" data-tab-id="${tab.projectId}">
        <span>${esc(tab.projectName)}</span>
        <button type="button" class="log-panel__tab-close" data-close-tab="${tab.projectId}">×</button>
      </button>
    `;
  }).join('');
}

function renderLogToolbar() {
  const activeTab = getActiveLogTab();
  if (!activeTab) return;

  // 更新流过滤器状态
  document.querySelectorAll('.log-panel__toolbar .tab').forEach((el) => {
    el.classList.toggle('is-active', el.dataset.stream === activeTab.filter);
  });

  // 更新自动滚动复选框
  $('f-autoscroll').checked = activeTab.autoscroll;
}

function getActiveLogTab() {
  if (!state.logs.activeTab) return null;
  return state.logs.tabs.get(state.logs.activeTab);
}

function stopLogPolling() {
  if (state.timers.log) clearInterval(state.timers.log);
  state.timers.log = null;
}

async function pollLogs() {
  const activeTab = getActiveLogTab();
  if (!activeTab) return;

  const params = new URLSearchParams({
    stdout_at: String(activeTab.offsets.stdout),
    stderr_at: String(activeTab.offsets.stderr),
  });
  if (activeTab.runId) params.set('run_id', activeTab.runId);

  const result = await api(`/api/projects/${activeTab.projectId}/logs?${params}`);
  if (!result.ok) {
    toast(result.message, 'err');
    return;
  }

  const data = result.data;
  if (data.run_id && data.run_id !== activeTab.runId) {
    activeTab.runId = data.run_id;
  }
  renderRunOptions(data.runs || [], activeTab.runId);

  let added = false;
  ['stdout', 'stderr'].forEach((stream) => {
    const chunk = data[stream];
    if (!chunk) return;
    activeTab.offsets[stream] = chunk.offset ?? activeTab.offsets[stream];
    if (!chunk.text) return;
    chunk.text.split('\n').forEach((line) => {
      if (line !== '') activeTab.lines.push({ stream, text: line });
    });
    added = true;
  });

  // 只保留最近 4000 行，否则长时间开着窗口会把浏览器拖慢
  if (activeTab.lines.length > 4000) activeTab.lines = activeTab.lines.slice(-4000);
  if (added) renderConsole();
}

function renderRunOptions(runs, current) {
  const select = $('log-runs');
  const signature = runs.map((r) => r.run_id).join(',') + `|${current}`;
  if (select.dataset.sig === signature) return;
  select.dataset.sig = signature;

  select.innerHTML = runs.map((run) => {
    const label = run.run_id.replace('_', ' ').replace(/-(\d{2})-(\d{2})(-\d{3})?$/, ':$1:$2');
    const selected = run.run_id === current ? 'selected' : '';
    return `<option value="${esc(run.run_id)}" ${selected}>${esc(label)}</option>`;
  }).join('') || '<option value="">（暂无运行记录）</option>';
}

function renderConsole() {
  const box = $('console');
  const activeTab = getActiveLogTab();
  if (!activeTab) {
    box.innerHTML = '';
    return;
  }

  const filtered = activeTab.filter === 'all'
    ? activeTab.lines
    : activeTab.lines.filter((line) => line.stream === activeTab.filter);

  box.innerHTML = filtered.map((line) => {
    const isMeta = /^[=-]{10,}$/.test(line.text.trim()) || /^(项目|启动时间|工作目录|启动命令|端口|环境变量|进程结束|退出码|结束原因)\s+:/.test(line.text);
    const cls = isMeta ? 'meta' : (line.stream === 'stderr' ? 'err' : '');
    const text = esc(line.text);
    return cls ? `<span class="${cls}">${text}</span>` : text;
  }).join('\n');

  if (activeTab.autoscroll) box.scrollTop = box.scrollHeight;
}

function switchRun(runId) {
  const activeTab = getActiveLogTab();
  if (!activeTab) return;

  activeTab.runId = runId || null;
  activeTab.offsets = { stdout: 0, stderr: 0 };
  activeTab.lines = [];
  $('console').innerHTML = '';
  pollLogs();
}

// ------------------------------------------------------------------ 事件绑定

function findProject(id) {
  return state.projects.find((p) => p.id === id);
}

function bindEvents() {
  $('btn-add').addEventListener('click', () => openForm(null));
  $('project-form').addEventListener('submit', submitForm);
  $('btn-add-env').addEventListener('click', () => {
    const row = envRow();
    $('env-rows').appendChild(row);
    row.querySelector('input').focus();
  });
  document.querySelectorAll('input[name="kind"]').forEach((el) => {
    el.addEventListener('change', syncKindFields);
  });

  // 卡片按钮统一委托，卡片重建后不用重新绑定
  $('grid').addEventListener('click', (event) => {
    const button = event.target.closest('[data-act]');
    if (!button) return;
    const id = Number(button.dataset.id);
    const project = findProject(id);
    if (!project) return;

    switch (button.dataset.act) {
      case 'start':
      case 'stop':
      case 'restart':
        runAction(id, button.dataset.act);
        break;
      case 'logs':
        openLogPanel(project);
        break;
      case 'edit':
        openForm(project);
        break;
      case 'delete':
        confirmDialog({
          title: '删除项目',
          text: `确定删除项目「${project.name}」？此操作不可恢复，日志文件会保留。`,
          confirmLabel: '确认删除',
          danger: true,
          onConfirm: () => deleteProject(id),
        });
        break;
    }
  });

  $('btn-confirm').addEventListener('click', async () => {
    const action = state.confirm;
    closeModal('confirm');
    if (action?.onConfirm) await action.onConfirm();
  });

  // 导入：点按钮弹文件选择器，选完确认再上传
  $('btn-import').addEventListener('click', () => $('import-file').click());
  $('import-file').addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';   // 清掉选择，允许下次重选同一个文件
    if (file) importBackup(file);
  });

  // 所有 data-close 元素（背景、× 按钮、取消按钮）
  document.querySelectorAll('[data-close]').forEach((el) => {
    el.addEventListener('click', () => closeModal(el.dataset.close));
  });

  // 日志面板相关事件
  $('btn-close-panel').addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    closeLogPanel();
  });

  $('log-tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('[data-tab-id]');
    if (tab && !event.target.closest('[data-close-tab]')) {
      switchLogTab(Number(tab.dataset.tabId));
    }

    const closeBtn = event.target.closest('[data-close-tab]');
    if (closeBtn) {
      closeLogTab(Number(closeBtn.dataset.closeTab), event);
    }
  });

  document.querySelectorAll('.log-panel__toolbar .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const activeTab = getActiveLogTab();
      if (!activeTab) return;

      document.querySelectorAll('.log-panel__toolbar .tab').forEach((t) => t.classList.remove('is-active'));
      tab.classList.add('is-active');
      activeTab.filter = tab.dataset.stream;
      renderConsole();
    });
  });

  $('log-runs').addEventListener('change', (event) => switchRun(event.target.value));
  $('btn-clear-view').addEventListener('click', () => {
    const activeTab = getActiveLogTab();
    if (!activeTab) return;

    // 只清视图不删文件：进程可能正持有文件句柄，截断它会让写入位置错乱
    activeTab.lines = [];
    $('console').innerHTML = '';
    toast('已清空当前视图（日志文件保留在 logs/ 目录）', 'ok', 3000);
  });
  $('f-autoscroll').addEventListener('change', (event) => {
    const activeTab = getActiveLogTab();
    if (!activeTab) return;

    activeTab.autoscroll = event.target.checked;
    if (activeTab.autoscroll) renderConsole();
  });

  // Esc 关闭最上层弹窗或日志面板
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;

    // 先检查日志面板是否打开
    if (!$('log-panel').hidden) {
      closeLogPanel();
      return;
    }

    // 再检查弹窗
    for (const name of ['confirm', 'form']) {
      if (!$(`modal-${name}`).hidden) {
        closeModal(name);
        break;
      }
    }
  });

  // 切到后台就停止轮询，省电省 CPU
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopPolling();
    } else {
      refresh();
      startPolling();
    }
  });
}

// ------------------------------------------------------------------ 启动

(async function init() {
  bindEvents();
  await refresh();
  startPolling();
})();
