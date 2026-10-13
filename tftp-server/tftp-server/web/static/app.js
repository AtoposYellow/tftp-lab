/* ==================================================================
   TFTP 服务管理台前端逻辑（原生 JS，无依赖）
   结构：1 工具函数 / 2 状态存储 / 3 API / 4 路由 / 5 状态与启停
         6 概览渲染 / 7 传输渲染与详情抽屉 / 8 文件 / 9 日志
         10 设置与服务操作 / 11 通用交互（Toast/确认/复制）/ 12 启动
   ================================================================== */
'use strict';

/* ---------------- 1. 工具函数 ---------------- */

const $ = (id) => document.getElementById(id);

/** HTML 转义：所有动态文本插入 DOM 前必须经过本函数，避免注入 */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(2) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n || 0) + ' B';
}
function fmtSpeed(bps) {
  if (bps >= 1048576) return (bps / 1048576).toFixed(2) + ' MB/s';
  if (bps >= 1024) return (bps / 1024).toFixed(1) + ' KB/s';
  return (bps || 0).toFixed(0) + ' B/s';
}
function fmtDuration(sec) {
  if (sec >= 3600) return Math.floor(sec / 3600) + ' 时 ' + Math.floor((sec % 3600) / 60) + ' 分';
  if (sec >= 60) return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  if (sec >= 10) return Math.round(sec) + ' 秒';
  return (sec || 0).toFixed(1) + ' 秒';
}
function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = String(Math.floor((sec % 86400) / 3600)).padStart(2, '0');
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
  const s = String(Math.floor(sec % 60)).padStart(2, '0');
  return d > 0 ? `${d} 天 ${h}:${m}:${s}` : `${h}:${m}:${s}`;
}
/** 根目录缩略：仅保留最后两级（悬停 title 显示完整路径） */
function shortenPath(p) {
  const parts = String(p || '').replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 2) return parts.join('/');
  return '…/' + parts.slice(-2).join('/');
}
/** 失败原因短摘要（完整原因保留在详情抽屉） */
function shortReason(err) {
  const s = String(err || '');
  if (s.includes('文件未找到')) return '文件未找到';
  if (s.includes('已经存在')) return '文件已存在';
  if (s.includes('Access violation') || s.includes('越权') || s.includes('路径')) return '访问被拒绝';
  if (s.includes('超时') || s.includes('ACK')) return '传输超时';
  if (s.includes('中断') || s.includes('重置')) return '客户端中断';
  if (s.includes('磁盘')) return '磁盘错误';
  if (!s) return '';
  return s.length > 12 ? s.slice(0, 12) + '…' : s;
}
/** 客户端概览显示只保留 IP（端口进入详情） */
function ipOnly(client) {
  const i = String(client || '').lastIndexOf(':');
  return i > 0 ? client.slice(0, i) : client;
}
/** 概览只呈现事件结论；诊断原文留在日志页。 */
function recentSummary(log) {
  const message = String(log.message || '');
  if (log.event === 'success' || log.event === 'failure') {
    const file = /文件="([^"]+)"/.exec(message)?.[1];
    const action = message.startsWith('上传') ? '上传' : message.startsWith('下载') ? '下载' : '传输';
    const result = log.event === 'success' ? '成功' : '失败';
    const reason = log.event === 'failure' ? shortReason(message.split('原因：')[1] || '') : '';
    return `${file || '文件'}${action}${result}${reason ? ' · ' + reason : ''}`;
  }
  if (log.event === 'retransmit') return '传输发生重传';
  if (log.event === 'denied') return '请求被拒绝';
  if (message.includes('Web 管理界面已启动')) return '管理界面已启动';
  if (message.includes('TFTP 服务器已启动')) return 'TFTP 服务已启动';
  if (message.includes('TFTP 服务器已停止')) return 'TFTP 服务已停止';
  if (message.includes('配置已更新')) return '服务器设置已更新';
  return '系统事件';
}
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');

/* ---------------- 2. 状态存储 ---------------- */

const store = {
  view: 'overview',          // 当前视图
  state: null,               // 最近一次 /api/state
  connected: true,           // 管理接口连通性
  transferFilter: 'all',     // 传输筛选
  lastServerError: '',       // 最近一次启停失败的提示
  logs: [],                  // 内存中的日志（最多 500 条）
  lastLogId: 0,              // 日志增量游标（与筛选无关，始终前进）
  logFilter: 'all',
  hideHttp: true,
  followPaused: false,       // 用户向上滚动时暂停跟随
  filesPath: '',             // 当前浏览的相对目录
  filesSearch: '',
  files: [],                 // 最近一次目录列表
  seenTids: new Set(),       // 已渲染过的传输（用于新增行动画）
  seenTidsInit: false,
  rowCache: new Map(),       // tid -> tr（ keyed 行复用，保证进度条平滑过渡）
  miniCache: new Map(),
  drawer: null,              // {type:'transfer', tid} | {type:'file', path}
  restarting: false,
};

const VIEW_META = {
  overview: { title: '概览', sub: '监控 TFTP 服务运行状态和传输活动' },
  transfers: { title: '传输', sub: '管理和监控文件传输任务' },
  files: { title: '文件与日志', sub: '管理服务器文件，查看运行日志' },
  settings: { title: '设置', sub: '配置 TFTP 服务参数' },
};

const LOG_BADGE = {
  request: '请求', success: '成功', failure: '失败', retransmit: '重传',
  denied: '拒绝', system: '系统', http: 'HTTP',
};

/* ---------------- 3. API ---------------- */

async function api(url, opts) {
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}
const getState = () => api('/api/state');
const getLogs = () => api('/api/logs?after_id=' + store.lastLogId);
const getFiles = (path) => api('/api/files' + (path ? '?path=' + encodeURIComponent(path) : ''));

/* ---------------- 4. 路由 ---------------- */

