// ═══════════════════════════════════════════════════════════════
//  自选新闻 · localStorage 持久化（轻量版）
//  只存元数据索引，不存 HTML 副本 — 跨报告跳转时从源文件加载。
//  MARKET//BRIEF Daily Stock Report
// ═══════════════════════════════════════════════════════════════

var STORAGE_KEY = 'market_brief_favs_v2';  // v2: 不再存储 html 字段
var MAX_FAVS = 200;
var FAV_DATE_KEY, FAV_DATE, FAV_SESSION, PAGE_BASE_URL, CURRENT_SESSION;

function initMBConfig() {
  var cfg = window.MB_CONFIG || {};
  FAV_DATE_KEY = cfg.fav_date_key || '';
  FAV_DATE = cfg.today || '';
  FAV_SESSION = cfg.session_label || '';
  PAGE_BASE_URL = cfg.page_base_url || '';
  CURRENT_SESSION = cfg.session_slug || '';
}

// ── 早报 / 晚报 切换 ──
function switchSession(session) {
  if (session === CURRENT_SESSION) return;
  CURRENT_SESSION = session;
  var amBtn = document.getElementById('stAm');
  var pmBtn = document.getElementById('stPm');
  if (amBtn) amBtn.classList.toggle('active', session === 'am');
  if (pmBtn) pmBtn.classList.toggle('active', session === 'pm');
  var picker = document.getElementById('historyPicker');
  if (picker && picker.value) {
    var p = picker.value.split('-');
    window.location.href = PAGE_BASE_URL + 'report_' + p[0] + p[1] + p[2] + '_' + session + '.html';
  }
}

function goToDate() {
  var d = document.getElementById('historyPicker').value;
  if (d) {
    var p = d.split('-');
    window.location.href = PAGE_BASE_URL + 'report_' + p[0] + p[1] + p[2] + '_' + CURRENT_SESSION + '.html';
  }
}

// ── 读写 localStorage（带错误处理 + 旧格式迁移）──
function getFavs() {
  try {
    var raw = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (!Array.isArray(raw)) return [];
    // 迁移 v1 → v2: 丢弃 html 字段，只保留元数据
    var migrated = false;
    raw = raw.map(function(f) {
      if (f.html !== undefined) { migrated = true; }
      return {
        id: f.id || '',
        sid: f.sid || '',
        date: f.date || '',
        session: f.session || '',
        title: f.title || '',
        saved_at: f.saved_at || ''
      };
    });
    if (migrated) {
      saveFavsRaw(raw);
      console.log('[Favs] 已从 v1 迁移到 v2 (丢弃HTML副本)');
    }
    return raw;
  } catch(e) { return []; }
}

function saveFavsRaw(favs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(favs));
    renderFavPanel();
    updateAllStarBtns();
  } catch(e) {
    // localStorage 满了 → 裁剪最旧的 25%
    if (e.name === 'QuotaExceededError') {
      var drop = Math.ceil(favs.length * 0.25);
      var trimmed = favs.slice(drop);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
      showToast('存储已满，已自动清理' + drop + '条旧收藏');
      renderFavPanel();
      updateAllStarBtns();
    }
  }
}

function saveFavs(favs) {
  saveFavsRaw(favs);
}

// ── 估算存储占用 ──
function estimateStorage() {
  try {
    var raw = localStorage.getItem(STORAGE_KEY);
    return raw ? new Blob([raw]).size : 0;
  } catch(e) { return 0; }
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(1) + ' MB';
}

// ── 从 sec-h 提取纯文本标题 ──
function getTitleFromSec(sid) {
  var sec = document.querySelector('[data-section-id="' + sid + '"]');
  if (!sec) return '';
  var hEl = sec.querySelector('.sec-h');
  if (!hEl) return '';
  var btn = hEl.querySelector('.fav-btn');
  var btnText = btn ? btn.textContent : '';
  return (hEl.textContent || '').replace(btnText, '').trim();
}

