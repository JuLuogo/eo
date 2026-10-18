"""Web 管理页面的前端资源（纯静态，无外部依赖，内网也能正常渲染）。"""

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>边缘节点优选 · 控制台</title>
<style>
  :root{
    --bg:#0f1419; --panel:#171d26; --panel2:#1e2530; --line:#2b3442;
    --text:#e6edf3; --dim:#8b98a5; --accent:#3b82f6; --ok:#22c55e;
    --warn:#f59e0b; --err:#ef4444; --mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:14px;padding:14px 20px;background:var(--panel);
         border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10;flex-wrap:wrap}
  header h1{font-size:16px;margin:0;font-weight:600}
  .pill{padding:3px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);
        background:var(--panel2);color:var(--dim)}
  .pill.on{color:#052e16;background:var(--ok);border-color:var(--ok);font-weight:600}
  .pill.busy{color:#431407;background:var(--warn);border-color:var(--warn);font-weight:600}
  .grow{flex:1}
  main{padding:18px;display:grid;gap:16px;grid-template-columns:minmax(340px,1fr) minmax(420px,1.4fr);
       align-items:start;max-width:1600px;margin:0 auto}
  @media(max-width:1080px){main{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .card>h2{margin:0;padding:12px 16px;font-size:13px;font-weight:600;color:var(--dim);
           background:var(--panel2);border-bottom:1px solid var(--line);
           display:flex;align-items:center;gap:10px;text-transform:uppercase;letter-spacing:.5px}
  .card>.body{padding:16px}
  label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
  input,select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);
               background:#0d1218;color:var(--text);font-size:13px;font-family:inherit}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .field{margin-bottom:12px}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  button{padding:9px 15px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);
         color:var(--text);cursor:pointer;font-size:13px;font-family:inherit;font-weight:500}
  button:hover{border-color:var(--accent)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.danger{background:#3b1414;border-color:#7f1d1d;color:#fecaca}
  .hint{font-size:11.5px;color:var(--dim);margin-top:5px;line-height:1.5}
  .switch{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text);cursor:pointer}
  .switch input{width:auto}
  /* 进度 */
  .bar{height:10px;border-radius:999px;background:#0d1218;border:1px solid var(--line);overflow:hidden}
  .bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#2563eb,#22c55e);
         transition:width .3s ease}
  .prog-head{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);margin-bottom:6px}
  .plat-prog{display:grid;grid-template-columns:90px 1fr 130px;gap:10px;align-items:center;
             font-size:12px;margin-top:8px}
  .plat-prog .t{color:var(--dim)}
  /* 日志 */
  #log{height:420px;overflow-y:auto;background:#0b0f14;border:1px solid var(--line);
       border-radius:8px;padding:10px;font-family:var(--mono);font-size:12px;line-height:1.6}
  #log div{white-space:pre-wrap;word-break:break-all}
  #log .t{color:#4b5563;margin-right:6px}
  .lv-warn{color:var(--warn)} .lv-error{color:var(--err)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500;font-size:11.5px}
  .tag{font-size:11px;padding:2px 7px;border-radius:5px;background:var(--panel2);
       border:1px solid var(--line);color:var(--dim)}
  .tag.ok{color:var(--ok);border-color:#14532d}
  .tag.off{color:var(--dim)}
  details{border:1px solid var(--line);border-radius:8px;padding:0;margin-bottom:10px;background:var(--panel2)}
  details>summary{padding:10px 12px;cursor:pointer;font-size:13px;display:flex;gap:8px;align-items:center}
  details[open]>summary{border-bottom:1px solid var(--line)}
  details .inner{padding:12px}
  .toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--line);
         border-left:3px solid var(--accent);padding:11px 15px;border-radius:8px;font-size:13px;
         opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;max-width:420px}
  .toast.show{opacity:1;transform:none}
  .toast.err{border-left-color:var(--err)} .toast.ok{border-left-color:var(--ok)}
</style>
</head>
<body>
<header>
  <h1>边缘节点优选 · 控制台</h1>
  <span class="pill" id="p-state">空闲</span>
  <span class="pill" id="p-cycle">第 0 轮</span>
  <span class="pill" id="p-next">—</span>
  <span class="grow"></span>
  <button id="btn-run">▶ 开始循环</button>
  <button id="btn-stop" class="danger">■ 停止</button>
  <button id="btn-once">⟳ 立刻跑一轮</button>
</header>