function setView(name) {
  if (!VIEW_META[name]) return;
  store.view = name;
  document.querySelectorAll('.nav-item').forEach((btn) => {
    const active = btn.dataset.nav === name;
    btn.classList.toggle('is-active', active);
    if (active) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
  document.querySelectorAll('.view').forEach((sec) => {
    const active = sec.dataset.view === name;
    sec.hidden = !active;
    sec.classList.remove('enter');
  });
  const sec = document.querySelector(`.view[data-view="${name}"]`);
  void sec.offsetWidth;              // 重启动画
  sec.classList.add('enter');

  $('pageTitle').textContent = VIEW_META[name].title;
  $('pageSub').textContent = VIEW_META[name].sub;
  $('btnRefreshView').hidden = !(name === 'transfers' || name === 'files');

  if (name === 'settings') fillSettings();
  if (name === 'files') { refreshFiles(); rebuildLogs(); }
  if (name === 'transfers') renderTransfers();
  if (name === 'overview') renderOverview();
}

/* ---------------- 5. 状态轮询与启停 ---------------- */

let pulseLastRunning = null;

/** 每秒执行：拉取状态并渲染当前视图 */
async function fetchState() {
  let st;
  try {
    st = await getState();
    setConnected(true);
  } catch (e) {
    setConnected(false);
    return;
  }
  store.state = st;
  renderStatusUI(st);
  if (store.view === 'overview') renderOverview();
  else if (store.view === 'transfers') renderTransfers();
  else if (store.view === 'settings') renderOpsArea(st);
  updateDrawerLive();
}

function setConnected(ok) {
  if (store.connected === ok) return;
  store.connected = ok;
  if (!ok) {
    $('globalText').textContent = '连接异常';
    $('globalDot').className = 'dot err';
    $('navStatusText').textContent = '连接异常';
    $('navDot').className = 'dot sm err';
    toast('无法连接管理接口，正在重试…', true);
  }
}

/** 状态相关 UI：顶栏胶囊 / 侧边栏 / 概览状态卡 / 设置服务操作区 */
function renderStatusUI(st) {
  const running = !!st.server.running;
  // 状态切换时给状态点一次性 pulse
  if (pulseLastRunning !== null && pulseLastRunning !== running) {
    [$('globalDot'), $('svcDot'), $('opsDot')].forEach((d) => {
      d.classList.remove('pulse');
      void d.offsetWidth;
      d.classList.add('pulse');
    });
  }
  pulseLastRunning = running;

  const text = store.connected ? (running ? '运行中' : '已停止') : '连接异常';
  $('globalText').textContent = text;
  $('globalDot').className = 'dot ' + (store.connected ? (running ? 'on' : 'off') : 'err');
  $('navStatusText').textContent = text;
  $('navDot').className = 'dot sm ' + (store.connected ? (running ? 'on' : 'off') : 'err');

  // 概览服务状态卡
  $('svcBadge').textContent = running ? '运行中' : '已停止';
  $('svcBadge').className = 'badge ' + (running ? 'ok' : 'fail');
  $('svcDot').className = 'svc-dot ' + (running ? 'on' : 'off');
  $('svcPort').textContent = 'UDP :' + st.server.tftp_port;
  const rootEl = $('svcRoot');
  rootEl.textContent = '根目录 ' + shortenPath(st.server.root_dir);
  rootEl.title = st.server.root_dir;
  const btn = $('btnToggleSvc');
  btn.textContent = running ? '停止服务' : '启动服务';
  btn.className = 'btn ' + (running ? 'danger' : 'primary');
  const errEl = $('svcError');
  if (!running && store.lastServerError) {
    errEl.textContent = store.lastServerError;
    errEl.classList.remove('hidden');
  } else {
    errEl.classList.add('hidden');
  }
}

/* ---------------- 6. 概览渲染 ---------------- */

function dirBadgeHtml(direction) {
  return direction === 'upload'
    ? '<span class="dir-badge up"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 16V4M5 9l5-5 5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>上传</span>'
    : '<span class="dir-badge down"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M5 11l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>下载</span>';
}

function progressHtml(t) {
  if (t.state === 'active' && t.progress === null) {
    return '<div class="prog"><div class="prog-bar indet"><i></i></div><span class="prog-pct">—</span></div>';
  }
  const known = t.progress !== null && t.progress !== undefined;
  const pct = t.state === 'success' ? 100 : known ? Math.round(t.progress) : null;
  const cls = t.state === 'success' ? ' done' : (t.state === 'failed' ? ' fail' : '');
  return `<div class="prog"><div class="prog-bar${cls}"><i style="width:${pct ?? 0}%"></i></div><span class="prog-pct">${pct === null ? '—' : pct + '%'}</span></div>`;
}

/** 更新已有进度节点，保留进度条元素以便 CSS 宽度过渡生效。 */
function patchProgress(container, transfer) {
  const template = document.createElement('template');
  template.innerHTML = progressHtml(transfer);
  const next = template.content.firstElementChild;
  const current = container.firstElementChild;
  if (!current || current.querySelector('.prog-bar')?.className !== next.querySelector('.prog-bar')?.className) {
    container.replaceChildren(next);
    return;
  }
  current.querySelector('.prog-bar i').style.width = next.querySelector('.prog-bar i').style.width;
  current.querySelector('.prog-pct').textContent = next.querySelector('.prog-pct').textContent;
}

/** 通用 keyed 渲染：复用已有 DOM 节点，保证进度条宽度过渡平滑 */
function keyedRender(container, items, keyFn, buildFn, patchFn, animateNew) {
  const seen = new Set();
  items.forEach((item, idx) => {
    const key = keyFn(item);
    let node = animateNew.cache.get(key);
    if (!node) {
      node = buildFn(item);
      animateNew.cache.set(key, node);
    }
    patchFn(node, item);
    if (container.children[idx] !== node) container.insertBefore(node, container.children[idx] || null);
    seen.add(key);
  });
  Array.from(container.children).forEach((child) => {
    const key = child.dataset.key;
    if (!seen.has(key)) {
      animateNew.cache.delete(key);
      container.removeChild(child);
    }
  });
}

function renderOverview() {
  const st = store.state;
  if (!st) return;
  $('mActive').textContent = st.transfers.active.length;
  $('mActiveSub').textContent = st.transfers.active.length
    ? '当前正在进行的任务' : '当前没有正在进行的任务';
  $('mTotal').textContent = st.stats.transfers_total;
  const total = st.stats.transfers_total;
  $('mRate').textContent = total ? Math.round(st.stats.transfers_ok * 100 / total) + '%' : '—';
  $('mRateSub').textContent = total
    ? `成功 ${st.stats.transfers_ok} / 失败 ${st.stats.transfers_failed} · 累计重传 ${st.stats.retransmits_total} 次`
    : '暂无传输记录';
  $('mBytes').textContent = fmtSize(st.stats.bytes_total);

  // 活动传输摘要（keyed 复用）
  const cache = store.miniCache;
  const box = $('ovActive');
  const items = st.transfers.active;
  if (!items.length) {
    box.innerHTML = `<div class="empty"><div class="empty-ic">
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3v9M6.5 8.5 10 12l3.5-3.5M4 15.5h12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div><p class="empty-title">暂无进行中的传输</p><p class="empty-sub">用 TFTP 客户端发起一次上传或下载吧</p></div>`;
    cache.clear();
  } else {
    const empty = box.querySelector('.empty');
    if (empty) empty.remove();
    keyedRender(box, items,
      (t) => String(t.tid),
      (t) => {
        const row = document.createElement('div');
        row.className = 'mini-row';
        row.dataset.key = String(t.tid);
        row.innerHTML = `<span data-f="dir"></span>
          <span class="mini-name" data-f="name" title=""></span>
          <span data-f="prog"></span>
          <span class="mini-speed" data-f="speed"></span>`;
        if (store.seenTidsInit && !store.seenTids.has(t.tid)) row.classList.add('row-enter');
        row.addEventListener('click', () => openTransferDrawer(t.tid));
        return row;
      },
      (row, t) => {
        row.querySelector('[data-f=dir]').innerHTML = dirBadgeHtml(t.direction);
        const name = row.querySelector('[data-f=name]');
        name.textContent = t.filename;
        name.title = t.filename;
        patchProgress(row.querySelector('[data-f=prog]'), t);
        row.querySelector('[data-f=speed]').textContent = fmtSpeed(t.speed_now);
      },
      { cache });
    markSeen(items);
  }

  // 最近活动（取最近 5 条业务/系统日志）
  const interesting = store.logs
    .filter((l) => ['success', 'failure', 'retransmit', 'denied', 'system'].includes(l.event))
    .slice(-5).reverse();
  const icons = {
    success: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4.5 10.5 3.5 3.5 7.5-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    failure: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    retransmit: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16.5 10a6.5 6.5 0 1 1-1.9-4.6M16.5 2.5v3.4h-3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    denied: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5 5 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    system: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 6v5M10 14h.01" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const colors = {
    success: 'var(--success-soft)', failure: 'var(--danger-soft)',
    retransmit: 'var(--warning-soft)', denied: 'var(--danger-soft)',
    system: 'var(--surface-soft)',
  };
  const iconColors = {
    success: 'var(--success)', failure: 'var(--danger)', retransmit: '#b07814',
    denied: 'var(--danger)', system: 'var(--text-muted)',
  };
  $('ovRecent').innerHTML = interesting.length
    ? interesting.map((l) => `<li>
        <span class="r-ic" style="background:${colors[l.event] || colors.system};color:${iconColors[l.event] || iconColors.system}">${icons[l.event] || icons.system}</span>
        <div class="r-main">
          <div class="r-msg">${esc(recentSummary(l))}</div>
          <div class="r-time">${esc(l.time)}</div>
        </div>
      </li>`).join('')
    : '<li><div class="empty slim"><p>暂无活动记录</p></div></li>';
}

function markSeen(items) {
  items.forEach((t) => store.seenTids.add(t.tid));
  store.seenTidsInit = true;
}

/* ---------------- 7. 传输渲染与详情抽屉 ---------------- */

function mergedTransfers() {
  const st = store.state;
  if (!st) return [];
  const active = st.transfers.active;
  const history = st.transfers.history.slice(0, 30);
  const all = [...active, ...history];
  const f = store.transferFilter;
  if (f === 'all') return all;
  return all.filter((t) => {
    if (f === 'upload') return t.direction === 'upload';
    if (f === 'download') return t.direction === 'download';
    if (f === 'active') return t.state === 'active';
    if (f === 'success') return t.state === 'success';
    if (f === 'failed') return t.state === 'failed';
    return true;
  });
}

function renderTransfers() {
  const st = store.state;
  if (!st) return;
  const all = mergedTransfers();

  // 筛选计数
  const count = {
    all: st.transfers.active.length + st.transfers.history.length,
    upload: 0, download: 0, active: 0, success: 0, failed: 0,
  };
  [...st.transfers.active, ...st.transfers.history].forEach((t) => {
    count[t.direction]++;
    count[t.state]++;
  });
  document.querySelectorAll('#transferFilters .chip').forEach((chip) => {
    const f = chip.dataset.filter;
    chip.innerHTML = f === 'all'
      ? `全部 <span class="cnt">(${count.all})</span>`
      : `${chip.dataset.label || (chip.dataset.label = chip.textContent.split('(')[0].trim())} <span class="cnt">(${count[f] ?? 0})</span>`;
  });

  // keyed 行渲染
  const tbody = $('transferBody');
  const buildRow = (t) => {
    const tr = document.createElement('tr');
    tr.dataset.tid = String(t.tid);
    tr.tabIndex = 0;
    tr.setAttribute('role', 'button');
    tr.setAttribute('aria-label', '查看传输详情');
    tr.innerHTML = `
      <td data-f="dir"></td>
      <td class="cell-file"><div class="f-name"></div><div class="f-sub"></div></td>
      <td class="cell-client hide-md"></td>
      <td data-f="prog"></td>
      <td class="cell-num hide-md"><span data-f="speed"></span><span class="sub" data-f="speedSub"></span></td>
      <td class="cell-num hide-sm" data-f="dur"></td>
      <td data-f="status"></td>
      <td class="td-op"><button class="icon-btn" data-f="op" aria-label="查看详情" tabindex="-1">
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="4" cy="10" r="1.6" fill="currentColor"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/><circle cx="16" cy="10" r="1.6" fill="currentColor"/></svg>
      </button></td>`;
    if (store.seenTidsInit && !store.seenTids.has(t.tid)) tr.classList.add('row-enter');
    const open = () => openTransferDrawer(t.tid);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
    return tr;
  };
  const patchRow = (tr, t) => {
    tr.dataset.key = String(t.tid);
    tr.querySelector('[data-f=dir]').innerHTML = dirBadgeHtml(t.direction);
    const name = tr.querySelector('.f-name');
    name.textContent = t.filename;
    name.title = t.filename;
    const sub = tr.querySelector('.f-sub');
    const sizeText = t.total_bytes
      ? `${fmtSize(t.bytes_transferred)} / ${fmtSize(t.total_bytes)}`
      : fmtSize(t.bytes_transferred);
    if (sub.textContent !== sizeText) sub.textContent = sizeText;
    const client = tr.querySelector('.cell-client');
    if (client.textContent !== t.client) client.textContent = t.client;
    client.title = t.client;
    patchProgress(tr.querySelector('[data-f=prog]'), t);
    const speed = t.state === 'active' ? t.speed_now : t.speed_avg;
    const speedEl = tr.querySelector('[data-f=speed]');
    if (speedEl.textContent !== fmtSpeed(speed)) speedEl.textContent = fmtSpeed(speed);
    tr.querySelector('[data-f=speedSub]').textContent = t.state === 'active' ? '实时' : '平均';
    const dur = tr.querySelector('[data-f=dur]');
    const durText = fmtDuration(t.duration);
    if (dur.textContent !== durText) dur.textContent = durText;

    const statusEl = tr.querySelector('[data-f=status]');
    let html;
    if (t.state === 'active') html = '<span class="status-line"><span class="badge run">进行中</span></span>';
    else if (t.state === 'success') html = '<span class="status-line"><span class="badge ok">成功</span></span>';
    else html = `<span class="status-line"><span class="badge fail">失败</span><span class="reason" title="${esc(t.error)}">${esc(shortReason(t.error))}</span></span>`;
    if (statusEl.innerHTML !== html) statusEl.innerHTML = html;
  };
  keyedRender(tbody, all, (t) => String(t.tid), buildRow, patchRow, { cache: store.rowCache });
  markSeen(all);
  $('tfEmpty').hidden = all.length > 0;
}

/* ---- 详情抽屉 ---- */

function openDrawer() {
  const drawer = $('drawer'), mask = $('drawerMask');
  if (!drawer.hidden) return;
  store.drawerReturnFocus = document.activeElement;
  drawer.hidden = false;
  mask.classList.remove('hidden');
  requestAnimationFrame(() => {
    mask.classList.add('mask-show');
    drawer.classList.add('open');
  });
  drawer.querySelector('#drawerClose').focus({ preventScroll: true });
}
function closeDrawer() {
  const drawer = $('drawer'), mask = $('drawerMask');
  if (drawer.hidden) return;
  drawer.classList.remove('open');
  mask.classList.remove('mask-show');
  store.drawer = null;
  const returnFocus = store.drawerReturnFocus;
  setTimeout(() => {
    drawer.hidden = true;
    mask.classList.add('hidden');
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }, REDUCED_MOTION.matches ? 0 : 220);
}

function stateHead(t) {
  if (t.state === 'active') {
    return ['run',
      `<span class="d-ic"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="30 14" stroke-linecap="round"/></svg></span>`,
      `${t.direction_name}进行中`, `#${t.tid} · ${t.filename}`];
  }
  if (t.state === 'success') {
    return ['ok',
      `<span class="d-ic"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4.5 10.5 3.5 3.5 7.5-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`,
      `${t.direction_name}成功`, `#${t.tid} · ${t.filename}`];
  }
  return ['fail',
    `<span class="d-ic"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span>`,
    `${t.direction_name}失败`, `#${t.tid} · ${t.filename}`];
}

function kv(label, value, wrap) {
  return `<div class="kv${wrap ? ' wrap' : ''}"><span>${label}</span><b title="${esc(value)}">${esc(value)}</b></div>`;
}

function transferDrawerHtml(t) {
  const [cls, icon, t1, t2] = stateHead(t);
  const sizeText = t.total_bytes
    ? fmtSize(t.total_bytes)
    : `${fmtSize(t.bytes_transferred)}（已接收）`;
  const keepOpen = $('drawerBody').querySelector('.d-details')?.open ? ' open' : '';
  return `
    <div class="d-state ${cls}">${icon}<div><div class="d-t1">${t1}</div><div class="d-t2">${esc(t2)}</div></div></div>
    ${t.state === 'active' ? `<div class="d-progress">${progressHtml(t)}</div>` : ''}
    <div class="d-sec"><h4>基本信息</h4><div class="kv-list">
      ${kv('方向', t.direction === 'upload' ? '上传（客户端 → 服务器）' : '下载（服务器 → 客户端）')}
      ${kv('文件名', t.filename, true)}
      ${kv('客户端', t.client)}
      ${kv('传输模式', t.mode)}
      ${kv('文件大小', sizeText)}
    </div></div>
    <div class="d-sec"><h4>性能</h4><div class="kv-list">
      ${kv('实时吞吐量', fmtSpeed(t.speed_now))}
      ${kv('平均吞吐量', fmtSpeed(t.speed_avg))}
      ${kv('耗时', fmtDuration(t.duration))}
      ${kv('重传次数', String(t.retransmits) + ' 次')}
    </div></div>
    <div class="d-sec"><h4>完整性</h4><div class="kv-list">
      <div class="kv wrap"><span>MD5</span><b><span class="d-md5"><code>${t.md5 ? esc(t.md5) : '传输结束后生成'}</code>
        ${t.md5 ? `<button class="icon-btn" data-copy="${esc(t.md5)}" aria-label="复制 MD5" title="复制 MD5">
          <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="9" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M13 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
        </button>` : ''}</span></b></div>
    </div></div>
    ${t.state === 'failed' ? `<div class="d-sec"><h4>失败原因</h4><div class="d-error">${esc(t.error)}</div></div>` : ''}
    <div class="d-sec"><h4>协议细节</h4>
      <details class="d-details"${keepOpen}>
        <summary>展开查看协议字段</summary>
        <div class="d-details-body"><div class="kv-list">
          ${kv('内部传输编号', '#' + t.tid)}
          ${kv('当前线上块号', String(t.block))}
          ${kv('已传输字节', fmtSize(t.bytes_transferred || 0))}
          ${kv('进度', t.progress !== null && t.progress !== undefined ? Math.round(t.progress) + '%' : '—')}
        </div></div>
      </details>
    </div>`;
}

function openTransferDrawer(tid) {
  const t = findTransfer(tid);
  if (!t) return;
  store.drawer = { type: 'transfer', tid };
  $('drawerTitle').innerHTML = '<strong>传输详情</strong>';
  const body = $('drawerBody');
  body.innerHTML = transferDrawerHtml(t);
  body.scrollTop = 0;
  openDrawer();
}

function fileIcon(kind) {
  const paths = {
    dir: '<path d="M2.5 5.5c0-1.1.9-2 2-2h3l2 2h6c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2h-11c-1.1 0-2-.9-2-2v-9z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
    img: '<rect x="3" y="4" width="14" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="7.5" cy="8.5" r="1.5" fill="currentColor"/><path d="m4.5 14 3.5-3.5 3 3 2.5-2.5 2 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    arc: '<path d="M10 3v8M7 8l3 3 3-3M4 14.5h12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
    txt: '<path d="M5 2.5h7l3 3V17a.9.9 0 0 1-1 1H5a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1zM12 2.5V6h3M6.5 10h7M6.5 13h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>',
    file: '<path d="M5 2.5h7l3 3V17a.9.9 0 0 1-1 1H5a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1zM12 2.5V6h3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>',
  };
  return `<svg viewBox="0 0 20 20" aria-hidden="true">${paths[kind] || paths.file}</svg>`;
}

function fileKind(entry) {
  if (entry.is_dir) return 'dir';
  const m = /\.([a-z0-9]+)$/i.exec(entry.name);
  const ext = m ? m[1].toLowerCase() : '';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico', 'bmp'].includes(ext)) return 'img';
  if (['zip', 'rar', '7z', 'gz', 'tgz', 'bz2', 'xz'].includes(ext)) return 'arc';
  if (['txt', 'md', 'log', 'csv', 'json', 'ini', 'cfg'].includes(ext)) return 'txt';
  return 'file';
}

function openFileDrawer(entry, basePath) {
  const rel = basePath ? basePath + '/' + entry.name : entry.name;
  store.drawer = { type: 'file', path: rel };
  $('drawerTitle').innerHTML = '<strong>文件详情</strong>';
  const kind = fileKind(entry);
  const kindName = { dir: '目录', img: '图片', arc: '压缩包', txt: '文本', file: '文件' }[kind];
  $('drawerBody').innerHTML = `
    <div class="d-state run"><span class="d-ic">${fileIcon(kind)}</span>
      <div><div class="d-t1">${esc(entry.name)}</div><div class="d-t2">${kindName}</div></div></div>
    <div class="d-sec"><h4>基本信息</h4><div class="kv-list">
      ${kv('名称', entry.name, true)}
      ${kv('类型', kindName)}
      ${entry.is_dir ? kv('大小', '—') : kv('大小', fmtSize(entry.size))}
      ${kv('修改时间', new Date(entry.mtime * 1000).toLocaleString('zh-CN'))}
      ${kv('相对路径', rel, true)}
    </div></div>
    <div class="d-sec"><h4>操作</h4><div class="kv-list">
      <div class="kv"><span>复制文件名</span><b><button class="icon-btn" data-copy="${esc(entry.name)}" aria-label="复制文件名" title="复制文件名">
        <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="9" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M13 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
      </button></b></div>
    </div></div>`;
  $('drawerBody').scrollTop = 0;
  openDrawer();
}

function findTransfer(tid) {
  const st = store.state;
  if (!st) return null;
  return st.transfers.active.find((t) => t.tid === tid)
    || st.transfers.history.find((t) => t.tid === tid);
}

/** 抽屉打开期间每秒跟随刷新（保留滚动位置与折叠区状态） */
function updateDrawerLive() {
  if (!store.drawer || store.drawer.type !== 'transfer') return;
  const t = findTransfer(store.drawer.tid);
  const body = $('drawerBody');
  if (!t) { closeDrawer(); return; }
  const scrollY = body.scrollTop;
  const next = transferDrawerHtml(t);
  if (body.innerHTML !== next) {
    const focusedCopy = document.activeElement?.closest?.('[data-copy]');
    body.innerHTML = next;
    body.scrollTop = scrollY;
    if (focusedCopy) body.querySelector('[data-copy]')?.focus({ preventScroll: true });
  }
}

/* ---------------- 8. 文件 ---------------- */

async function refreshFiles() {
  try {
    const data = await getFiles(store.filesPath);
    if (data.error) { toast(data.error, true); return; }
    store.files = data.entries || [];
    renderFiles();
  } catch (e) { /* 连接异常由全局状态提示 */ }
}

function renderFiles() {
  const kw = store.filesSearch.trim().toLowerCase();
  const entries = store.files.filter((f) => !kw || f.name.toLowerCase().includes(kw));

  // 面包屑
  const crumbs = $('fileCrumbs');
  const parts = store.filesPath ? store.filesPath.split('/') : [];
  let html = `<button data-p="">根目录</button>`;
  let acc = '';
  parts.forEach((p) => {
    acc = acc ? acc + '/' + p : p;
    html += `<span class="sep">/</span><button data-p="${esc(acc)}">${esc(p)}</button>`;
  });
  html += `<span class="sep">/</span><span class="cur">${entries.length} 项</span>`;
  crumbs.innerHTML = html;
  crumbs.querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => { store.filesPath = b.dataset.p; refreshFiles(); }));

  const list = $('fileList');
  $('filesEmpty').hidden = entries.length > 0;
  list.innerHTML = entries.map((f) => {
    const kind = fileKind(f);
    return `<div class="file-row" data-name="${esc(f.name)}" data-dir="${f.is_dir ? 1 : 0}" tabindex="0" role="button"
      aria-label="${f.is_dir ? '打开目录' : '查看文件'} ${esc(f.name)}">
      <span class="file-ic ${kind}">${fileIcon(kind)}</span>
      <span class="file-name" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="file-size">${f.is_dir ? '—' : fmtSize(f.size)}</span>
      <span class="file-mtime">${new Date(f.mtime * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
      <span class="file-chevron"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5.5 5.5-5.5 5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
    </div>`;
  }).join('');

  list.querySelectorAll('.file-row').forEach((row) => {
    const activate = () => {
      const entry = store.files.find((f) => f.name === row.dataset.name);
      if (!entry) return;
      if (entry.is_dir) {
        store.filesPath = store.filesPath ? store.filesPath + '/' + entry.name : entry.name;
        refreshFiles();
      } else {
        openFileDrawer(entry, store.filesPath);
      }
    };
    row.addEventListener('click', activate);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter') activate(); });
  });
}

