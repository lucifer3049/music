"use strict";

// 與後端 app/matching/matcher.py 的 HIGH_CONFIDENCE 一致，僅用於視覺標示
const HIGH_CONFIDENCE = 0.92;

const TERMINAL_STATUSES = new Set(["done", "failed", "skipped"]);
const BLANK_COVER = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";

const jobsEl = document.getElementById("jobs");
const submitBtn = document.getElementById("submit");
const urlsEl = document.getElementById("urls");
const submitStatus = document.getElementById("submit-status");
const jobTemplate = document.getElementById("job-template");
const trackTemplate = document.getElementById("track-template");

// track id -> 使用者目前選中的候選索引
const selections = new Map();
// job id -> 開啟中的 EventSource，避免刷新頁面或重複呼叫 watchJob 時
// 對同一個 job 疊加多條串流連線。
const openStreams = new Map();

submitBtn.addEventListener("click", async () => {
  const urls = urlsEl.value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (urls.length === 0) return;

  submitBtn.disabled = true;
  submitStatus.textContent = "探測中，請稍候…";
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { job_ids: jobIds } = await response.json();
    submitStatus.textContent = `已建立 ${jobIds.length} 個任務`;
    urlsEl.value = "";
    jobIds.forEach(watchJob);
  } catch (err) {
    submitStatus.textContent = `送出失敗：${err.message}`;
  } finally {
    submitBtn.disabled = false;
  }
});

function watchJob(jobId) {
  // 已經有一條串流盯著這個 job 就不要再開一條——重複開啟會讓同一個 job
  // 的畫面更新被兩條（甚至更多）連線交錯覆蓋，且連線只會越積越多不會
  // 自己收斂。呼叫端（初次載入時逐一 job、送出後逐一新 job）本來就不會
  // 對同一個 id 呼又第二次，這裡的檢查是防禦性的。
  if (openStreams.has(jobId)) return;
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  openStreams.set(jobId, source);
  source.onmessage = (event) => renderJob(JSON.parse(event.data));
  source.onerror = () => {
    source.close();
    openStreams.delete(jobId);
  };
}

function renderJob(job) {
  let section = document.getElementById(`job-${job.id}`);
  if (!section) {
    const node = jobTemplate.content.cloneNode(true);
    section = node.querySelector(".job");
    section.id = `job-${job.id}`;
    jobsEl.prepend(section);
  }

  section.querySelector(".job-title").textContent = `任務 #${job.id}　${job.url}`;
  // job 層級的錯誤（整支影片下架、地區限制、網址不支援等）：探測階段
  // 就整批失敗，tracks 會是空陣列。不把這個欄位顯示出來的話，使用者看到
  // 的就只是一個永遠不會冒出曲目的空白任務，完全不知道發生了什麼事。
  section.querySelector(".job-error").textContent = job.error || "";

  const container = section.querySelector(".tracks");
  container.replaceChildren(...job.tracks.map(renderTrack));

  const finished =
    job.error !== null ||
    (job.tracks.length > 0 && job.tracks.every((t) => TERMINAL_STATUSES.has(t.status)));
  if (finished) {
    // 後端 SSE 端點在這個條件下也會結束串流；這裡同步把追蹤表清掉，
    // 避免留著一個實際上已經關閉（或即將關閉）的 EventSource 參照。
    openStreams.get(job.id)?.close();
    openStreams.delete(job.id);
  }
}

function renderTrack(track) {
  const node = trackTemplate.content.cloneNode(true);
  const article = node.querySelector(".track");
  article.dataset.trackId = track.id;

  article.querySelector(".raw-title").textContent = track.raw_title;
  const statusEl = article.querySelector(".status");
  statusEl.textContent = statusLabel(track.status);
  statusEl.dataset.status = track.status;
  article.querySelector(".error").textContent = track.error || "";
  article.querySelector(".action-error").textContent = "";

  const candidatesEl = article.querySelector(".candidates");
  const selected = selections.get(track.id) ?? 0;
  track.candidates.forEach((candidate, index) => {
    candidatesEl.append(renderCandidate(track.id, candidate, index, index === selected));
  });

  const editable = track.status === "awaiting_confirm";
  const confirmBtn = article.querySelector(".confirm");
  const skipBtn = article.querySelector(".skip");
  confirmBtn.disabled = !editable || track.candidates.length === 0;
  skipBtn.disabled = !editable;
  confirmBtn.addEventListener("click", () =>
    confirmTrack(article, track, selections.get(track.id) ?? 0)
  );
  skipBtn.addEventListener("click", () => skipTrack(article, track.id));

  return node;
}