function makeFavId(sid) {
  return FAV_DATE_KEY + '_' + sid;
}

// ── 收藏 / 取消收藏 ──
function toggleFav(btn) {
  var sid = btn.getAttribute('data-sid');
  var favId = makeFavId(sid);
  var title = getTitleFromSec(sid);
  if (!title) return;

  var favs = getFavs();
  var idx = favs.findIndex(function(f) { return f.id === favId; });
  if (idx >= 0) {
    favs.splice(idx, 1);
    saveFavs(favs);
    showToast('已取消收藏');
  } else {
    if (favs.length >= MAX_FAVS) {
      showToast('收藏已达上限 (' + MAX_FAVS + '条)，请先清理旧收藏');
      return;
    }
    favs.push({
      id: favId,
      sid: sid,
      date: FAV_DATE,
      session: FAV_SESSION,
      title: title,
      saved_at: new Date().toISOString()
    });
    saveFavs(favs);
    showToast('已加入自选 ');
  }
}

// ── 删除收藏项 ──
function delFav(favId) {
  var favs = getFavs();
  favs = favs.filter(function(f) { return f.id !== favId; });
  saveFavs(favs);
  showToast('已删除');
}

// ── 跳转到收藏项所在报告 ──
function gotoFav(favId) {
  var favs = getFavs();
  var f = favs.find(function(x) { return x.id === favId; });
  if (!f) return;
  var sid = f.sid || '';
  var dateDigits = f.date.replace(/-/g, '');

  // 当前页面已有该 section → 直接滚动
  var local = document.querySelector('[data-section-id="' + sid + '"]');
  if (local) {
    local.scrollIntoView({ behavior: 'smooth', block: 'center' });
    local.style.boxShadow = '0 0 0 3px var(--amber)';
    setTimeout(function() { local.style.boxShadow = ''; }, 2000);
    return;
  }

  // 跨报告跳转：构建目标 URL
  var sessionSuffix = (f.session === '晚报') ? '_pm' : '_am';
  var targetUrl = PAGE_BASE_URL + 'report_' + dateDigits + sessionSuffix + '.html';
  window.location.href = targetUrl + '#' + sid;
}

// ── 渲染底部自选面板 ──
function renderFavPanel() {
  var favs = getFavs();
  var countEl = document.getElementById('favCount');
  var listEl = document.getElementById('favList');
  var summaryEl = document.getElementById('favSummary');
  var panel = document.getElementById('favPanel');
  var sizeEl = document.getElementById('favSize');

  var storageSize = estimateStorage();
  if (countEl) countEl.textContent = favs.length + '条';
  if (sizeEl) sizeEl.textContent = fmtSize(storageSize);
  if (summaryEl) summaryEl.textContent = favs.length
    ? favs.map(function(f) { return f.date.slice(5) + (f.session === '晚报' ? '晚' : '早'); }).slice(-10).join(' · ')
    : '';
  if (panel && favs.length > 0) panel.style.display = 'block';
  else if (panel && favs.length === 0) panel.style.display = 'none';

  if (!listEl) return;
  if (favs.length === 0) {
    listEl.innerHTML = '<div class="fav-empty">暂无收藏 · 点击报告中任意分析板块旁的 ☆ 即可收藏</div>';
    return;
  }

  // 最新在前，最多展示最近 100 条
  var sorted = [].concat(favs).reverse().slice(0, 100);
  listEl.innerHTML = sorted.map(function(f) {
    return '<div class="fav-item">' +
      '<span class="fav-item-date">' + f.date.slice(5) + (f.session === '晚报' ? '晚' : '早') + '</span>' +
      '<span class="fav-item-title" onclick="gotoFav(\'' + f.id + '\')" title="点击跳转到 ' + f.date + ' ' + f.session + ' · ' + f.title + '">' + f.title + '</span>' +
      '<button class="fav-item-del" onclick="delFav(\'' + f.id + '\')" title="删除">✕</button>' +
      '</div>';
  }).join('');

  // 如果超过 100 条，显示提示
  if (favs.length > 100) {
    listEl.innerHTML += '<div class="fav-item" style="color:var(--text-muted);font-size:11px;justify-content:center;">… 还有 ' + (favs.length - 100) + ' 条更早的收藏（已折叠）</div>';
  }
}