/* ---------------- 9. 日志 ---------------- */

function logVisible(l) {
  if (store.hideHttp && l.event === 'http') return false;
  if (store.logFilter !== 'all' && l.event !== store.logFilter) return false;
  return true;
}

function logRowHtml(l) {
  const badge = LOG_BADGE[l.event] || '信息';
  return `<div class="log-row ev-${esc(l.event)}">
    <span class="log-time">${esc(l.time)}</span>
    <span class="log-badge">${esc(badge)}</span>
    <span class="log-msg" title="${esc(l.message)}">${esc(l.message)}</span>
  </div>`;
}

function appendLogRows(entries) {
  const box = $('logTimeline');
  const empty = box.querySelector('.log-empty');
  if (empty) empty.remove();
  const frag = document.createElement('template');
  frag.innerHTML = entries.map(logRowHtml).join('');
  while (frag.content.firstChild) box.appendChild(frag.content.firstChild);
  // 上限保护：DOM 超过 600 行时从顶部裁剪
  while (box.children.length > 600) box.removeChild(box.firstChild);
  maybeFollow();
}

function rebuildLogs() {
  const box = $('logTimeline');
  const visible = store.logs.filter(logVisible);
  box.innerHTML = visible.length
    ? visible.map(logRowHtml).join('')
    : '<div class="log-empty">暂无日志</div>';
  maybeFollow(true);
}

function isNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}
function maybeFollow(force) {
  const followOn = $('logFollowChk').checked;
  const box = $('logTimeline');
  if (!followOn) { store.followPaused = false; $('logResume').classList.add('hidden'); return; }
  if (force || !store.followPaused) {
    scrollToBottom(force);
    store.followPaused = false;
    $('logResume').classList.add('hidden');
  }
}

/** 程序滚动到底部：平滑滚动期间抑制 scroll 监听，避免把自己误判为"向上滚动" */
function scrollToBottom(smooth) {
  const box = $('logTimeline');
  store.programmaticScroll = true;
  box.scrollTop = box.scrollHeight;
  clearTimeout(store._progTimer);
  store._progTimer = setTimeout(() => { store.programmaticScroll = false; },
    smooth && !REDUCED_MOTION.matches ? 600 : 80);
}

async function fetchLogs() {
  let data;
  try { data = await getLogs(); } catch (e) { return; }
  if (!data.logs || !data.logs.length) return;
  store.lastLogId = Math.max(store.lastLogId, ...data.logs.map((l) => Number(l.id) || 0));
  data.logs.forEach((l) => store.logs.push(l));
  if (store.logs.length > 500) store.logs = store.logs.slice(-500);
  const fresh = data.logs.filter(logVisible);
  if (store.view === 'files' && fresh.length) appendLogRows(fresh);
}