<main>
  <!-- 左栏：配置 -->
  <div style="display:grid;gap:16px">
    <section class="card">
      <h2>常用变量 <span class="pill" id="v-src">—</span>
        <span class="grow"></span>
        <span class="pill" id="v-envfile">—</span>
      </h2>
      <div class="body">
        <div class="hint" style="margin-bottom:12px">
          这里列出的变量既可以写在 <code>.env</code> 里，也可以直接在下面改。
          生效规则：<b>网页改过就用网页的值</b>，否则用 <code>.env</code> 的值，都没有才用默认值。
          「来源」列会告诉你当前到底哪个在生效。
        </div>
        <table id="vars-table">
          <tr><th>变量</th><th>当前生效值</th><th>来源</th><th>在 .env 里</th><th>在网页里</th></tr>
        </table>
        <div class="grid3" style="margin-top:14px">
          <div class="field">
            <label>并发连接上限（连接/秒，全平台合计）</label>
            <input type="number" id="v-rate" min="0" step="10">
          </div>
          <div class="field">
            <label>轮询间隔（秒）</label>
            <input type="number" id="v-interval" min="60" step="30">
          </div>
          <div class="field">
            <label>时间窗口（留空=全天）</label>
            <input type="text" id="v-window" placeholder="00:00-07:00">
          </div>
          <div class="field">
            <label>单平台并发数</label>
            <input type="number" id="v-workers" min="1" step="10">
          </div>
          <div class="field">
            <label>探测超时（秒）</label>
            <input type="number" id="v-timeout" min="1" step="0.5">
          </div>
          <div class="field">
            <label>失败重试次数</label>
            <input type="number" id="v-retries" min="0" step="1">
          </div>
          <div class="field">
            <label>日志保留条数</label>
            <input type="number" id="v-logbuf" min="50" step="50">
          </div>
          <div class="field">
            <label>DNS 下发模式</label>
            <select id="v-dnsmode">
              <option value="none">关闭</option>
              <option value="dry-run">预览</option>
              <option value="push">执行</option>
            </select>
          </div>
          <div class="field">
            <label>通知 Webhook</label>
            <input type="text" id="v-webhook" placeholder="可选">
          </div>
        </div>
        <div class="row" style="margin-top:6px">
          <button class="primary" id="btn-vsave">保存常用变量</button>
          <button id="btn-vreload">重新读取</button>
          <span class="hint" style="margin:0">保存后下一轮扫描即生效</span>
        </div>

        <details style="margin-top:14px">
          <summary>部署信息（只读，来自 .env 与环境变量）</summary>
          <div class="inner"><table id="info-table"></table></div>
        </details>
      </div>
    </section>

    <section class="card">
      <h2>运行参数（细分项）</h2>
      <div class="body">
        <div class="grid2">
          <div class="field">
            <label>并发连接上限（连接/秒，全平台合计）</label>
            <input type="number" id="c-rate" min="0" step="10">
            <div class="hint">0 表示不限速。默认 100，用于避免影响同网络的其他使用者。</div>
          </div>
          <div class="field">
            <label>轮询间隔（秒，最小 60）</label>
            <input type="number" id="c-interval" min="60" step="30">
          </div>
          <div class="field">
            <label>时间窗口（留空=全天）</label>
            <input type="text" id="c-window" placeholder="00:00-07:00">
          </div>
          <div class="field">
            <label>日志保留条数（内存环形缓冲）</label>
            <input type="number" id="c-logbuf" min="50" step="50">
          </div>
          <div class="field">
            <label>并发数（每平台线程数）</label>
            <input type="number" id="c-workers" min="1" step="10">
          </div>
          <div class="field">
            <label>探测超时（秒）</label>
            <input type="number" id="c-timeout" min="1" step="0.5">
          </div>
          <div class="field">
            <label>失败重试次数</label>
            <input type="number" id="c-retries" min="0" step="1">
          </div>
          <div class="field">
            <label>通知 Webhook（可选）</label>
            <input type="text" id="c-webhook" placeholder="飞书/企业微信机器人地址">
          </div>
        </div>
        <div class="row" style="gap:18px;margin:6px 0 12px">
          <label class="switch"><input type="checkbox" id="c-ping"> 启用 ICMP 预筛选（不推荐）</label>
          <label class="switch"><input type="checkbox" id="c-verbose"> 日志逐 IP 输出</label>
          <label class="switch"><input type="checkbox" id="c-notify"> 每轮都通知</label>
          <label class="switch"><input type="checkbox" id="c-autorun"> 启动即开始循环</label>
        </div>
        <div class="row">
          <button class="primary" id="btn-save">保存配置</button>
          <span class="hint" style="margin:0">保存后下一轮立即生效</span>
        </div>
      </div>
    </section>

    <section class="card">
      <h2>平台设置</h2>
      <div class="body" id="platforms"></div>
    </section>

    <section class="card">
      <h2>DNS 下发</h2>
      <div class="body">
        <div class="grid3" style="margin-bottom:14px">
          <div class="field">
            <label>下发模式</label>
            <select id="d-dnsmode">
              <option value="none">关闭（只优选不下发）</option>
              <option value="dry-run">预览（只打印将要下发的记录）</option>
              <option value="push">执行（真实修改线上解析）</option>
            </select>
          </div>
          <div class="field">
            <label>确认开关</label>
            <label class="switch" style="margin-top:8px">
              <input type="checkbox" id="d-dnsconfirm"> 我确认允许修改线上解析
            </label>
          </div>
          <div class="field">
            <label>&nbsp;</label>
            <div class="row">
              <button id="btn-preview">预览下发</button>
              <button class="danger" id="btn-push">立即下发</button>
            </div>
          </div>
        </div>
        <div class="hint" style="margin-bottom:12px">
          安全设计：模式下必须选「执行」且勾选确认，才会真正改 DNS；任一条件不满足自动降级为预览。
        </div>
        <div id="dns-panels"></div>
      </div>
    </section>
  </div>

  <!-- 右栏：进度 + 日志 -->
  <div style="display:grid;gap:16px">
    <section class="card">
      <h2>任务状态 <span class="pill" id="p-phase">idle</span>
        <span class="grow"></span>
        <span class="pill" id="p-sched">—</span>
      </h2>
      <div class="body">
        <table>
          <tr><th style="width:34%">项目</th><th>值</th></tr>
          <tr><td>运行状态</td><td id="t-state">空闲</td></tr>
          <tr><td>本轮类型</td><td id="t-kind">—</td></tr>
          <tr><td>已完成轮数</td><td id="t-cycle">0</td></tr>
          <tr><td>当前轮间隔</td><td id="t-interval">—</td></tr>
          <tr><td>上轮结束时间</td><td id="t-last">—</td></tr>
          <tr><td>下轮开始时间</td><td id="t-next">—</td></tr>
          <tr><td>下次倒计时</td><td id="t-countdown">—</td></tr>
          <tr><td>时间窗口</td><td id="t-window">全天</td></tr>
          <tr><td>当前平台</td><td id="t-platforms">—</td></tr>
          <tr><td>最近错误</td><td id="t-error">无</td></tr>
        </table>
      </div>
    </section>

    <section class="card">
      <h2>测试进度 <span class="pill" id="p-phase2">idle</span></h2>
      <div class="body">
        <div class="prog-head"><span id="prog-text">尚未开始</span><span id="prog-pct">0%</span></div>
        <div class="bar"><i id="prog-bar"></i></div>
        <div id="plat-prog"></div>
      </div>
    </section>

    <section class="card">
      <h2>实时日志 <span class="pill" id="p-logcount">0 / 500</span>
        <span class="grow"></span>
        <button id="btn-clear" style="padding:4px 10px;font-size:12px">清空</button>
        <button id="btn-autoscroll" style="padding:4px 10px;font-size:12px">自动滚动：开</button>
      </h2>
      <div class="body" style="padding:12px">
        <div id="log"></div>
        <div class="hint">日志仅保存在内存中，按设定条数自动淘汰最旧记录，不写磁盘、不占空间。</div>
      </div>
    </section>

    <section class="card">
      <h2>最近一轮结果</h2>
      <div class="body"><div id="results" class="hint">暂无数据</div></div>
    </section>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || localStorage.getItem('eo_token') || '';
