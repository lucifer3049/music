"use strict";

// 與後端 app/matching/matcher.py 的 HIGH_CONFIDENCE 一致，僅用於視覺標示
const HIGH_CONFIDENCE = 0.92;

const TERMINAL_STATUSES = new Set(["done", "failed", "skipped"]);
const BLANK_COVER = "data:image/gif;base64,R0lGODlhAQABAAAAACw=";

// 與後端 app/api/routes.py job_events() 結束串流的條件一致：job 層級探測
// 失敗，或曲目非空且全部到達終結狀態。兩處各自判斷「這個 job 還要不要繼續
// 追蹤」，條件對不齊的話其中一處就會在另一處已經放棄時繼續等待，或反過來
// 提早放棄一個其實還在跑的 job。
function isJobFinished(job) {
  return (
    job.error !== null ||
    (job.tracks.length > 0 && job.tracks.every((t) => TERMINAL_STATUSES.has(t.status)))
  );
}

const jobsEl = document.getElementById("jobs");
const submitBtn = document.getElementById("submit");
const urlsEl = document.getElementById("urls");
const submitStatus = document.getElementById("submit-status");
const jobTemplate = document.getElementById("job-template");
const trackTemplate = document.getElementById("track-template");

// track id -> 使用者目前選中的候選索引
const selections = new Map();
// track id -> 目前編輯表單內容（字串形式，直接對應 input.value）。
// 選取候選時整包覆寫成該候選的內容；使用者在欄位裡打字時逐欄位更新。
// renderTrack() 每次重繪都從這裡讀值回填，讓使用者的修改撐過下一次 SSE
// 推播——否則後端每秒一次的整包重繪會把使用者還沒送出的修改蓋回候選原文。
const edits = new Map();
// job id -> 開啟中的 EventSource，避免刷新頁面或重複呼叫 watchJob 時
// 對同一個 job 疊加多條串流連線。
const openStreams = new Map();

// 會寫進 MetaPayload 的可編輯欄位（見 app/api/routes.py）。刻意不含
// cover_url／duration／source_url：cover_url 會被
// app/sources/musicbrainz.py 的 release_mbid_for_genre_lookup() 反解出
// release MBID 拿去查 genre，開放編輯等於讓使用者的任意字串在伺服器端
// 被當成下載網址使用，因此這三欄一律原封不動從選中的候選帶過去。
const EDITABLE_FIELDS = [
  "title",
  "artists",
  "album_artist",
  "album",
  "year",
  "track_no",
  "track_total",
  "genre",
];

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
  // 後端每秒不做內容比對地整包推 SSE payload，renderJob() 平常就整包
  // replaceChildren() 換掉所有曲目節點。如果使用者正在某首曲目的編輯
  // 表單裡打字，這個換節點的動作會連游標位置一起丟掉，而且一秒一次的
  // 頻率讓使用者幾乎打不完一個字。與其在每次重繪後把 focus／caret 精準
  // 搬回新節點，這裡直接讓「正在編輯中」的那首曲目節點原地保留、跳過這
  // 一輪重繪；其餘曲目照常重繪。使用者放開該欄位（切去編輯別首、或按下
  // 確認／跳過）之後，下一輪 SSE 自然會用最新資料把它接回來。
  const editingTrackId = focusedEditTrackId(container);
  const existingByTrackId = new Map();
  container.querySelectorAll(".track").forEach((el) => {
    existingByTrackId.set(el.dataset.trackId, el);
  });
  container.replaceChildren(
    ...job.tracks.map((track) => {
      const idStr = String(track.id);
      if (idStr === editingTrackId) {
        const existing = existingByTrackId.get(idStr);
        if (existing) return existing;
      }
      return renderTrack(track);
    })
  );

  if (isJobFinished(job)) {
    // 後端 SSE 端點在這個條件下也會結束串流；這裡同步把追蹤表清掉，
    // 避免留著一個實際上已經關閉（或即將關閉）的 EventSource 參照。
    openStreams.get(job.id)?.close();
    openStreams.delete(job.id);
  }
}