/* ---------------- 10. 设置与服务操作 ---------------- */

function fillSettings() {
  const st = store.state;
  if (!st) return;
  const s = st.server;
  $('cfgPort').value = s.tftp_port;
  $('cfgWebPort').value = s.web_port;
  $('cfgRoot').value = s.root_dir;
  $('cfgTimeout').value = s.timeout;
  $('cfgRetries').value = s.retries;
  $('cfgGrace').value = s.grace_seconds;
  $('cfgMax').value = s.max_concurrent;
  $('cfgOverwrite').checked = !!s.overwrite;
  $('advVersion').textContent = 'v' + st.version;
  clearInvalid();
  renderOpsArea(st);
}

function renderOpsArea(st) {
  const running = !!st.server.running;
  $('opsBadge').textContent = running ? '运行中' : '已停止';
  $('opsBadge').className = 'badge ' + (running ? 'ok' : 'fail');
  $('opsDot').className = 'svc-dot ' + (running ? 'on' : 'off');
  $('opsMeta').textContent =
    `UDP :${st.server.tftp_port} · 运行时长 ${fmtUptime(st.server.uptime_s)} · 活动传输 ${st.transfers.active.length} 个`;
  const btn = $('btnStopStart');
  btn.textContent = running ? '停止服务' : '启动服务';
  btn.className = 'btn ' + (running ? 'danger' : 'primary');
}

