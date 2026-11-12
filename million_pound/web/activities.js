"use strict";
import {$, action, api, deleteMedia, formatTime, initSession, notice, petNames} from "/assets/client.js";
import {PETS, activityCandidate, activityKey, durationLabel, groupActivities, timelineLayout, timelinePhotos} from "/assets/timeline.js";

const statuses = {pending: "正在判断", error: "判断失败", skipped: "积压未判断", cancelled: "已中断"};
const SUSPECT_PREFIX = "suspect:";
const clock = (value, seconds = false) => new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", ...(seconds ? {second: "2-digit"} : {}), hour12: false}).format(new Date(value));
const today = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
const records = new Map();
const compactTimeline = window.matchMedia("(max-width:760px)");
let before = null, generation = 0, filter = "", labels = {}, episodesById = new Map(), clustersById = new Map(), photosById = new Map(), detail = null, photoIndex = 0, photoTrigger = null, loadingOlder = false;
let deletingMedia = false;
$("#day").value = today();

function labelFor(key) {
  if (typeof key !== "string") return "未知";
  if (key.startsWith(SUSPECT_PREFIX)) key = key.slice(SUSPECT_PREFIX.length);
  return key === "unknown" ? "未知" : key === "walking" ? "走动" : labels[key] || "未知";
}

function renderTimeline() {
  const episodes = groupActivities([...records.values()]);
  episodesById = new Map(episodes.map(episode => [episode.id, episode]));
  for (const pet of Object.keys(PETS)) {
    const header = document.querySelector('.pet-header[data-pet="' + pet + '"]');
    const latest = episodes.find(episode => episode.pet === pet);
    header.querySelector(".pet-current").textContent = latest ? labelFor(latest.key) + " · " + clock(latest.end) : "暂无可展示的活动";
  }
  const windowStart = Date.parse($("#day").value + "T00:00:00+08:00");
  const windowEnd = windowStart + 24 * 3600000;
  const starts = [...records.values()].map(item => Date.parse(item.started_at));
  const ends = [...records.values()].map(item => Date.parse(item.ended_at));
  const layout = timelineLayout(episodes, Math.max(windowStart, Math.min(...starts)), Math.min(windowEnd, Math.max(...ends)), compactTimeline.matches, Number($("#timeline-scale").value) * 60000);
  clustersById = new Map(layout.clusters.map(cluster => [cluster.id, cluster]));
  const photos = timelinePhotos([...records.values()], layout.ticks);
  photosById = new Map(photos.map(photo => [photo.id, photo]));
  const fragment = document.createDocumentFragment();
  for (const tick of layout.ticks) {
    const row = document.createElement("li"); row.className = "timeline-tick"; row.style.top = tick.top + "px";
    const time = document.createElement("time"); time.dateTime = new Date(tick.time).toISOString(); time.textContent = clock(tick.time, layout.step < 60000);
    row.append(time); fragment.append(row);
  }
  for (const photo of photos) {
    const thumbnail = $("#grid-photo-template").content.cloneNode(true);
    const item = thumbnail.querySelector("li"); item.dataset.pet = photo.pet; item.style.top = photo.top + "px";
    const button = thumbnail.querySelector("button"); button.dataset.gridPhoto = photo.id;
    const description = PETS[photo.pet] + (photo.key ? "，" + labelFor(photo.key) : "") + "，" + clock(photo.time, true) + "的入镜画面，" + (photo.item.video ? "播放动态画面" : "查看照片");
    button.setAttribute("aria-label", description); button.title = description;
    const image = thumbnail.querySelector("img"); image.src = "/api/activities/" + photo.item.id + "/images/" + photo.index;
    image.alt = PETS[photo.pet] + "在" + clock(photo.time, true) + "的入镜画面";
    image.addEventListener("error", () => item.remove());
    const time = thumbnail.querySelector("time"); time.dateTime = new Date(photo.time).toISOString(); time.textContent = clock(photo.time);
    fragment.append(thumbnail);
  }
  for (const {episode, top, height, lane, labelTop} of layout.intervals) {
    const interval = $("#episode-template").content.cloneNode(true);
    const item = interval.querySelector("li"); item.dataset.pet = episode.pet;
    item.style.top = top + "px"; item.style.height = height + "px";
    item.style.setProperty("--interval-lane", lane);
    const button = interval.querySelector("button"); button.dataset.episode = episode.id;
    button.classList.toggle("is-suspect", episode.key.startsWith(SUSPECT_PREFIX));
    button.classList.toggle("is-overflow", labelTop === null);
    const description = PETS[episode.pet] + "，" + labelFor(episode.key) + "，" + clock(episode.start, true) + "至" + clock(episode.end, true) + "，查看画面";
    button.setAttribute("aria-label", description); button.title = description;
    interval.querySelector(".episode-title").textContent = labelFor(episode.key);
    interval.querySelector(".episode-duration").textContent = durationLabel(episode.end - episode.start);
    const label = interval.querySelector(".episode-label"), leader = interval.querySelector(".episode-leader");
    if (labelTop === null) { label.remove(); leader.remove(); }
    else {
      label.style.top = labelTop - top + "px";
      leader.setAttribute("viewBox", "0 0 20 " + height);
      leader.querySelector("path").setAttribute("d", "M20 " + height / 2 + "L0 " + (labelTop - top));
    }
    fragment.append(interval);
  }
  for (const cluster of layout.clusters) {
    const item = document.createElement("li"); item.className = "timeline-cluster"; item.dataset.pet = cluster.pet; item.style.top = cluster.top + "px";
    const button = document.createElement("button"); button.type = "button"; button.className = "episode-overflow"; button.dataset.cluster = cluster.id;
    button.textContent = "+" + cluster.hidden + " 段活动";
    button.setAttribute("aria-label", PETS[cluster.pet] + "，" + clock(cluster.start) + "至" + clock(cluster.end) + "，共" + cluster.intervals.length + "段活动，展开查看");
    button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("popovertarget", "interval-activities"); button.setAttribute("popovertargetaction", "show");
    item.append(button); fragment.append(item);
  }
  if (!layout.intervals.length && !photos.length) {
    const empty = document.createElement("li"); empty.className = "timeline-empty";
    empty.textContent = records.size ? "这段时间没有可展示的活动。" : "这一天还没有小日常，换个日期看看吧。";
    fragment.append(empty);
  }
  const focused = document.activeElement?.dataset.episode;
  const focusedCluster = document.activeElement?.dataset.cluster;
  const focusedPhoto = document.activeElement?.dataset.gridPhoto;
  $("#activity-list").style.height = layout.height + "px";
  $("#activity-list").replaceChildren(fragment);
  if (focused) document.querySelector('[data-episode="' + focused + '"]')?.focus({preventScroll: true});
  else if (focusedCluster) document.querySelector('[data-cluster="' + focusedCluster + '"]')?.focus({preventScroll: true});
  else if (focusedPhoto) document.querySelector('[data-grid-photo="' + focusedPhoto + '"]')?.focus({preventScroll: true});
  $("#older-activities").hidden = !before;
  $("#history-note").textContent = before ? "继续向下滚动，会自动加载更早的日常。" : "这一天的记录，到这里了。";
  $("#selected-date").textContent = $("#day").value ? new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long"}).format(new Date($("#day").value + "T12:00:00+08:00")) : "请选择日期";
}

