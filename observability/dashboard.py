def dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>my-agent trace explorer</title>
  <style>
    :root {
      --bg: #f8fafc;
      --panel: #ffffff;
      --muted: #64748b;
      --text: #0f172a;
      --border: #e2e8f0;
      --sky: #0284c7;
      --mint: #059669;
      --amber: #d97706;
      --coral: #dc2626;
      --violet: #7c3aed;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--sans);
      background: var(--bg);
      color: var(--text);
    }
    .app {
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 18px;
    }
    .eyebrow {
      font-family: var(--mono);
      color: var(--sky);
      text-transform: uppercase;
      letter-spacing: .08em;
      font-size: 11px;
      margin-bottom: 6px;
    }
    h1 {
      margin: 0;
      font-size: 26px;
      line-height: 1.15;
    }
    .subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      max-width: 760px;
      line-height: 1.5;
    }
    .token-box {
      display: flex;
      gap: 8px;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px;
      min-width: 320px;
    }
    input, select, button {
      font: inherit;
      border: 1px solid var(--border);
      border-radius: 9px;
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
    }
    input { min-width: 0; }
    button {
      cursor: pointer;
      background: var(--sky);
      color: white;
      border-color: var(--sky);
      font-weight: 600;
    }
    button.secondary {
      background: #fff;
      color: var(--text);
      border-color: var(--border);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }
    .grid {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, .04);
    }
    .panel-pad { padding: 14px; }
    .filters {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 12px;
    }
    .filters input, .filters select { width: 100%; }
    .overview {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric {
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--panel);
      border-left: 4px solid var(--sky);
    }
    .metric:nth-child(2) { border-left-color: var(--mint); }
    .metric:nth-child(3) { border-left-color: var(--amber); }
    .metric:nth-child(4) { border-left-color: var(--violet); }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      font-family: var(--mono);
      margin-bottom: 6px;
    }
    .metric-value {
      font-size: 22px;
      font-weight: 700;
    }
    .trace-list {
      max-height: 720px;
      overflow: auto;
      border-top: 1px solid var(--border);
    }
    .trace-row {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
    }
    .trace-row:hover, .trace-row.active {
      background: #f1f5f9;
    }
    .trace-main {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
    }
    .trace-id {
      font-family: var(--mono);
      font-size: 12px;
      word-break: break-all;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .pill.success { color: var(--mint); background: #ecfdf5; }
    .pill.error { color: var(--coral); background: #fef2f2; }
    .pill.retry, .pill.running { color: var(--amber); background: #fffbeb; }
    .meta {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .detail-empty {
      display: grid;
      place-items: center;
      min-height: 520px;
      color: var(--muted);
      text-align: center;
    }
    .detail-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 14px;
      border-bottom: 1px solid var(--border);
    }
    .detail-title {
      font-family: var(--mono);
      font-size: 13px;
      word-break: break-all;
      margin-bottom: 6px;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr);
      gap: 14px;
      padding: 14px;
    }
    .section-title {
      font-size: 13px;
      font-weight: 700;
      margin: 0 0 10px;
      color: var(--muted);
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .tree {
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      background: #fff;
    }
    .tree-node {
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      font-size: 13px;
      display: flex;
      justify-content: space-between;
      gap: 8px;
    }
    .tree-node:last-child { border-bottom: 0; }
    .tree-node:hover, .tree-node.active { background: #f8fafc; }
    .tree-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tree-ms {
      font-family: var(--mono);
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }
    .waterfall {
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
    }
    .bar-row {
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr) 120px;
      gap: 10px;
      align-items: center;
      font-size: 12px;
    }
    .bar-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
    }
    .track {
      height: 11px;
      background: #e2e8f0;
      border-radius: 999px;
      overflow: hidden;
      position: relative;
    }
    .fill {
      height: 100%;
      background: var(--sky);
      min-width: 2px;
      position: absolute;
      top: 0;
    }
    .fill.error { background: var(--coral); }
    .fill.retry, .fill.running { background: var(--amber); }
    .duration {
      text-align: right;
      font-family: var(--mono);
      color: var(--muted);
      white-space: nowrap;
    }
    .timeline-note {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      margin: -2px 0 10px;
    }
    pre {
      margin: 0;
      padding: 12px;
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 12px;
      overflow: auto;
      max-height: 420px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.45;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .notice-box {
      color: var(--coral);
      background: #fef2f2;
      border: 1px solid #fecaca;
      border-radius: 10px;
      padding: 10px;
      margin-bottom: 12px;
      display: none;
    }
    .notice-box.success {
      color: var(--mint);
      background: #ecfdf5;
      border-color: #bbf7d0;
    }
    .notice-box.error {
      color: var(--coral);
      background: #fef2f2;
      border-color: #fecaca;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    @media (max-width: 980px) {
      .grid, .detail-grid, .split, .overview {
        grid-template-columns: 1fr;
      }
      .token-box {
        min-width: 0;
        width: 100%;
      }
      .header {
        flex-direction: column;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="header">
      <div>
        <div class="eyebrow">local observability</div>
        <h1>my-agent trace explorer</h1>
        <p class="subtitle">Inspect persisted traces, span trees, timing waterfalls, span attributes, errors, and redacted raw JSON from the local control-plane API.</p>
      </div>
      <div class="token-box">
        <input id="tokenInput" type="password" placeholder="API token, if configured">
        <button id="saveToken" class="secondary">Save</button>
      </div>
    </header>

    <div id="noticeBox" class="notice-box"></div>

    <section id="overview" class="overview"></section>

    <main class="grid">
      <section class="panel">
        <div class="panel-pad">
          <div class="eyebrow">trace filters</div>
          <div class="filters">
            <select id="statusFilter">
              <option value="">any status</option>
              <option value="success">success</option>
              <option value="error">error</option>
              <option value="retry">retry</option>
              <option value="running">running</option>
            </select>
            <input id="limitInput" type="number" min="1" max="100" value="20" placeholder="limit">
            <input id="sessionInput" placeholder="session_id">
            <input id="entrypointInput" placeholder="entrypoint">
            <input id="startInput" placeholder="start ISO time">
            <input id="endInput" placeholder="end ISO time">
          </div>
          <div class="toolbar">
            <button id="refreshBtn">Refresh</button>
            <button id="loadMoreBtn" class="secondary" disabled>Load more</button>
            <button id="repairBtn" class="secondary">Repair index</button>
          </div>
        </div>
        <div id="traceList" class="trace-list"></div>
      </section>

      <section id="detailPanel" class="panel">
        <div class="detail-empty">
          <div>
            <div class="eyebrow">trace detail</div>
            <p>Select a trace to inspect its span tree and timing waterfall.</p>
          </div>
        </div>
      </section>
    </main>
  </div>

  <script>
  (function() {
    var state = {
      traces: [],
      nextCursor: null,
      selectedTraceId: null,
      selectedSpanId: null,
      currentTrace: null
    };

    var tokenInput = document.getElementById('tokenInput');
    var storedToken = localStorage.getItem('trace_api_token') || '';
    tokenInput.value = storedToken;

    document.getElementById('saveToken').addEventListener('click', function() {
      localStorage.setItem('trace_api_token', tokenInput.value.trim());
      showNotice('');
      setButtonBusy('saveToken', true, 'Checking...');
      apiFetch('/api/auth/check')
        .then(function() {
          showNotice('Token valid. Data refreshed.', 'success');
          return Promise.all([loadOverview(), loadTraces(true)]);
        })
        .catch(showFetchError)
        .finally(function() {
          setButtonBusy('saveToken', false);
        });
    });
    document.getElementById('refreshBtn').addEventListener('click', function() {
      showNotice('');
      setButtonBusy('refreshBtn', true, 'Refreshing...');
      Promise.all([loadOverview(), loadTraces(true)])
        .finally(function() {
          setButtonBusy('refreshBtn', false);
        });
    });
    document.getElementById('loadMoreBtn').addEventListener('click', function() {
      if (state.nextCursor) {
        showNotice('');
        setButtonBusy('loadMoreBtn', true, 'Loading...');
        loadTraces(false).finally(function() {
          setButtonBusy('loadMoreBtn', false);
          updateLoadMoreButton();
        });
      }
    });
    document.getElementById('repairBtn').addEventListener('click', function() {
      showNotice('');
      setButtonBusy('repairBtn', true, 'Repairing...');
      apiFetch('/api/index/repair', { method: 'POST' })
        .then(function(result) {
          return Promise.all([loadOverview(), loadTraces(true)]).then(function() {
            showNotice('Index repair complete: repaired=' + result.repaired + ', total=' + result.total, 'success');
          });
        })
        .catch(showFetchError)
        .finally(function() {
          setButtonBusy('repairBtn', false);
        });
    });

    ['statusFilter', 'limitInput', 'sessionInput', 'entrypointInput', 'startInput', 'endInput'].forEach(function(id) {
      document.getElementById(id).addEventListener('change', function() {
        loadTraces(true);
      });
    });

    function authHeaders() {
      var token = (localStorage.getItem('trace_api_token') || tokenInput.value || '').trim();
      return token ? { 'Authorization': 'Bearer ' + token } : {};
    }

    function apiFetch(path, options) {
      options = options || {};
      options.headers = Object.assign({}, authHeaders(), options.headers || {});
      return fetch(path, options).then(function(resp) {
        return resp.text().then(function(text) {
          var payload = text ? JSON.parse(text) : {};
          if (!resp.ok) {
            var err = new Error(payload.error || resp.statusText);
            err.payload = payload;
            err.status = resp.status;
            throw err;
          }
          return payload;
        });
      });
    }

    function loadOverview() {
      return apiFetch('/api/overview')
        .then(renderOverview)
        .catch(showFetchError);
    }

    function buildTraceQuery(reset) {
      var params = new URLSearchParams();
      params.set('limit', document.getElementById('limitInput').value || '20');
      var fields = [
        ['status', 'statusFilter'],
        ['session_id', 'sessionInput'],
        ['entrypoint', 'entrypointInput'],
        ['start', 'startInput'],
        ['end', 'endInput']
      ];
      fields.forEach(function(pair) {
        var value = document.getElementById(pair[1]).value.trim();
        if (value) {
          params.set(pair[0], value);
        }
      });
      if (!reset && state.nextCursor) {
        params.set('before', state.nextCursor);
      }
      return params.toString();
    }

    function loadTraces(reset) {
      if (reset) {
        state.traces = [];
        state.nextCursor = null;
      }
      return apiFetch('/api/traces?' + buildTraceQuery(reset))
        .then(function(payload) {
          state.traces = reset ? payload.traces : state.traces.concat(payload.traces);
          state.nextCursor = payload.next_cursor;
          renderTraceList();
          updateLoadMoreButton();
        })
        .catch(showFetchError);
    }

    function loadTrace(traceId) {
      state.selectedTraceId = traceId;
      renderTraceList();
      showNotice('');
      return apiFetch('/api/traces/' + encodeURIComponent(traceId))
        .then(function(payload) {
          state.currentTrace = payload.trace;
          state.selectedSpanId = payload.trace.root_span_id || ((payload.trace.spans || [])[0] || {}).span_id;
          renderTraceDetail(payload);
        })
        .catch(showFetchError);
    }

    function renderOverview(payload) {
      var cards = [
        ['Traces', payload.trace_count],
        ['Avg total ms', payload.avg_total_ms == null ? '-' : payload.avg_total_ms],
        ['Latest', payload.latest_created_at || '-'],
        ['Errors', (payload.status_counts || {}).error || 0]
      ];
      var root = document.getElementById('overview');
      root.innerHTML = '';
      cards.forEach(function(card) {
        var el = document.createElement('div');
        el.className = 'metric';
        appendText(el, 'div', 'metric-label', card[0]);
        appendText(el, 'div', 'metric-value', String(card[1]));
        root.appendChild(el);
      });
    }

    function renderTraceList() {
      var list = document.getElementById('traceList');
      list.innerHTML = '';
      if (!state.traces.length) {
        var empty = document.createElement('div');
        empty.className = 'panel-pad';
        empty.textContent = 'No traces found.';
        list.appendChild(empty);
        return;
      }
      state.traces.forEach(function(trace) {
        var row = document.createElement('div');
        row.className = 'trace-row' + (trace.trace_id === state.selectedTraceId ? ' active' : '');
        row.addEventListener('click', function() { loadTrace(trace.trace_id); });
        var main = document.createElement('div');
        main.className = 'trace-main';
        appendText(main, 'div', 'trace-id', trace.trace_id);
        appendText(main, 'span', 'pill ' + (trace.status || 'unknown'), trace.status || 'unknown');
        row.appendChild(main);
        var meta = document.createElement('div');
        meta.className = 'meta';
        [trace.created_at, (trace.total_ms == null ? '-' : trace.total_ms + 'ms'), trace.root_name || '-', 'spans=' + trace.span_count].forEach(function(value) {
          appendText(meta, 'span', '', String(value || '-'));
        });
        row.appendChild(meta);
        list.appendChild(row);
      });
    }

    function renderTraceDetail(payload) {
      var panel = document.getElementById('detailPanel');
      panel.innerHTML = '';
      var trace = payload.trace;
      var header = document.createElement('div');
      header.className = 'detail-header';
      var titleBox = document.createElement('div');
      appendText(titleBox, 'div', 'detail-title', trace.trace_id || trace.request_id || '-');
      appendText(titleBox, 'div', 'meta', (trace.status || '-') + ' | ' + (trace.total_ms || '-') + 'ms | spans=' + ((trace.spans || []).length));
      header.appendChild(titleBox);
      var actions = document.createElement('div');
      var exportBtn = document.createElement('button');
      exportBtn.textContent = 'Download bundle';
      exportBtn.addEventListener('click', function() {
        setButtonBusy(exportBtn, true, 'Downloading...');
        downloadDebugBundle(trace).finally(function() {
          setButtonBusy(exportBtn, false);
        });
      });
      actions.appendChild(exportBtn);
      header.appendChild(actions);
      panel.appendChild(header);

      var grid = document.createElement('div');
      grid.className = 'detail-grid';
      var left = document.createElement('div');
      appendText(left, 'h3', 'section-title', 'Span tree');
      left.appendChild(buildSpanTree(trace));
      grid.appendChild(left);

      var right = document.createElement('div');
      appendText(right, 'h3', 'section-title', 'Timeline');
      right.appendChild(buildWaterfall(trace));
      var split = document.createElement('div');
      split.className = 'split';
      var spanBox = document.createElement('div');
      appendText(spanBox, 'h3', 'section-title', 'Selected span');
      var spanPre = document.createElement('pre');
      spanPre.id = 'spanJson';
      spanBox.appendChild(spanPre);
      var rawBox = document.createElement('div');
      appendText(rawBox, 'h3', 'section-title', 'Raw trace');
      var rawPre = document.createElement('pre');
      rawPre.textContent = JSON.stringify(trace, null, 2);
      rawBox.appendChild(rawPre);
      split.appendChild(spanBox);
      split.appendChild(rawBox);
      right.appendChild(split);
      grid.appendChild(right);
      panel.appendChild(grid);
      renderSelectedSpan();
    }

    function buildSpanTree(trace) {
      var spans = trace.spans || [];
      var children = {};
      spans.forEach(function(span) {
        var key = span.parent_span_id || 'root';
        children[key] = children[key] || [];
        children[key].push(span);
      });
      var tree = document.createElement('div');
      tree.className = 'tree';
      var roots = children.root || (spans[0] ? [spans[0]] : []);
      roots.forEach(function(span) { appendSpanNode(tree, span, children, 0); });
      return tree;
    }

    function appendSpanNode(root, span, children, depth) {
      var node = document.createElement('div');
      node.className = 'tree-node' + (span.span_id === state.selectedSpanId ? ' active' : '');
      node.style.paddingLeft = (10 + depth * 18) + 'px';
      node.addEventListener('click', function(event) {
        event.stopPropagation();
        state.selectedSpanId = span.span_id;
        renderTraceDetail({ trace: state.currentTrace });
      });
      appendText(node, 'span', 'tree-label', span.name || span.span_kind || '-');
      appendText(node, 'span', 'tree-ms', (span.duration_ms == null ? '-' : span.duration_ms + 'ms'));
      root.appendChild(node);
      (children[span.span_id] || []).forEach(function(child) {
        appendSpanNode(root, child, children, depth + 1);
      });
    }

    function buildWaterfall(trace) {
      var spans = trace.spans || [];
      var horizonMs = spans.reduce(function(max, span) {
        var start = numberOrZero(span.start_offset_ms);
        var duration = numberOrZero(span.duration_ms);
        return Math.max(max, start + duration);
      }, numberOrZero(trace.total_ms) || 1);
      horizonMs = Math.max(horizonMs, 1);

      var hasOffsets = spans.some(function(span) {
        return span.start_offset_ms !== undefined && span.start_offset_ms !== null;
      }, 1);
      var box = document.createElement('div');
      box.className = 'waterfall';
      appendText(
        box,
        'div',
        'timeline-note',
        hasOffsets
          ? 'Waterfall uses span start_offset_ms relative to trace start. Horizon: ' + formatMs(horizonMs) + '.'
          : 'Legacy trace without start offsets; bars fall back to duration-only display.'
      );
      spans.forEach(function(span) {
        var startMs = hasOffsets ? numberOrZero(span.start_offset_ms) : 0;
        var durationMs = numberOrZero(span.duration_ms);
        var leftPct = Math.min(100, Math.max(0, (startMs / horizonMs) * 100));
        var widthPct = Math.min(100 - leftPct, Math.max(0.35, (durationMs / horizonMs) * 100));
        var row = document.createElement('div');
        row.className = 'bar-row';
        appendText(row, 'div', 'bar-label', span.name || span.span_kind || '-');
        var track = document.createElement('div');
        track.className = 'track';
        var fill = document.createElement('div');
        fill.className = 'fill ' + (span.status || '');
        fill.style.left = leftPct + '%';
        fill.style.width = widthPct + '%';
        fill.title = 'start=' + formatMs(startMs) + ', duration=' + formatMs(durationMs);
        track.appendChild(fill);
        row.appendChild(track);
        appendText(row, 'div', 'duration', formatMs(startMs) + ' + ' + formatMs(durationMs));
        box.appendChild(row);
      });
      return box;
    }

    function numberOrZero(value) {
      var parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatMs(value) {
      var parsed = numberOrZero(value);
      return Math.round(parsed * 100) / 100 + 'ms';
    }

    function renderSelectedSpan() {
      if (!state.currentTrace) {
        return;
      }
      var span = (state.currentTrace.spans || []).find(function(item) {
        return item.span_id === state.selectedSpanId;
      }) || (state.currentTrace.spans || [])[0] || {};
      var pre = document.getElementById('spanJson');
      if (pre) {
        pre.textContent = JSON.stringify(span, null, 2);
      }
    }

    function downloadDebugBundle(trace) {
      var traceId = trace.trace_id || trace.request_id || '';
      return apiFetch('/api/traces/' + encodeURIComponent(traceId) + '/bundle')
        .then(function(bundle) {
          var blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = 'trace-bundle-' + traceId + '.json';
          a.click();
          URL.revokeObjectURL(url);
          showNotice('Debug bundle downloaded.', 'success');
        })
        .catch(showFetchError);
    }

    function appendText(parent, tag, className, text) {
      var el = document.createElement(tag);
      if (className) {
        el.className = className;
      }
      el.textContent = text;
      parent.appendChild(el);
      return el;
    }

    function showNotice(message, kind) {
      var box = document.getElementById('noticeBox');
      if (!message) {
        box.style.display = 'none';
        box.textContent = '';
        box.className = 'notice-box';
        return;
      }
      box.style.display = 'block';
      box.textContent = typeof message === 'string' ? message : String(message);
      box.className = 'notice-box ' + (kind === 'success' ? 'success' : 'error');
    }

    function showFetchError(error) {
      if (error && error.status === 401) {
        showNotice('Unauthorized. Save the API token and retry.', 'error');
        return;
      }
      showNotice(error && error.message ? error.message : error, 'error');
    }

    function setButtonBusy(buttonOrId, busy, label) {
      var button = typeof buttonOrId === 'string' ? document.getElementById(buttonOrId) : buttonOrId;
      if (!button) {
        return;
      }
      if (!button.dataset.originalText) {
        button.dataset.originalText = button.textContent;
      }
      button.disabled = !!busy;
      button.textContent = busy && label ? label : button.dataset.originalText;
      if (!busy && button.id === 'loadMoreBtn') {
        updateLoadMoreButton();
      }
    }

    function updateLoadMoreButton() {
      var button = document.getElementById('loadMoreBtn');
      if (!button) {
        return;
      }
      button.disabled = !state.nextCursor;
    }

    loadOverview();
    loadTraces(true).finally(updateLoadMoreButton);
  })();
  </script>
</body>
</html>"""