function clearInvalid() {
  document.querySelectorAll('#settingsForm .invalid').forEach((el) => el.classList.remove('invalid'));
}

function validateSettings() {
  clearInvalid();
  const rules = [
    ['cfgPort', 1, 65535, true],
    ['cfgWebPort', 1, 65535, true],
    ['cfgTimeout', 0.2, 30, false],
    ['cfgRetries', 1, 30, true],
    ['cfgGrace', 0, 60, true],
    ['cfgMax', 1, 512, true],
  ];
  let firstBad = null;
  for (const [id, min, max, isInt] of rules) {
    const el = $(id);
    const v = Number(el.value);
    const bad = el.value === '' || !Number.isFinite(v) || v < min || v > max || (isInt && !Number.isInteger(v));
    if (bad) { el.classList.add('invalid'); firstBad = firstBad || el; }
  }
  const root = $('cfgRoot');
  if (!root.value.trim()) { root.classList.add('invalid'); firstBad = firstBad || root; }
  if (firstBad) {
    firstBad.focus();
    setMsg('请检查标红的输入项', true);
    return null;
  }
  return {
    tftp_port: Number($('cfgPort').value),
    web_port: Number($('cfgWebPort').value),
    root_dir: $('cfgRoot').value.trim(),
    timeout: Number($('cfgTimeout').value),
    retries: Number($('cfgRetries').value),
    grace_seconds: Number($('cfgGrace').value),
    max_concurrent: Number($('cfgMax').value),
    overwrite: $('cfgOverwrite').checked,
  };
}