async function loadActivities(older = false) {
  if (deletingMedia) return;
  if (!$("#day").value) { notice("请选择日期", true); return; }
  const revision = ++generation;
  const nextFilter = $("#day").value;
  if (filter !== nextFilter) older = false;
  const params = new URLSearchParams({day: $("#day").value, limit: "1000"});
  if (older && before) params.set("before", before);
  const result = await api("/api/activities?" + params);
  if (revision !== generation) return;
  const changed = filter !== nextFilter;
  const missed = !older && records.size && result.items.length && !result.items.some(item => records.has(item.id));
  if (changed || missed) records.clear();
  if (changed || missed || older || !records.size) before = result.next_before;
  filter = nextFilter; labels = result.labels;
  for (const item of result.items) records.set(item.id, item);
  if (!$("#activity-details").open && !$("#media-manager").open && !$("#interval-activities").matches(":popover-open")) {
    renderTimeline();
    autoLoadOlder();
  }
}

function stopVideo() {
  const video = $("#detail-video");
  video.pause(); video.removeAttribute("src"); video.removeAttribute("poster"); video.load(); video.hidden = true;
}

function showPhoto(imageIndex = 0) {
  stopVideo();
  const item = detail.records[photoIndex];
  const pet = detail.pet;
  const image = $("#detail-image"), link = $("#detail-original"), video = $("#detail-video");
  const hasImage = item.images.length > 0;
  link.hidden = !hasImage; $("#detail-missing").hidden = hasImage;
  if (hasImage) {
    link.href = "/api/activities/" + item.id + "/images/" + imageIndex;
    image.src = link.href; image.alt = PETS[pet] + "在" + formatTime(item.started_at) + "这段录像中的画面";
  } else { image.removeAttribute("src"); link.removeAttribute("href"); }
  if (item.video) {
    video.poster = link.href; video.src = item.video; video.hidden = false; link.hidden = true;
    video.play().catch(() => {});
  }
  $("#detail-photo-index").textContent = (photoIndex + 1) + " / " + detail.records.length;
  $("#previous-photo").disabled = photoIndex === 0;
  $("#next-photo").disabled = photoIndex === detail.records.length - 1;
  $("#photo-position").max = detail.records.length - 1;
  $("#photo-position").value = photoIndex;
  $("#photo-position").disabled = detail.records.length === 1;
  const when = Date.parse(item.started_at) + (item.frame_offsets[imageIndex] || 0) * 1000;
  $("#detail-caption").textContent = "采集约 " + formatTime(when) + (item.video ? " · 加速录像，可暂停或拖动进度核对动作" : hasImage ? " · 本段未保存录像，可点击图片查看原图" : " · 媒体已删除或过期");
  $("#delete-current-media").disabled = deletingMedia || (!hasImage && !item.video);
  $("#photo-position").setAttribute("aria-valuetext", "第 " + (photoIndex + 1) + " 张画面，采集约 " + formatTime(when));
  const decision = item.pets[pet];
  const legacy = item.status === "done" && decision?.activity === "sleeping" && !decision.visibility;
  const key = activityKey(item, pet);
  const suspected = typeof key === "string" && key.startsWith(SUSPECT_PREFIX);
  const uncertain = decision?.proposed_activity === "uncertain";
  const candidate = suspected || uncertain ? activityCandidate(decision) : null;
  let observation = statuses[item.status] || "判断不清";
  if (item.status === "done" && decision) {
    observation = legacy ? "旧模型曾判断：睡觉（未做入镜检查）" : "这次观察：" + (key ? labelFor(key) : "活动未确认");
    if (suspected) {
      observation += "（" + (uncertain ? "模型输出不确定，取最高分的非未知活动" : "未达确认条件") + "）";
    }
    if (decision.continued) observation += uncertain ? "（候选状态未变，延续前段活动；本次模型输出不确定）" : "（本段第一名仍相同，延续前段活动）";
    if (suspected || uncertain) observation += " · 候选分数 " + Math.round((decision.probabilities?.[candidate] ?? decision.confidence) * 100) + "%";
    if (uncertain) observation += " · 不确定分数 " + Math.round(decision.confidence * 100) + "%";
    else if (!suspected) observation += " · 模型分数 " + Math.round(decision.confidence * 100) + "%";
  }
  $("#detail-confidence").textContent = observation;
  $("#detail-identity").hidden = item.status !== "done" || !decision;
  if (decision?.visibility) {
    const identity = decision.visibility === "present" ? "已入镜" : decision.visibility === "absent" ? "未检测到入镜" : "入镜不确定";
    $("#detail-identity").textContent = "入镜检查：" + identity + " · 模型分数 " + Math.round(decision.visibility_confidence * 100) + "%";
  } else if (decision) $("#detail-identity").textContent = "入镜检查：旧版未执行，原判断仅供结合画面核对";
  const message = legacy ? "这条旧记录没有独立检查宠物是否入镜，原来的睡觉判断尚未核验，请结合照片查看。" : item.message;
  $("#detail-message").textContent = petNames(message); $("#detail-message").hidden = !message;
}

