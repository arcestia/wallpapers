let wallpapers = [];
    let selectedIds = new Set();
    let currentCategory = 'All';
    let lightboxIndex = 0;
    let lastClickedIndex = -1;
    let searchTimeout = null;
    let cachedStats = null;
    let cmdActiveIndex = 0;
    let auditResults = [];
    let taskPollTimer = null;
    let CATEGORIES = ["Abstract", "Animals", "Anime", "Architecture", "Cars", "City", "Comics", "Cyberpunk", "Digital Art", "Fantasy", "Gaming", "Horror", "Landscape", "Military", "Minimalism", "Music", "Nature", "Ocean", "People", "Pixel Art", "Sci-Fi", "Space", "Sports", "Vehicles", "Other"];

    function populateCategoryDropdowns(cats) {
      if (cats && cats.length) CATEGORIES = cats;
      const opts = CATEGORIES.map(c => `<option value="${c}">${c}</option>`).join("");
      ["deviantart-cat-hint"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = '<option value="">Auto-Detect</option>' + opts;
      });

      ['batch-move-cat', 'lb-change-cat', 'wh-search-cat-hint', 'wh-ids-cat-hint', 'web-urls-cat-hint', 'local-folder-cat-hint', 'deviantart-cat-hint'].forEach(id => {
        const sel = document.getElementById(id);
        if (sel) {
          sel.innerHTML = '<option value="">Auto-Detect</option>';
          CATEGORIES.forEach(c => {
            sel.innerHTML += `<option value="${c}">${c}</option>`;
          });
        }
      });
    }

    populateCategoryDropdowns(CATEGORIES);

    async function initCategories() {
      try {
        const res = await fetch('/api/categories');
        if (res.ok) {
          const data = await res.json();
          const catNames = data.map(d => d.name);
          populateCategoryDropdowns(catNames);
        }
      } catch (e) {}
    }

    async function loadStats() {
      const res = await fetch('/api/stats');
      const data = await res.json();
      cachedStats = data;

      document.getElementById('stat-curated').innerText = data.curated;
      document.getElementById('stat-pending').innerText = data.pending;
      document.getElementById('stat-rejected').innerText = data.rejected;
      document.getElementById('stat-total').innerText = data.total;

      document.getElementById('badge-all-count').innerText = data.total;
      document.getElementById('badge-curated-sidebar').innerText = data.curated;
      document.getElementById('badge-rejected-sidebar').innerText = data.rejected;

      const sidebar = document.getElementById('sidebar-categories');
      while (sidebar.children.length > 4) {
        sidebar.removeChild(sidebar.lastChild);
      }

      Object.entries(data.category_breakdown).forEach(([cat, stats]) => {
        const div = document.createElement('div');
        div.className = `cat-item ${currentCategory === cat ? 'active' : ''}`;
        div.onclick = function() { selectCategory(cat, this); };
        div.innerHTML = `
          <span>${cat}</span>
          <div class="cat-badges">
            <span class="badge-cur-count" title="Curated in ${cat}">✓ ${stats.curated}</span>
            <span class="badge-count" title="Total local in ${cat}">${stats.total}</span>
          </div>
          <div class="cat-progress-bg" style="width: ${stats.pct}%;"></div>
        `;
        sidebar.appendChild(div);
      });
    }

    function quickFilterStatus(statusVal, el) {
      document.getElementById('filter-status').value = statusVal;
      currentCategory = 'All';
      document.querySelectorAll('.cat-item').forEach(i => i.classList.remove('active'));
      if (el) el.classList.add('active');
      loadWallpapers();
    }

    function setDensity(density) {
      document.querySelectorAll('.density-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(`d-${density}`).classList.add('active');
      const root = document.documentElement;
      if (density === 'sm') {
        root.style.setProperty('--card-min-w', '200px');
        root.style.setProperty('--card-img-h', '130px');
      } else if (density === 'md') {
        root.style.setProperty('--card-min-w', '280px');
        root.style.setProperty('--card-img-h', '180px');
      } else if (density === 'lg') {
        root.style.setProperty('--card-min-w', '380px');
        root.style.setProperty('--card-img-h', '240px');
      }
    }

    function openStatsModal() {
      if (!cachedStats) return;
      const data = cachedStats;

      document.getElementById('m-curated').innerText = data.curated;
      document.getElementById('m-curated-pct').innerText = `${data.curated_pct}% of total library`;
      document.getElementById('m-size').innerText = data.curated_size;
      document.getElementById('m-raw-size').innerText = `Total local: ${data.total_size}`;
      document.getElementById('m-res').innerText = data.avg_resolution;
      document.getElementById('m-pending').innerText = data.pending;
      document.getElementById('m-rejected').innerText = data.rejected;
      document.getElementById('m-total').innerText = `Total: ${data.total} wallpapers`;

      const activeCount = Object.values(data.category_breakdown).filter(c => c.curated > 0).length;
      document.getElementById('m-active-cats').innerText = `${activeCount} / ${Object.keys(data.category_breakdown).length} categories active`;

      const progList = document.getElementById('m-category-progress-list');
      progList.innerHTML = '';
      Object.entries(data.category_breakdown).forEach(([cat, st]) => {
        const row = document.createElement('div');
        row.className = 'cat-progress-row';
        row.onclick = () => { closeModal('stats-modal'); selectCategory(cat, null); };
        row.innerHTML = `
          <div style="font-weight:700; color:var(--text-bright);">${cat}</div>
          <div class="progress-track">
            <div class="progress-fill" style="width: ${st.pct}%;"></div>
          </div>
          <div style="font-size:0.75rem; font-weight:700; color:var(--text-muted); text-align:right;">${st.curated} / ${st.total} (${st.pct}%)</div>
        `;
        progList.appendChild(row);
      });

      const orientPills = document.getElementById('m-orientation-pills');
      orientPills.innerHTML = '';
      if (Object.keys(data.by_orientation).length === 0) {
        orientPills.innerHTML = '<span style="color:var(--text-muted); font-size:0.8rem;">No curated wallpapers yet</span>';
      } else {
        Object.entries(data.by_orientation).forEach(([orient, cnt]) => {
          orientPills.innerHTML += `<span class="pill">${orient}: <strong>${cnt}</strong></span>`;
        });
      }

      const formatPills = document.getElementById('m-format-pills');
      formatPills.innerHTML = '';
      if (Object.keys(data.by_format).length === 0) {
        formatPills.innerHTML = '<span style="color:var(--text-muted); font-size:0.8rem;">No curated wallpapers yet</span>';
      } else {
        Object.entries(data.by_format).forEach(([fmt, cnt]) => {
          formatPills.innerHTML += `<span class="pill">${fmt}: <strong>${cnt}</strong></span>`;
        });
      }

      document.getElementById('stats-modal').style.display = 'flex';
    }

    function openShortcutsModal() {
      document.getElementById('shortcuts-modal').style.display = 'flex';
    }

    function openIngestModal() {
      document.getElementById('ingest-modal').style.display = 'flex';
      refreshWallhavenStatus();
    }

    function openClassifierModal() {
      document.getElementById('classifier-modal').style.display = 'flex';
    }

    function closeModal(id, e) {
      if (e && e.target !== document.getElementById(id) && !e.target.classList.contains('modal-close')) return;
      document.getElementById(id).style.display = 'none';
    }

    let currentRatio = 'all';
    let duplicateClusters = [];

    function setRatioFilter(ratio) {
      currentRatio = ratio;
      ['all', '16-9', '21-9', '32-9', '9-16'].forEach(r => {
        const pill = document.getElementById(`pill-ratio-${r}`);
        if (pill) pill.classList.remove('active');
      });
      const activePill = document.getElementById(`pill-ratio-${ratio.replace(':', '-')}`);
      if (activePill) activePill.classList.add('active');
      loadWallpapers();
    }

    async function loadWallpapers() {
      const status = document.getElementById('filter-status').value;
      const search = document.getElementById('search-input').value;
      const minRes = document.getElementById('filter-min-res') ? document.getElementById('filter-min-res').value : 'all';
      const sort = document.getElementById('filter-sort') ? document.getElementById('filter-sort').value : 'id_asc';

      const url = `/api/wallpapers?category=${encodeURIComponent(currentCategory)}&status=${status}&q=${encodeURIComponent(search)}&ratio=${encodeURIComponent(currentRatio)}&min_res=${minRes}&sort=${sort}`;
      const res = await fetch(url);
      wallpapers = await res.json();
      selectedIds.clear();
      updateSelectionUI();
      renderGrid();
      loadStats();
    }

    async function setAsWallpaper(id) {
      showToast(`🖼️ Setting ID #${id} as Windows wallpaper...`);
      try {
        const res = await fetch('/api/wallpaper/set-desktop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await res.json();
        if (data.success) {
          showToast(`✨ Desktop wallpaper set to ${data.category} #${id} (${data.resolution})!`);
        } else {
          showToast(`❌ Failed to set wallpaper: ${data.error}`);
        }
      } catch (e) {
        showToast(`❌ Error: ${e.message}`);
      }
    }

    function downloadCurrentLightbox() {
      const w = wallpapers[lightboxIndex];
      if (!w) return;
      const link = document.createElement('a');
      link.href = `/download/${encodeURIComponent(w.category)}/${encodeURIComponent(w.filename)}`;
      link.download = `${w.category}_${w.filename}`;
      link.click();
    }

    function setWallpaperCurrentLightbox() {
      const w = wallpapers[lightboxIndex];
      if (!w) return;
      setAsWallpaper(w.id);
    }

    function openDuplicatesModal() {
      document.getElementById('duplicates-modal').style.display = 'flex';
      scanDuplicates();
    }

    async function scanDuplicates() {
      const container = document.getElementById('duplicates-results-container');
      container.innerHTML = '<div style="text-align:center; padding:40px; color:var(--accent);">🔍 Scanning database for duplicate SHA-256 and dimension clusters...</div>';
      try {
        const res = await fetch('/api/duplicates/scan');
        const data = await res.json();
        if (!data.success) {
          container.innerHTML = `<div style="text-align:center; padding:30px; color:var(--red);">Error: ${data.error}</div>`;
          return;
        }

        duplicateClusters = data.clusters || [];
        const btnPurgeAll = document.getElementById('btn-purge-all-dups');

        if (duplicateClusters.length === 0) {
          btnPurgeAll.style.display = 'none';
          container.innerHTML = '<div style="text-align:center; padding:50px; color:var(--green); font-weight:800; font-size:1.1rem;">✅ 0 Duplicates Detected! Your wallpaper archive is 100% unique and clean.</div>';
          return;
        }

        btnPurgeAll.style.display = 'block';
        btnPurgeAll.innerText = `🗑️ Purge All Duplicates (${data.total_duplicates})`;

        let html = '<div style="display:flex; flex-direction:column; gap:16px; padding:10px 0;">';
        duplicateClusters.forEach((cl, cIdx) => {
          html += `
            <div style="background:#090d14; border:1px solid var(--border); border-radius:10px; padding:14px; display:flex; flex-direction:column; gap:10px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:700; color:var(--yellow); font-size:0.85rem;">⚠️ ${cl.type}</span>
                <span style="font-size:0.75rem; color:var(--text-muted);">${cl.duplicates.length} duplicate file(s)</span>
              </div>
              <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(180px, 1fr)); gap:12px;">
                <!-- Master (Keep) -->
                <div style="background:var(--card); border:2px solid var(--green); border-radius:8px; overflow:hidden; padding:8px; display:flex; flex-direction:column; gap:6px;">
                  <span style="font-size:0.72rem; font-weight:800; color:var(--green);">✓ MASTER (Keep)</span>
                  <div style="height:90px; background:#000; border-radius:4px; overflow:hidden;">
                    <img src="/image/${encodeURIComponent(cl.master.category)}/${encodeURIComponent(cl.master.filename)}" style="width:100%; height:100%; object-fit:cover;" />
                  </div>
                  <div style="font-size:0.75rem; color:var(--text-bright); font-weight:700;">#${cl.master.id} - ${cl.master.category}</div>
                  <div style="font-size:0.7rem; color:var(--text-muted);">${cl.master.width}×${cl.master.height} (${(cl.master.filesize/(1024*1024)).toFixed(1)} MB)</div>
                </div>

                <!-- Duplicates (Purge) -->
                ${cl.duplicates.map(dup => `
                  <div style="background:var(--card); border:1px solid var(--red); border-radius:8px; overflow:hidden; padding:8px; display:flex; flex-direction:column; gap:6px;">
                    <span style="font-size:0.72rem; font-weight:800; color:var(--red);">✕ DUPLICATE</span>
                    <div style="height:90px; background:#000; border-radius:4px; overflow:hidden;">
                      <img src="/image/${encodeURIComponent(dup.category)}/${encodeURIComponent(dup.filename)}" style="width:100%; height:100%; object-fit:cover;" />
                    </div>
                    <div style="font-size:0.75rem; color:var(--text-bright); font-weight:700;">#${dup.id} - ${dup.category}</div>
                    <div style="font-size:0.7rem; color:var(--text-muted);">${dup.width}×${dup.height} (${(dup.filesize/(1024*1024)).toFixed(1)} MB)</div>
                    <button class="btn-quick btn-quick-reject" onclick="purgeSingleDuplicate(${dup.id})" style="width:100%; justify-content:center; margin-top:2px;">🗑️ Purge</button>
                  </div>
                `).join('')}
              </div>
            </div>
          `;
        });
        html += '</div>';
        container.innerHTML = html;
      } catch (e) {
        container.innerHTML = `<div style="text-align:center; padding:30px; color:var(--red);">Scan error: ${e.message}</div>`;
      }
    }

    async function purgeSingleDuplicate(id) {
      const res = await fetch('/api/duplicates/purge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [id] })
      });
      const data = await res.json();
      if (data.success) {
        showToast(`🗑️ Purged duplicate ID #${id}`);
        scanDuplicates();
        loadWallpapers();
      }
    }

    async function purgeAllDuplicates() {
      const allDupIds = [];
      duplicateClusters.forEach(cl => {
        cl.duplicates.forEach(d => allDupIds.push(d.id));
      });
      if (allDupIds.length === 0) return;

      showToast(`⏳ Purging ${allDupIds.length} duplicate wallpapers...`);
      const res = await fetch('/api/duplicates/purge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: allDupIds })
      });
      const data = await res.json();
      if (data.success) {
        showToast(`✨ Purged ${data.purged_count} duplicates!`);
        scanDuplicates();
        loadWallpapers();
      }
    }


    function selectCategory(cat, el) {
      currentCategory = cat;
      document.querySelectorAll('.cat-item').forEach(i => i.classList.remove('active'));
      if (el) el.classList.add('active');
      loadWallpapers();
    }

    let renderedCount = 0;
    const CHUNK_SIZE = 48;
    let gridScrollBound = false;

    function createCardElement(w, idx) {
      const isSelected = selectedIds.has(w.id);
      const card = document.createElement('div');
      const isCur = w.is_curated === 1;
      const isRej = w.is_curated === -1;
      const imgSrc = (isCur && w.s3_url)
        ? w.s3_url
        : `/thumb/${encodeURIComponent(w.category)}/${encodeURIComponent(w.filename)}`;
      const fallbackSrc = `/image/${encodeURIComponent(w.category)}/${encodeURIComponent(w.filename)}`;

      card.className = `card ${isSelected ? 'selected' : ''} ${isCur ? 'is-curated' : ''} ${isRej ? 'is-rejected' : ''}`;
      card.dataset.id = w.id;
      card.dataset.idx = idx;
      card.onclick = (e) => handleCardClick(idx, w.id, e);
      card.ondblclick = () => openLightbox(idx);

      card.innerHTML = `
        <div class="card-checkbox"></div>
        ${isCur ? `<div class="card-ribbon curated">✓ ${w.curated_filename || 'Curated'}</div>` : ''}
        ${isRej ? `<div class="card-ribbon rejected">✕ Rejected</div>` : ''}
        <div class="card-thumb">
          <img src="${imgSrc}" loading="lazy" decoding="async" onerror="if(this.src!=='${fallbackSrc}')this.src='${fallbackSrc}'" />
        </div>

        <!-- Hover Quick Actions -->
        <div class="card-hover-actions">
          <div style="display: flex; justify-content: flex-end; gap: 6px;">
            <a href="/download/${encodeURIComponent(w.category)}/${encodeURIComponent(w.filename)}" download onclick="event.stopPropagation()" class="btn-quick" title="Download Full Resolution (4K)">💾</a>
            <button class="btn-quick" onclick="event.stopPropagation(); setAsWallpaper(${w.id})" title="Set as Windows Desktop Wallpaper">🖼️</button>
          </div>
          <div class="card-quick-btn-row">
            <button class="btn-quick btn-quick-approve" onclick="event.stopPropagation(); quickCurate(${w.id}, 'approve', ${idx})">✓ Approve</button>
            <button class="btn-quick btn-quick-reject" onclick="event.stopPropagation(); quickCurate(${w.id}, 'reject', ${idx})">✕ Reject</button>
            <button class="btn-quick btn-quick-classify" onclick="event.stopPropagation(); quickClassifySingle(${w.id})">🤖 Classify</button>
            <button class="btn-quick btn-quick-zoom" onclick="event.stopPropagation(); openLightbox(${idx})">🔍 Inspect</button>
          </div>
        </div>

        <div class="card-meta">
          <span class="card-title">${w.category} <span class="card-sub">#${w.id}</span></span>
          <span class="card-sub">${w.width}×${w.height}</span>
        </div>
      `;
      return card;
    }

    function appendNextChunk() {
      const grid = document.getElementById('grid-view');
      if (!grid || renderedCount >= wallpapers.length) return;
      const nextBatch = wallpapers.slice(renderedCount, renderedCount + CHUNK_SIZE);
      const fragment = document.createDocumentFragment();
      nextBatch.forEach((w, i) => {
        fragment.appendChild(createCardElement(w, renderedCount + i));
      });
      grid.appendChild(fragment);
      renderedCount += nextBatch.length;
    }

    function renderGrid() {
      const grid = document.getElementById('grid-view');
      grid.innerHTML = '';
      renderedCount = 0;

      if (wallpapers.length === 0) {
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 60px 20px; color: var(--text-muted);"><h2 style="color:var(--text-bright); margin-bottom:8px;">No wallpapers found</h2><p>Try switching category or click "📥 Ingest Studio" above</p></div>';
        return;
      }

      appendNextChunk();

      if (!gridScrollBound) {
        gridScrollBound = true;
        grid.addEventListener('scroll', () => {
          if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 600) {
            appendNextChunk();
          }
        }, { passive: true });
      }
    }

    function syncCardSelectionDOM() {
      document.querySelectorAll('.card').forEach(c => {
        const id = parseInt(c.dataset.id);
        if (selectedIds.has(id)) c.classList.add('selected');
        else c.classList.remove('selected');
      });
    }

    function handleCardClick(idx, id, e) {
      if (e.shiftKey && lastClickedIndex !== -1) {
        const start = Math.min(lastClickedIndex, idx);
        const end = Math.max(lastClickedIndex, idx);
        for (let i = start; i <= end; i++) {
          if (wallpapers[i]) selectedIds.add(wallpapers[i].id);
        }
      } else {
        if (selectedIds.has(id)) {
          selectedIds.delete(id);
        } else {
          selectedIds.add(id);
        }
      }
      lastClickedIndex = idx;
      updateSelectionUI();
      syncCardSelectionDOM();
    }

    function updateSelectionUI() {
      const count = selectedIds.size;
      document.getElementById('selection-counter').innerText = `${count} selected`;
      const batchBar = document.getElementById('batch-action-bar');
      if (count > 0) {
        batchBar.style.display = 'flex';
        document.getElementById('batch-count-label').innerText = `${count} Selected`;
      } else {
        batchBar.style.display = 'none';
      }
    }

    function toggleSelectAll() {
      if (selectedIds.size === wallpapers.length && wallpapers.length > 0) {
        selectedIds.clear();
      } else {
        wallpapers.forEach(w => selectedIds.add(w.id));
      }
      updateSelectionUI();
      syncCardSelectionDOM();
    }

    function clearSelection() {
      selectedIds.clear();
      updateSelectionUI();
      document.querySelectorAll('.card').forEach(c => c.classList.remove('selected'));
    }

    async function quickCurate(id, action, idx) {
      const res = await fetch('/api/curate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, action: action })
      });
      const data = await res.json();

      const card = document.querySelector(`.card[data-id="${id}"]`);

      if (action === 'approve') {
        showToast(`✨ Approved as ${data.curated_path}`);
        if (wallpapers[idx]) {
          wallpapers[idx].is_curated = 1;
          wallpapers[idx].curated_filename = data.curated_filename;
        }
        if (card) {
          card.classList.add('is-curated');
          card.classList.remove('is-rejected');
          let rib = card.querySelector('.card-ribbon');
          if (!rib) {
            rib = document.createElement('div');
            card.prepend(rib);
          }
          rib.className = 'card-ribbon curated';
          rib.innerText = `✓ ${data.curated_filename || 'Curated'}`;
        }
      } else if (action === 'reject') {
        showToast(`✕ Rejected ID #${id}`);
        if (wallpapers[idx]) wallpapers[idx].is_curated = -1;
        if (card) {
          card.classList.add('is-rejected');
          card.classList.remove('is-curated');
          let rib = card.querySelector('.card-ribbon');
          if (!rib) {
            rib = document.createElement('div');
            card.prepend(rib);
          }
          rib.className = 'card-ribbon rejected';
          rib.innerText = '✕ Rejected';
        }
      }

      const status = document.getElementById('filter-status').value;
      if (status === 'uncurated') {
        if (card) card.remove();
        wallpapers.splice(idx, 1);
      }
      loadStats();
    }

    async function quickClassifySingle(id) {
      showToast(`🤖 Analyzing ID #${id}...`);
      const res = await fetch('/api/classifier/single', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id })
      });
      const data = await res.json();
      if (data.success) {
        showToast(`🤖 ID #${id}: Suggested '${data.suggested_category}' (${data.signals})`);
      } else {
        showToast(`❌ Classifier error: ${data.error}`);
      }
    }

    async function batchAct(action) {
      if (selectedIds.size === 0) return;
      const ids = Array.from(selectedIds);
      showToast(`⏳ Processing ${ids.length} wallpapers...`);

      const res = await fetch('/api/curate-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids, action: action })
      });
      const data = await res.json();

      if (action === 'approve') {
        showToast(`✨ Approved & renumbered ${data.count} wallpapers!`);
      } else if (action === 'reject') {
        showToast(`✕ Rejected ${data.count} wallpapers`);
      } else {
        showToast(`↩ Reset status for ${data.count} wallpapers`);
      }

      clearSelection();
      loadWallpapers();
    }

    async function batchMove(newCat) {
      if (!newCat || selectedIds.size === 0) return;
      const ids = Array.from(selectedIds);
      showToast(`⏳ Moving ${ids.length} wallpapers to ${newCat}...`);

      await fetch('/api/curate-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids, action: 'skip', new_category: newCat })
      });

      showToast(`📁 Moved ${ids.length} wallpapers to ${newCat}`);
      document.getElementById('batch-move-cat').value = '';
      clearSelection();
      loadWallpapers();
    }

    async function batchClassifySelected() {
      if (selectedIds.size === 0) return;
      const ids = Array.from(selectedIds);
      startTaskPoll('/api/classifier/batch', { ids: ids, auto_apply: true });
      clearSelection();
    }

    /* Lightbox Focus Functions */
    function openLightbox(idx) {
      lightboxIndex = idx;
      renderLightbox();
      document.getElementById('lightbox-modal').style.display = 'flex';
      fetchLightboxClassification();
    }

    function closeLightbox() {
      document.getElementById('lightbox-modal').style.display = 'none';
    }

    function navLightbox(delta) {
      lightboxIndex = (lightboxIndex + delta + wallpapers.length) % wallpapers.length;
      renderLightbox();
      fetchLightboxClassification();
    }

    function renderLightbox() {
      const w = wallpapers[lightboxIndex];
      if (!w) return;

      const lbSrc = (w.is_curated === 1 && w.s3_url)
        ? w.s3_url
        : `/image/${encodeURIComponent(w.category)}/${encodeURIComponent(w.filename)}`;
      document.getElementById('lb-image').src = lbSrc;
      document.getElementById('lb-title').innerText = `${w.category} #${w.id}`;
      document.getElementById('lb-cat').innerText = w.category;
      document.getElementById('lb-res').innerText = `${w.width} × ${w.height}`;
      document.getElementById('lb-ratio').innerText = w.aspect_ratio || '16:9';
      document.getElementById('lb-fmt').innerText = (w.format || 'JPEG').toUpperCase();
      document.getElementById('lb-size').innerText = w.filesize ? `${(w.filesize / (1024*1024)).toFixed(2)} MB` : '-';
      document.getElementById('lb-cur-name').innerText = w.curated_filename || '-';

      const badge = document.getElementById('lb-curated-badge');
      if (w.is_curated === 1) {
        badge.style.display = 'inline-block';
        badge.innerText = `✓ Curated (${w.curated_filename || ''})`;
      } else {
        badge.style.display = 'none';
      }
    }

    async function fetchLightboxClassification() {
      const w = wallpapers[lightboxIndex];
      if (!w) return;

      document.getElementById('lb-ai-cat').innerText = 'Analyzing...';
      document.getElementById('lb-ai-conf').innerText = '';
      document.getElementById('lb-ai-signals').innerText = '';

      const res = await fetch('/api/classifier/single', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: w.id })
      });
      const data = await res.json();
      if (data.success) {
        document.getElementById('lb-ai-cat').innerText = data.suggested_category;
        document.getElementById('lb-ai-conf').innerText = `Confidence: ${data.confidence}% (${data.type})`;
        document.getElementById('lb-ai-signals').innerText = `Signals: ${data.signals}`;
        document.getElementById('btn-lb-apply-sugg').style.display = (data.suggested_category !== w.category) ? 'inline-block' : 'none';
      }
    }

    async function applyLightboxSuggestion() {
      const w = wallpapers[lightboxIndex];
      const newCat = document.getElementById('lb-ai-cat').innerText;
      if (!newCat || newCat === 'Analyzing...') return;
      await changeLightboxCategory(newCat);
    }

    async function actLightbox(action) {
      const w = wallpapers[lightboxIndex];
      await quickCurate(w.id, action, lightboxIndex);
      renderLightbox();
    }

    async function changeLightboxCategory(newCat) {
      if (!newCat) return;
      const w = wallpapers[lightboxIndex];
      await fetch('/api/curate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: w.id, action: 'skip', new_category: newCat })
      });
      w.category = newCat;
      showToast(`📁 Moved ID #${w.id} to ${newCat}`);
      document.getElementById('lb-change-cat').value = '';
      renderLightbox();
      fetchLightboxClassification();
    }

    /* Multi-Source Ingestion Functions */
    function switchIngestTab(tab) {
      ['wh-search', 'wh-ids', 'deviantart', 'web-urls', 'local-folder'].forEach(t => {
        const btn = document.getElementById(`tab-${t}`);
        const pane = document.getElementById(`pane-${t}`);
        if (btn) btn.classList.remove('active');
        if (pane) pane.style.display = 'none';
      });
      const activeBtn = document.getElementById(`tab-${tab}`);
      const activePane = document.getElementById(`pane-${tab}`);
      if (activeBtn) activeBtn.classList.add('active');
      if (activePane) activePane.style.display = 'flex';

      if (tab === 'wh-search' || tab === 'wh-ids') {
        refreshWallhavenStatus();
      }
      if (tab === 'deviantart') {
        refreshDeviantArtStatus();
      }
    }

    let whFoundItems = [];
    let whHasApiKey = false;

    async function refreshWallhavenStatus() {
      const badge = document.getElementById('wh-status-badge');
      if (!badge) return;
      badge.innerText = 'Checking...';
      badge.style.background = '#475569';
      try {
        const res = await fetch('/api/wallhaven/status');
        const data = await res.json();
        whHasApiKey = data.has_api_key;
        if (whHasApiKey) {
          badge.innerText = '✓ API Key Active';
          badge.style.background = 'var(--green)';
          badge.style.color = '#000';
        } else {
          badge.innerText = 'No API Key';
          badge.style.background = '#475569';
          badge.style.color = '#fff';
        }
        document.querySelectorAll('.wh-nsfw-option').forEach(opt => {
          opt.disabled = !whHasApiKey;
        });
      } catch (e) {
        badge.innerText = 'Unknown';
      }
    }

    function handleThumbError(imgEl, originalUrl) {
      if (!imgEl.dataset.proxied && originalUrl && originalUrl.startsWith('http')) {
        imgEl.dataset.proxied = '1';
        imgEl.src = `/api/proxy-image?url=${encodeURIComponent(originalUrl)}`;
      } else {
        imgEl.classList.add('wh-thumb-error');
        imgEl.closest('.wh-thumb')?.classList.add('has-error');
        imgEl.closest('.wh-thumb')?.classList.remove('is-loading');
      }
    }

    async function searchWallhaven() {
      const q = document.getElementById('wh-q').value.trim();
      const sort = document.getElementById('wh-sort').value;
      const topRange = document.getElementById('wh-top-range').value;
      const ratio = document.getElementById('wh-ratio').value;
      const purity = document.getElementById('wh-purity').value;
      const categories = document.getElementById('wh-categories')?.value || '111';
      const atleast = document.getElementById('wh-atleast')?.value || '2560x1440';

      document.getElementById('wh-search-status').innerText = '🔍 Searching Wallhaven...';
      const grid = document.getElementById('wh-results-grid');
      grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--accent);">Fetching 2K+ wallpapers from Wallhaven...</div>';

      const quantity = parseInt(document.getElementById('wh-ingest-limit')?.value || '24');
      const url = `/api/wallhaven/search?q=${encodeURIComponent(q)}&sorting=${sort}&top_range=${topRange}&ratios=${ratio}&purity=${purity}&categories=${encodeURIComponent(categories)}&atleast=${encodeURIComponent(atleast)}&limit=${quantity}`;
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (!data.success) {
          grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--red);">Error: ${data.error}</div>`;
          return;
        }

        whFoundItems = data.data || [];
        document.getElementById('wh-search-status').innerText = `Found ${whFoundItems.length} wallpapers`;
        const btnAll = document.getElementById('btn-wh-ingest-all');
        if (whFoundItems.length > 0) {
          btnAll.style.display = 'block';
          btnAll.innerText = `📥 Ingest (${Math.min(whFoundItems.length, quantity)})`;
        } else {
          btnAll.style.display = 'none';
        }

        grid.innerHTML = '';
        if (whFoundItems.length === 0) {
          grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-muted);">No 2K+ wallpapers matched this filter. Try adjusting query or purity.</div>';
          return;
        }

        whFoundItems.forEach(item => {
          const card = document.createElement('div');
          card.className = 'wh-card';
          card.innerHTML = `
            <div class="wh-thumb is-loading">
              <div class="wh-thumb-placeholder"></div>
              <img src="${item.thumb || ''}" loading="eager" referrerpolicy="no-referrer" onload="this.closest('.wh-thumb')?.classList.remove('is-loading'); this.closest('.wh-thumb')?.classList.add('is-loaded')" onerror="handleThumbError(this, '${(item.thumb || '').replace(/'/g, '%27')}')" />
              <div class="wh-thumb-fallback">🖼️ <span style="font-size:0.72rem; margin-left: 6px;">Preview unavailable</span></div>
              <span class="wh-res">${item.resolution || ''}</span>
            </div>
            <div class="wh-meta">
              <div style="font-weight: 700; color: var(--text-bright); display: flex; justify-content: space-between; gap: 8px; min-width: 0;">
                <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">#${item.id}</span>
                <span style="color: var(--accent); white-space: nowrap;">${item.category || ''}</span>
              </div>
              <button class="btn-quick" style="width: 100%; justify-content: center; background: #0284c7; color: white; border: none; margin-top: 4px;" onclick="ingestSingleFromSearch('${item.id}')">📥 Ingest</button>
            </div>
          `;
          grid.appendChild(card);
        });
      } catch (e) {
        grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--red);">Search failed: ${e.message}</div>`;
      }
    }

    document.getElementById('wh-ingest-limit')?.addEventListener('change', () => {
      const lim = parseInt(document.getElementById('wh-ingest-limit').value || '16');
      const btn = document.getElementById('btn-wh-ingest-all');
      if (btn && whFoundItems.length) btn.textContent = `📥 Ingest (${Math.min(whFoundItems.length, lim)})`;
    });
    function ingestSingleFromSearch(whId) {
      const catHint = document.getElementById('wh-search-cat-hint').value;
      startTaskPoll('/api/ingest/wallhaven-ids', { items: [whId], category_hint: catHint });
      closeModal('ingest-modal');
    }

    function ingestAllFromSearch() {
      const selLimit = parseInt(document.getElementById('wh-ingest-limit')?.value || '24');
      if (whFoundItems.length === 0) return;
      const ids = whFoundItems.slice(0, selLimit).map(it => it.id);
      const catHint = document.getElementById('wh-search-cat-hint').value;
      startTaskPoll('/api/ingest/wallhaven-ids', { items: ids, category_hint: catHint });
      closeModal('ingest-modal');
    }

    function ingestWallhavenIds() {
      const text = document.getElementById('wh-ids-text').value.trim();
      if (!text) return;
      const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
      const catHint = document.getElementById('wh-ids-cat-hint').value;
      startTaskPoll('/api/ingest/wallhaven-ids', { items: lines, category_hint: catHint });
      closeModal('ingest-modal');
    }

    function ingestWebUrls() {
      const text = document.getElementById('web-urls-text').value.trim();
      if (!text) return;
      const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
      const catHint = document.getElementById('web-urls-cat-hint').value;
      startTaskPoll('/api/ingest/urls', { urls: lines, category_hint: catHint });
      closeModal('ingest-modal');
    }

    function ingestLocalFolder() {
      const folderPath = document.getElementById('local-folder-path').value.trim();
      if (!folderPath) return;
      const move = document.getElementById('local-folder-move').checked;
      const catHint = document.getElementById('local-folder-cat-hint').value;
      startTaskPoll('/api/ingest/local-folder', { folder_path: folderPath, move: move, category_hint: catHint });
      closeModal('ingest-modal');
    }

    /* Background Task Poller */
        let hudLogsVisible = false;

    function toggleHudLogs() {
      const drawer = document.getElementById('hud-logs-drawer');
      if (!drawer) return;
      hudLogsVisible = !hudLogsVisible;
      drawer.style.display = hudLogsVisible ? 'flex' : 'none';
    }

    async function startTaskPoll(apiEndpoint, payload) {
      try {
        const res = await fetch(apiEndpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!data.success) {
          showToast(`❌ Error: ${data.error || 'Failed to start task'}`);
          return;
        }

        // Show Floating HUD & Top Pill immediately
        showTaskHud();
        clearInterval(taskPollTimer);
        taskPollTimer = setInterval(pollTaskStatus, 600);
      } catch (e) {
        showToast(`❌ Task trigger failed: ${e.message}`);
      }
    }

    function showTaskHud() {
      const hud = document.getElementById('ingest-hud');
      const topPill = document.getElementById('top-task-pill');
      if (hud) hud.style.display = 'flex';
      if (topPill) topPill.style.display = 'flex';
    }

    async function pollTaskStatus() {
      try {
        const res = await fetch('/api/task-status');
        const st = await res.json();

        const hud = document.getElementById('ingest-hud');
        const topPill = document.getElementById('top-task-pill');
        const fill = document.getElementById('hud-bar-fill');
        const title = document.getElementById('hud-title');
        const itemLabel = document.getElementById('hud-item-label');
        const pctLabel = document.getElementById('hud-pct-label');
        const topLabel = document.getElementById('top-task-label');
        const okBadge = document.getElementById('hud-ok-count');
        const dupBadge = document.getElementById('hud-dup-count');
        const failBadge = document.getElementById('hud-fail-count');
        const logsDiv = document.getElementById('hud-logs-drawer');

        if (st.status === 'running') {
          showTaskHud();
          if (title) title.innerText = st.task_name;
          if (itemLabel) itemLabel.innerText = st.current_item || 'Downloading...';
          if (pctLabel) pctLabel.innerText = `${st.progress}% (${st.completed}/${st.total})`;
          if (topLabel) topLabel.innerText = `⏳ ${st.completed}/${st.total} (${st.progress}%)`;
          if (fill) fill.style.width = `${st.progress}%`;

          if (okBadge) okBadge.innerText = `✓ ${st.completed} Added`;
          if (dupBadge) dupBadge.innerText = `⚠️ ${st.duplicates || 0} Dup`;
          if (failBadge) failBadge.innerText = `✕ ${st.failed || 0} Err`;

          if (logsDiv && st.logs) {
            logsDiv.innerHTML = st.logs.slice(-12).map(l => {
              let color = '#94a3b8';
              if (l.includes('✅') || l.includes('Ingested')) color = '#10b981';
              else if (l.includes('⚠️') || l.includes('duplicate')) color = '#f59e0b';
              else if (l.includes('❌') || l.includes('failed') || l.includes('Rejected')) color = '#ef4444';
              return `<div style="color:${color};">${l}</div>`;
            }).join('');
            logsDiv.scrollTop = logsDiv.scrollHeight;
          }
        } else if (st.status === 'completed' || st.status === 'failed' || st.status === 'cancelled') {
          clearInterval(taskPollTimer);
          if (fill) fill.style.width = '100%';
          if (topPill) topPill.style.display = 'none';

          if (st.status === 'completed') {
            if (title) title.innerText = `✨ ${st.task_name} Finished!`;
            if (itemLabel) itemLabel.innerText = `Successfully processed ${st.completed} of ${st.total} wallpapers.`;
            showToast(`✨ ${st.task_name} completed! (${st.completed} added)`);
          } else if (st.status === 'cancelled') {
            if (title) title.innerText = `⚠️ Task Cancelled`;
            showToast(`⚠️ Task cancelled by user`);
          } else {
            if (title) title.innerText = `❌ Task Failed`;
            showToast(`❌ Task failed: ${st.result ? st.result.error || '' : ''}`);
          }

          // Trigger full immediate refresh & stats recalculation
          await loadStats();
          await loadWallpapers();

          // Auto-hide HUD after 4 seconds
          setTimeout(() => {
            if (hud) hud.style.display = 'none';
          }, 4000);
        }
      } catch (e) {
        clearInterval(taskPollTimer);
      }
    }

    async function cancelCurrentTask() {
      await fetch('/api/task-cancel', { method: 'POST' });
      showToast('⏳ Cancelling task...');
    }


    // ── DeviantArt Ingest ──
    let deviantartFoundItems = [];

    async function refreshDeviantArtStatus() {
      const badge = document.getElementById('deviantart-status-badge');
      if (!badge) return;
      badge.innerText = 'Checking...';
      badge.style.background = '#475569';
      try {
        const res = await fetch('/api/sources');
        const data = await res.json();
        const da = (data.sources || []).find(s => s.key === 'deviantart');
        if (da && da.is_configured) {
          badge.innerText = '✓ Ready';
          badge.style.background = 'var(--green)';
          badge.style.color = '#000';
        } else {
          badge.innerText = 'Key Needed';
          badge.style.background = '#475569';
          badge.style.color = '#fff';
        }
      } catch (e) {
        badge.innerText = 'Unknown';
      }
    }

    async function previewDeviantArt() {
      const q = document.getElementById('deviantart-query')?.value.trim() || '';
      const limit = parseInt(document.getElementById('deviantart-limit')?.value || '10');
      const catHint = document.getElementById('deviantart-cat-hint')?.value || '';
      const mature = Boolean(document.getElementById('deviantart-mature')?.checked);
      const grid = document.getElementById('deviantart-preview-grid');
      const btnAll = document.getElementById('btn-deviantart-ingest-previewed');
      if (!grid) return;

      grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 30px; color: var(--accent);">🔍 Fetching DeviantArt previews...</div>';
      if (btnAll) btnAll.style.display = 'none';

      try {
        const url = `/api/sources/preview?source=deviantart&q=${encodeURIComponent(q)}&limit=${limit}&category_hint=${encodeURIComponent(catHint)}&mature=${mature}`;
        const res = await fetch(url);
        const data = await res.json();
        const items = data.success && Array.isArray(data.items) ? data.items : [];
        deviantartFoundItems = items;

        if (!items.length) {
          grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 30px; color: var(--text-muted);">${data.error || 'No downloadable artworks found. Check credentials or try another query.'}</div>`;
          if (btnAll) btnAll.style.display = 'none';
          return;
        }

        if (btnAll) {
          btnAll.style.display = 'inline-flex';
          btnAll.innerText = `📥 Ingest Found (${items.length})`;
        }
        grid.innerHTML = renderPreviewGrid(items, 'deviantart', 'deviantart-cat-hint');
      } catch (e) {
        grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 30px; color: var(--red);">Preview error: ${e.message}</div>`;
      }
    }

    function ingestSingleDeviantArtItem(idx) {
      const item = deviantartFoundItems[idx];
      if (!item || !item.image_url) return;
      const catHint = document.getElementById('deviantart-cat-hint')?.value || item.category || null;
      startTaskPoll('/api/ingest/urls', { urls: [item.image_url], category_hint: catHint });
      closeModal('ingest-modal');
    }

    function ingestAllPreviewedDeviantArt() {
      if (!deviantartFoundItems.length) return;
      const urls = deviantartFoundItems.map(it => it.image_url).filter(Boolean);
      const catHint = document.getElementById('deviantart-cat-hint')?.value || null;
      startTaskPoll('/api/ingest/urls', { urls: urls, category_hint: catHint });
      closeModal('ingest-modal');
    }

    function runDeviantArtIngest() {
      const q = document.getElementById('deviantart-query')?.value.trim() || '';
      const limit = parseInt(document.getElementById('deviantart-limit')?.value || '10');
      const catHint = document.getElementById('deviantart-cat-hint')?.value || null;
      const mature = Boolean(document.getElementById('deviantart-mature')?.checked);

      startTaskPoll('/api/ingest/sources', {
        sources: ['deviantart'],
        query: q,
        limit_per_source: limit,
        category_hint: catHint,
        include_mature: mature
      });
      closeModal('ingest-modal');
    }

    function renderPreviewGrid(items, sourcePrefix, catHintId) {
      return items.map((it, idx) => `
        <div style="border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--card); display: flex; flex-direction: column;">
          <div style="width: 100%; height: 115px; background: #000; overflow: hidden; position: relative;">
            <img src="${it.thumb}" style="width: 100%; height: 100%; object-fit: cover; display: block;" loading="lazy" onerror="handleThumbError(this, '${(it.thumb || '').replace(/'/g, '%27')}')" referrerpolicy="no-referrer" />
            <span style="position: absolute; bottom: 4px; right: 6px; background: rgba(0,0,0,0.75); color: #fff; font-size: 0.68rem; font-weight: 700; padding: 1px 5px; border-radius: 4px;">${it.resolution || '2K+'}</span>
            <span style="position: absolute; top: 4px; left: 6px; background: var(--accent); color: #000; font-size: 0.65rem; font-weight: 800; padding: 1px 5px; border-radius: 3px;">${it.source_name ? it.source_name.split(' ')[0] : 'Src'}</span>
          </div>
          <div style="padding: 8px 10px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; gap: 6px;">
            <div>
              <div style="font-size: 0.8rem; font-weight: 700; color: var(--text-bright); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${it.title || ''}">${it.title || 'Wallpaper'}</div>
              <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${it.author ? it.author : ''}</div>
            </div>
            <button class="btn-quick" style="width: 100%; justify-content: center; background: #0284c7; color: white; border: none; font-size: 0.75rem; padding: 4px 6px;" onclick="ingestSingleDeviantArtItem(${idx})">📥 Ingest</button>
          </div>
        </div>
      `).join('');
    }

    /* Classifier Audit Functions */
    async function runClassifierAudit() {
      const container = document.getElementById('audit-results-container');
      container.innerHTML = '<div style="text-align:center; padding:40px; color:var(--accent);">🤖 Evaluating category signals across wallpapers...</div>';

      const res = await fetch(`/api/classifier/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: wallpapers.map(w => w.id), auto_apply: false })
      });
      const data = await res.json();
      if (!data.success) {
        container.innerHTML = `<div style="color:var(--red); text-align:center; padding:30px;">Error: ${data.error}</div>`;
        return;
      }

      auditResults = (data.suggestions || []).filter(s => s.is_different);
      const btnApply = document.getElementById('btn-apply-all-audit');

      if (auditResults.length === 0) {
        btnApply.style.display = 'none';
        container.innerHTML = '<div style="text-align:center; padding:40px; color:var(--green); font-weight:700;">✅ All wallpapers in this view are categorized accurately! No mismatches detected.</div>';
        return;
      }

      btnApply.style.display = 'block';
      btnApply.innerText = `⚡ Apply All Suggestions (${auditResults.length})`;

      let html = `
        <table class="audit-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Current Category</th>
              <th>Suggested Category</th>
              <th>Confidence & Signals</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
      `;

      auditResults.forEach(item => {
        html += `
          <tr>
            <td><strong>#${item.id}</strong></td>
            <td><span style="color:var(--red); font-weight:700;">${item.current_category}</span></td>
            <td><span style="color:var(--green); font-weight:800;">✓ ${item.suggested_category}</span></td>
            <td>
              <div style="font-size:0.75rem; color:var(--text-bright);">${Math.round(item.confidence * 100)}% (${item.type})</div>
              <div style="font-size:0.7rem; color:var(--text-muted);">${item.signals}</div>
            </td>
            <td>
              <button class="btn-quick btn-quick-approve" onclick="applySingleAudit(${item.id}, '${item.suggested_category}')">⚡ Apply</button>
            </td>
          </tr>
        `;
      });
      html += '</tbody></table>';
      container.innerHTML = html;
    }

    async function applySingleAudit(id, newCat) {
      await fetch('/api/curate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, action: 'skip', new_category: newCat })
      });
      showToast(`🔄 Reclassified ID #${id} -> ${newCat}`);
      runClassifierAudit();
      loadWallpapers();
    }

    async function applyAllClassifierSuggestions() {
      if (auditResults.length === 0) return;
      const ids = auditResults.map(s => s.id);
      showToast(`⏳ Applying ${ids.length} category corrections...`);

      startTaskPoll('/api/classifier/batch', { ids: ids, auto_apply: true });
      closeModal('classifier-modal');
    }

    /* Command Palette / Command Bar */
    const COMMANDS = [
      { id: 'ingest-wh', name: 'Open Ingestion Studio (Wallhaven / Bing / NASA)', category: 'Ingestion', icon: '📥', kbd: 'I', action: () => openIngestModal() },
      { id: 'clean-dups', name: 'Run Visual & Hash Duplicate Cleaner', category: 'Tools', icon: '🧹', action: () => openDuplicatesModal() },
      { id: 'ratio-all', name: 'Aspect Ratio Filter: All Displays', category: 'Layout', icon: '▦', action: () => setRatioFilter('all') },
      { id: 'ratio-16-9', name: 'Aspect Ratio Filter: 16:9 Standard (4K UHD)', category: 'Layout', icon: '🖥️', action: () => setRatioFilter('16:9') },
      { id: 'ratio-21-9', name: 'Aspect Ratio Filter: 21:9 Ultrawide', category: 'Layout', icon: '🖼️', action: () => setRatioFilter('21:9') },
      { id: 'ratio-32-9', name: 'Aspect Ratio Filter: 32:9 Super Ultrawide', category: 'Layout', icon: '📺', action: () => setRatioFilter('32:9') },
      { id: 'ratio-9-16', name: 'Aspect Ratio Filter: 9:16 Mobile / Portrait', category: 'Layout', icon: '📱', action: () => setRatioFilter('9:16') },
      { id: 'classify-audit', name: 'Run AI Classifier Library Audit', category: 'Classifier', icon: '🤖', action: () => openClassifierModal() },
      { id: 'approve-selected', name: 'Approve Selected Wallpapers', category: 'Actions', icon: '✓', kbd: 'A', action: () => batchAct('approve') },
      { id: 'reject-selected', name: 'Reject Selected Wallpapers', category: 'Actions', icon: '✕', kbd: 'R', action: () => batchAct('reject') },
      { id: 'classify-selected', name: 'Auto-Classify Selected Wallpapers', category: 'Classifier', icon: '⚡', action: () => batchClassifySelected() },
      { id: 'select-all', name: 'Select All Visible Wallpapers', category: 'Actions', icon: '▦', kbd: 'Ctrl+A', action: () => toggleSelectAll() },
      { id: 'clear-selection', name: 'Clear Selection', category: 'Actions', icon: '⦸', kbd: 'Esc', action: () => clearSelection() },
      { id: 'publish-cdn', name: 'Publish Curated Collection to CDN & Git', category: 'Actions', icon: '🚀', action: () => publishToCdn() },
      { id: 'open-stats', name: 'Open Curation Analytics & Statistics', category: 'Views', icon: '📊', action: () => openStatsModal() },
      { id: 'open-shortcuts', name: 'Open Keyboard Shortcuts Cheat Sheet', category: 'Views', icon: '⌨️', action: () => openShortcutsModal() },
      { id: 'show-uncurated', name: 'Show Uncurated Wallpapers Only', category: 'Filters', icon: '⏳', action: () => quickFilterStatus('uncurated', null) },
      { id: 'show-curated', name: 'Show Curated Collection Only', category: 'Filters', icon: '✨', action: () => quickFilterStatus('curated', null) },
      { id: 'show-rejected', name: 'Show Rejected Wallpapers', category: 'Filters', icon: '🚫', action: () => quickFilterStatus('rejected', null) },
      { id: 'show-all', name: 'Show All Wallpapers (Everything)', category: 'Filters', icon: '📁', action: () => quickFilterStatus('all', null) },
      { id: 'density-sm', name: 'Set Grid Density: Compact (Small)', category: 'Layout', icon: '▦', action: () => setDensity('sm') },
      { id: 'density-md', name: 'Set Grid Density: Normal (Medium)', category: 'Layout', icon: '▦', action: () => setDensity('md') },
      { id: 'density-lg', name: 'Set Grid Density: Large (Expanded)', category: 'Layout', icon: '▦', action: () => setDensity('lg') },
    ];

    CATEGORIES.forEach(cat => {
      COMMANDS.push({
        id: `goto-${cat.toLowerCase()}`,
        name: `Jump to Category: ${cat}`,
        category: 'Categories',
        icon: '📁',
        action: () => selectCategory(cat, null)
      });
    });

    function openCommandBar() {
      document.getElementById('command-bar-modal').style.display = 'flex';
      const input = document.getElementById('cmd-input');
      input.value = '';
      input.focus();
      cmdActiveIndex = 0;
      filterCommands();
    }

    function closeCommandBar(e) {
      if (e && e.target !== document.getElementById('command-bar-modal')) return;
      document.getElementById('command-bar-modal').style.display = 'none';
    }

    function filterCommands() {
      const query = document.getElementById('cmd-input').value.toLowerCase().trim();
      const list = document.getElementById('cmd-list');
      list.innerHTML = '';

      const filtered = COMMANDS.filter(cmd => cmd.name.toLowerCase().includes(query) || cmd.category.toLowerCase().includes(query));

      if (filtered.length === 0) {
        list.innerHTML = '<div style="padding:24px; text-align:center; color:var(--text-muted); font-size:0.9rem;">No matching commands</div>';
        return;
      }

      let lastCat = '';
      filtered.forEach((cmd, idx) => {
        if (cmd.category !== lastCat) {
          list.innerHTML += `<div class="cmd-section-title">${cmd.category}</div>`;
          lastCat = cmd.category;
        }

        const div = document.createElement('div');
        div.className = `cmd-item ${idx === cmdActiveIndex ? 'active' : ''}`;
        div.onclick = () => {
          document.getElementById('command-bar-modal').style.display = 'none';
          cmd.action();
        };

        div.innerHTML = `
          <div class="cmd-item-left">
            <span>${cmd.icon}</span>
            <span>${cmd.name}</span>
          </div>
          ${cmd.kbd ? `<kbd>${cmd.kbd}</kbd>` : ''}
        `;
        list.appendChild(div);
      });
    }

    function handleCmdKey(e) {
      const items = document.querySelectorAll('.cmd-item');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        cmdActiveIndex = (cmdActiveIndex + 1) % items.length;
        updateCmdActive(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        cmdActiveIndex = (cmdActiveIndex - 1 + items.length) % items.length;
        updateCmdActive(items);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (items[cmdActiveIndex]) items[cmdActiveIndex].click();
      } else if (e.key === 'Escape') {
        document.getElementById('command-bar-modal').style.display = 'none';
      }
    }

    function updateCmdActive(items) {
      items.forEach((it, i) => it.classList.toggle('active', i === cmdActiveIndex));
      if (items[cmdActiveIndex]) {
        items[cmdActiveIndex].scrollIntoView({ block: 'nearest' });
      }
    }

    async function publishToCdn() {
      showToast('☁️ Publishing curated wallpapers to CDN & syncing README...');
      const res = await fetch('/api/publish-cdn', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
      const data = await res.json();
      if (data.success && data.task_id) {
        showToast(`🚀 CDN Publish started (Task #${data.task_id})`);
      } else {
        showToast(`❌ Publish error: ${data.error || 'Failed to start'}`);
      }
    }

    function debounceSearch() {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(loadWallpapers, 300);
    }

    function showToast(msg) {
      const toast = document.getElementById('toast');
      toast.innerText = msg;
      toast.style.display = 'block';
      setTimeout(() => { toast.style.display = 'none'; }, 3000);
    }

    // Global Hotkeys Listener
    window.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') {
        if (e.key === 'Escape') {
          e.target.blur();
          if (document.getElementById('command-bar-modal').style.display === 'flex') {
            document.getElementById('command-bar-modal').style.display = 'none';
          }
        }
        return;
      }

      const cmdOpen = document.getElementById('command-bar-modal').style.display === 'flex';
      const lbOpen = document.getElementById('lightbox-modal').style.display === 'flex';
      const ingOpen = document.getElementById('ingest-modal').style.display === 'flex';
      const clsOpen = document.getElementById('classifier-modal').style.display === 'flex';

      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        openCommandBar();
      } else if (e.key === '/') {
        e.preventDefault();
        openCommandBar();
      } else if (e.key === 'Escape') {
        if (cmdOpen) document.getElementById('command-bar-modal').style.display = 'none';
        else if (lbOpen) closeLightbox();
        else if (ingOpen) closeModal('ingest-modal');
        else if (clsOpen) closeModal('classifier-modal');
        else if (document.getElementById('stats-modal').style.display === 'flex') closeModal('stats-modal');
        else if (document.getElementById('shortcuts-modal').style.display === 'flex') closeModal('shortcuts-modal');
        else clearSelection();
      } else if (e.key === '?') {
        openShortcutsModal();
      } else if (e.key === 'i' || e.key === 'I') {
        openIngestModal();
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        e.preventDefault();
        toggleSelectAll();
      } else if (lbOpen) {
        if (e.key === 'a' || e.key === 'A' || e.key === 'Enter') actLightbox('approve');
        else if (e.key === 'r' || e.key === 'R' || e.key === 'Delete') actLightbox('reject');
        else if (e.key === 'ArrowRight' || e.key === ' ') navLightbox(1);
        else if (e.key === 'ArrowLeft') navLightbox(-1);
      } else {
        if (e.key === 'a' || e.key === 'A') batchAct('approve');
        else if (e.key === 'r' || e.key === 'R') batchAct('reject');
      }
    });

    initCategories();
    loadWallpapers();