function setMsg(text, isErr) {
  const el = $('setMsg');
  el.textContent = text;
  el.className = isErr ? 'err' : 'ok';
  if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, 5000);
}

async function saveSettings(e) {
  e.preventDefault();
  const body = validateSettings();
  if (!body) return;
  try {
    const data = await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (data.ok === false) { setMsg(data.error || '保存失败', true); return; }
    const notes = (data.notes || []).join('；');
    setMsg('设置已保存' + (notes ? `（${notes}）` : ''), false);
    toast('设置已保存');
    await fetchState();
    fillSettings();
  } catch (err) {
    setMsg(err.message || '保存失败', true);
  }
}

/* ---- 根目录浏览选择器（服务端目录浏览：浏览器无法直接返回本地绝对路径） ---- */

let dirPickerPath = '/';

function dirCrumbHtml(path) {
  // 面包屑：根 + 逐级子目录
  const norm = String(path || '/').replace(/\\/g, '/');
  const parts = norm.split('/').filter(Boolean);
  let html = `<button data-p="${esc(parts.length ? '/' + parts[0] : '/')}">` +
             (parts.length ? esc('/' + parts[0]) : '/') + `（根）</button>`;
  let acc = parts.length ? '/' + parts[0] : '';
  for (let i = 1; i < parts.length; i++) {
    acc += '/' + parts[i];
    html += `<span class="sep">/</span><button data-p="${esc(acc)}">${esc(parts[i])}</button>`;
  }
  return html;
}

async function navigateDirPicker(path) {
  const list = $('dirList');
  list.innerHTML = '<div class="dir-empty">加载中…</div>';
  let data;
  try {
    data = await api('/api/dirs?path=' + encodeURIComponent(path));
  } catch (e) {
    list.innerHTML = `<div class="dir-empty">${esc(e.message || '加载失败')}</div>`;
    return;
  }
  if (data.error) {
    // 目录不存在（如手动输入过无效路径）：逐级向上回退
    const parent = String(path).replace(/[^/]+\/?$/, '');
    if (parent && parent !== path) return navigateDirPicker(parent || '/');
    list.innerHTML = `<div class="dir-empty">${esc(data.error)}</div>`;
    return;
  }
  dirPickerPath = data.path;
  const crumbs = $('dirCrumbs');
  crumbs.innerHTML = dirCrumbHtml(data.path);
  crumbs.querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => navigateDirPicker(b.dataset.p)));
  $('dirCurrent').textContent = data.path;
  $('dirCurrent').title = data.path;
  list.innerHTML = data.dirs.length
    ? data.dirs.map((name) => `<button class="dir-row" data-d="${esc(name)}">
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 5.5c0-1.1.9-2 2-2h3l2 2h6c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2h-11c-1.1 0-2-.9-2-2v-9z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
        <span>${esc(name)}</span>
      </button>`).join('')
    : '<div class="dir-empty">没有子目录</div>';
  list.querySelectorAll('.dir-row').forEach((row) =>
    row.addEventListener('click', () => {
      navigateDirPicker((dirPickerPath === '/' ? '' : dirPickerPath) + '/' + row.dataset.d);
    }));
  list.scrollTop = 0;
}

function openDirPicker(startPath) {
  $('dirMask').classList.remove('hidden');
  requestAnimationFrame(() => $('dirMask').classList.add('mask-show'));
  $('dirOk').focus();
  navigateDirPicker(startPath || '/');
}

function closeDirPicker() {
  const mask = $('dirMask');
  if (mask.classList.contains('hidden')) return;
  mask.classList.remove('mask-show');
  setTimeout(() => mask.classList.add('hidden'), REDUCED_MOTION.matches ? 0 : 180);
}

/* ---- 确认对话框（Promise 化） ---- */
let confirmResolve = null;

function showConfirm({ title, desc, warn, okText = '确认' }) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $('confirmTitle').textContent = title;
    $('confirmDesc').textContent = desc;
    const warnEl = $('confirmWarn');
    if (warn) { warnEl.textContent = warn; warnEl.classList.remove('hidden'); }
    else warnEl.classList.add('hidden');
    $('confirmOk').textContent = okText;
    $('confirmMask').classList.remove('hidden');
    requestAnimationFrame(() => $('confirmMask').classList.add('mask-show'));
    $('confirmOk').focus();
  });
}

function settleConfirm(result) {
  if (!confirmResolve) return;
  const mask = $('confirmMask');
  mask.classList.remove('mask-show');
  setTimeout(() => mask.classList.add('hidden'), REDUCED_MOTION.matches ? 0 : 180);
  const resolve = confirmResolve;
  confirmResolve = null;
  resolve(result);
}

/* ---- 启动 / 停止 / 重启 ---- */

async function controlService(action) {
  const data = await api('/api/server/' + action, { method: 'POST' });
  if (data.ok === false) throw new Error(data.error || '操作失败');
  return data;
}

async function stopOrStartService() {
  if (store.restarting) return;
  const st = store.state;
  const running = st && st.server.running;
  const activeCount = st ? st.transfers.active.length : 0;
  try {
    if (running) {
      if (activeCount > 0) {
        const ok = await showConfirm({
          title: '停止 TFTP 服务？',
          desc: '停止后客户端将无法连接。',
          warn: `当前有 ${activeCount} 个进行中的传输，将被中断。`,
          okText: '停止服务',
        });
        if (!ok) return;
      }
      await controlService('stop');
      store.lastServerError = '';
      toast('TFTP 服务已停止');
    } else {
      await controlService('start');
      store.lastServerError = '';
      toast('TFTP 服务已启动');
    }
  } catch (err) {
    store.lastServerError = err.message;
    toast(err.message || '操作失败', true);
  }
  await fetchState();
}