function openDetails(episode, imageIndex = 0) {
  if ($("#interval-activities").matches(":popover-open")) $("#interval-activities").hidePopover();
  photoTrigger = document.activeElement?.dataset.gridPhoto;
  detail = episode; photoIndex = episode.records.length - 1;
  $("#activity-details .media-message").hidden = true;
  $("#detail-pet").textContent = PETS[episode.pet] + "的小日常";
  $("#activity-details").dataset.pet = episode.pet;
  $("#detail-title").textContent = episode.key ? labelFor(episode.key) + " · " + durationLabel(episode.end - episode.start) : "当时画面";
  $("#detail-period").textContent = formatTime(episode.start) + " — " + formatTime(episode.end);
  $("#detail-count").textContent = "合并了 " + episode.records.length + " 次观察。活动根据录像判断，画面供你核对。";
  showPhoto(imageIndex); $("#activity-details").showModal();
}

function updateMediaSelection() {
  const boxes = [...document.querySelectorAll("#media-records input")];
  const count = boxes.filter(box => box.checked).length;
  const all = $("#select-all-activities");
  all.checked = boxes.length > 0 && count === boxes.length;
  all.indeterminate = count > 0 && count < boxes.length;
  all.disabled = deletingMedia || !boxes.length;
  $("#delete-selected-activities").textContent = "删除选中 (" + count + ")";
  $("#delete-selected-activities").disabled = deletingMedia || !count;
}

