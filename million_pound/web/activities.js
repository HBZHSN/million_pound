"use strict";
import {$, action, api, formatTime, initSession, notice, petNames} from "/assets/client.js";
import {PETS, activityKey, durationLabel, groupActivities, timelineMoments} from "/assets/timeline.js";

const statuses = {pending: "正在判断", error: "判断失败", skipped: "积压未判断", cancelled: "已中断"};
const quietActivities = new Set(["uncertain", "gap", "not_visible"]);
const clock = value => new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(value));
const today = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
const records = new Map();
let before = null, generation = 0, filter = "", labels = {}, episodesById = new Map(), detail = null, photoIndex = 0, browsingOlder = false;
$("#day").value = today();
for (let hour = 0; hour < 24; hour++) {
  const option = document.createElement("option"); option.value = hour;
  option.textContent = String(hour).padStart(2, "0") + ":00 — " + String(hour + 1).padStart(2, "0") + ":00";
  $("#hour").append(option);
}

function labelFor(key) {
  return key === "gap" ? "这段暂无判断" : key === "walking" ? "走动" : labels[key] || "判断不清";
}

function renderTimeline() {
  const selected = $("#pet").value;
  const episodes = groupActivities([...records.values()]);
  episodesById = new Map(episodes.map(episode => [episode.id, episode]));
  for (const pet of Object.keys(PETS)) {
    const header = document.querySelector('.pet-header[data-pet="' + pet + '"]');
    const latest = episodes.find(episode => episode.pet === pet);
    header.classList.toggle("is-muted", Boolean(selected && selected !== pet));
    header.querySelector(".pet-current").textContent = latest ? labelFor(latest.key) + " · " + clock(latest.end) : "这一天还没有记录";
  }
  const moments = timelineMoments(episodes.filter(episode => !selected || episode.pet === selected));
  const fragment = document.createDocumentFragment();
  for (const [index, moment] of moments.entries()) {
    const row = $("#moment-template").content.cloneNode(true);
    if (index === 0) row.querySelector("li").classList.add("is-latest");
    const onlyQuiet = Object.keys(PETS).every(pet => !moment[pet] || quietActivities.has(moment[pet].key));
    row.querySelector("li").classList.toggle("is-muted-moment", onlyQuiet);
    const time = row.querySelector("time"); time.dateTime = new Date(moment.time).toISOString(); time.textContent = clock(moment.time);
    for (const pet of Object.keys(PETS)) {
      const episode = moment[pet];
      if (!episode) continue;
      const card = $("#episode-template").content.cloneNode(true);
      const button = card.querySelector("button"); button.dataset.episode = episode.id;
      button.classList.toggle("is-quiet", quietActivities.has(episode.key));
      button.setAttribute("aria-label", PETS[pet] + "，" + labelFor(episode.key) + "，" + clock(episode.start) + "至" + clock(episode.end) + "，查看照片");
      card.querySelector(".episode-title").textContent = labelFor(episode.key);
      card.querySelector(".episode-duration").textContent = durationLabel(episode.end - episode.start);
      card.querySelector(".episode-range").textContent = clock(episode.start) === clock(episode.end) ? clock(episode.start) + " 左右" : clock(episode.start) + " — " + clock(episode.end);
      const icon = ["sleeping", "eating", "walking", "playing", "resting", "not_visible", "other"].includes(episode.key) ? episode.key : "uncertain";
      card.querySelector(".episode-symbol use").setAttribute("href", "#icon-" + icon);
      row.querySelector('.timeline-cell[data-pet="' + pet + '"]').append(card);
    }
    fragment.append(row);
  }
  if (!moments.length) {
    const empty = document.createElement("li"); empty.className = "timeline-empty";
    empty.textContent = records.size ? "新的一段日常正在整理，稍等片刻。" : "这一天还没有小日常，换个日期看看吧。";
    fragment.append(empty);
  }
  const focused = document.activeElement?.dataset.episode;
  $("#activity-list").replaceChildren(fragment);
  if (focused) document.querySelector('[data-episode="' + focused + '"]')?.focus({preventScroll: true});
  $("#older-activities").hidden = !before;
  $("#history-note").textContent = before ? "相同活动合并展示，原始画面都在详情里。" : "这一天的已加载记录，到这里了。";
  $("#selected-date").textContent = $("#day").value ? new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long"}).format(new Date($("#day").value + "T12:00:00+08:00")) : "请选择日期";
}

async function loadActivities(older = false) {
  if (!$("#day").value) { notice("请选择日期", true); return; }
  const revision = ++generation;
  const nextFilter = $("#day").value + "/" + $("#hour").value;
  if (filter !== nextFilter) older = false;
  const params = new URLSearchParams({day: $("#day").value});
  if ($("#hour").value !== "") params.set("hour", $("#hour").value);
  if (older && before) params.set("before", before);
  const result = await api("/api/activities?" + params);
  if (revision !== generation) return;
  browsingOlder = older;
  const changed = filter !== nextFilter;
  const missed = !older && records.size && result.items.length && !result.items.some(item => records.has(item.id));
  if (changed || missed) records.clear();
  if (changed || missed || older || !records.size) before = result.next_before;
  filter = nextFilter; labels = result.labels;
  for (const item of result.items) records.set(item.id, item);
  if (!$("#activity-details").open) renderTimeline();
}

