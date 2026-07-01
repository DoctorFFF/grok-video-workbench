const state = {
  tasks: [],
  activeTaskId: null,
  bootstrap: null,
  cpaFiles: [],
  cpaDetailsOpen: {},
  cpaModelsOpen: {},
  sceneDrafts: {},
  globalDrafts: {},
  sceneSelections: {},
  sceneSelectionTouched: {},
  toastTimer: null,
  lastCpaRefresh: "",
  auth: {
    initialized: false,
    authenticated: false,
    locked: false,
    minKeyLength: 12,
  },
  appStarted: false,
  globalButtonsBound: false,
  refreshTimers: [],
};

const MAX_REFERENCE_IMAGES = 7;
const TOO_MANY_IMAGES_MESSAGE = "只能上传 7 个，请先删除部分图片再上传";
const CPA_REFRESH_INTERVAL_MS = 60000;

const $ = (id) => document.getElementById(id);

function detailMessage(detail, fallback = "请求失败") {
  if (typeof detail === "string") return detail || fallback;
  const payload = detail?.detail || detail;
  if (typeof payload === "string") return payload || fallback;
  return payload?.message || payload?.zh || JSON.stringify(payload || detail || fallback);
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail;
    try { detail = await res.json(); } catch { detail = await res.text(); }
    if (res.status === 401 && !String(path).startsWith("/api/auth/")) {
      lockForAuth("登录状态已失效，请重新输入授权密钥。");
    }
    throw new Error(detailMessage(detail));
  }
  return res.json();
}

function toast(message, duration = 8000) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.classList.remove("show"), duration);
}