function renderMediaManager() {
  const items = [...records.values()].filter(item => item.images.length || item.video).sort((a, b) => b.id - a.id);
  const list = $("#media-records");
  list.replaceChildren(...items.map(item => {
    const label = document.createElement("label"); label.className = "media-check";
    const box = document.createElement("input"); box.type = "checkbox"; box.value = item.id;
    label.append(box);
    if (item.images.length) {
      const image = document.createElement("img"); image.src = "/api/activities/" + item.id + "/images/0";
      image.alt = ""; image.loading = "lazy"; image.addEventListener("error", () => image.remove()); label.append(image);
    }
    const text = document.createElement("span"), info = document.createElement("small");
    text.textContent = formatTime(item.started_at) + " — " + clock(item.ended_at, true);
    info.textContent = item.images.length + " 张截图" + (item.video ? " · 视频" : "");
    text.append(info); label.append(text); return label;
  }));
  if (!items.length) list.textContent = "已加载的记录没有可删除的图片或视频。";
  updateMediaSelection();
}

async function removeActivityMedia(ids) {
  deletingMedia = true; generation++;
  try {
    const result = await deleteMedia("activities", ids);
    if (!result) return;
    const affected = new Set(result.activities);
    for (const item of [...records.values(), ...(detail?.records || [])]) {
      if (affected.has(item.id)) { item.images = []; item.frame_offsets = []; item.video = null; }
    }
    renderTimeline();
    if ($("#media-manager").open) renderMediaManager();
    if ($("#activity-details").open) showPhoto();
  } finally { deletingMedia = false; }
}

async function loadStatus() {
  const status = await api("/api/status");
  const element = $("#capture-status");
  element.classList.toggle("is-live", status.recording);
  element.textContent = status.recording ? "正在记录日常" : status.enabled ? "等待下一段日常" : "记录已暂停";
  if (status.last_error) element.textContent = "记录遇到问题，请到首页查看";
}

async function loadOlder() {
  if (loadingOlder || !before) return;
  loadingOlder = true;
  let loaded = false;
  try { await loadActivities(true); loaded = true; }
  finally { loadingOlder = false; }
  if (loaded) autoLoadOlder();
}

function autoLoadOlder() {
  if (!before || loadingOlder || deletingMedia || $("#activity-details").open || $("#media-manager").open || $("#interval-activities").matches(":popover-open")) return;
  if (olderButton.getBoundingClientRect().top > window.innerHeight + 600) return;
  action(olderButton, loadOlder);
}

let scrollScheduled = false;
window.addEventListener("scroll", () => {
  if (scrollScheduled) return;
  scrollScheduled = true;
  requestAnimationFrame(() => { scrollScheduled = false; autoLoadOlder(); });
}, {passive: true});
window.addEventListener("resize", autoLoadOlder);
compactTimeline.addEventListener("change", () => {
  if (!$("#activity-details").open && !$("#media-manager").open && !$("#interval-activities").matches(":popover-open")) renderTimeline();
});