function showPhoto() {
  const item = detail.records[photoIndex];
  const pet = detail.pet;
  const image = $("#detail-image"), link = $("#detail-original");
  const hasImage = item.images.length > 0;
  link.hidden = !hasImage; $("#detail-missing").hidden = hasImage;
  if (hasImage) {
    link.href = "/api/activities/" + item.id + "/images/0";
    image.src = link.href; image.alt = PETS[pet] + "在" + formatTime(item.started_at) + "这段录像中的画面";
  } else image.removeAttribute("src");
  $("#detail-photo-index").textContent = (photoIndex + 1) + " / " + detail.records.length;
  $("#previous-photo").disabled = photoIndex === 0;
  $("#next-photo").disabled = photoIndex === detail.records.length - 1;
  $("#photo-position").max = detail.records.length - 1;
  $("#photo-position").value = photoIndex;
  $("#photo-position").disabled = detail.records.length === 1;
  const when = Date.parse(item.started_at) + (item.frame_offsets[0] || 0) * 1000;
  $("#detail-caption").textContent = "采集约 " + formatTime(when) + " · 可点击图片查看原图";
  $("#photo-position").setAttribute("aria-valuetext", "第 " + (photoIndex + 1) + " 张画面，采集约 " + formatTime(when));
  const decision = item.pets[pet];
  $("#detail-confidence").textContent = item.status === "done" && decision ? "这次观察：" + labelFor(activityKey(item, pet)) + " · 模型置信度 " + Math.round(decision.confidence * 100) + "%" : statuses[item.status] || "判断不清";
  $("#detail-message").textContent = petNames(item.message); $("#detail-message").hidden = !item.message;
}

function openDetails(episode) {
  detail = episode; photoIndex = episode.records.length - 1;
  $("#detail-pet").textContent = PETS[episode.pet] + "的小日常";
  $("#activity-details").dataset.pet = episode.pet;
  $("#detail-title").textContent = labelFor(episode.key) + " · " + durationLabel(episode.end - episode.start);
  $("#detail-period").textContent = formatTime(episode.start) + " — " + formatTime(episode.end);
  $("#detail-count").textContent = "合并了 " + episode.records.length + " 次观察。活动根据录像判断，画面供你核对。";
  showPhoto(); $("#activity-details").showModal();
}

async function loadStatus() {
  const status = await api("/api/status");
  const element = $("#capture-status");
  element.classList.toggle("is-live", status.recording);
  element.textContent = status.recording ? "正在记录日常" : status.enabled ? "等待下一段日常" : "记录已暂停";
  if (status.last_error) element.textContent = "记录遇到问题，请到首页查看";
}

$("#activity-list").addEventListener("click", event => {
  const id = event.target.closest("[data-episode]")?.dataset.episode;
  if (id && episodesById.has(id)) openDetails(episodesById.get(id));
});
$("#close-details").addEventListener("click", () => $("#activity-details").close());
$("#activity-details").addEventListener("close", () => {
  renderTimeline();
  if (detail) document.querySelector('[data-episode="' + detail.id + '"]')?.focus({preventScroll: true});
});
$("#activity-details").addEventListener("click", event => { if (event.target === $("#activity-details")) event.target.close(); });
$("#detail-image").addEventListener("error", () => { $("#detail-original").hidden = true; $("#detail-missing").hidden = false; });
$("#previous-photo").addEventListener("click", () => { photoIndex--; showPhoto(); });
$("#next-photo").addEventListener("click", () => { photoIndex++; showPhoto(); });
$("#photo-position").addEventListener("change", event => { photoIndex = Number(event.target.value); showPhoto(); });
$("#timeline-filters").addEventListener("submit", event => { event.preventDefault(); action($("#refresh-activities"), () => loadActivities()); });
for (const selector of ["#day", "#hour"]) $(selector).addEventListener("change", () => action($("#refresh-activities"), () => loadActivities()));
$("#pet").addEventListener("change", renderTimeline);
for (const [selector, offset] of [["#previous-day", -1], ["#next-day", 1]]) $(selector).addEventListener("click", () => {
  if (!$("#day").value) return;
  const day = new Date($("#day").value + "T12:00:00Z"); day.setUTCDate(day.getUTCDate() + offset);
  $("#day").value = day.toISOString().slice(0, 10); action($(selector), () => loadActivities());
});
$("#older-activities").addEventListener("click", event => action(event.target, () => loadActivities(true)));
$("#logout").addEventListener("click", event => action(event.target, async () => { await api("/api/logout", "POST"); window.location.assign("/login"); }));
(async () => {
  try {
    await initSession(); await Promise.all([loadActivities(), loadStatus()]);
    setInterval(() => {
      if (!document.hidden) Promise.all([loadStatus(), !browsingOlder && !$("#older-activities").disabled && $("#day").value === today() ? loadActivities() : Promise.resolve()]).catch(error => notice(error.message, true));
    }, 5000);
  } catch (error) { notice(error.message, true); }
})();