if (TOKEN) localStorage.setItem('eo_token', TOKEN);
let LOG_CURSOR = 0, AUTOSCROLL = true, STATE = null, PLAT_CACHE = [], CONFIG = null;
/* 平台面板里需要按数字提交的字段（漏了也不会存坏 —— 后端会按字段类型转换，
   但发对类型更清楚，也少一层依赖） */
const NUMERIC_PLATFORM_FIELDS = ['workers','timeout','retries','sample_per_24',
                                 'max_latency_ms','top_n'];

function headers(extra){ const h = Object.assign({'Content-Type':'application/json'}, extra||{});
  if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN; return h; }
function q(path){ return TOKEN ? path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN) : path; }

async function api(path, options){
  const res = await fetch(q(path), Object.assign({headers: headers()}, options||{}));
  if (res.status === 401){ toast('访问令牌无效，请在地址后加 ?token=你的令牌', 'err'); throw new Error('unauthorized'); }
  return res.json();
}
async function post(path, body){ return api(path, {method:'POST', body: JSON.stringify(body||{})}); }

function toast(msg, kind){
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = 'toast show ' + (kind||'');
  clearTimeout(el._t); el._t = setTimeout(()=>{ el.className='toast'; }, 3200);
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

/* ---------------- 日志 ---------------- */
function addLog(item){
  const box = document.getElementById('log');
  const div = document.createElement('div');
  if (item.level === 'warn' || item.level === 'error') div.className = 'lv-' + item.level;
  div.innerHTML = '<span class="t">' + esc(item.time) + '</span>' + esc(item.line);
  box.appendChild(div);
  while (box.childElementCount > CONFIG_MAX_LOGS()) box.removeChild(box.firstChild);
  if (AUTOSCROLL) box.scrollTop = box.scrollHeight;
  document.getElementById('p-logcount').textContent = box.childElementCount + ' / ' + CONFIG_MAX_LOGS();
}
/* 日志有两个来源：SSE 实时推送 + 每 2 秒的轮询补偿。两者会同时到达同一批
   序号（比如断线重连那一下），直接 addLog 就会出现重复行。按 seq 去重：
   已经显示过的序号一律跳过，同时推进游标。 */
function addLogOnce(item){
  if (item && typeof item.seq === 'number'){
    if (item.seq <= LOG_CURSOR) return;
    LOG_CURSOR = item.seq;
  }
  addLog(item);
}
function CONFIG_MAX_LOGS(){ return (CONFIG && CONFIG.log_buffer) || 500; }

async function pollLogs(){
  try{
    const data = await api('/api/logs?after=' + LOG_CURSOR + '&limit=500');
    (data.logs||[]).forEach(it=>{ addLogOnce(it); });
    if (data.capacity) document.getElementById('p-logcount').textContent =
      document.getElementById('log').childElementCount + ' / ' + data.capacity;
  }catch(e){}
}

/* ---------------- 常用变量面板 ---------------- */
function srcTag(source, origin){
  const map = {web: ['网页设置', 'ok'], env: ['环境变量', ''], default: ['默认值', 'off']};
  const [label, cls] = map[source] || ['?', ''];
  // 环境变量再区分来源：Docker / systemd 是注入到进程环境的，容器里并没有 .env 文件
  const tip = origin ? ' title="' + esc(origin) + '"' : '';
  return '<span class="tag ' + cls + '"' + tip + '>' + label + '</span>';
}
function fmtVal(v){
  if (v === '' || v === null || v === undefined) return '<span style="color:#4b5563">—</span>';
  if (typeof v === 'boolean') return v ? '是' : '否';
  return esc(String(v));
}

async function loadVars(){
  const data = await api('/api/vars');
  const tbody = document.getElementById('vars-table');
  let html = '<tr><th>变量</th><th>当前生效值</th><th>来源</th><th>环境变量里的值</th><th>在网页里</th></tr>';
  data.vars.forEach(v=>{
    html += '<tr>' +
      '<td><b>' + esc(v.label) + '</b><br><span class="hint">' + esc(v.key) +
        (v.env_key ? ' · ' + esc(v.env_key) : '') + '</span></td>' +
      '<td><b>' + fmtVal(v.effective) + '</b></td>' +
      '<td>' + srcTag(v.source, v.env_origin) + '</td>' +
      '<td>' + fmtVal(v.env_value) + '</td>' +
      '<td>' + fmtVal(v.web_value) + '</td>' +
      '</tr>';
  });
  tbody.innerHTML = html;

  // 部署信息
  const info = document.getElementById('info-table');
  info.innerHTML = '<tr><th>项目</th><th>值</th></tr>' + (data.info||[]).map(r =>
    '<tr><td>' + esc(r.label) + ' <span class="hint">' + esc(r.key) + '</span></td><td>' +
    esc(r.value || '—') + '</td></tr>').join('');

  document.getElementById('v-envfile').textContent = data.env_exists ? '.env 已加载' : '无 .env';
  document.getElementById('v-src').textContent =
    '网页优先 / 共 ' + (data.vars||[]).length + ' 项';

  // 表单填充（用生效值）
  const byKey = {};
  (data.vars||[]).forEach(v => byKey[v.key] = v.effective);
  const set = (id, key) => { const el = document.getElementById(id); if (el) el.value = byKey[key] ?? ''; };
  set('v-rate','rate'); set('v-interval','interval'); set('v-window','window');
  set('v-workers','workers'); set('v-timeout','timeout'); set('v-retries','retries');
  set('v-logbuf','log_buffer'); set('v-webhook','webhook');
  const dm = document.getElementById('v-dnsmode');
  if (dm) dm.value = byKey['dns_mode'] || 'none';
}

async function saveVars(){
  const patch = {
    rate: Number(document.getElementById('v-rate').value),
    interval: Number(document.getElementById('v-interval').value),
    window: document.getElementById('v-window').value.trim(),
    workers: Number(document.getElementById('v-workers').value),
    timeout: Number(document.getElementById('v-timeout').value),
    retries: Number(document.getElementById('v-retries').value),
    log_buffer: Number(document.getElementById('v-logbuf').value),
    dns_mode: document.getElementById('v-dnsmode').value,
    webhook: document.getElementById('v-webhook').value.trim(),
  };
  const data = await post('/api/config', patch);
  renderConfig(data.config);
  await loadVars();
  toast('常用变量已保存', 'ok');
}

/* ---------------- 状态 / 进度 ---------------- */
function setText(id, value){ const el = document.getElementById(id); if (el) el.textContent = value; }
function fmtTime(epoch){
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleTimeString('zh-CN', {hour12:false});
}
function fmtCountdown(seconds){
  if (seconds <= 0) return '即将开始';
  const m = Math.floor(seconds/60), s = seconds % 60;
  return m > 0 ? (m + ' 分 ' + s + ' 秒') : (s + ' 秒');
}

let LAST_STATE_AT = 0;
function renderStatus(st){
  STATE = st;
  const r = st.runner || {};
  const stateMap = {idle:'空闲', running:'正在执行', sleeping:'等待下一轮', stopping:'停止中'};
  const stateText = stateMap[r.state] || r.state || '—';
  const busy = r.state === 'running';
  const pill = document.getElementById('p-state');
  pill.textContent = stateText;
  pill.className = 'pill' + (busy ? ' busy' : (r.running ? ' on' : ''));
  setText('p-phase', st.progress && st.progress.phase || 'idle');
  setText('p-phase2', st.progress && st.progress.phase || 'idle');

  setText('t-state', stateText);
  setText('t-kind', r.manual ? '手动触发的一次性任务' : (r.running ? '循环任务（定时）' : '无'));
  setText('t-cycle', (r.cycle || 0) + ' 轮');
  // 间隔：本轮实际用的 vs 网页上新配的（改了要下一轮生效，这里明确标出来）
  const ci = r.configured_interval || 0;
  setText('t-interval', r.interval
    ? (r.interval + ' 秒' + (ci && ci !== r.interval ? '（下轮 ' + ci + ' 秒）' : ''))
    : '—');
  setText('t-last', fmtTime(st.last_finished_at || LAST_STATE_AT));
  setText('t-next', fmtTime(r.next_run_at));
  const win = (CONFIG && CONFIG.window) || '';
  setText('t-window', win ? win : '全天（不受限制）');
  setText('t-platforms', (st.platforms||[]).filter(p=>p.enabled).map(p=>p.key).join('、') || '无');
  setText('t-error', r.last_error || '无');
  setText('p-sched', r.next_run_at ? ('下轮 ' + fmtTime(r.next_run_at)) : (r.running ? '运行中' : '已停止'));

  const cd = r.next_run_at ? Math.max(0, r.next_run_at - Math.floor(Date.now()/1000)) : -1;
  // 本轮进行中时倒计时没有意义（结束时间不可预知），明确显示「本轮进行中」
  setText('t-countdown', r.next_run_at ? fmtCountdown(cd)
                         : (r.state === 'running' ? '本轮进行中' : '—'));

  document.getElementById('p-cycle').textContent = '第 ' + (r.cycle||0) + ' 轮';
  document.getElementById('p-next').textContent = r.next_run_at
    ? '下轮 ' + fmtTime(r.next_run_at) : '—';
  document.getElementById('btn-run').disabled = !!r.running;
  document.getElementById('btn-stop').disabled = !r.running;
  document.getElementById('btn-once').disabled = !!r.running;

  const p = st.progress || {};
  const total = p.total||0, done = p.done||0;
  const pct = total ? Math.min(100, Math.round(done/total*100)) : 0;
  document.getElementById('prog-bar').style.width = pct + '%';
  document.getElementById('prog-pct').textContent = pct + '%';
  document.getElementById('prog-text').textContent = total
    ? ('已测 ' + done + ' / ' + total + ' 个 IP') : (p.phase === 'running' ? '正在准备…' : '尚未开始');

  const box = document.getElementById('plat-prog');
  box.innerHTML = '';
  Object.entries(p.platforms||{}).forEach(([key, v])=>{
    const t = v.total||0, d = v.done||0;
    const pct2 = t ? Math.round(d/t*100) : 0;
    const row = document.createElement('div');
    row.className = 'plat-prog';
    row.innerHTML = '<span>' + esc(v.name||key) + '</span>' +
      '<span class="bar"><i style="width:' + pct2 + '%"></i></span>' +
      '<span class="t">' + d + '/' + t + '，可用 ' + (v.alive||0) + '</span>';
    box.appendChild(row);
  });

  const res = st.last_results && st.last_results.platforms;
  if (res){
    let html = '<table><tr><th>平台</th><th>可用</th><th>探测总数</th><th>状态</th></tr>';
    Object.entries(res).forEach(([k,v])=>{
      html += '<tr><td>' + esc(k) + '</td><td>' + (v.alive||0) + '</td><td>' + (v.total||0) +
        '</td><td>' + (v.error ? '<span class="lv-error">' + esc(v.error) + '</span>' : '<span class="tag ok">正常</span>') +
        '</td></tr>';
    });
    setText('t-last', st.last_results.time || fmtTime(st.last_finished_at || LAST_STATE_AT));
    document.getElementById('results').innerHTML = html + '</table>';
  }
}

/* ---------------- 配置渲染 ---------------- */
function renderConfig(cfg){
  CONFIG = cfg;
  document.getElementById('c-rate').value = cfg.rate;
  document.getElementById('c-interval').value = cfg.interval;
  document.getElementById('c-window').value = cfg.window || '';
  document.getElementById('c-logbuf').value = cfg.log_buffer;
  document.getElementById('c-workers').value = cfg.workers;
  document.getElementById('c-timeout').value = cfg.timeout;
  document.getElementById('c-retries').value = cfg.retries;
  document.getElementById('c-webhook').value = cfg.webhook || '';
  document.getElementById('c-ping').checked = !!cfg.ping;
  document.getElementById('c-verbose').checked = !!cfg.verbose;
  document.getElementById('c-notify').checked = !!cfg.notify_always;
  document.getElementById('c-autorun').checked = !!cfg.auto_run;
  document.getElementById('d-dnsmode').value = cfg.dns_mode || 'none';
  document.getElementById('d-dnsconfirm').checked = !!cfg.dns_confirm;
  renderPlatforms(cfg);
  renderDns(cfg);
}

function renderPlatforms(cfg){
  const box = document.getElementById('platforms');
  box.innerHTML = '';
  Object.entries(cfg.platforms||{}).forEach(([key, p])=>{
    const d = document.createElement('details');
    d.innerHTML =
      '<summary><input type="checkbox" data-k="'+key+'" data-f="enabled" '+(p.enabled?'checked':'')+
      ' onclick="event.stopPropagation()"> <b>'+esc(key.toUpperCase())+'</b>'+
      '<span class="tag">'+esc(p.host||'未配置域名')+'</span>'+
      '<span class="tag">'+esc(p.targets_file||'无网段文件')+'</span></summary>'+
      '<div class="inner"><div class="grid2">'+
      field('校验域名（CNAME 到该平台边缘）', key, 'host', p.host) +
      selStatic('协议', key, 'scheme', p.scheme || 'https',
                [['https','https（443，推荐）'],['http','http（80，域名还没配证书时用）']]) +
      field('网段文件路径', key, 'targets_file', p.targets_file) +
      field('结果输出路径', key, 'output_file', p.output_file) +
      field('请求路径', key, 'http_path', p.http_path) +
      field('期望状态码（逗号分隔）', key, 'status_codes', p.status_codes) +
      field('并发数（留空=默认）', key, 'workers', p.workers) +
      field('超时秒数（留空=默认）', key, 'timeout', p.timeout) +
      field('重试次数（留空=默认）', key, 'retries', p.retries) +
      field('粗扫抽样（每 /24 取几个 IP，留空=默认）', key, 'sample_per_24', p.sample_per_24) +
      field('DNS 下发延迟上限（ms，超过不下发；0/空=不限）', key, 'max_latency_ms', p.max_latency_ms) +
      field('DNS 下发数量（只下发最快的 N 个；0/空=全部）', key, 'top_n', p.top_n) +
      field('自动拉取网段（cloudfront / cloudflare，留空=关闭）', key, 'auto_targets', p.auto_targets) +
      '</div><label class="switch"><input type="checkbox" data-k="'+key+'" data-f="ping" '+
      (p.ping?'checked':'')+'> 启用 ICMP 预筛选</label></div>';
    box.appendChild(d);
  });
  box.querySelectorAll('input[type=checkbox]').forEach(cb=>{
    cb.addEventListener('change', ()=>{ savePlatform(cb.dataset.k, cb.dataset.f, cb.checked); });
  });
}
function field(label, key, name, value){
  return '<div class="field"><label>'+esc(label)+'</label>'+
    '<input data-k="'+key+'" data-f="'+name+'" value="'+esc(value==null?'':value)+'"></div>';
}
function selStatic(label, key, name, value, options){
  const opts = options.map(([v,t])=>'<option value="'+v+'"'+(v===value?' selected':'')+'>'+esc(t)+'</option>').join('');
  return '<div class="field"><label>'+esc(label)+'</label>'+
    '<select data-k="'+key+'" data-f="'+name+'">'+opts+'</select></div>';
}

function collectPlatformsPatch(){
  const patch = {};
  document.querySelectorAll('#platforms input[data-k], #platforms select[data-k]').forEach(el=>{
    const k = el.dataset.k, f = el.dataset.f;
    patch[k] = patch[k] || {};
    if (el.type === 'checkbox') patch[k][f] = el.checked;
    else if (el.tagName === 'SELECT') patch[k][f] = el.value;
    else if (el.value !== '') {
      // 数值字段要转成数字再发（后端也会兜一层类型转换，
      // 这里漏一个字段不致命，但发对类型更清楚）
      patch[k][f] = NUMERIC_PLATFORM_FIELDS.includes(f) ? Number(el.value) : el.value;
    } else { patch[k][f] = ''; }
  });
  return patch;
}
async function savePlatform(key, fieldName, value){
  const patch = {platforms:{}}; patch.platforms[key] = {}; patch.platforms[key][fieldName] = value;
  await post('/api/config', patch); toast('已更新 ' + key + '.' + fieldName, 'ok'); await loadAll();
}

function renderDns(cfg){
  const box = document.getElementById('dns-panels');
  box.innerHTML = '';
  Object.entries(cfg.dns||{}).forEach(([key, d])=>{
    const el = document.createElement('details');
    el.innerHTML =
      '<summary><input type="checkbox" data-dk="'+key+'" data-df="enabled" '+(d.enabled?'checked':'')+
      ' onclick="event.stopPropagation()"> <b>'+esc(key.toUpperCase())+'</b>'+
      '<span class="tag">'+esc(d.provider||'未选服务商')+'</span>'+
      '<span class="tag">'+esc(d.record_name||'未填记录名')+'</span></summary>'+
      '<div class="inner"><div class="grid2">'+
      sel('DNS 服务商', key, 'provider', d.provider, [['none','不下发'],['huawei','华为云 DNS'],['cloudflare','Cloudflare']]) +
      field('Zone / 域名', key, 'zone_name', d.zone_name) +
      field('记录名（如 eo-lt.1.example.com）', key, 'record_name', d.record_name) +
      field('区域（华为云 region）', key, 'region', d.region) +
      field('TTL（秒）', key, 'ttl', d.ttl) +
      field('每个记录集 IP 数（<=50）', key, 'batch_size', d.batch_size) +
      field('AccessKey ID', key, 'access_key_id', d.access_key_id, 'secret') +
      field('AccessKey Secret', key, 'access_key_secret', d.access_key_secret, 'secret') +
      field('Cloudflare API Token', key, 'api_token', d.api_token, 'secret') +
      field('Cloudflare Zone ID（可留空自动查）', key, 'zone_id', d.zone_id) +
      '</div><div class="hint">密钥留空表示不修改已保存的值；显示为 ****** 即为已配置。</div></div>';
    box.appendChild(el);
  });
  box.querySelectorAll('input[data-dk]').forEach(cb=>{
    cb.addEventListener('change', async ()=>{
      const patch = {dns:{}}; patch.dns[cb.dataset.dk] = {enabled: cb.checked};
      await post('/api/config', patch); toast('已更新', 'ok'); await loadAll();
    });
  });
}
function sel(label, key, name, value, options){
  let opts = options.map(([v,t])=>'<option value="'+v+'"'+(v===value?' selected':'')+'>'+esc(t)+'</option>').join('');
  return '<div class="field"><label>'+esc(label)+'</label>'+
    '<select data-dk="'+key+'" data-df="'+name+'">'+opts+'</select></div>';
}
function collectDnsPatch(){
  const patch = {};
  document.querySelectorAll('#dns-panels input[data-dk], #dns-panels select[data-dk]').forEach(el=>{
    const k = el.dataset.dk, f = el.dataset.df;
    patch[k] = patch[k] || {};
    if (el.type === 'checkbox') patch[k][f] = el.checked;
    else if (el.tagName === 'SELECT') patch[k][f] = el.value;
    else if (el.value !== '') {
      patch[k][f] = ['ttl','batch_size'].includes(f) ? Number(el.value) : el.value;
    }
  });
  return patch;
}

/* ---------------- 动作 ---------------- */
async function saveAll(){
  const patch = {
    rate: Number(document.getElementById('c-rate').value),
    interval: Number(document.getElementById('c-interval').value),
    window: document.getElementById('c-window').value.trim(),
    log_buffer: Number(document.getElementById('c-logbuf').value),
    workers: Number(document.getElementById('c-workers').value),
    timeout: Number(document.getElementById('c-timeout').value),
    retries: Number(document.getElementById('c-retries').value),
    webhook: document.getElementById('c-webhook').value.trim(),
    ping: document.getElementById('c-ping').checked,
    verbose: document.getElementById('c-verbose').checked,
    notify_always: document.getElementById('c-notify').checked,
    auto_run: document.getElementById('c-autorun').checked,
    dns_mode: document.getElementById('d-dnsmode').value,
    dns_confirm: document.getElementById('d-dnsconfirm').checked,
    platforms: collectPlatformsPatch(),
    dns: collectDnsPatch()
  };
  const data = await post('/api/config', patch);
  renderConfig(data.config);
  toast('配置已保存', 'ok');
}

function bind(){
  document.getElementById('btn-save').onclick = saveAll;
  document.getElementById('btn-vsave').onclick = saveVars;
  document.getElementById('btn-vreload').onclick = () => { loadVars().then(()=>toast('已重新读取')); };
  document.getElementById('btn-run').onclick = async ()=>{
    const r = await post('/api/control/start'); toast(r.message, r.ok?'ok':'err'); refresh(); };
  document.getElementById('btn-stop').onclick = async ()=>{
    const r = await post('/api/control/stop'); toast(r.message, r.ok?'ok':'err'); refresh(); };
  document.getElementById('btn-once').onclick = async ()=>{
    saveAll().catch(()=>{});
    const r = await post('/api/control/run-once'); toast(r.message, r.ok?'ok':'err'); refresh(); };
  document.getElementById('btn-preview').onclick = async ()=>{
    const r = await post('/api/control/dns-preview'); toast(r.message, r.ok?'ok':'err'); };
  document.getElementById('btn-push').onclick = async ()=>{
    if (!document.getElementById('d-dnsconfirm').checked){
      toast('请先勾选「我确认允许修改线上解析」', 'err'); return; }
    if (!confirm('确定要修改线上 DNS 解析吗？')) return;
    const r = await post('/api/control/dns-push'); toast(r.message, r.ok?'ok':'err'); };
  document.getElementById('btn-clear').onclick = async ()=>{
    await post('/api/logs/clear');
    document.getElementById('log').innerHTML=''; LOG_CURSOR=0; toast('日志已清空'); };
  document.getElementById('btn-autoscroll').onclick = (e)=>{
    AUTOSCROLL = !AUTOSCROLL; e.target.textContent = '自动滚动：' + (AUTOSCROLL?'开':'关'); };
}

async function loadAll(){
  const [cfg, st] = await Promise.all([api('/api/config'), api('/api/status')]);
  renderConfig(cfg.config); renderStatus(st);
  await loadVars();
}
async function refresh(){
  // 首次加载失败过就趁机补上：否则页面会一直停在空白状态，
  // 只能靠用户手动刷新 —— 换个网络、服务刚重启时很容易碰到。
  if (!CONFIG || !STATE){
    try{ await loadAll(); return; }catch(e){ return; }
  }
  try{ renderStatus(await api('/api/status')); }catch(e){}
}

/* ---------------- 启动 ---------------- */
(async function(){
  bind();
  // 首屏失败也要继续往下走：后面每 3 秒的 refresh() 会自动把配置补回来。
  // 原来这里是 catch(e){ return; }，一次失败就再也不会轮询，页面永久空白。
  try{
    await loadAll();
  }catch(e){
    toast('首次加载失败，正在自动重试…', 'err');
  }
  // SSE 实时推送；失败自动退化为轮询
  try{
    const es = new EventSource(q('/api/events'));
    es.addEventListener('log', ev=>{ addLogOnce(JSON.parse(ev.data)); });
    es.addEventListener('progress', ev=>{
      if (STATE){ STATE.progress = JSON.parse(ev.data); renderStatus(STATE); } });
    es.addEventListener('runner', ev=>{ if (STATE){ STATE.runner = JSON.parse(ev.data); renderStatus(STATE); } });
    es.onerror = ()=>{};
  }catch(e){}
  setInterval(pollLogs, 2000);
  setInterval(refresh, 3000);
})();
</script>
</body>
</html>
"""