$("#activity-list").addEventListener("click", event => {
  const photo = photosById.get(event.target.closest("[data-grid-photo]")?.dataset.gridPhoto);
  if (photo) {
    openDetails({id: photo.pet + "-" + photo.item.id, pet: photo.pet, key: photo.key,
      start: Date.parse(photo.item.started_at), end: Date.parse(photo.item.ended_at), records: [photo.item]}, photo.index);
    if (photo.key) $("#detail-count").textContent = "本格优先展示：" + labelFor(photo.key) + "。原始活动判断与分数供你结合画面核对。";
    photoTrigger = photo.id;
    return;
  }
  const id = event.target.closest("[data-episode]")?.dataset.episode;
  if (id && episodesById.has(id)) openDetails(episodesById.get(id));
  const cluster = clustersById.get(event.target.closest("[data-cluster]")?.dataset.cluster);
  if (!cluster) return;
  $("#interval-activities").dataset.pet = cluster.pet;
  $("#interval-pet").textContent = PETS[cluster.pet] + "的这 " + (cluster.end - cluster.start) / 60000 + " 分钟";
  $("#interval-title").textContent = clock(cluster.start) + " — " + clock(cluster.end);
  $("#interval-items").replaceChildren(...cluster.intervals.map(({episode}) => {
    const item = document.createElement("li"), button = document.createElement("button");
    button.type = "button"; button.className = "interval-choice"; button.dataset.episode = episode.id;
    button.textContent = labelFor(episode.key) + " · " + clock(episode.start, true) + " — " + clock(episode.end, true) + " · " + durationLabel(episode.end - episode.start);
    item.append(button); return item;
  }));
});
$("#interval-items").addEventListener("click", event => {
  const id = event.target.closest("[data-episode]")?.dataset.episode;
  if (id && episodesById.has(id)) openDetails(episodesById.get(id));
});
$("#interval-activities").addEventListener("toggle", event => {
  if (event.newState === "closed" && !$("#activity-details").open) { renderTimeline(); autoLoadOlder(); }
});
$("#close-details").addEventListener("click", () => $("#activity-details").close());
$("#activity-details").addEventListener("close", () => {
  stopVideo();
  renderTimeline();
  if (photoTrigger) document.querySelector('[data-grid-photo="' + photoTrigger + '"]')?.focus({preventScroll: true});
  else if (detail) document.querySelector('[data-episode="' + detail.id + '"]')?.focus({preventScroll: true});
  autoLoadOlder();
});
$("#activity-details").addEventListener("click", event => { if (event.target === $("#activity-details")) event.target.close(); });
$("#detail-video").addEventListener("error", () => {
  if (!$("#detail-video").error) return;
  stopVideo();
  const image = $("#detail-image"), hasImage = image.hasAttribute("src") && (!image.complete || image.naturalWidth > 0);
  $("#detail-original").hidden = !hasImage; $("#detail-missing").hidden = hasImage;
  $("#detail-caption").textContent = "录像已过期或暂时不可用，显示静态截图。";
});
$("#detail-image").addEventListener("error", () => {
  if ($("#detail-video").hidden) { $("#detail-original").hidden = true; $("#detail-missing").hidden = false; }
});
$("#previous-photo").addEventListener("click", () => { photoIndex--; showPhoto(); });
$("#next-photo").addEventListener("click", () => { photoIndex++; showPhoto(); });
$("#photo-position").addEventListener("change", event => { photoIndex = Number(event.target.value); showPhoto(); });
$("#manage-media").addEventListener("click", () => {
  renderMediaManager(); $("#media-manager .media-message").hidden = true; $("#media-manager").showModal();
});
$("#close-media-manager").addEventListener("click", () => $("#media-manager").close());
$("#media-manager").addEventListener("close", () => { renderTimeline(); autoLoadOlder(); });
$("#media-records").addEventListener("change", updateMediaSelection);
$("#select-all-activities").addEventListener("change", event => {
  for (const box of document.querySelectorAll("#media-records input")) box.checked = event.target.checked;
  updateMediaSelection();
});
$("#delete-selected-activities").addEventListener("click", event => {
  const ids = [...document.querySelectorAll("#media-records input:checked")].map(box => Number(box.value));
  action(event.currentTarget, () => removeActivityMedia(ids)).then(updateMediaSelection);
});
$("#delete-current-media").addEventListener("click", event => {
  action(event.currentTarget, () => removeActivityMedia([detail.records[photoIndex].id])).then(() => {
    const item = detail.records[photoIndex]; $("#delete-current-media").disabled = !item.images.length && !item.video;
  });
});
$("#timeline-scale").addEventListener("change", () => { renderTimeline(); autoLoadOlder(); });
$("#day").addEventListener("change", () => loadActivities().catch(error => notice(error.message, true)));
for (const [selector, offset] of [["#previous-day", -1], ["#next-day", 1]]) $(selector).addEventListener("click", () => {
  if (!$("#day").value) return;
  const day = new Date($("#day").value + "T12:00:00Z"); day.setUTCDate(day.getUTCDate() + offset);
  $("#day").value = day.toISOString().slice(0, 10); action($(selector), () => loadActivities());
});
const olderButton = $("#older-activities");
olderButton.addEventListener("click", event => action(event.target, loadOlder));
$("#logout").addEventListener("click", event => action(event.target, async () => { await api("/api/logout", "POST"); window.location.assign("/login"); }));
(async () => {
  try {
    await initSession();
    let refreshing = false;
    const refresh = async () => {
      if (document.hidden || refreshing) return;
      refreshing = true;
      try {
        await Promise.all([loadStatus(), loadingOlder ? Promise.resolve() : loadActivities()]);
        if ($("#day").value && !$("#activity-details").open && !$("#media-manager").open) notice("");
      }
      catch (error) { notice(error.message, true); }
      finally { refreshing = false; }
    };
    setInterval(refresh, 5000);
    document.addEventListener("visibilitychange", refresh);
    await refresh();
  } catch (error) { notice(error.message, true); }
})();