function renderCandidate(trackId, candidate, index, isSelected) {
  const meta = candidate.meta;
  const div = document.createElement("div");
  div.className = "candidate" + (isSelected ? " selected" : "");
  div.addEventListener("click", () => {
    selections.set(trackId, index);
    div.parentElement.querySelectorAll(".candidate").forEach((el) => {
      el.classList.remove("selected");
    });
    div.classList.add("selected");
  });

  const cover = document.createElement("img");
  cover.alt = "";
  cover.loading = "lazy";
  cover.src = meta.cover_url || BLANK_COVER;
  // 沒有封面（cover_url 為空）跟封面連結失效（Cover Art Archive 404 /
  // 網路錯誤）看起來對使用者是同一回事：都應該是安靜的空白方塊，而不是
  // 瀏覽器內建的壞圖示。
  cover.addEventListener(
    "error",
    () => {
      cover.onerror = null;
      cover.src = BLANK_COVER;
    },
    { once: true }
  );
  div.append(cover);

  const fields = document.createElement("div");
  fields.className = "fields";
  const trackNo = meta.track_no ? `${String(meta.track_no).padStart(2, "0")}. ` : "";
  fields.innerHTML = [
    `<div><strong>${escapeHtml(trackNo + meta.title)}</strong></div>`,
    `<div>${escapeHtml(meta.artists.join("; "))}</div>`,
    `<div>${escapeHtml(meta.album)}${meta.year ? ` · ${escapeHtml(String(meta.year))}` : ""}</div>`,
  ].join("");
  div.append(fields);

  const score = document.createElement("span");
  score.className = "score " + (candidate.score >= HIGH_CONFIDENCE ? "high" : "low");
  score.textContent = candidate.score > 0
    ? `${Math.round(candidate.score * 100)}%`
    : "未配對";
  div.append(score);

  return div;
}

async function confirmTrack(article, track, index) {
  const meta = track.candidates[index]?.meta;
  if (!meta) return;
  const actionErrorEl = article.querySelector(".action-error");
  actionErrorEl.textContent = "";
  try {
    const response = await fetch(`/api/tracks/${track.id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meta }),
    });
    // 曲目可能在使用者按下按鈕的當下已經不再是 awaiting_confirm 了
    // （例如另一個分頁已經確認過、或已經被跳過）：後端這時回 409，
    // 若前端不理會狀態碼，使用者會以為確認送出去了，實際上什麼都沒發生。
    if (!response.ok) {
      actionErrorEl.textContent = await extractErrorMessage(response);
    }
  } catch (err) {
    actionErrorEl.textContent = `連線失敗：${err.message}`;
  }
}

async function skipTrack(article, trackId) {
  const actionErrorEl = article.querySelector(".action-error");
  actionErrorEl.textContent = "";
  try {
    const response = await fetch(`/api/tracks/${trackId}/skip`, { method: "POST" });
    if (!response.ok) {
      actionErrorEl.textContent = await extractErrorMessage(response);
    }
  } catch (err) {
    actionErrorEl.textContent = `連線失敗：${err.message}`;
  }
}

async function extractErrorMessage(response) {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // 回應不是 JSON（理論上不會發生），退回通用訊息
  }
  return `操作失敗（HTTP ${response.status}）`;
}

function statusLabel(status) {
  return {
    pending: "等待中",
    matching: "比對中",
    awaiting_confirm: "待確認",
    downloading: "下載中",
    tagging: "寫入標籤",
    done: "完成",
    failed: "失敗",
    skipped: "已跳過",
  }[status] || status;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

// 重新整理頁面後，把還在進行中的任務接回 SSE 串流（job 沒有錯誤，且至少
// 有一首曲目還沒到終結狀態）。
fetch("/api/jobs")
  .then((r) => r.json())
  .then(({ jobs }) => {
    jobs.forEach((job) => {
      renderJob(job);
      const active =
        job.error === null &&
        job.tracks.some((t) => !TERMINAL_STATUSES.has(t.status));
      if (active) watchJob(job.id);
    });
  })
  .catch(() => {});