async function restartService() {
  if (store.restarting) return;
  const st = store.state;
  const activeCount = st ? st.transfers.active.length : 0;
  const ok = await showConfirm({
    title: '重启 TFTP 服务？',
    desc: '重启会先停止再启动 TFTP 监听，期间客户端无法连接。',
    warn: activeCount > 0
      ? `当前有 ${activeCount} 个进行中的传输，将被中断！`
      : '当前没有进行中的传输。',
    okText: '确认重启',
  });
  if (!ok) return;

  const btn = $('btnRestart');
  const toggle = $('btnStopStart');
  store.restarting = true;
  btn.disabled = true;
  toggle.disabled = true;
  btn.textContent = '重启中…';
  const waitState = (want, timeoutMs) => new Promise((resolve) => {
    const t0 = Date.now();
    const timer = setInterval(async () => {
      let cur = null;
      try { cur = (await getState()).server.running; } catch (e) { /* 忽略单次失败 */ }
      if (cur === want || Date.now() - t0 > timeoutMs) { clearInterval(timer); resolve(cur === want); }
    }, 300);
  });
  try {
    await controlService('stop');
    await waitState(false, 5000);
    await new Promise((r) => setTimeout(r, 400));   // 略等端口释放
    await controlService('start');
    const good = await waitState(true, 5000);
    if (good) { toast('TFTP 服务已重启'); store.lastServerError = ''; }
    else throw new Error('重启后服务未进入运行状态，请检查端口占用');
  } catch (err) {
    store.lastServerError = err.message;
    toast('重启失败：' + err.message, true);
  } finally {
    store.restarting = false;
    btn.disabled = false;
    toggle.disabled = false;
    btn.textContent = '重启服务';
    await fetchState();
  }
}

/* ---------------- 11. 通用交互 ---------------- */

function toast(msg, isErr) {
  const wrap = $('toastWrap');
  const el = document.createElement('div');
  el.className = 'toast' + (isErr ? ' err' : '');
  el.textContent = msg;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), REDUCED_MOTION.matches ? 0 : 200);
  }, 3000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制');
  } catch (e) {
    // 剪贴板 API 不可用（非安全上下文等）时的兜底
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制'); }
    catch (e2) { toast('复制失败', true); }
    ta.remove();
  }
}

/* ---------------- 12. 启动与事件绑定 ---------------- */

function bindEvents() {
  // 导航与跳转
  document.querySelectorAll('.nav-item').forEach((btn) =>
    btn.addEventListener('click', () => setView(btn.dataset.nav)));
  document.querySelectorAll('[data-goto]').forEach((btn) =>
    btn.addEventListener('click', () => setView(btn.dataset.goto)));

  // 概览启停
  $('btnToggleSvc').addEventListener('click', stopOrStartService);

  // 传输筛选
  document.querySelectorAll('#transferFilters .chip').forEach((chip) =>
    chip.addEventListener('click', () => {
      store.transferFilter = chip.dataset.filter;
      document.querySelectorAll('#transferFilters .chip').forEach((c) =>
        c.classList.toggle('is-active', c === chip));
      store.rowCache.clear();
      $('transferBody').innerHTML = '';
      renderTransfers();
    }));

  // 顶栏刷新
  $('btnRefreshView').addEventListener('click', async (e) => {
    const ic = e.currentTarget.querySelector('svg');
    ic.classList.add('spin');
    if (store.view === 'files') await refreshFiles();
    else await fetchState();
    setTimeout(() => ic.classList.remove('spin'), 600);
  });

  // 文件
  $('btnRefreshFiles').addEventListener('click', refreshFiles);
  $('fileSearch').addEventListener('input', (e) => {
    store.filesSearch = e.target.value;
    renderFiles();
  });

  // 日志
  $('logFilterSel').addEventListener('change', (e) => {
    store.logFilter = e.target.value;
    rebuildLogs();
  });
  $('logFollowChk').addEventListener('change', () => maybeFollow());
  $('btnClearLogs').addEventListener('click', () => {
    store.logs = [];
    rebuildLogs();
  });
  const timeline = $('logTimeline');
  timeline.addEventListener('scroll', () => {
    if (store.programmaticScroll) return;     // 程序滚动中，不判定用户意图
    if (!$('logFollowChk').checked) return;   // 跟随关闭时不干预
    if (isNearBottom(timeline)) {
      if (store.followPaused) { store.followPaused = false; $('logResume').classList.add('hidden'); }
    } else if (!store.followPaused) {
      store.followPaused = true;              // 用户向上查看历史：暂停跟随
      $('logResume').classList.remove('hidden');
    }
  });
  $('logResume').addEventListener('click', () => {
    store.followPaused = false;
    $('logResume').classList.add('hidden');
    scrollToBottom(true);
  });

  // 设置
  $('settingsForm').addEventListener('submit', saveSettings);
  $('btnStopStart').addEventListener('click', stopOrStartService);
  $('btnRestart').addEventListener('click', restartService);

  // 根目录浏览选择
  $('btnBrowseRoot').addEventListener('click', () => openDirPicker($('cfgRoot').value.trim()));
  $('dirCancel').addEventListener('click', closeDirPicker);
  $('dirMask').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeDirPicker();
  });
  $('dirOk').addEventListener('click', () => {
    $('cfgRoot').value = dirPickerPath;
    $('cfgRoot').classList.remove('invalid');
    setMsg('已选择目录，点击"保存设置"后生效');
    closeDirPicker();
  });

  // 抽屉
  $('drawerClose').addEventListener('click', closeDrawer);
  $('drawerMask').addEventListener('click', closeDrawer);
  $('drawerBody').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy]');
    if (btn) copyText(btn.dataset.copy);
  });

  // 确认框
  $('confirmOk').addEventListener('click', () => settleConfirm(true));
  $('confirmCancel').addEventListener('click', () => settleConfirm(false));
  $('confirmMask').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) settleConfirm(false);
  });

  // Esc：先关确认框，再关抽屉
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (confirmResolve) { settleConfirm(false); return; }
    if (!$('dirMask').classList.contains('hidden')) { closeDirPicker(); return; }
    if (!$('drawer').hidden) closeDrawer();
  });
}

function init() {
  bindEvents();
  setView('overview');
  fetchState();
  fetchLogs().then(renderOverview);
  setInterval(fetchState, 1000);
  setInterval(fetchLogs, 1000);
  setInterval(() => { if (store.view === 'files') refreshFiles(); }, 5000);
}

init();