// 給定容器，回傳目前 focus 停在其 .edit-fields input 裡的曲目 id
// （dataset.trackId 的字串形式），沒有的話回傳 null。
function focusedEditTrackId(container) {
  const active = document.activeElement;
  if (!active || !container.contains(active)) return null;
  if (!active.matches(".edit-fields input")) return null;
  const trackEl = active.closest(".track");
  return trackEl ? trackEl.dataset.trackId : null;
}

// 候選的 meta（title/artists/.../genre）轉成編輯表單用的字串值。
// artists 是後端的 list[str]，用 "; " 接成單一輸入框內容——與檔案標籤
// 「參與演出者」欄位的顯示分隔號一致，回寫時再用同一個分隔號拆回陣列。
function metaToEditValues(meta) {
  return {
    title: meta.title ?? "",
    artists: (meta.artists ?? []).join("; "),
    album_artist: meta.album_artist ?? "",
    album: meta.album ?? "",
    year: meta.year != null ? String(meta.year) : "",
    track_no: meta.track_no != null ? String(meta.track_no) : "",
    track_total: meta.track_total != null ? String(meta.track_total) : "",
    genre: meta.genre ?? "",
  };
}

// 數字欄位（year/track_no/track_total）：空字串要送 null，不能送 ""
// 或 0——MetaPayload 這幾欄是 int | None，送空字串或非數字會 422。
function numOrNull(value) {
  const trimmed = (value ?? "").trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

// 把編輯表單的字串值 + 選中候選的 meta 合成要送給 /confirm 的 MetaPayload。
// cover_url／duration／source_url 永遠原封不動取自 baseMeta，不理會表單
// （表單裡本來就沒有這三欄的輸入框）。
function buildMetaPayload(baseMeta, values) {
  const genre = (values.genre ?? "").trim();
  return {
    title: (values.title ?? "").trim(),
    artists: (values.artists ?? "")
      .split(";")
      .map((s) => s.trim())
      .filter(Boolean),
    album_artist: (values.album_artist ?? "").trim(),
    album: (values.album ?? "").trim(),
    year: numOrNull(values.year),
    track_no: numOrNull(values.track_no),
    track_total: numOrNull(values.track_total),
    genre: genre === "" ? null : genre,
    cover_url: baseMeta.cover_url,
    duration: baseMeta.duration,
    source_url: baseMeta.source_url,
  };
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
  const selectedIndex = selections.get(track.id) ?? 0;
  track.candidates.forEach((candidate, index) => {
    candidatesEl.append(
      renderCandidate(track.id, candidate, index, index === selectedIndex, article)
    );
  });

  // 編輯欄位：有既有編輯內容（使用者打過字，或先前選過候選）就用那份；
  // 否則從目前選中的候選帶初值。track.candidates 為空（探測失敗降級到
  // 完全查無資料的極端情況）就留空白讓使用者從零開始填。
  const editFieldsEl = article.querySelector(".edit-fields");
  const fieldInputs = {};
  EDITABLE_FIELDS.forEach((field) => {
    fieldInputs[field] = editFieldsEl.querySelector(`[data-field="${field}"]`);
  });

  let values = edits.get(track.id);
  if (!values) {
    const selectedCandidate = track.candidates[selectedIndex];
    values = selectedCandidate ? metaToEditValues(selectedCandidate.meta) : metaToEditValues({});
    edits.set(track.id, values);
  }
  EDITABLE_FIELDS.forEach((field) => {
    fieldInputs[field].value = values[field] ?? "";
    fieldInputs[field].addEventListener("input", () => {
      const current = edits.get(track.id) ?? {};
      current[field] = fieldInputs[field].value;
      edits.set(track.id, current);
    });
  });

  const editable = track.status === "awaiting_confirm";
  EDITABLE_FIELDS.forEach((field) => {
    fieldInputs[field].disabled = !editable;
  });

  const confirmBtn = article.querySelector(".confirm");
  const skipBtn = article.querySelector(".skip");
  confirmBtn.disabled = !editable || track.candidates.length === 0;
  skipBtn.disabled = !editable;
  confirmBtn.addEventListener("click", () => {
    const baseCandidate = track.candidates[selections.get(track.id) ?? 0];
    if (!baseCandidate) return;
    const currentValues = edits.get(track.id) ?? values;
    confirmTrack(confirmBtn, track.id, baseCandidate.meta, currentValues);
  });
  skipBtn.addEventListener("click", () => skipTrack(skipBtn, track.id));

  return node;
}

function renderCandidate(trackId, candidate, index, isSelected, article) {
  const meta = candidate.meta;
  const div = document.createElement("div");
  div.className = "candidate" + (isSelected ? " selected" : "");
  div.addEventListener("click", () => {
    selections.set(trackId, index);
    div.parentElement.querySelectorAll(".candidate").forEach((el) => {
      el.classList.remove("selected");
    });
    div.classList.add("selected");

    // 換候選＝使用者要的是這個候選的內容，覆寫掉先前的編輯表單內容
    // （通常是屬於上一個候選的殘留），並立刻同步進畫面上的 input——
    // 這裡不透過整包重繪，避免打斷其他曲目正在進行的編輯。
    const newValues = metaToEditValues(meta);
    edits.set(trackId, newValues);
    const editFieldsEl = article.querySelector(".edit-fields");
    EDITABLE_FIELDS.forEach((field) => {
      const input = editFieldsEl.querySelector(`[data-field="${field}"]`);
      if (input) input.value = newValues[field] ?? "";
    });
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

function confirmTrack(button, trackId, baseMeta, values) {
  const meta = buildMetaPayload(baseMeta, values);
  return postAction(
    button,
    trackId,
    `/api/tracks/${trackId}/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meta }),
    }
  );
}

function skipTrack(button, trackId) {
  return postAction(button, trackId, `/api/tracks/${trackId}/skip`, { method: "POST" });
}

// confirmTrack/skipTrack 共用的請求邏輯。
//
// SSE 每秒推一次 payload、完全不做內容比對（見 app/api/routes.py
// job_events()），renderJob() 收到就整包 replaceChildren()，把所有曲目
// DOM 節點連根換掉。若在使用者點擊與這個 fetch resolve 之間剛好有一次
// 重繪，呼叫當下捕捉到的 .action-error 節點就已經被丟棄、不在文件裡了：
// 寫進去的錯誤訊息使用者永遠看不到。409（最常見的失敗）恰好又是最容易
// 撞上重繪的情況——「操作失敗」多半意味著別的地方剛好也改了這條曲目的
// 狀態，而那也正是會觸發重繪的事件。因此這裡不跨 await 保留 DOM 參照，
// 只記住 track id，在要寫入的當下才重新查詢還活著的節點；查不到就代表
// 這條曲目已經不在畫面上了，直接放棄寫入。
async function postAction(button, trackId, url, options) {
  const liveErrorEl = () =>
    document.querySelector(`[data-track-id="${trackId}"] .action-error`);

  const initialErrorEl = liveErrorEl();
  if (initialErrorEl) initialErrorEl.textContent = "";

  button.disabled = true;
  try {
    const response = await fetch(url, options);
    // 曲目可能在使用者按下按鈕的當下已經不再是 awaiting_confirm 了
    // （例如另一個分頁已經確認過、或已經被跳過）：後端這時回 409，
    // 若前端不理會狀態碼，使用者會以為確認送出去了，實際上什麼都沒發生。
    if (!response.ok) {
      const message = await extractErrorMessage(response);
      const el = liveErrorEl();
      if (el) el.textContent = message;
    }
  } catch (err) {
    const el = liveErrorEl();
    if (el) el.textContent = `連線失敗：${err.message}`;
  } finally {
    button.disabled = false;
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
      if (!isJobFinished(job)) watchJob(job.id);
    });
  })
  .catch(() => {});