function ensureModal() {
  let modal = $("confirmModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "confirmModal";
  modal.className = "modal-backdrop";
  modal.innerHTML = `
    <div class="modal-panel">
      <div class="modal-kicker">操作确认</div>
      <h3></h3>
      <p></p>
      <div class="modal-actions">
        <button class="btn ghost modal-cancel">取消</button>
        <button class="btn primary modal-confirm">确定</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

function modalDialog({ title, message, confirmText = "确定", cancelText = "取消", showCancel = true, danger = false }) {
  const modal = ensureModal();
  modal.querySelector("h3").textContent = title;
  modal.querySelector("p").textContent = message;
  const confirm = modal.querySelector(".modal-confirm");
  const cancel = modal.querySelector(".modal-cancel");
  confirm.textContent = confirmText;
  cancel.textContent = cancelText;
  confirm.className = `btn ${danger ? "danger" : "primary"} modal-confirm`;
  cancel.style.display = showCancel ? "" : "none";
  modal.classList.add("show");
  return new Promise((resolve) => {
    const cleanup = (value) => {
      modal.classList.remove("show");
      confirm.onclick = null;
      cancel.onclick = null;
      modal.onclick = null;
      resolve(value);
    };
    confirm.onclick = () => cleanup(true);
    cancel.onclick = () => cleanup(false);
    modal.onclick = (event) => {
      if (event.target === modal && showCancel) cleanup(false);
    };
  });
}

function alertDialog(title, message) {
  return modalDialog({ title, message, confirmText: "知道了", showCancel: false });
}

function ensurePromptEditorModal() {
  let modal = $("promptEditorModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "promptEditorModal";
  modal.className = "modal-backdrop prompt-editor-backdrop";
  modal.innerHTML = `
    <div class="modal-panel prompt-editor-panel">
      <div class="modal-kicker">大屏编辑</div>
      <h3></h3>
      <textarea class="prompt-editor-textarea"></textarea>
      <div class="prompt-editor-reference-tags"></div>
      <div class="modal-actions">
        <button class="btn ghost prompt-editor-close">关闭</button>
        <button class="btn primary prompt-editor-save">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

function promptEditorDialog({ title, value, imageUrls }) {
  const modal = ensurePromptEditorModal();
  const textarea = modal.querySelector(".prompt-editor-textarea");
  modal.querySelector("h3").textContent = title;
  textarea.value = value || "";
  modal.querySelector(".prompt-editor-reference-tags").innerHTML = renderReferenceImageTags(imageUrls);
  modal.classList.add("show");
  setTimeout(() => textarea.focus(), 0);
  return new Promise((resolve) => {
    const save = modal.querySelector(".prompt-editor-save");
    const close = modal.querySelector(".prompt-editor-close");
    const cleanup = (result) => {
      modal.classList.remove("show");
      save.onclick = null;
      close.onclick = null;
      modal.onclick = null;
      modal.querySelectorAll(".reference-token").forEach((btn) => { btn.onclick = null; });
      resolve(result);
    };
    modal.querySelectorAll(".reference-token").forEach((btn) => {
      btn.onclick = () => insertAtCursor(textarea, btn.dataset.token || "");
    });
    save.onclick = () => cleanup(textarea.value);
    close.onclick = () => cleanup(null);
    modal.onclick = (event) => {
      if (event.target === modal) cleanup(null);
    };
  });
}

function downloadThreadLimits() {
  const info = state.bootstrap?.download_threads || {};
  const max = Math.max(1, Number(info.max || navigator.hardwareConcurrency || 1));
  const recommended = Math.max(1, Math.min(max, Number(info.recommended || Math.floor(max * 0.75) || 1)));
  const configured = Math.max(1, Math.min(max, Number(state.bootstrap?.settings?.download_thread_count || info.default || recommended)));
  return { max, recommended, configured };
}

function ensureDownloadThreadModal() {
  let modal = $("downloadThreadModal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "downloadThreadModal";
  modal.className = "modal-backdrop";
  modal.innerHTML = `
    <div class="modal-panel download-thread-panel">
      <div class="modal-kicker">多线程下载</div>
      <h3>选择下载线程数</h3>
      <p class="download-thread-desc"></p>
      <label class="checkline download-thread-default"><input class="download-use-default" type="checkbox" checked /> 使用全局配置</label>
      <label>线程数</label>
      <input class="input download-thread-input" type="number" min="1" step="1" />
      <div class="hint download-thread-hint"></div>
      <div class="modal-actions">
        <button class="btn ghost download-thread-cancel">取消</button>
        <button class="btn primary download-thread-confirm">开始下载</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

async function chooseDownloadThreads() {
  const limits = downloadThreadLimits();
  const modal = ensureDownloadThreadModal();
  const useDefault = modal.querySelector(".download-use-default");
  const input = modal.querySelector(".download-thread-input");
  const hint = modal.querySelector(".download-thread-hint");
  modal.querySelector(".download-thread-desc").textContent = `推荐线程数：${limits.recommended}；本机最大支持：${limits.max}。`;
  useDefault.checked = true;
  input.value = limits.configured;
  input.disabled = true;
  hint.textContent = `当前全局配置：${limits.configured} 线程。取消勾选后可为本次下载临时修改。`;
  modal.classList.add("show");
  return new Promise((resolve) => {
    const confirm = modal.querySelector(".download-thread-confirm");
    const cancel = modal.querySelector(".download-thread-cancel");
    useDefault.onchange = () => {
      input.disabled = useDefault.checked;
      if (useDefault.checked) input.value = limits.configured;
      hint.textContent = useDefault.checked
        ? `当前全局配置：${limits.configured} 线程。取消勾选后可为本次下载临时修改。`
        : `最小 1 线程，最大 ${limits.max} 线程。`;
    };
    const cleanup = (value) => {
      modal.classList.remove("show");
      confirm.onclick = null;
      cancel.onclick = null;
      useDefault.onchange = null;
      modal.onclick = null;
      resolve(value);
    };
    cancel.onclick = () => cleanup(null);
    modal.onclick = (event) => {
      if (event.target === modal) cleanup(null);
    };
    confirm.onclick = async () => {
      let count = useDefault.checked ? limits.configured : Number(input.value);
      if (!Number.isFinite(count)) count = limits.configured;
      count = Math.floor(count);
      if (count < 1) count = 1;
      if (count > limits.max) {
        modal.classList.remove("show");
        const ok = await modalDialog({
          title: "线程数超过本机支持",
          message: `当前填写 ${count} 线程，本机最大支持 ${limits.max} 线程。继续下载将自动使用 ${limits.max} 线程。`,
          confirmText: `使用 ${limits.max} 线程下载`,
          cancelText: "取消下载",
          danger: true,
        });
        cleanup(ok ? limits.max : null);
        return;
      }
      cleanup(count);
    };
  });
}

function optionList(select, values, labeler = (x) => x) {
  select.innerHTML = values.map((v) => `<option value="${v.id || v}">${labeler(v)}</option>`).join("");
}

function activeTask() {
  return state.tasks.find((task) => task.id === state.activeTaskId) || state.tasks[0] || null;
}

function isUserEditing() {
  const el = document.activeElement;
  if (!el) return false;
  if (el.closest("#toast")) return false;
  return el.matches("input:not([type='checkbox']):not([type='file']), textarea, select");
}

function mergeByIdOrName(existing, incoming) {
  const keyOf = (item) => item.auth_index || item.name || item.id;
  const map = new Map(existing.map((item) => [keyOf(item), item]));
  incoming.forEach((item) => {
    const key = keyOf(item);
    const old = map.get(key) || {};
    const merged = { ...old, ...item };
    if (old.billing && !item.billing) merged.billing = old.billing;
    if (old.billing_error && !item.billing_error) merged.billing_error = old.billing_error;
    map.set(key, merged);
  });
  return [...map.values()];
}

function sceneDraftKey(taskId, sceneId) {
  return `${taskId}:${sceneId}`;
}

function sceneDraft(taskId, sceneId) {
  return state.sceneDrafts[sceneDraftKey(taskId, sceneId)] || {};
}

function rememberSceneDraft(taskId, sceneId, card) {
  state.sceneDrafts[sceneDraftKey(taskId, sceneId)] = collectSceneDraft(card);
}

function clearSceneDraft(taskId, sceneId) {
  delete state.sceneDrafts[sceneDraftKey(taskId, sceneId)];
}

function globalParamsForTask(task) {
  return {
    ...(task?.global_params || {}),
    ...(state.globalDrafts[task?.id] || {}),
  };
}

function rememberGlobalDraft() {
  const task = activeTask();
  if (!task) return;
  state.globalDrafts[task.id] = globalParams();
}

function defaultSceneSelected(scene) {
  return Boolean(scene.local_video || scene.status === "succeeded" || scene.download_status === "downloaded");
}

function parseLocalTime(value) {
  if (!value) return null;
  const date = new Date(String(value).replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? null : date;
}

function withinHours(value, hours = 24) {
  const date = parseLocalTime(value);
  if (!date) return false;
  const age = Date.now() - date.getTime();
  return age >= 0 && age <= hours * 60 * 60 * 1000;
}

function sceneRequestFresh(scene) {
  return Boolean(scene?.request_id) && withinHours(scene.request_id_created_at || scene.started_at || scene.updated_at, 24);
}

function sceneVideoFresh(scene) {
  const sourceTime = scene?.manual_url && !scene?.request_id
    ? (scene.manual_url_created_at || scene.updated_at)
    : (scene?.execution_started_at || scene?.request_id_created_at || scene?.started_at || scene?.video_url_created_at || scene?.finished_at);
  return Boolean(scene?.video_url) && withinHours(sourceTime, 24);
}

function sceneHasDownloadedVideo(scene) {
  return Boolean(scene?.local_video || scene?.download_status === "downloaded");
}

function parseImageUrls(value) {
  return String(value || "")
    .split(/[\r\n,，]+/)
    .map((url) => url.trim())
    .filter(Boolean)
    .filter((url, index, all) => all.indexOf(url) === index);
}

function appendImageUrls(currentValue, urls) {
  const existing = parseImageUrls(currentValue);
  urls.forEach((url) => {
    if (url && !existing.includes(url)) existing.push(url);
  });
  return existing.join("\n");
}

function imageReferenceToken(index) {
  return `<IMAGE_${index + 1}>`;
}

function labelForImageUrl(url, index) {
  try {
    const parsed = new URL(url);
    const name = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).pop() || parsed.hostname);
    return name.length > 28 ? `${name.slice(0, 25)}...` : name;
  } catch {
    return `参考图 ${index + 1}`;
  }
}

function renderReferenceImageTags(value, { showDelete = false, showThumbnails = false } = {}) {
  const urls = parseImageUrls(value);
  if (!urls.length) return "";
  return `
    <div class="reference-image-tags">
      ${urls.map((url, index) => `
        <div class="reference-image-item" data-url="${esc(url)}">
          <div class="reference-image-row">
            <button class="reference-token" type="button" data-token="${esc(imageReferenceToken(index))}" title="${esc(url)}">
              <strong>${esc(imageReferenceToken(index))}</strong>
              <span>${esc(labelForImageUrl(url, index))}</span>
            </button>
            ${showDelete ? `<button class="reference-delete" type="button" data-url="${esc(url)}" title="只从当前分镜移除，不删除图床文件">删除</button>` : ""}
          </div>
          ${showThumbnails ? `
            <div class="reference-thumbnail">
              <img src="${esc(url)}" alt="${esc(imageReferenceToken(index))} 预览" loading="lazy" referrerpolicy="no-referrer" />
            </div>
          ` : ""}
        </div>
      `).join("")}
    </div>
  `;
}

async function removeReferenceImageUrl(card, url) {
  const input = card.querySelector(".scene-image");
  const urls = parseImageUrls(input.value);
  const nextUrls = urls.filter((item) => item !== url);
  if (!url || !urls.length || nextUrls.length === urls.length) {
    input.value = "";
    updateReferenceImageTags(card);
    rememberSceneDraft(state.activeTaskId, Number(card.dataset.scene), card);
    await alertDialog("未检测到匹配内容", "未检测到匹配内容，已清空当前 URL 输入框。");
    return;
  }
  input.value = nextUrls.join("\n");
  updateReferenceImageTags(card);
  rememberSceneDraft(state.activeTaskId, Number(card.dataset.scene), card);
  toast("已从当前分镜移除图片链接，不会删除图床文件", 10000);
}

async function clearReferenceImageUrls(card) {
  const input = card.querySelector(".scene-image");
  if (!parseImageUrls(input.value).length) {
    input.value = "";
    updateReferenceImageTags(card);
    rememberSceneDraft(state.activeTaskId, Number(card.dataset.scene), card);
    await alertDialog("未检测到匹配内容", "未检测到匹配内容，已清空当前 URL 输入框。");
    return;
  }
  input.value = "";
  updateReferenceImageTags(card);
  rememberSceneDraft(state.activeTaskId, Number(card.dataset.scene), card);
  toast("已清空当前分镜图片链接，不会删除图床文件", 10000);
}

function updateReferenceImageTags(card) {
  const holder = card.querySelector(".reference-image-tags-wrap");
  const input = card.querySelector(".scene-image");
  if (holder && input) holder.innerHTML = renderReferenceImageTags(input.value, { showDelete: true, showThumbnails: true });
  wireReferenceTokenButtons(card);
  wireReferenceDeleteButtons(card);
}

function insertAtCursor(textarea, text) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? textarea.value.length;
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  const prefix = before && !/\s$/.test(before) ? " " : "";
  const suffix = after && !/^\s/.test(after) ? " " : "";
  textarea.value = `${before}${prefix}${text}${suffix}${after}`;
  const nextPosition = before.length + prefix.length + text.length + suffix.length;
  textarea.focus();
  textarea.setSelectionRange(nextPosition, nextPosition);
}

function wireReferenceTokenButtons(card) {
  card.querySelectorAll(".reference-token").forEach((btn) => {
    btn.onclick = () => {
      const promptInput = card.querySelector(".scene-prompt");
      insertAtCursor(promptInput, btn.dataset.token || "");
      rememberSceneDraft(state.activeTaskId, Number(card.dataset.scene), card);
    };
  });
}

function wireReferenceDeleteButtons(card) {
  card.querySelectorAll(".reference-delete").forEach((btn) => {
    btn.onclick = async () => removeReferenceImageUrl(card, btn.dataset.url || "");
  });
}

function imageLimitError(value, extraCount = 0) {
  return parseImageUrls(value).length + extraCount > MAX_REFERENCE_IMAGES;
}

async function ensureImageLimit(value, extraCount = 0) {
  if (!imageLimitError(value, extraCount)) return true;
  await alertDialog("图片数量超限", TOO_MANY_IMAGES_MESSAGE);
  return false;
}

function sceneSelectionMap(task) {
  if (!task) return {};
  const selected = state.sceneSelections[task.id] || {};
  const touched = state.sceneSelectionTouched[task.id] || {};
  const validIds = new Set(task.scenes.map((scene) => String(scene.id)));
  task.scenes.forEach((scene) => {
    const id = String(scene.id);
    if (!(id in selected) || !touched[id]) {
      selected[id] = defaultSceneSelected(scene);
    }
  });
  Object.keys(selected).forEach((id) => {
    if (!validIds.has(id)) delete selected[id];
  });
  Object.keys(touched).forEach((id) => {
    if (!validIds.has(id)) delete touched[id];
  });
  state.sceneSelections[task.id] = selected;
  state.sceneSelectionTouched[task.id] = touched;
  return selected;
}

function setSceneSelected(taskId, sceneId, checked) {
  if (!state.sceneSelections[taskId]) state.sceneSelections[taskId] = {};
  if (!state.sceneSelectionTouched[taskId]) state.sceneSelectionTouched[taskId] = {};
  state.sceneSelections[taskId][String(sceneId)] = checked;
  state.sceneSelectionTouched[taskId][String(sceneId)] = true;
}

function statusText(scene) {
  if (scene.manual_upload && scene.local_video) return "已上传";
  if (scene.download_status === "downloaded") return "已下载";
  if (scene.download_status === "pending") return "下载排队";
  if (scene.download_status === "downloading") return "下载中";
  if (scene.download_status === "failed") return "下载失败";
  const map = {
    draft: "草稿",
    queued: "排队",
    submitting: "提交中",
    polling: "生成中",
    succeeded: "成功",
    failed: "失败",
    downloading: "下载中",
    downloaded: "已下载",
  };
  return map[scene.status] || scene.status;
}

function statusKey(scene) {
  if (scene.manual_upload && scene.local_video) return "downloaded";
  if (scene.download_status === "downloaded") return "downloaded";
  if (scene.download_status === "pending") return "queued";
  if (scene.download_status === "downloading") return "downloading";
  if (scene.download_status === "failed") return "failed";
  return scene.status;
}

function renderTasks() {
  const box = $("taskList");
  box.innerHTML = state.tasks.map((task) => `
    <div class="task-item ${task.id === state.activeTaskId ? "active" : ""}" data-task="${task.id}">
      <div class="task-row">
        <strong>${esc(task.name)}</strong>
        <button class="task-delete" data-task="${esc(task.id)}" title="删除任务">×</button>
      </div>
      <p>${task.scenes.length} 个分镜 · ${esc(task.updated_at || "")}</p>
    </div>
  `).join("");
  box.querySelectorAll(".task-item").forEach((el) => {
    el.onclick = () => { state.activeTaskId = el.dataset.task; renderAll(); };
  });
  box.querySelectorAll(".task-delete").forEach((btn) => {
    btn.onclick = async (event) => {
      event.stopPropagation();
      const ok = await modalDialog({
        title: "删除这个任务？",
        message: "会删除任务记录以及该任务目录下的本地视频和合并视频；分镜稿也会随任务一起删除。",
        confirmText: "删除任务",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
      await api(`/api/tasks/${btn.dataset.task}`, { method: "DELETE" });
      if (state.activeTaskId === btn.dataset.task) state.activeTaskId = null;
      await refreshTasks({ force: true });
      toast("任务已删除", 10000);
    };
  });
}

function renderGlobal(task) {
  if (!task) return;
  const g = globalParamsForTask(task);
  setSelectValue("globalModel", g.model || "grok-imagine-video", "grok-imagine-video");
  setSelectValue("globalDuration", g.duration || 5, 5);
  setSelectValue("globalResolution", g.resolution || "720p", "720p");
  setSelectValue("globalAspect", g.aspect_ratio || "16:9", "16:9");
  setSelectValue("submitInterval", g.submit_interval_seconds || 5, 5);
}

function setSelectValue(id, value, fallback) {
  const select = $(id);
  const target = String(value ?? "");
  const hasValue = [...select.options].some((option) => option.value === target);
  select.value = hasValue ? target : String(fallback);
}

function positiveNumberFrom(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function sceneParams(task, scene, draft = {}) {
  const g = globalParamsForTask(task);
  return {
    model: draft.model ?? scene.params?.model ?? g.model ?? "grok-imagine-video",
    duration: draft.duration ?? scene.params?.duration ?? g.duration ?? 5,
    resolution: draft.resolution ?? scene.params?.resolution ?? g.resolution ?? "720p",
    aspect_ratio: draft.aspect_ratio ?? scene.params?.aspect_ratio ?? g.aspect_ratio ?? "16:9",
  };
}

function clampPercent(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n)));
}

function productionProgressState(scene) {
  if (scene.manual_upload && scene.local_video) {
    return {
      title: "服务器制作",
      label: "用户已上传本地视频",
      detail: "manual_upload=true · progress=100%",
      progress: 100,
      active: false,
      failed: false,
      done: true,
    };
  }
  const progress = scene.status === "succeeded" || scene.local_video ? 100 : clampPercent(scene.progress);
  const rawStatus = scene.raw_result?.status || (scene.status === "polling" ? "pending" : scene.status || "draft");
  const active = ["queued", "submitting", "polling"].includes(scene.status);
  const failed = scene.status === "failed";
  const labels = {
    draft: "制作待开始",
    queued: "制作排队中",
    submitting: "正在提交制作请求",
    polling: "服务器制作中",
    succeeded: "制作完成",
    failed: "制作失败",
  };
  const reason = failed && scene.error ? ` · ${scene.error.zh || scene.error.label || "失败"}` : "";
  return {
    title: "服务器制作",
    label: labels[scene.status] || statusText(scene),
    detail: `status=${rawStatus} · progress=${progress}%${reason}`,
    progress,
    active,
    failed,
    done: progress >= 100 && !failed,
  };
}

function downloadProgressState(scene) {
  const status = scene.manual_upload ? "uploaded" : (scene.download_status || "not_downloaded");
  const progress = status === "downloaded" || status === "uploaded" ? 100 : clampPercent(scene.download_progress);
  const active = ["pending", "downloading"].includes(status);
  const failed = status === "failed";
  const labels = {
    not_downloaded: scene.video_url ? "等待下载" : "等待视频 URL",
    pending: "下载排队中",
    downloading: "正在下载到本地",
    downloaded: "下载完成",
    uploaded: "用户已上传本地视频",
    failed: "下载失败",
  };
  const total = Number(scene.download_bytes_total || 0);
  const done = Number(scene.download_bytes_done || 0);
  const bytes = total > 0 ? ` · ${formatBytes(done)} / ${formatBytes(total)}` : "";
  const attempt = scene.download_attempt ? ` · 第 ${scene.download_attempt}/3 次` : "";
  const threads = scene.download_thread_count ? ` · ${scene.download_thread_count} 线程` : "";
  const reason = failed || scene.download_error ? ` · ${scene.download_error || ""}` : "";
  return {
    title: "视频下载",
    label: labels[status] || status,
    detail: `status=${status} · progress=${progress}%${attempt}${threads}${bytes}${reason}`,
    progress,
    active,
    failed,
    done: progress >= 100 && !failed,
  };
}

function renderProgressLane(kind, state) {
  const cls = [kind, state.active ? "active" : "", state.failed ? "failed" : "", state.done ? "done" : ""].filter(Boolean).join(" ");
  return `
    <div class="progress-lane ${cls}">
      <div class="progress-line">
        <span>${esc(state.title)} · ${esc(state.label)}</span>
        <strong>${state.progress}%</strong>
      </div>
      <div class="meter"><div style="width:${state.progress}%"><i></i></div></div>
      <div class="progress-detail">${esc(state.detail)}</div>
    </div>
  `;
}

function renderScenes() {
  const task = activeTask();
  $("activeTaskTitle").textContent = task ? task.name : "请选择或创建任务";
  $("activeTaskMeta").textContent = task ? `${task.id} · ${task.scenes.length} 个分镜` : "分镜按 ID 从上到下执行";
  const list = $("sceneList");
  if (!task || (task.scenes.length === 0 && !(task.merges || []).length)) {
    list.className = "scene-list empty";
    list.innerHTML = "暂无分镜";
    renderSummary();
    return;
  }
  list.className = "scene-list";
  const selectedMap = sceneSelectionMap(task);
  const sceneCards = task.scenes.map((scene) => {
    const draft = sceneDraft(task.id, scene.id);
    const p = sceneParams(task, scene, draft);
    const prompt = draft.prompt ?? scene.prompt ?? "";
    const imageUrl = draft.image_url ?? scene.image_url ?? "";
    const notes = [];
    if (scene.error) {
      notes.push(`${esc(scene.error.label || "失败")}：${esc(scene.error.zh || "")}<br><small>${esc(scene.error.raw ? String(scene.error.raw).slice(0, 360) : "")}</small>`);
    }
    if (scene.download_error) {
      notes.push(`视频下载问题：${esc(scene.download_error)}`);
    }
    const err = notes.join("<br>");
    return `
      <article class="scene-card" data-scene="${scene.id}">
        <div class="scene-top">
          <div class="scene-titleline">
            <button class="drag-handle" type="button" draggable="true" title="拖拽改变分镜顺序">☰</button>
            <input class="scene-id-input" value="${scene.id}" inputmode="numeric" title="修改分镜 ID" />
            <span class="status ${statusKey(scene)}">${statusText(scene)}</span>
          </div>
          <div class="scene-actions">
            <label class="checkline"><input class="scene-check" type="checkbox" data-scene="${scene.id}" ${selectedMap[String(scene.id)] ? "checked" : ""}/> 勾选</label>
            <button class="btn ghost save-scene">保存</button>
            <button class="btn secondary run-scene">${scene.request_id ? "重新执行" : "执行"}</button>
            <button class="btn ghost refresh-result">拉取结果</button>
            <button class="btn ghost download-scene">下载</button>
            <button class="btn ghost upload-scene">上传视频</button>
            <input class="upload-video-input" type="file" accept=".mp4,.mov,.webm,.mkv,video/mp4,video/quicktime,video/webm,video/x-matroska" hidden />
            <button class="btn danger delete-scene">删除视频</button>
            <button class="btn danger remove-scene">删除分镜</button>
          </div>
        </div>
        <div class="progress-stack">
          ${renderProgressLane("produce", productionProgressState(scene))}
          ${renderProgressLane("download", downloadProgressState(scene))}
        </div>
        <div class="scene-fields">
          <div>
            <div class="field-label-row">
              <label>提示词</label>
              <button class="btn ghost expand-prompt" type="button">展开编辑</button>
            </div>
            <textarea class="scene-prompt">${esc(prompt)}</textarea>
            <label>图片 URL / 参考图 URL</label>
            <textarea class="scene-image image-url-list input" placeholder="留空则文生视频；1 张图走图生视频；多张图每行 1 个 URL，最多 7 张，作为 reference_images 提交">${esc(imageUrl)}</textarea>
            <div class="image-host-row">
              <button class="btn ghost upload-image-host" type="button">上传图片到图床</button>
              <button class="btn ghost clear-image-urls" type="button">清空链接</button>
              <input class="upload-image-input" type="file" accept=".png,.jpg,.jpeg,.webp,.gif,image/png,image/jpeg,image/webp,image/gif" multiple hidden />
              <span class="hint image-host-hint">${esc((state.bootstrap.settings.image_host_selected_url || state.bootstrap.settings.image_host_base_url || "https://img.remit.ee"))}</span>
            </div>
            <div class="reference-image-tags-wrap">${renderReferenceImageTags(imageUrl, { showDelete: true, showThumbnails: true })}</div>
          </div>
          <div>
            <label>模型</label>
            <select class="scene-model input">${state.bootstrap.models.map((m) => `<option value="${m.id}" ${p.model === m.id ? "selected" : ""}>${m.label}</option>`).join("")}</select>
            <div class="params-row">
              <div><label>时长</label><select class="scene-duration input">${state.bootstrap.durations.map((v) => `<option value="${v}" ${Number(p.duration) === v ? "selected" : ""}>${v}s</option>`).join("")}</select></div>
              <div><label>分辨率</label><select class="scene-resolution input">${state.bootstrap.image_resolutions.map((v) => `<option value="${v}" ${p.resolution === v ? "selected" : ""}>${v}</option>`).join("")}</select></div>
              <div><label>画幅</label><select class="scene-aspect input">${state.bootstrap.aspect_ratios.map((v) => `<option value="${v}" ${p.aspect_ratio === v ? "selected" : ""}>${v}</option>`).join("")}</select></div>
            </div>
            ${scene.video_url ? `
              <div class="remote-url-panel">
                <label>生成视频 URL</label>
                <div class="remote-url-row">
                  <input class="remote-url-value input" value="${esc(scene.video_url)}" readonly />
                  <button class="btn ghost copy-remote-url" data-url="${esc(scene.video_url)}">复制</button>
                </div>
              </div>
            ` : ""}
          </div>
        </div>
        <div class="error-box ${err ? "show" : ""}">${err}</div>
        <div class="links">
          ${scene.request_id ? `<span class="hint request-id">任务 ID：<code>${esc(scene.request_id)}</code></span><button class="btn ghost copy-request-id" data-id="${esc(scene.request_id)}">复制ID</button>` : ""}
          ${scene.video_url ? `<a href="${esc(scene.video_url)}" target="_blank">远程视频 URL</a>` : ""}
          ${scene.local_video_url ? `<a href="${esc(scene.local_video_url)}" target="_blank">本地视频</a>` : ""}
          ${scene.manual_upload ? `<span class="hint">手动上传：${esc(scene.upload_original_name || "本地视频")}</span>` : ""}
          ${scene.effective_params?.model_note ? `<span class="hint">${esc(scene.effective_params.model_note)}</span>` : ""}
        </div>
      </article>
    `;
  }).join("");
  list.innerHTML = sceneCards + renderMergeCards(task);
  wireSceneButtons();
  wireMergeButtons();
  renderSummary();
}

function mergeDownloadUrl(merge) {
  if (merge.download_url) return merge.download_url;
  return merge.url ? `${merge.url}${merge.url.includes("?") ? "&" : "?"}download=1` : "";
}

function mergeParamsText(merge) {
  const p = merge.params || {};
  const normalize = p.normalize ? "统一格式：是" : "统一格式：否";
  const resolution = p.resolution ? `分辨率：${p.resolution}` : "";
  const aspect = p.aspect_ratio ? `画幅：${p.aspect_ratio}` : "";
  const output = p.output_name ? `输出名：${p.output_name}.mp4` : "";
  return [normalize, resolution, aspect, output].filter(Boolean).join(" · ");
}

function renderMergeCards(task) {
  const merges = task?.merges || [];
  if (!merges.length) return "";
  return `
    <section class="merge-section">
      <div class="merge-section-title">合并视频</div>
      ${merges.map((merge, index) => {
        const order = merge.order_note || `按分镜顺序合并：${(merge.scene_ids || []).join(" -> ")}`;
        const params = mergeParamsText(merge);
        const downloadUrl = mergeDownloadUrl(merge);
        return `
          <article class="merge-card" data-merge="${esc(merge.id || index)}">
            <div class="merge-head">
              <div>
                <div class="scene-titleline"><span class="scene-id">合</span><span class="status downloaded">已合并</span></div>
                <h3>${esc(merge.filename || merge.file || "final.mp4")}</h3>
                <p>${esc(order)}</p>
              </div>
              <div class="merge-actions">
                ${merge.url ? `<a class="btn ghost" href="${esc(merge.url)}" target="_blank">预览</a>` : ""}
                ${downloadUrl ? `<a class="btn primary" href="${esc(downloadUrl)}" download>下载视频</a>` : ""}
                <button class="btn danger delete-merge" data-merge="${esc(merge.id || "")}">删除</button>
              </div>
            </div>
            <div class="merge-params">${esc(params || "使用默认合并参数")}</div>
            <div class="merge-meta">
              <span>创建时间：${esc(merge.created_at || "--")}</span>
              <span>文件大小：${formatBytes(merge.size || 0)}</span>
            </div>
          </article>
        `;
      }).join("")}
    </section>
  `;
}

function wireMergeButtons() {
  document.querySelectorAll(".delete-merge").forEach((btn) => {
    btn.onclick = async () => {
      const task = activeTask();
      const mergeId = btn.dataset.merge;
      if (!task || !mergeId) return toast("合并记录缺少 ID，无法删除", 12000);
      const ok = await modalDialog({
        title: "删除这个合并视频？",
        message: "只删除这一个合并好的本地 mp4 文件和合并记录，不会删除分镜稿。",
        confirmText: "删除合并视频",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
      await api(`/api/tasks/${task.id}/merges/${encodeURIComponent(mergeId)}`, { method: "DELETE" });
      toast("合并视频已删除", 10000);
      await refreshTasks({ force: true });
    };
  });
}

function collectSceneDraft(card) {
  const duration = Number(card.querySelector(".scene-duration").value);
  return {
    prompt: card.querySelector(".scene-prompt").value,
    image_url: card.querySelector(".scene-image").value,
    model: card.querySelector(".scene-model").value,
    duration: Number.isFinite(duration) && duration > 0 ? duration : 5,
    resolution: card.querySelector(".scene-resolution").value,
    aspect_ratio: card.querySelector(".scene-aspect").value,
  };
}

function scenePayload(card) {
  const draft = collectSceneDraft(card);
  return {
    prompt: draft.prompt.trim(),
    image_url: parseImageUrls(draft.image_url).join("\n"),
    model: draft.model,
    duration: draft.duration,
    resolution: draft.resolution,
    aspect_ratio: draft.aspect_ratio,
  };
}

function activeSceneCards() {
  return [...document.querySelectorAll(".scene-card")];
}

async function saveSceneCard(card, { clearDraft = true } = {}) {
  const sceneId = Number(card.dataset.scene);
  if (!(await ensureImageLimit(card.querySelector(".scene-image").value))) {
    return null;
  }
  const payload = scenePayload(card);
  await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  if (clearDraft) clearSceneDraft(state.activeTaskId, sceneId);
  return { sceneId, payload };
}

async function saveVisibleSceneDrafts() {
  const saved = [];
  for (const card of activeSceneCards()) {
    const result = await saveSceneCard(card);
    if (!result) return null;
    saved.push(result);
  }
  return saved;
}

function selectedSceneIds() {
  const task = activeTask();
  if (!task) return [];
  const selected = sceneSelectionMap(task);
  return task.scenes.filter((scene) => selected[String(scene.id)]).map((scene) => Number(scene.id));
}

async function persistSceneOrder(sceneIds) {
  const task = activeTask();
  if (!task) return;
  await api(`/api/tasks/${task.id}/scenes/reorder`, {
    method: "POST",
    body: JSON.stringify({ scene_ids: sceneIds.map(Number) }),
  });
  await refreshTasks({ force: true });
}

async function refreshSceneVideoUrlIfPossible(sceneId) {
  let task = activeTask();
  let scene = task?.scenes?.find((item) => Number(item.id) === Number(sceneId));
  if (scene?.video_url || !scene?.request_id || !sceneRequestFresh(scene)) return scene;
  try {
    await api(`/api/tasks/${task.id}/scenes/${sceneId}/refresh-result`, { method: "POST" });
    await refreshTasks({ force: true });
    task = activeTask();
    scene = task?.scenes?.find((item) => Number(item.id) === Number(sceneId));
    if (scene?.video_url) toast(`分镜 ${sceneId} 已自动拉取到视频 URL`, 10000);
    return scene;
  } catch (err) {
    toast(`自动拉取分镜 ${sceneId} 视频 URL 失败：${err.message}`, 15000);
    return scene;
  }
}

async function refreshMissingVideoUrls(sceneIds) {
  for (const sceneId of sceneIds) {
    await refreshSceneVideoUrlIfPossible(sceneId);
  }
  return activeTask();
}

function wireSceneButtons() {
  document.querySelectorAll(".scene-card").forEach((card) => {
    const sceneId = Number(card.dataset.scene);
    const dragHandle = card.querySelector(".drag-handle");
    dragHandle.ondragstart = (event) => {
      event.dataTransfer.setData("text/plain", String(sceneId));
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    };
    dragHandle.ondragend = () => card.classList.remove("dragging");
    card.ondragover = (event) => event.preventDefault();
    card.ondrop = async (event) => {
      event.preventDefault();
      const draggedId = Number(event.dataTransfer.getData("text/plain"));
      const targetId = sceneId;
      if (!draggedId || draggedId === targetId) return;
      const task = activeTask();
      const ids = task.scenes.map((scene) => Number(scene.id));
      const next = ids.filter((id) => id !== draggedId);
      const targetIndex = next.indexOf(targetId);
      next.splice(targetIndex < 0 ? next.length : targetIndex, 0, draggedId);
      await persistSceneOrder(next);
      toast("分镜顺序已更新", 10000);
    };
    const idInput = card.querySelector(".scene-id-input");
    idInput.onchange = async () => {
      const nextId = Number(idInput.value);
      if (!Number.isInteger(nextId) || nextId <= 0) {
        idInput.value = sceneId;
        return alertDialog("分镜 ID 无效", "分镜 ID 必须是正整数。");
      }
      if (nextId === sceneId) return;
      try {
        await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/id`, {
          method: "PATCH",
          body: JSON.stringify({ id: nextId }),
        });
        toast("分镜 ID 已更新", 10000);
        await refreshTasks({ force: true });
      } catch (err) {
        idInput.value = sceneId;
        await alertDialog("分镜 ID 修改失败", err.message);
      }
    };
    const check = card.querySelector(".scene-check");
    if (check) {
      check.onchange = () => setSceneSelected(state.activeTaskId, sceneId, check.checked);
    }
    card.querySelector(".expand-prompt").onclick = async () => {
      const promptInput = card.querySelector(".scene-prompt");
      const imageInput = card.querySelector(".scene-image");
      const nextPrompt = await promptEditorDialog({
        title: `分镜 ${sceneId} 提示词`,
        value: promptInput.value,
        imageUrls: imageInput.value,
      });
      if (nextPrompt === null) return;
      promptInput.value = nextPrompt;
      rememberSceneDraft(state.activeTaskId, sceneId, card);
      toast("提示词已更新到当前分镜", 8000);
    };
    card.querySelectorAll(".scene-prompt, .scene-image, .scene-model, .scene-duration, .scene-resolution, .scene-aspect").forEach((input) => {
      const remember = () => {
        if (input.classList.contains("scene-image")) updateReferenceImageTags(card);
        rememberSceneDraft(state.activeTaskId, sceneId, card);
      };
      input.addEventListener("input", remember);
      input.addEventListener("change", remember);
    });
    wireReferenceTokenButtons(card);
    wireReferenceDeleteButtons(card);
    card.querySelectorAll(".copy-remote-url").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(btn.dataset.url || "");
          toast("视频 URL 已复制", 10000);
        } catch {
          toast("复制失败，请选中 URL 手动复制", 12000);
        }
      };
    });
    card.querySelectorAll(".copy-request-id").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(btn.dataset.id || "");
          toast("任务 ID 已复制", 10000);
        } catch {
          toast("复制失败，请手动选中任务 ID", 12000);
        }
      };
    });
    card.querySelector(".save-scene").onclick = async () => {
      const saved = await saveSceneCard(card);
      if (!saved) return;
      toast("分镜已保存", 10000);
      await refreshTasks({ force: true });
    };
    card.querySelector(".run-scene").onclick = async () => {
      const currentTask = activeTask();
      const currentScene = currentTask?.scenes?.find((scene) => Number(scene.id) === sceneId);
      if (sceneHasDownloadedVideo(currentScene) || sceneRequestFresh(currentScene)) {
        const ok = await modalDialog({
          title: "确定要重新执行吗？",
          message: "重新执行会提交一个全新的视频制作请求，可能消耗额度。若只是想查询旧任务结果，请点“拉取结果”。",
          confirmText: "确定执行",
          cancelText: "取消",
          danger: true,
        });
        if (!ok) return;
      }
      const saved = await saveSceneCard(card);
      if (!saved) return;
      await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/run`, { method: "POST" });
      toast("已保存并加入后台执行", 10000);
      await refreshTasks({ force: true });
    };
    card.querySelector(".refresh-result").onclick = async () => {
      const currentTask = activeTask();
      const currentScene = currentTask?.scenes?.find((scene) => Number(scene.id) === sceneId);
      if (!currentScene?.request_id) {
        return alertDialog("不能拉取结果", "这个分镜没有旧任务 ID。请先点击“执行”提交制作。");
      }
      if (!sceneRequestFresh(currentScene)) {
        return alertDialog("旧任务 ID 已过期", "旧任务 ID 已超过 24 小时。请先点击“执行”重新提交制作。");
      }
      try {
        await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/refresh-result`, { method: "POST" });
        toast("已拉取一次上游结果", 10000);
        await refreshTasks({ force: true });
      } catch (err) {
        toast(`拉取结果失败：${err.message}`, 15000);
        await refreshTasks({ force: true });
      }
    };
    card.querySelector(".download-scene").onclick = async () => {
      const currentTask = activeTask();
      let currentScene = currentTask?.scenes?.find((scene) => Number(scene.id) === sceneId);
      if (!currentScene?.video_url) {
        currentScene = await refreshSceneVideoUrlIfPossible(sceneId);
      }
      if (!currentScene?.video_url) {
        return alertDialog("不能下载", "这个分镜还没有视频 URL。请先拉取结果，或手动填写/上传视频。");
      }
      if (!sceneVideoFresh(currentScene)) {
        return alertDialog("执行时间已过期", "本次执行时间已超过 24 小时。请重新执行，拿到新任务 ID 后再下载。");
      }
      const threadCount = await chooseDownloadThreads();
      if (!threadCount) return;
      await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/download`, {
        method: "POST",
        body: JSON.stringify({ thread_count: threadCount }),
      });
      toast(`已加入下载队列：${threadCount} 线程`, 10000);
    };
    const uploadInput = card.querySelector(".upload-video-input");
    card.querySelector(".upload-scene").onclick = () => uploadInput.click();
    uploadInput.onchange = async () => {
      const file = uploadInput.files?.[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      try {
        toast("正在上传并校验视频...", 12000);
        await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/upload-video`, { method: "POST", body: form });
        toast("本地视频已挂到该分镜，可参与合并", 12000);
        await refreshTasks({ force: true });
      } catch (err) {
        toast(`上传失败：${err.message}`, 15000);
      } finally {
        uploadInput.value = "";
      }
    };
    const imageInput = card.querySelector(".upload-image-input");
    card.querySelector(".clear-image-urls").onclick = async () => clearReferenceImageUrls(card);
    card.querySelector(".upload-image-host").onclick = async () => {
      if (!(await ensureImageLimit(card.querySelector(".scene-image").value))) return;
      imageInput.click();
    };
    imageInput.onchange = async () => {
      const files = [...(imageInput.files || [])];
      if (!files.length) return;
      const imageUrlInput = card.querySelector(".scene-image");
      if (!(await ensureImageLimit(imageUrlInput.value, files.length))) {
        imageInput.value = "";
        return;
      }
      try {
        toast(`正在上传 ${files.length} 张图片到图床，网络中断会自动重试 3 次...`, 12000);
        const uploadedUrls = [];
        for (const file of files) {
          const form = new FormData();
          form.append("file", file);
          form.append("image_host_url", state.bootstrap.settings.image_host_selected_url || state.bootstrap.settings.image_host_base_url || "https://img.remit.ee");
          const result = await api("/api/image-host/upload", { method: "POST", body: form });
          uploadedUrls.push(result.public_url);
          imageUrlInput.value = appendImageUrls(imageUrlInput.value, [result.public_url]);
          updateReferenceImageTags(card);
          rememberSceneDraft(state.activeTaskId, sceneId, card);
        }
        toast(`已上传 ${uploadedUrls.length} 张图片并回填 URL`, 15000);
      } catch (err) {
        await alertDialog("图床上传失败", `${err.message}\n\n请重新选择图片，再次点击上传图片到图床。已成功回填的 URL 会保留在输入框中。`);
      } finally {
        imageInput.value = "";
      }
    };
    card.querySelector(".delete-scene").onclick = async () => {
      const ok = await modalDialog({
        title: "删除这个分镜的视频？",
        message: "只会删除该分镜的视频结果和本地视频文件，提示词、图片 URL 和参数会保留。",
        confirmText: "删除视频",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
      await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}`, { method: "DELETE" });
      toast("分镜视频结果已删除，稿子已保留", 10000);
      await refreshTasks({ force: true });
    };
    card.querySelector(".remove-scene").onclick = async () => {
      const ok = await modalDialog({
        title: "删除整个分镜？",
        message: `只会删除分镜 ${sceneId}。该分镜的提示词、图片 URL、任务 ID、视频 URL、下载状态和已下载本地视频文件都会被删除，其它分镜不受影响。`,
        confirmText: "删除分镜",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
      await api(`/api/tasks/${state.activeTaskId}/scenes/${sceneId}/remove`, { method: "DELETE" });
      clearSceneDraft(state.activeTaskId, sceneId);
      setSceneSelected(state.activeTaskId, sceneId, false);
      toast(`分镜 ${sceneId} 已删除`, 10000);
      await refreshTasks({ force: true });
    };
  });
}

function renderSummary() {
  const task = activeTask();
  if (!task) {
    $("resultSummary").innerHTML = "暂无任务";
    $("mergeResults").innerHTML = "";
    return;
  }
  const counts = task.scenes.reduce((acc, s) => {
    acc[s.status] = (acc[s.status] || 0) + 1;
    if (s.local_video) acc.local = (acc.local || 0) + 1;
    if (s.download_status === "failed") acc.download_failed = (acc.download_failed || 0) + 1;
    return acc;
  }, {});
  $("resultSummary").innerHTML = `
    <span class="status succeeded">成功 ${counts.succeeded || 0}</span>
    <span class="status downloaded">可合并 ${counts.local || 0}</span>
    <span class="status failed">失败 ${counts.failed || 0}</span>
    <span class="status failed">下载失败 ${counts.download_failed || 0}</span>
    <span class="status polling">进行中 ${(counts.polling || 0) + (counts.submitting || 0) + (counts.queued || 0)}</span>
  `;
  $("mergeResults").innerHTML = (task.merges || []).map((m) => {
    const downloadUrl = mergeDownloadUrl(m);
    return `
      <div class="history-merge">
        <strong>${esc(m.filename || m.file || "final.mp4")}</strong>
        <span>${esc(m.order_note || `按分镜顺序合并：${(m.scene_ids || []).join(" -> ")}`)}</span>
        <small>${esc(mergeParamsText(m) || "使用默认合并参数")}</small>
        ${downloadUrl ? `<a href="${esc(downloadUrl)}" download>下载</a>` : ""}
      </div>
    `;
  }).join("") || "<p>暂无合并结果</p>";
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!n) return "--";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(2)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function formatDate(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 19).replace("T", " ");
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatShortDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(5, 16).replace("T", " ");
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function moneyFromCents(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return `US$${(n / 100).toFixed(2)}`;
}

function accountTitle(account) {
  return account.label || account.email || account.account || account.name || account.id || "xAI 账号";
}

function quotaInfo(account) {
  const billing = account.billing || {};
  const totalCents = Number(billing.monthly_limit_cents);
  const remainingCents = Number(billing.remaining_cents);
  const usedCents = Number(billing.used_cents);
  const hasBilling = Number.isFinite(totalCents) && Number.isFinite(remainingCents) && totalCents > 0;
  if (hasBilling) {
    const percent = Number.isFinite(Number(billing.remaining_percent)) ? Number(billing.remaining_percent) : Math.max(0, Math.min(100, Math.round((remainingCents / totalCents) * 100)));
    return {
      plan: billing.plan || "xAI 额度套餐",
      used: usedCents,
      total: totalCents,
      remaining: remainingCents,
      reset: billing.billing_period_end || "",
      percent,
      hasQuota: true,
      source: "billing",
      onDemand: billing.on_demand_enabled ? `已启用 ${moneyFromCents(billing.on_demand_cap_cents)}` : "未启用",
    };
  }
  const candidates = [
    account.quota,
    account.usage,
    account.billing,
    account.subscription,
    account.plan,
    account.limits,
  ].filter(Boolean);
  const merged = candidates.find((item) => typeof item === "object") || {};
  const plan = account.package || account.tier || account.plan_name || merged.plan || merged.package || merged.tier || "";
  const used = Number(account.used || account.monthly_used || merged.used || merged.monthly_used || merged.usage_usd || 0);
  const total = Number(account.total || account.monthly_total || merged.total || merged.monthly_total || merged.limit_usd || 0);
  const reset = account.reset_at || account.quota_reset_at || merged.reset_at || merged.quota_reset_at || "";
  const percent = total > 0 ? Math.max(0, Math.min(100, Math.round((used / total) * 100))) : null;
  return { plan, used, total, reset, percent, hasQuota: total > 0, source: "auth-files", onDemand: "" };
}

function healthBars(account) {
  const buckets = Array.isArray(account.recent_requests) ? account.recent_requests : [];
  if (!buckets.length) return `<div class="health-empty">--</div>`;
  return buckets.map((bucket) => {
    const cls = bucket.failed > 0 ? "bad" : bucket.success > 0 ? "good" : "idle";
    const title = `${bucket.time || ""} 成功 ${bucket.success || 0} / 失败 ${bucket.failed || 0}`;
    return `<span class="health-dot ${cls}" title="${esc(title)}"></span>`;
  }).join("");
}

function safeAccountJson(account) {
  const allowed = [
    "name", "provider", "label", "email", "account", "account_type", "auth_index",
    "status", "status_message", "disabled", "unavailable", "runtime_only", "source",
    "size", "modtime", "created_at", "updated_at", "last_refresh", "success", "failed",
    "websockets", "type", "billing", "billing_error",
  ];
  const safe = {};
  allowed.forEach((key) => {
    if (account[key] !== undefined) safe[key] = account[key];
  });
  return JSON.stringify(safe, null, 2);
}

function renderCpa() {
  const box = $("cpaAccounts");
  if (!state.cpaFiles.length) {
    box.innerHTML = "<p>暂无 xAI 账号或管理接口不可用</p>";
    return;
  }
  box.innerHTML = state.cpaFiles.map((a) => `
    ${(() => {
      const title = accountTitle(a);
      const q = quotaInfo(a);
      const key = a.auth_index || a.name || a.id;
      const detailsOpen = !!state.cpaDetailsOpen[key];
      const modelsOpen = !!state.cpaModelsOpen[key];
      return `
    <div class="account-card" data-auth="${esc(a.auth_index || "")}" data-name="${esc(a.name || "")}">
      <div class="account-main">
        <div class="account-check"></div>
        <div class="account-logo">x</div>
        <div class="account-info">
          <div class="account-title-row">
            <span class="mini-pill">xAI</span>
            <span class="mini-pill ${a.disabled ? "bad" : "good"}">${a.disabled ? "禁用" : "启用"}</span>
          </div>
          <strong title="${esc(title)}">${esc(title)}</strong>
          <p>大小：${formatBytes(a.size)}　修改时间：${formatDate(a.modtime || a.updated_at || a.last_refresh)}</p>
        </div>
      </div>
      <div class="account-pills">
        <span class="metric-pill good">成功 ${a.success || 0}</span>
        <span class="metric-pill bad">失败 ${a.failed || 0}</span>
        <span class="metric-pill">${esc(a.status || "unknown")}</span>
      </div>
      <div class="account-section-label">健康状态</div>
      <div class="health-row">${healthBars(a)}</div>
      <div class="account-meta">
        <span>套餐</span><strong>${esc(q.plan || (a.billing_error ? "查询失败" : "点击刷新查看额度"))}</strong>
        <span>按量付费</span><strong>${esc(q.onDemand || (a.billing_error ? "查询失败" : "刷新后显示"))}</strong>
      </div>
      <div class="quota-row">
        <strong>月度积分</strong>
        <span>${q.hasQuota ? `${q.percent}%　${moneyFromCents(q.remaining)} / ${moneyFromCents(q.total)}${q.reset ? `　${esc(formatShortDate(q.reset))}` : ""}` : esc(a.billing_error?.zh || "点击刷新查看额度")}</span>
      </div>
      <div class="quota-track"><div style="width:${q.percent ?? 0}%"></div></div>
      <div class="quota-actions">
        <button class="btn ghost refresh-quota" data-auth="${esc(a.auth_index || "")}">刷新查看额度</button>
      </div>
      ${modelsOpen ? `<div class="account-extra"><strong>当前工作台可用 Grok 视频模型</strong><p>${state.bootstrap.models.map((m) => esc(m.id)).join(" / ")}</p></div>` : ""}
      ${detailsOpen ? `<pre class="account-json">${esc(safeAccountJson(a))}</pre>` : ""}
      <div class="account-actions">
        <button class="btn ghost show-models" data-key="${esc(key)}">模型</button>
        <button class="btn ghost refresh-quota" data-auth="${esc(a.auth_index || "")}">刷新查看</button>
        <button class="btn ghost backup-auth" data-name="${esc(a.name || "")}">备份</button>
        <button class="btn ghost show-details" data-key="${esc(key)}">配置</button>
        <button class="btn danger delete-auth" data-name="${esc(a.name || "")}">删除</button>
        <label class="account-switch">
          <span>${a.disabled ? "禁用" : "启用"}</span>
          <input class="toggle-auth" type="checkbox" data-name="${esc(a.name || "")}" ${a.disabled ? "" : "checked"} />
          <i></i>
        </label>
      </div>
    </div>
      `;
    })()}
  `).join("");
  box.querySelectorAll(".show-models").forEach((btn) => btn.onclick = () => {
    state.cpaModelsOpen[btn.dataset.key] = !state.cpaModelsOpen[btn.dataset.key];
    renderCpa();
  });
  box.querySelectorAll(".show-details").forEach((btn) => btn.onclick = () => {
    state.cpaDetailsOpen[btn.dataset.key] = !state.cpaDetailsOpen[btn.dataset.key];
    renderCpa();
  });
  box.querySelectorAll(".refresh-quota").forEach((btn) => btn.onclick = async () => {
    if (!btn.dataset.auth) return toast("缺少 auth_index，无法刷新查看", 12000);
    try {
      const data = await api(`/api/cpa/xai-auth-files/${encodeURIComponent(btn.dataset.auth)}/refresh-view`);
      state.cpaFiles = mergeByIdOrName(state.cpaFiles, [data.file]);
      state.lastCpaRefresh = data.refreshed_at || "";
      renderCpa();
      if (data.status === "partial") {
        toast(`账号状态已刷新，但月度积分查询失败：${data.billing_error?.zh || data.billing_error?.raw || "未知错误"}`, 15000);
      } else {
        toast("已刷新该 xAI 账号月度积分", 12000);
      }
    } catch (err) {
      toast(`刷新查看失败：${err.message}`, 15000);
    }
  });
  box.querySelectorAll(".toggle-auth").forEach((input) => input.onchange = async () => {
    const disabled = !input.checked;
    if (!confirm(`${disabled ? "禁用" : "启用"}这个 xAI/Grok 账号？`)) {
      input.checked = !disabled;
      return;
    }
    await api(`/api/cpa/xai-auth-files/${encodeURIComponent(input.dataset.name)}/disabled`, {
      method: "PATCH",
      body: JSON.stringify({ disabled }),
    });
    toast(disabled ? "已禁用 xAI 账号" : "已启用 xAI 账号", 12000);
    await refreshCpa();
  });
  box.querySelectorAll(".backup-auth").forEach((btn) => btn.onclick = () => {
    window.open(`/api/cpa/xai-auth-files/${encodeURIComponent(btn.dataset.name)}/download`, "_blank");
  });
  box.querySelectorAll(".delete-auth").forEach((btn) => btn.onclick = async () => {
    if (!confirm("只会删除这个 xAI/Grok 账号文件，确认？")) return;
    await api(`/api/cpa/xai-auth-files/${encodeURIComponent(btn.dataset.name)}`, { method: "DELETE" });
    toast("已删除 xAI 账号", 12000);
    await refreshCpa();
  });
}

function renderSettings() {
  const s = state.bootstrap.settings;
  $("cpaBaseUrl").value = "";
  $("cpaApiKey").value = "";
  $("cpaManagementKey").value = "";
  $("imageHostBaseUrl").value = s.image_host_base_url || "https://img.remit.ee";
  const options = imageHostOptions();
  $("imageHostSelect").innerHTML = options.map((url) => `<option value="${esc(url)}">${esc(url)}</option>`).join("");
  $("imageHostSelect").value = options.includes(s.image_host_selected_url) ? s.image_host_selected_url : options[0];
  $("imageHostStatus").textContent = `图床：${$("imageHostSelect").value || "未设置"}`;
  const limits = downloadThreadLimits();
  $("downloadThreadCount").value = limits.configured;
  $("downloadThreadCount").max = limits.max;
  $("downloadThreadStatus").textContent = `推荐 ${limits.recommended} 线程；本机最大 ${limits.max} 线程。`;
  renderFfmpeg();
}

function imageHostOptions() {
  const s = state.bootstrap?.settings || {};
  const values = [s.image_host_base_url, s.image_host_selected_url, ...(s.image_host_options || [])]
    .map((url) => String(url || "").trim().replace(/\/+$/, ""))
    .filter((url) => url.startsWith("https://"));
  return [...new Set(values.length ? values : ["https://img.remit.ee"])];
}

async function testImageHost(target) {
  const url = String(target || "").trim();
  if (!url.startsWith("https://")) {
    return alertDialog("图床地址无效", "图床地址必须是完整 HTTPS 地址，例如 https://img.remit.ee。");
  }
  try {
    $("imageHostStatus").textContent = `正在校验：${url}`;
    const result = await api("/api/image-host/test", {
      method: "POST",
      body: JSON.stringify({ image_host_url: url }),
    });
    $("imageHostStatus").textContent = `图床可用：${result.image_host_url} · 第 ${result.attempt} 次成功`;
    toast(`图床校验成功：${result.public_url}`, 15000);
  } catch (err) {
    $("imageHostStatus").textContent = "图床校验失败";
    await alertDialog("图床校验失败", `${err.message}\n\n内置服务已最多重试 3 次。请稍后重新发起校验或更换图床地址。`);
  }
}

function renderFfmpeg() {
  const f = state.bootstrap.ffmpeg || {};
  $("ffmpegStatus").textContent = `ffmpeg: ${f.status || "unknown"} · ${f.message || ""}`;
}

function renderAll() {
  renderTasks();
  renderGlobal(activeTask());
  renderScenes();
  renderCpa();
}

async function refreshTasks(options = {}) {
  const data = await api("/api/tasks");
  state.tasks = data.tasks || [];
  if (!state.activeTaskId && state.tasks[0]) state.activeTaskId = state.tasks[0].id;
  if (!options.force && isUserEditing()) return;
  renderAll();
}

async function refreshCpa(options = {}) {
  try {
    const data = await api("/api/cpa/xai-auth-files");
    state.cpaFiles = mergeByIdOrName(state.cpaFiles, data.files || []);
    state.lastCpaRefresh = new Date().toLocaleTimeString();
  } catch (err) {
    toast(`CPA 管理不可用：${err.message}`, 15000);
    state.cpaFiles = [];
  }
  if (!options.force && isUserEditing()) return;
  renderCpa();
}

async function authRequest(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail;
    try { detail = await res.json(); } catch { detail = await res.text(); }
    throw new Error(detailMessage(detail));
  }
  return res.json();
}

function applyAuthStatus(status) {
  state.auth.initialized = Boolean(status.initialized);
  state.auth.authenticated = Boolean(status.authenticated);
  state.auth.locked = Boolean(status.locked);
  state.auth.minKeyLength = Number(status.min_key_length || state.auth.minKeyLength || 12);
  state.auth.message = status.message || "";
}

function clearAppTimers() {
  state.refreshTimers.forEach((timer) => clearInterval(timer));
  state.refreshTimers = [];
  state.appStarted = false;
}

function showAuthGate(message = "") {
  const initialized = state.auth.initialized;
  const locked = state.auth.locked;
  document.body.classList.add("auth-locked");
  $("authTitle").textContent = locked ? "授权配置异常" : (initialized ? "输入授权密钥" : "设置授权密钥");
  $("authMessage").textContent = locked
    ? "服务已拒绝开放，请检查后端认证配置。"
    : (initialized ? "请输入授权密钥后继续。" : `首次访问需要设置授权密钥，至少 ${state.auth.minKeyLength} 个字符。`);
  $("authConfirmGroup").style.display = initialized || locked ? "none" : "";
  $("authSubmit").textContent = initialized ? "解锁" : "设置并进入";
  $("authKey").disabled = locked;
  $("authConfirmKey").disabled = locked;
  $("authSubmit").disabled = locked;
  $("authError").textContent = message || state.auth.message || "";
  $("authKey").autocomplete = initialized ? "current-password" : "new-password";
  if (!locked) setTimeout(() => $("authKey").focus(), 0);
}

function lockForAuth(message = "") {
  clearAppTimers();
  state.auth.authenticated = false;
  showAuthGate(message);
}

function unlockAuthGate() {
  document.body.classList.remove("auth-locked");
  $("authError").textContent = "";
  $("authKey").value = "";
  $("authConfirmKey").value = "";
}

function bindAuthForm() {
  const form = $("authForm");
  if (!form || form.dataset.bound) return;
  form.dataset.bound = "1";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const key = $("authKey").value;
    const confirmKey = $("authConfirmKey").value;
    const settingUp = !state.auth.initialized;
    const minLength = state.auth.minKeyLength || 12;
    $("authError").textContent = "";
    if (key.length < minLength) {
      $("authError").textContent = `授权密钥至少需要 ${minLength} 个字符。`;
      $("authKey").focus();
      return;
    }
    if (settingUp && key !== confirmKey) {
      $("authError").textContent = "两次输入的授权密钥不一致。";
      $("authConfirmKey").focus();
      return;
    }
    $("authSubmit").disabled = true;
    $("authSubmit").textContent = settingUp ? "正在设置..." : "正在校验...";
    try {
      const result = await authRequest(settingUp ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        body: JSON.stringify(settingUp ? { key, confirm_key: confirmKey } : { key }),
      });
      applyAuthStatus({ ...result, min_key_length: minLength });
      unlockAuthGate();
      await startApp();
    } catch (err) {
      try {
        const status = await authRequest("/api/auth/status");
        applyAuthStatus(status);
      } catch {
        state.auth.locked = true;
      }
      showAuthGate(err.message || "授权校验失败。");
      $("authKey").select();
    } finally {
      if (!state.auth.locked) {
        $("authSubmit").disabled = false;
        $("authSubmit").textContent = state.auth.initialized ? "解锁" : "设置并进入";
      }
    }
  });
}

async function boot() {
  bindAuthForm();
  const status = await authRequest("/api/auth/status");
  applyAuthStatus(status);
  if (!state.auth.authenticated) {
    showAuthGate();
    return;
  }
  unlockAuthGate();
  await startApp();
}

async function startApp() {
  if (state.appStarted) return;
  state.appStarted = true;
  try {
    state.bootstrap = await api("/api/bootstrap");
    optionList($("globalModel"), state.bootstrap.models, (m) => m.label);
    optionList($("globalDuration"), state.bootstrap.durations, (v) => `${v}s`);
    optionList($("globalResolution"), state.bootstrap.image_resolutions);
    optionList($("globalAspect"), state.bootstrap.aspect_ratios);
    renderSettings();
    await refreshTasks({ force: true });
    await refreshCpa({ force: true });
    bindGlobalButtons();
    state.refreshTimers = [
      setInterval(() => refreshTasks().catch(() => {}), 2000),
      setInterval(() => refreshCpa().catch(() => {}), CPA_REFRESH_INTERVAL_MS),
    ];
  } catch (err) {
    state.appStarted = false;
    throw err;
  }
}

function bindGlobalButtons() {
  if (state.globalButtonsBound) return;
  state.globalButtonsBound = true;
  ["globalModel", "globalDuration", "globalResolution", "globalAspect", "submitInterval"].forEach((id) => {
    $(id).addEventListener("input", rememberGlobalDraft);
    $(id).addEventListener("change", rememberGlobalDraft);
  });
  $("newTaskBtn").onclick = async () => {
    const global_params = globalParams();
    const task = await api("/api/tasks", { method: "POST", body: JSON.stringify({ name: $("newTaskName").value, global_params }) });
    state.activeTaskId = task.id;
    $("newTaskName").value = "";
    await refreshTasks({ force: true });
  };
  $("addSceneBtn").onclick = async () => {
    const task = activeTask();
    if (!task) return toast("请先创建任务");
    await api(`/api/tasks/${task.id}/scenes`, { method: "POST", body: JSON.stringify({ prompt: "", image_url: "" }) });
    await refreshTasks({ force: true });
  };
  $("submitTaskBtn").onclick = async () => {
    const task = activeTask();
    if (!task) return toast("请先创建任务");
    const saved = await saveVisibleSceneDrafts();
    if (!saved) return;
    const emptyScenes = saved.filter((item) => !item.payload.prompt).map((item) => item.sceneId);
    if (emptyScenes.length) {
      await refreshTasks({ force: true });
      return toast(`分镜 ${emptyScenes.join(", ")} 提示词为空，已保存但不会提交生成`, 12000);
    }
    await api(`/api/tasks/${task.id}/submit`, { method: "POST", body: JSON.stringify({ submit_interval_seconds: positiveNumberFrom("submitInterval", 5) }) });
    toast("已保存全部分镜并提交到后台", 10000);
    await refreshTasks({ force: true });
  };
  $("applyGlobalBtn").onclick = async () => {
    const task = activeTask();
    if (!task) return;
    const params = globalParams();
    await api(`/api/tasks/${task.id}/global-params`, { method: "PUT", body: JSON.stringify(params) });
    for (const scene of task.scenes) {
      await api(`/api/tasks/${task.id}/scenes/${scene.id}`, {
        method: "PUT",
        body: JSON.stringify({ prompt: scene.prompt, image_url: scene.image_url, ...params }),
      });
      clearSceneDraft(task.id, scene.id);
    }
    delete state.globalDrafts[task.id];
    toast("全局参数已应用到全部分镜", 10000);
    await refreshTasks({ force: true });
  };
  $("downloadSelectedBtn").onclick = async () => {
    let task = activeTask();
    const scene_ids = selectedSceneIds();
    if (!task || !scene_ids.length) return toast("请勾选分镜");
    task = await refreshMissingVideoUrls(scene_ids);
    const missingUrl = task.scenes.filter((scene) => scene_ids.includes(scene.id) && !scene.video_url).map((scene) => scene.id);
    if (missingUrl.length) {
      return alertDialog("不能批量下载", `分镜 ${missingUrl.join(", ")} 没有视频 URL，不能自动下载。`);
    }
    const expiredUrl = task.scenes.filter((scene) => scene_ids.includes(scene.id) && !sceneVideoFresh(scene)).map((scene) => scene.id);
    if (expiredUrl.length) {
      return alertDialog("执行时间已过期", `分镜 ${expiredUrl.join(", ")} 的本次执行时间已超过 24 小时。请重新执行，拿到新任务 ID 后再下载。`);
    }
    const threadCount = await chooseDownloadThreads();
    if (!threadCount) return;
    await api(`/api/tasks/${task.id}/download`, { method: "POST", body: JSON.stringify({ scene_ids, thread_count: threadCount }) });
    toast(`已开始并发下载：每个视频 ${threadCount} 线程`, 10000);
  };
  $("mergeBtn").onclick = async () => {
    const task = activeTask();
    const scene_ids = selectedSceneIds();
    if (!task || scene_ids.length < 2) return toast("至少勾选两个已下载分镜");
    const missingLocal = task.scenes.filter((scene) => scene_ids.includes(scene.id) && !scene.local_video).map((scene) => scene.id);
    if (missingLocal.length) return toast(`分镜 ${missingLocal.join(", ")} 还没有本地视频，请先下载或上传`);
    try {
      await api(`/api/tasks/${task.id}/merge`, {
        method: "POST",
        body: JSON.stringify({
          scene_ids,
          normalize: $("normalizeMerge").checked,
          resolution: $("mergeResolution").value,
          aspect_ratio: $("mergeAspect").value,
        }),
      });
      toast("合并完成，已放到当前任务分镜列表底部", 12000);
      await refreshTasks({ force: true });
    } catch (err) {
      toast(`合并失败：${err.message}`, 15000);
    }
  };
  $("saveSettingsBtn").onclick = async () => {
    const imageHostBase = $("imageHostBaseUrl").value.trim() || "https://img.remit.ee";
    const selectedHost = $("imageHostSelect").value || imageHostBase;
    const imageHostOptionsValue = [...new Set([imageHostBase, selectedHost, ...imageHostOptions()].map((url) => String(url || "").trim().replace(/\/+$/, "")).filter(Boolean))];
    const limits = downloadThreadLimits();
    let downloadThreads = Math.floor(positiveNumberFrom("downloadThreadCount", limits.recommended));
    if (downloadThreads < 1) downloadThreads = 1;
    if (downloadThreads > limits.max) {
      const ok = await modalDialog({
        title: "默认线程数超过本机支持",
        message: `当前填写 ${downloadThreads} 线程，本机最大支持 ${limits.max} 线程。继续保存将使用 ${limits.max} 线程。`,
        confirmText: `保存为 ${limits.max} 线程`,
        cancelText: "取消保存",
        danger: true,
      });
      if (!ok) return;
      downloadThreads = limits.max;
      $("downloadThreadCount").value = limits.max;
    }
    const body = {
      cpa_base_url: $("cpaBaseUrl").value,
      cpa_api_key: $("cpaApiKey").value,
      cpa_management_key: $("cpaManagementKey").value,
      image_host_base_url: imageHostBase,
      image_host_selected_url: selectedHost,
      image_host_options: imageHostOptionsValue,
      image_host_upload_path: "/api/upload",
      poll_interval_seconds: 5,
      download_thread_count: downloadThreads,
    };
    await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
    state.bootstrap = await api("/api/bootstrap");
    renderSettings();
    toast("设置已保存", 12000);
  };
  $("testBaseImageHostBtn").onclick = () => testImageHost($("imageHostBaseUrl").value);
  $("testSelectedImageHostBtn").onclick = () => testImageHost($("imageHostSelect").value);
  $("downloadFfmpegBtn").onclick = async () => {
    await api("/api/tools/ffmpeg/download", { method: "POST" });
    toast("ffmpeg 检测/下载已启动", 12000);
  };
  $("refreshCpaBtn").onclick = async () => {
    await refreshCpa({ force: true });
    toast("已刷新 xAI 账号", 10000);
  };
  $("uploadXaiAuthBtn").onclick = () => $("uploadXaiAuthInput").click();
  $("uploadXaiAuthInput").onchange = async () => {
    const file = $("uploadXaiAuthInput").files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      await api("/api/cpa/xai-auth-files/upload", { method: "POST", body: form });
      toast("xAI 账号文件已上传", 12000);
      await refreshCpa({ force: true });
    } catch (err) {
      toast(`上传失败：${err.message}`, 15000);
    } finally {
      $("uploadXaiAuthInput").value = "";
    }
  };
}

function globalParams() {
  return {
    model: $("globalModel").value,
    duration: positiveNumberFrom("globalDuration", 8),
    resolution: $("globalResolution").value,
    aspect_ratio: $("globalAspect").value,
    submit_interval_seconds: positiveNumberFrom("submitInterval", 5),
  };
}

boot().catch((err) => {
  state.auth.locked = true;
  showAuthGate(`启动失败：${err.message}`);
});