// ── 批量清空 ──
function clearAllFavs() {
  if (confirm('确定要清空全部收藏吗？此操作不可恢复。')) {
    localStorage.removeItem(STORAGE_KEY);
    renderFavPanel();
    updateAllStarBtns();
    showToast('已清空全部收藏');
  }
}

// ── 导出收藏为 JSON 文件 ──
function exportFavs() {
  var favs = getFavs();
  if (favs.length === 0) { showToast('没有收藏可导出'); return; }
  var blob = new Blob([JSON.stringify(favs, null, 2)], { type: 'application/json' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'market_brief_favs_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(url);
  showToast('已导出 ' + favs.length + ' 条收藏');
}

// ── 导入收藏（合并去重）──
function importFavs() {
  var input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json';
  input.onchange = function() {
    var file = this.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function(e) {
      try {
        var incoming = JSON.parse(e.target.result);
        if (!Array.isArray(incoming)) throw new Error('格式错误');
        var existing = getFavs();
        var existingIds = new Set(existing.map(function(f) { return f.id; }));
        var added = 0;
        for (var i = 0; i < incoming.length; i++) {
          var f = incoming[i];
          if (!f.id || !f.title) continue;
          if (existingIds.has(f.id)) continue;
          // 丢弃 html 字段（兼容旧格式）
          existing.push({
            id: f.id, sid: f.sid || '', date: f.date || '',
            session: f.session || '', title: f.title,
            saved_at: f.saved_at || new Date().toISOString()
          });
          existingIds.add(f.id);
          added++;
        }
        if (existing.length > MAX_FAVS) {
          existing = existing.slice(existing.length - MAX_FAVS);
        }
        saveFavs(existing);
        showToast('导入了 ' + added + ' 条，合并后共 ' + existing.length + ' 条');
      } catch(err) {
        showToast('导入失败：文件格式不正确');
      }
    };
    reader.readAsText(file);
  };
  input.click();
}

// ── 更新所有 ☆ 按钮状态 ──
function updateAllStarBtns() {
  var favs = getFavs();
  var favIdSet = new Set(favs.map(function(f) { return f.id; }));
  document.querySelectorAll('.fav-btn').forEach(function(btn) {
    var sid = btn.getAttribute('data-sid');
    var favId = makeFavId(sid);
    if (favIdSet.has(favId)) {
      btn.textContent = '★';
      btn.classList.add('on');
    } else {
      btn.textContent = '☆';
      btn.classList.remove('on');
    }
  });
}

// ── 收起/展开面板 ──
function toggleFavPanel() {
  var list = document.getElementById('favList');
  var toggle = document.getElementById('favToggle');
  var actions = document.getElementById('favActions');
  if (list.style.display === 'none') {
    list.style.display = '';
    if (actions) actions.style.display = '';
    toggle.textContent = '▾';
  } else {
    list.style.display = 'none';
    if (actions) actions.style.display = 'none';
    toggle.textContent = '▸';
  }
}

// ── Toast 提示 ──
function showToast(msg) {
  var toast = document.getElementById('favToast');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toast._tid);
  toast._tid = setTimeout(function() { toast.classList.remove('show'); }, 1500);
}

// ── 初始化 ──
(function initFavs() {
  initMBConfig();
  renderFavPanel();
  updateAllStarBtns();
  // 处理跨页面锚点跳转
  var hash = window.location.hash;
  if (hash) {
    var target = document.querySelector('[data-section-id="' + hash.slice(1) + '"]');
    if (target) {
      setTimeout(function() {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.style.boxShadow = '0 0 0 3px var(--amber)';
        setTimeout(function() { target.style.boxShadow = ''; }, 2500);
      }, 300);
    }
  }
})();
